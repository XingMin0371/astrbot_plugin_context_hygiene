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
import base64
import copy
import hashlib
import ipaddress
import json
import mimetypes
import os
import re
import socket
import time
import urllib.parse
import urllib.request
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

try:
    from mcp.types import CallToolResult, ImageContent
except ImportError:  # pragma: no cover
    CallToolResult = None
    ImageContent = None

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
VERSION = "1.2.2"
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
        self._last_artifact_cleanup = 0.0

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
        seconds = max(0, as_int(self._cfg("persistent.command_cooldown_seconds", 120), 120))
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
        ttl_minutes = max(0, as_int(self._cfg("tools.gating.keep_recent_minutes", 25), 25))
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

    def _clean_config(self) -> dict:
        """Keep tool cleanup single-owner when the memory plugin handles it."""
        try:
            cfg = copy.deepcopy(self.config)
        except Exception:
            cfg = dict(self.config) if hasattr(self.config, "items") else {}
        if not isinstance(cfg, dict):
            cfg = {}
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path
            path = Path(get_astrbot_data_path()) / "config" / "memory5_config.json"
            if path.exists():
                other = json.loads(path.read_text(encoding="utf-8-sig"))
                if as_bool(other.get("inject", {}).get("tool_context_compress", True), True):
                    tools = cfg.get("tools")
                    if not isinstance(tools, dict):
                        tools = {}
                    tools["skip_cleanup"] = True
                    cfg["tools"] = tools
        except Exception:
            pass
        return cfg

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
                tail_cleaned, _tail_stats = clean_history(contexts[base_len:], self._clean_config())
                combined = list(cleaned_prefix) + tail_cleaned
                stats = CleanStats(
                    before_chars=sum(message_chars(m) for m in contexts),
                    after_chars=sum(message_chars(m) for m in combined),
                    messages_before=len(contexts),
                    messages_after=len(combined),
                    changed=True,
                )
                return combined, stats

        cleaned, stats = clean_history(contexts, self._clean_config())
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

    def _image_artifact_dir(self) -> Path:
        return self._plugin_data_dir() / "image_artifacts"

    @staticmethod
    def _public_image_host(host: str) -> bool:
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False
        for info in infos:
            try:
                ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                return False
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
        return True

    async def _load_image_bytes(self, url: str) -> tuple[bytes, str] | None:
        """Load only a size-capped data URL or a public HTTP(S) image."""
        try:
            max_bytes = 8 * 1024 * 1024
            if url.startswith("data:"):
                header, _, data = url.partition(",")
                if len(data) > max_bytes * 2:
                    return None
                mime = header[5:].split(";", 1)[0] or "image/png"
                raw = base64.b64decode(data, validate=False)
            elif url.startswith("file:"):
                return None
            elif url.startswith(("http://", "https://")):
                parsed = urllib.parse.urlsplit(url)
                if not parsed.hostname or not self._public_image_host(parsed.hostname):
                    return None
                def _fetch() -> bytes:
                    req = urllib.request.Request(url, headers={"User-Agent": "context-hygiene/1.1"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        final = urllib.parse.urlsplit(resp.geturl())
                        if not final.hostname or not self._public_image_host(final.hostname):
                            return b""
                        return resp.read(max_bytes + 1)
                raw = await asyncio.to_thread(_fetch)
                mime = "image/jpeg"
            else:
                return None
            if not raw or len(raw) > max_bytes:
                return None
            return raw, mime
        except Exception:
            return None

    def _image_artifact_key(self) -> bytes:
        return self._backup_key()

    def _store_image_artifact(self, raw: bytes, mime: str) -> str:
        artifact_id = hashlib.sha1(raw).hexdigest()[:16]
        ext = mimetypes.guess_extension(mime or "image/jpeg") or ".bin"
        directory = self._image_artifact_dir()
        directory.mkdir(parents=True, exist_ok=True)
        self._prune_image_artifacts()
        path = directory / (artifact_id + ext)
        if not path.exists():
            if AESGCM is not None:
                nonce = os.urandom(12)
                body = b"IMG1" + nonce + AESGCM(self._image_artifact_key()).encrypt(nonce, raw, b"image_artifact")
            else:
                body = raw
            path.write_bytes(body)
        return artifact_id

    def _read_image_artifact(self, path: Path) -> bytes:
        raw = path.read_bytes()
        if raw.startswith(b"IMG1"):
            if AESGCM is None or len(raw) < 16:
                raise RuntimeError("image artifact encryption unavailable")
            nonce = raw[4:16]
            return AESGCM(self._image_artifact_key()).decrypt(nonce, raw[16:], b"image_artifact")
        # Migrate legacy plaintext artifacts on first read.
        if raw and AESGCM is not None:
            try:
                nonce = os.urandom(12)
                body = b"IMG1" + nonce + AESGCM(self._image_artifact_key()).encrypt(nonce, raw, b"image_artifact")
                path.write_bytes(body)
            except Exception:
                pass
        return raw

    def _prune_image_artifacts(self) -> None:
        """Keep image artifacts bounded by age and count."""
        now = time.time()
        if now - self._last_artifact_cleanup < 3600:
            return
        self._last_artifact_cleanup = now
        directory = self._image_artifact_dir()
        if not directory.is_dir():
            return
        max_days = max(1, as_int(self._cfg("images.artifact_retention_days", 15), 15))
        max_files = max(10, as_int(self._cfg("images.artifact_max_files", 500), 500))
        files = [p for p in directory.iterdir() if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        cutoff = now - max_days * 86400
        for idx, path in enumerate(files):
            try:
                if idx >= max_files or path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _find_image_artifact(self, artifact_id: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{16}", artifact_id or ""):
            return None
        directory = self._image_artifact_dir()
        for path in directory.glob(artifact_id + ".*"):
            if path.is_file():
                return path
        return None

    @filter.llm_tool(name="read_image_artifact")
    async def read_image_artifact(self, event: AstrMessageEvent, artifact_id: str):
        """读取图片入库时保存的原图；仅在需要看清图片细节时调用。"""
        path = self._find_image_artifact(str(artifact_id or "").strip())
        if path is None or CallToolResult is None or ImageContent is None:
            return "没有找到对应的图片原图。"
        try:
            data = base64.b64encode(self._read_image_artifact(path)).decode("ascii")
            mime = mimetypes.guess_type(path.name)[0] or "image/png"
            return CallToolResult(content=[ImageContent(type="image", data=data, mimeType=mime)])
        except Exception:
            return "图片原图读取失败。"

    def _caption_cache_path(self) -> Path:
        return self._plugin_data_dir() / "image_captions.json"

    def _load_caption_cache(self) -> dict:
        try:
            path = self._caption_cache_path()
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            pass
        return {}

    def _save_caption_cache(self, cache: dict) -> None:
        try:
            path = self._caption_cache_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.debug("context_hygiene: caption cache save skipped (%s)", type(exc).__name__)

    async def _caption_current_images(self, req: ProviderRequest) -> None:
        """Caption new images once, before they enter history; never rewrite old images."""
        if not as_bool(self._cfg("images.ingest_caption", False), False):
            return
        urls = getattr(req, "image_urls", None)
        if not isinstance(urls, list) or not urls:
            return
        provider = None
        provider_id = str(self._cfg("images.caption_provider_id", "") or "").strip()
        try:
            if provider_id and hasattr(self.context, "get_provider_by_id"):
                provider = self.context.get_provider_by_id(provider_id)
            if provider is None:
                provider = self.context.get_using_provider()
        except Exception:
            return
        tag_max = max(1, as_int(self._cfg("images.tag_max_chars", 4), 4))
        desc_max = max(1, as_int(self._cfg("images.desc_max_chars", 15), 15))
        prompt = (
            "请用简短中文描述这张图片，供聊天机器人理解。"
            f"标签最多{tag_max}字，描述最多{desc_max}字。"
            '只返回 JSON：{"tag":"标签","desc":"描述"}'
        )
        cache = self._load_caption_cache()
        labels = []
        changed = False
        kept_urls = []
        fail_ttl = max(10, as_int(self._cfg("images.caption_failure_ttl_seconds", 60), 60))
        now = time.time()
        for url in urls:
            if not isinstance(url, str) or not url:
                kept_urls.append(url)
                continue
            loaded = await self._load_image_bytes(url)
            artifact_id = ""
            content_key = ""
            if loaded is not None:
                artifact_id = self._store_image_artifact(loaded[0], loaded[1])
                content_key = hashlib.sha1(loaded[0]).hexdigest()
            url_key = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()
            key = content_key or url_key
            item = cache.get(key) or cache.get(url_key)
            if isinstance(item, dict) and item.get("failed_at"):
                if now - float(item.get("failed_at") or 0) < fail_ttl:
                    kept_urls.append(url)
                    continue
                item = None
            if provider is not None and hasattr(provider, "text_chat"):
                if not isinstance(item, dict):
                    try:
                        out = await provider.text_chat(
                            prompt=prompt, contexts=[], image_urls=[url], session_id=None,
                        )
                        raw = str(getattr(out, "completion_text", "") or "").strip()
                        obj = json.loads(raw[raw.find("{"): raw.rfind("}") + 1]) if "{" in raw else {}
                        tag = str(obj.get("tag") or "").strip()[:tag_max]
                        desc = str(obj.get("desc") or "").strip()[:desc_max]
                        if not tag:
                            raise ValueError("empty caption")
                        item = {"tag": tag, "desc": desc, "artifact": artifact_id}
                        cache[key] = item
                        changed = True
                    except Exception:
                        # Keep the original image and avoid retrying every turn.
                        cache[key] = {"failed_at": now}
                        changed = True
                        kept_urls.append(url)
                        continue
                elif artifact_id and item.get("artifact") != artifact_id:
                    item["artifact"] = artifact_id
                    changed = True
            if isinstance(item, dict) and item.get("tag"):
                tag = str(item.get("tag") or "")[:tag_max]
                desc = str(item.get("desc") or "")[:desc_max]
                art = str(item.get("artifact") or "")[:16]
                suffix = f" | art:{art}" if art else ""
                labels.append(f"[图片:{tag}" + (f" {desc}" if desc else "") + suffix + "]")
            else:
                kept_urls.append(url)
        if changed:
            self._save_caption_cache(cache)
        if labels:
            req.image_urls = kept_urls
            req.prompt = (req.prompt or "").rstrip() + "\n" + "\n".join(labels)

    def _cache_guard_active(self, umo: str, now: float) -> bool:
        minutes = max(0, as_int(self._cfg("cache_guard_minutes", 120), 120))
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
            await self._caption_current_images(req)
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
            logger.warning("context_hygiene: request filter skipped (%s: %s)", type(exc).__name__, exc)

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
