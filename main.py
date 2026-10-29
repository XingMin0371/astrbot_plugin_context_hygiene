"""AstrBot context hygiene plugin.

Safe-by-default context filter:
- removes old/duplicate system reminders from the request copy
- trims old tool-call pairs and orphan tool messages
- replaces old images with a text marker
- optionally compresses old plain-text history without an LLM

Persistent cleanup is admin-only, disabled by default, uses the public
ConversationManager API, and creates a logical JSON backup before writing.
No SQLite access and no external network calls.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # encrypted backups unavailable on minimal installs
    AESGCM = None  # type: ignore[assignment,misc]

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

try:  # exported from AstrBot 4.29; older/newer builds may differ
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - compatibility fallback
    StarTools = None  # type: ignore[assignment,misc]

try:
    from .cleaner import (
        CleanStats,
        as_bool,
        as_int,
        clean_history,
        flatten_config,
        get_cfg,
        message_chars,
    )
    from .tool_policy import clean_tool_definitions, gate_official_tools
except ImportError:  # local/single-file import fallback
    from cleaner import (  # type: ignore[no-redef]
        CleanStats,
        as_bool,
        as_int,
        clean_history,
        flatten_config,
        get_cfg,
        message_chars,
    )
    from tool_policy import clean_tool_definitions, gate_official_tools  # type: ignore[no-redef]

PLUGIN_NAME = "astrbot_plugin_context_hygiene"
VERSION = "1.1.8"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


@register(
    PLUGIN_NAME,
    "DSH",
    "节省上下文：去重提醒、清理工具残留、压缩旧上下文（安全模式默认不写库）",
    VERSION,
)
class ContextHygiene(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._flat = flatten_config(self.config)
        self._last_backup_maintenance = 0.0
        self._recent_tools: dict[str, dict[str, float]] = {}
        self._last_admin_action: dict[str, float] = {}
        self._context_cache: dict[str, tuple[str, int, list[dict]]] = {}
        self._last_request_at: dict[str, float] = {}

    def _cfg(self, key: str, default):
        if key in self._flat:
            return self._flat.get(key, default)
        return get_cfg(self.config, key, default)

    def _enabled(self) -> bool:
        return as_bool(self._cfg("enabled", True), True)

    def _admin(self, event: AstrMessageEvent) -> bool:
        if not as_bool(self._cfg("persistent.require_admin", True), True):
            return True
        def values(key):
            raw = self._cfg(key, [])
            if isinstance(raw, str):
                raw = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
            if isinstance(raw, (list, tuple, set)):
                return {str(x) for x in raw}
            return set()

        ids = values("persistent.admin_ids")
        names = values("persistent.admin_names")
        if ids or names:
            try:
                sender_id = str(event.get_sender_id())
            except (AttributeError, TypeError, ValueError):
                sender_id = ""
            try:
                sender_name = str(event.get_sender_name())
            except (AttributeError, TypeError, ValueError):
                sender_name = ""
            # Names are intentionally case-sensitive.
            return sender_id in ids or sender_name in names
        try:
            checker = getattr(event, "is_admin", None)
            if callable(checker):
                return bool(checker())
        except Exception:  # noqa: BLE001
            return False
        return False

    @staticmethod
    def _sender_id(event: AstrMessageEvent) -> str:
        try:
            return str(event.get_sender_id())
        except (AttributeError, TypeError, ValueError):
            return "unknown"

    def _check_cooldown(self, event: AstrMessageEvent) -> bool:
        seconds = max(0, as_int(self._cfg("persistent.command_cooldown_seconds", 60), 60))
        if not seconds:
            return True
        sender = self._sender_id(event)
        now = time.time()
        last = self._last_admin_action.get(sender, 0)
        if now - last < seconds:
            return False
        self._last_admin_action[sender] = now
        return True

    def _audit(self, event: AstrMessageEvent, action: str, detail: dict) -> None:
        if not as_bool(self._cfg("persistent.audit_log", True), True):
            return
        try:
            path = self._plugin_data_dir() / "audit.jsonl"
            row = {
                "at": datetime.now(timezone.utc).isoformat(),
                "sender": self._sender_id(event),
                "umo": str(getattr(event, "unified_msg_origin", "") or ""),
                "action": action,
                "detail": detail,
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_hygiene: audit skipped (%s)", type(exc).__name__)

    @staticmethod
    def _tool_call_names(messages: list) -> list[str]:
        """Return tool names from the latest assistant tool-call turn only.

        Historical tool calls must not make every old official tool look
        "active"; that would defeat gating.
        """
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            calls = message.get("tool_calls") or []
            if not calls:
                continue
            names: list[str] = []
            for call in calls:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function")
                name = fn.get("name") if isinstance(fn, dict) else None
                name = name or call.get("name")
                if name:
                    names.append(str(name))
            return names
        return []

    def _apply_tool_policy(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Apply tool-definition cleanup and optional official-tool gating.

        Definition cleanup is safe for every tool.  Gating is a separate
        feature and only removes tools in the configured official allowlist;
        third-party plugin tools are always passed through.
        """
        toolset = getattr(req, "func_tool", None)
        tools = getattr(toolset, "tools", None)
        if not isinstance(tools, list) or not tools:
            return
        contexts = getattr(req, "contexts", None) or []
        pending = self._tool_call_names(contexts)
        text = ""
        try:
            text = str(getattr(event, "message_str", "") or getattr(req, "prompt", "") or "")
        except Exception:  # noqa: BLE001
            text = ""

        recent: list[str] = []
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        ttl_minutes = max(0, as_int(self._cfg("tools.gating.keep_recent_minutes", 10), 10))
        if len(self._recent_tools) > 256:
            cutoff = time.time() - 86400
            for key in list(self._recent_tools):
                state = {n: ts for n, ts in self._recent_tools[key].items() if ts >= cutoff}
                if state:
                    self._recent_tools[key] = state
                else:
                    del self._recent_tools[key]
        now = time.time()
        if umo and ttl_minutes:
            state = self._recent_tools.get(umo, {})
            cutoff = now - ttl_minutes * 60
            state = {name: ts for name, ts in state.items() if ts >= cutoff}
            for name in pending:
                state[name] = now
            self._recent_tools[umo] = state
            recent = list(state)

        cleaned, cleaned_count = clean_tool_definitions(tools, self.config)
        gated: list = cleaned
        removed = 0
        try:
            gated, removed, _kept = gate_official_tools(
                cleaned, text, pending, recent, self.config
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_hygiene: tool gating skipped (%s)", type(exc).__name__)
            gated, removed = cleaned, 0
        if cleaned_count or removed:
            try:
                toolset.tools = gated
                logger.info(
                    "context_hygiene: tool definitions adjusted | cleanup=%s gated=%s",
                    cleaned_count,
                    removed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("context_hygiene: tool assignment skipped (%s)", type(exc).__name__)

    def _clean_contexts_cached(
        self, umo: str, contexts: list[dict]
    ) -> tuple[list[dict], CleanStats]:
        """Reuse a frozen cleaned prefix so the prompt-cache prefix stays stable.

        New messages are cleaned only in the tail.  If the old prefix changes,
        the cache is dropped and the full context is cleaned again.
        """
        cacheable = bool(umo) and as_bool(
            self._cfg("compression.cache_friendly", True), True
        )
        entry = self._context_cache.get(umo) if cacheable else None
        if entry and len(contexts) >= entry[1]:
            digest, base_len, cleaned_prefix = entry
            try:
                raw_prefix = json.dumps(
                    contexts[:base_len], ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
                current_digest = hashlib.sha256(raw_prefix).hexdigest()
            except (TypeError, ValueError):
                current_digest = ""
            if current_digest and current_digest == digest:
                tail_cleaned, _tail_stats = clean_history(contexts[base_len:], self.config)
                combined = list(cleaned_prefix) + tail_cleaned
                stats = CleanStats(
                    before_chars=sum(message_chars(m) for m in contexts),
                    after_chars=sum(message_chars(m) for m in combined),
                    messages_before=len(contexts),
                    messages_after=len(combined),
                    changed=True,
                )
                return combined, stats

        cleaned, stats = clean_history(contexts, self.config)
        if cacheable and len(contexts) <= 600:
            try:
                raw_prefix = json.dumps(
                    contexts, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
                key = hashlib.sha256(raw_prefix).hexdigest()
                if len(self._context_cache) >= 32:
                    self._context_cache.pop(next(iter(self._context_cache)))
                self._context_cache[umo] = (key, len(contexts), copy.deepcopy(cleaned))
            except (TypeError, ValueError, MemoryError):
                self._context_cache.pop(umo, None)
        return cleaned, stats

    def _cache_guard_active(self, umo: str, now: float) -> bool:
        minutes = max(0, as_int(self._cfg("cache_guard_minutes", 20), 20))
        if minutes <= 0:
            return False
        last = self._last_request_at.get(umo)
        return bool(last and now - last < minutes * 60)

    @filter.on_llm_request()
    async def filter_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """Clean only the request copy.  The stored conversation is untouched."""
        if not self._enabled():
            return
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            now = time.time()
            active = self._cache_guard_active(umo, now)
            self._last_request_at[umo] = now
            if len(self._last_request_at) > 32:
                self._last_request_at.pop(next(iter(self._last_request_at)))
            if active:
                # Do not rewrite a warm prompt-cache prefix during an active
                # conversation.  Cleanup runs once after the guard expires.
                return
            self._maybe_cleanup_backups()
            self._apply_tool_policy(event, req)
            contexts = getattr(req, "contexts", None)
            if not isinstance(contexts, list) or not contexts:
                return
            cleaned, stats = self._clean_contexts_cached(umo, contexts)
            if stats.changed:
                req.contexts = cleaned
                logger.info(
                    "context_hygiene: request cleaned | chars %s -> %s | reminders %s | tool_msgs %s | images %s",
                    stats.before_chars,
                    stats.after_chars,
                    stats.reminder_blocks_removed,
                    stats.tool_messages_removed,
                    stats.images_replaced,
                )
        except Exception as exc:  # noqa: BLE001
            # Fail open: an error must never break the user's request.
            logger.warning("context_hygiene: request filter skipped (%s)", type(exc).__name__)

    def _format_stats(self, stats: CleanStats, title: str) -> str:
        lines = [
            f"{title}",
            f"字符：{stats.before_chars} → {stats.after_chars}（预计省 {stats.saved_chars}，约 {stats.estimated_tokens_saved} token）",
            f"消息：{stats.messages_before} → {stats.messages_after}",
            f"system_reminder：-{stats.reminder_blocks_removed}",
            f"工具调用：-{stats.tool_calls_removed}，工具消息：-{stats.tool_messages_removed}",
            f"图片替换：{stats.images_replaced}",
            f"压缩消息：{stats.messages_compressed}",
            f"重复用户消息：-{stats.duplicate_user_messages_removed}",
            f"重复工具对：-{stats.duplicate_tool_pairs_removed}",
            f"空消息残留：-{stats.empty_messages_removed}",
        ]
        if not stats.changed:
            lines.append("结果：无需清理")
        return "\n".join(lines)

    def _conversation_manager(self):
        mgr = getattr(self.context, "conversation_manager", None)
        if mgr is None:
            return None
        if not all(
            callable(getattr(mgr, name, None))
            for name in ("get_curr_conversation_id", "get_conversation", "update_conversation")
        ):
            return None
        return mgr

    async def _update_history(
        self, umo: str, cid: str, history: list[dict], token_usage: int | None = None
    ) -> None:
        mgr = self._conversation_manager()
        if mgr is None:
            raise RuntimeError("ConversationManager API is unavailable")
        try:
            await mgr.update_conversation(
                umo, conversation_id=cid, history=history, token_usage=token_usage
            )
        except TypeError:
            try:
                # Older 4.x builds may not accept token_usage.
                await mgr.update_conversation(umo, conversation_id=cid, history=history)
            except TypeError:
                # Very old builds may expose only positional parameters.
                await mgr.update_conversation(umo, cid, history)

    async def _current_history(self, event: AstrMessageEvent):
        mgr = self._conversation_manager()
        if mgr is None:
            return None, None, []
        umo = event.unified_msg_origin
        try:
            cid = await mgr.get_curr_conversation_id(umo)
        except (AttributeError, TypeError, ValueError):
            return None, None, []
        if not cid:
            return None, None, []
        try:
            conv = await mgr.get_conversation(umo, cid)
        except (AttributeError, TypeError, ValueError):
            return cid, None, []
        if not conv:
            return cid, None, []
        try:
            history = json.loads(conv.history or "[]")
        except (TypeError, ValueError):
            history = []
        if not isinstance(history, list):
            history = []
        return cid, conv, history

    def _plugin_data_dir(self) -> Path:
        if StarTools is not None:
            try:
                return Path(StarTools.get_data_dir(PLUGIN_NAME))
            except (AttributeError, OSError, RuntimeError, ValueError):
                pass
        try:  # older AstrBot builds
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        except (ImportError, AttributeError, OSError):
            raise RuntimeError("plugin data directory is unavailable")

    def _backup_dir(self) -> Path:
        root = self._plugin_data_dir()
        path = Path(root) / "backups"
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        return path

    def _maybe_cleanup_backups(self) -> None:
        """Bound maintenance work to at most once per day."""
        now = time.time()
        if now - self._last_backup_maintenance < 86400:
            return
        self._last_backup_maintenance = now
        self._cleanup_backups()

    def _cleanup_backups(self) -> int:
        days = max(1, as_int(self._cfg("persistent.backup_retention_days", 15), 15))
        cutoff = time.time() - days * 86400
        removed = 0
        try:
            paths = [*self._backup_dir().glob("*.json"), *self._backup_dir().glob("*.bin")]
            for path in paths:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
        except (OSError, RuntimeError) as exc:
            logger.warning("context_hygiene: backup retention skipped (%s)", type(exc).__name__)
        return removed

    def _backup_key(self) -> bytes:
        root = self._plugin_data_dir()
        path = Path(root) / "backup.key"
        if path.exists():
            data = path.read_bytes()
            if len(data) == 32:
                return data
            raise RuntimeError("invalid backup key")
        key = os.urandom(32)
        path.write_bytes(key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key

    @staticmethod
    def _payload_checksum(payload: dict) -> str:
        clean = {k: v for k, v in payload.items() if k != "sha256"}
        raw = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _write_backup(self, umo: str, cid: str, history: list[dict]) -> Path | None:
        try:
            self._cleanup_backups()
            encrypt = as_bool(self._cfg("persistent.encrypt_backups", True), True)
            if encrypt and AESGCM is None:
                raise RuntimeError("encrypted backups unavailable")
            safe = _SAFE_NAME.sub("_", str(cid))[:60] or "conversation"
            suffix = ".bin" if encrypt else ".json"
            name = f"{time.strftime('%Y%m%d-%H%M%S')}-{safe}{suffix}"
            path = self._backup_dir() / name
            payload = {
                "version": 2,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "umo": umo,
                "cid": cid,
                "history": history,
            }
            payload["sha256"] = self._payload_checksum(payload)
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            if encrypt:
                nonce = os.urandom(12)
                body = b"CHB1" + nonce + AESGCM(self._backup_key()).encrypt(nonce, body, None)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return path
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_hygiene: backup failed (%s)", type(exc).__name__)
            return None

    def _read_backup(self, path: Path) -> dict:
        raw = path.read_bytes()
        if path.suffix == ".bin":
            if AESGCM is None or not raw.startswith(b"CHB1") or len(raw) < 16:
                raise RuntimeError("encrypted backup unavailable")
            nonce = raw[4:16]
            raw = AESGCM(self._backup_key()).decrypt(nonce, raw[16:], None)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid backup")
        checksum = payload.get("sha256")
        if checksum and checksum != self._payload_checksum(payload):
            raise ValueError("backup checksum mismatch")
        return payload

    async def _run_persistent(self, event: AstrMessageEvent, confirmed: bool = False) -> str:
        if not as_bool(self._cfg("persistent.allow", False), False):
            return "持久清理未开启。请先在插件配置中开启 persistent.allow。"
        if as_bool(self._cfg("persistent.require_confirmation", True), True) and not confirmed:
            return "持久清理需要确认：/节省上下文 run confirm"
        if not self._check_cooldown(event):
            return "操作过于频繁，请稍后再试。"
        cid, conv, history = await self._current_history(event)
        if not cid:
            return "当前会话没有可清理的对话。"
        if conv is None:
            return "当前会话没有可清理的对话。"
        cleaned, stats = clean_history(history, self.config)
        if not stats.changed:
            return self._format_stats(stats, "节省上下文：无需写入")
        backup = await asyncio.to_thread(self._write_backup, event.unified_msg_origin, cid, history)
        if backup is None:
            self._audit(event, "persistent_cleanup_failed", {"reason": "backup_failed"})
            return "清理已取消：逻辑备份失败。"
        try:
            await self._update_history(
                event.unified_msg_origin,
                cid,
                cleaned,
                token_usage=max(1, stats.after_chars // 3),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("context_hygiene: update failed (%s)", type(exc).__name__)
            self._audit(event, "persistent_cleanup_failed", {"reason": "update_failed"})
            return f"清理失败，原对话未确认写入。备份：{backup.name}"
        self._context_cache.pop(event.unified_msg_origin, None)
        self._audit(
            event,
            "persistent_cleanup",
            {
                "before_chars": stats.before_chars,
                "after_chars": stats.after_chars,
                "backup": backup.name,
            },
        )
        return self._format_stats(stats, f"节省上下文：已写入\n备份：{backup.name}")

    async def _list_backups(self) -> str:
        self._maybe_cleanup_backups()
        try:
            paths = [*self._backup_dir().glob("*.json"), *self._backup_dir().glob("*.bin")]
            files = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        except (OSError, RuntimeError):
            files = []
        if not files:
            return "没有逻辑备份。"
        lines = ["最近逻辑备份："]
        for path in files:
            lines.append(f"- {path.name} ({path.stat().st_size} B)")
        return "\n".join(lines)

    async def _restore(self, event: AstrMessageEvent, name: str, confirmed: bool = False) -> str:
        if not as_bool(self._cfg("persistent.allow", False), False):
            return "恢复未开启。请先在插件配置中开启 persistent.allow。"
        if as_bool(self._cfg("persistent.require_confirmation", True), True) and not confirmed:
            return "恢复需要确认：/节省上下文 restore <文件名> confirm"
        name = Path(str(name or "")).name
        if not name or not name.endswith((".json", ".bin")):
            return "备份文件名不合法。"
        if name.endswith(".json") and not as_bool(
            self._cfg("persistent.allow_legacy_plaintext_backups", False), False
        ):
            return "默认拒绝恢复明文旧备份。请启用 persistent.allow_legacy_plaintext_backups。"
        if not self._check_cooldown(event):
            return "操作过于频繁，请稍后再试。"
        path = self._backup_dir() / name
        try:
            if path.resolve().parent != self._backup_dir().resolve():
                return "备份文件路径不合法。"
            payload = await asyncio.to_thread(self._read_backup, path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_hygiene: restore read failed (%s)", type(exc).__name__)
            return "读取备份失败。"
        if payload.get("umo") != event.unified_msg_origin:
            return "备份不属于当前会话，已拒绝恢复。"
        cid = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
        if not cid or payload.get("cid") != cid:
            return "备份对应的对话已变化，已拒绝恢复。"
        history = payload.get("history")
        if not isinstance(history, list):
            return "备份内容不合法，已拒绝恢复。"
        try:
            await self._update_history(
                event.unified_msg_origin,
                cid,
                history,
                token_usage=max(1, sum(message_chars(m) for m in history) // 3),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("context_hygiene: restore failed (%s)", type(exc).__name__)
            self._audit(event, "restore_failed", {"backup": name})
            return "恢复失败。"
        self._context_cache.pop(event.unified_msg_origin, None)
        self._audit(event, "restore", {"backup": name})
        return f"已恢复备份：{name}"

    @filter.command("节省上下文", alias={"上下文清理", "ctxclean"})
    async def context_clean_command(self, event: AstrMessageEvent):
        """用法：/节省上下文 status|dry-run|run|backups|restore <文件名>"""
        try:
            if not self._admin(event):
                yield event.plain_result("只有管理员可以使用节省上下文。")
                return
            raw = (event.message_str or "").strip()
            parts = re.split(r"[\s,，]+", raw, maxsplit=2)
            sub = parts[1].lower() if len(parts) > 1 and parts[1] else "status"
            arg = parts[2].strip() if len(parts) > 2 else ""

            if sub in {"status", "dry-run", "dryrun", "预览"}:
                _cid, _conv, history = await self._current_history(event)
                if not history:
                    yield event.plain_result("当前会话没有可读取的历史。")
                    return
                cleaned, stats = clean_history(history, self.config)
                title = "节省上下文：预览" if sub in {"dry-run", "dryrun", "预览"} else "节省上下文：状态"
                yield event.plain_result(self._format_stats(stats, title))
                return

            if sub in {"run", "执行"}:
                confirmed = arg.lower() in {"confirm", "确认", "yes"}
                yield event.plain_result(await self._run_persistent(event, confirmed))
                return

            if sub in {"backups", "备份"}:
                yield event.plain_result(await self._list_backups())
                return

            if sub in {"restore", "恢复"}:
                if not arg:
                    yield event.plain_result("用法：/节省上下文 restore <备份文件名> [confirm]")
                    return
                tokens = arg.split()
                name = tokens[0]
                confirmed = any(x.lower() in {"confirm", "确认", "yes"} for x in tokens[1:])
                yield event.plain_result(await self._restore(event, name, confirmed))
                return

            yield event.plain_result(
                "用法：/节省上下文 status | dry-run | run [confirm] | backups | restore <文件名> [confirm]"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("context_hygiene: command failed (%s)", type(exc).__name__)
            yield event.plain_result("节省上下文执行失败，已保持原状。")
