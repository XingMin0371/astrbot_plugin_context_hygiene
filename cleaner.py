"""Pure context-cleaning logic for astrbot_plugin_context_hygiene.

No AstrBot imports here.  Keeping this module dependency-free makes it easy to
unit-test and keeps the plugin's data transformations reviewable.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.S | re.I)
IMAGE_TYPES = {"image_url", "image"}
TEXT_TYPES = {"text", "plain"}


@dataclass
class CleanStats:
    before_chars: int = 0
    after_chars: int = 0
    messages_before: int = 0
    messages_after: int = 0
    reminder_blocks_removed: int = 0
    tool_calls_removed: int = 0
    tool_messages_removed: int = 0
    orphan_tool_messages_removed: int = 0
    tool_results_summarized: int = 0
    images_replaced: int = 0
    messages_compressed: int = 0
    duplicate_user_messages_removed: int = 0
    duplicate_tool_pairs_removed: int = 0
    empty_messages_removed: int = 0
    memory_blocks_removed: int = 0
    changed: bool = False

    @property
    def saved_chars(self) -> int:
        return max(0, self.before_chars - self.after_chars)

    @property
    def estimated_tokens_saved(self) -> int:
        return self.saved_chars // 3

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
            "saved_chars": self.saved_chars,
            "estimated_tokens_saved": self.estimated_tokens_saved,
            "messages_before": self.messages_before,
            "messages_after": self.messages_after,
            "reminder_blocks_removed": self.reminder_blocks_removed,
            "tool_calls_removed": self.tool_calls_removed,
            "tool_messages_removed": self.tool_messages_removed,
            "orphan_tool_messages_removed": self.orphan_tool_messages_removed,
            "tool_results_summarized": self.tool_results_summarized,
            "images_replaced": self.images_replaced,
            "messages_compressed": self.messages_compressed,
            "duplicate_user_messages_removed": self.duplicate_user_messages_removed,
            "duplicate_tool_pairs_removed": self.duplicate_tool_pairs_removed,
            "empty_messages_removed": self.empty_messages_removed,
            "memory_blocks_removed": self.memory_blocks_removed,
            "changed": self.changed,
        }


DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "threshold_chars": 116364,
    "threshold_tokens": 64000,
    "keep_recent_turns": 12,
    "reminder.dedupe": True,
    "reminder.keep_latest": True,
    "tools.ttl_minutes": 65,
    "tools.keep_recent_pairs": 3,
    "tools.remove_orphan": True,
    "tools.summarize_long": True,
    "tools.max_result_chars": 6000,
    "tools.max_current_result_chars": 12000,
    "images.clean_old": True,
    "images.placeholder": "[图片已省略]",
    "images.only_base64": True,
    "tools.keep_recent_turns": 30,
    "compression.enabled": True,
    "compression.keep_ratio": 0.333,
    "compression.min_chars": 16000,
    "compression.cache_friendly": True,
    "compression.cache_prefix_messages": 24,
    "persistent.allow": False,
    "persistent.require_admin": True,
    "persistent.backup_retention_days": 15,
}


def flatten_config(config: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten AstrBot's nested config dict into dotted keys."""
    out: dict[str, Any] = {}
    if not isinstance(config, dict):
        return out
    for key, value in config.items():
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict) and not ("type" in value and "default" in value):
            out.update(flatten_config(value, name))
        else:
            out[name] = value
    return out


def get_cfg(config: Any, key: str, default: Any = None) -> Any:
    flat = config if isinstance(config, dict) and "." in str(key) and key in config else flatten_config(config)
    value = flat.get(key, DEFAULTS.get(key, default))
    return default if value is None else value


def as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def text_of(message: Any) -> str:
    """Extract all text from a message, without mutating it."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in TEXT_TYPES:
                parts.append(str(part.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def message_chars(message: Any) -> int:
    """Fast estimate; avoid serialising every message on every request."""
    if not isinstance(message, dict):
        return 0
    chars = len(text_of(message))
    # Tool schemas/calls are structured; add a small bounded allowance instead
    # of json.dumps() (which was a measurable hot path).
    if message.get("tool_calls"):
        chars += 80
    return chars


def estimate_tokens(chars: int) -> int:
    """Cheap, deliberately conservative estimate; no tokenizer dependency."""
    return max(0, int(chars * 0.55))


def _new_message(message: dict) -> dict:
    """Copy only the mutable parts; avoid deep-copying the whole envelope."""
    out = dict(message)
    if "content" in message:
        out["content"] = copy.deepcopy(message.get("content"))
    if "tool_calls" in message:
        out["tool_calls"] = copy.deepcopy(message.get("tool_calls"))
    return out


def _is_empty_content(content: Any) -> bool:
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    if isinstance(content, list):
        if not content:
            return True
        for part in content:
            if not isinstance(part, dict):
                return False
            ptype = part.get("type")
            if ptype in TEXT_TYPES:
                if str(part.get("text", "")).strip():
                    return False
            elif ptype in {"think", "reasoning"}:
                body = part.get("think", part.get("text", ""))
                if str(body or "").strip():
                    return False
            else:
                return False
        return True
    return False


def _set_content(message: dict, content: Any) -> None:
    message["content"] = content


def _strip_reminder_text(text: str, keep_last: bool) -> tuple[str, int]:
    blocks = REMINDER_RE.findall(text or "")
    if not blocks:
        return text, 0
    if keep_last:
        last = blocks[-1]
        cleaned = REMINDER_RE.sub("", text)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        cleaned = f"{cleaned}\n{last}".strip() if cleaned else last
        return cleaned, max(0, len(blocks) - 1)
    return REMINDER_RE.sub("", text), len(blocks)


def clean_reminders(messages: list[dict], cfg: Any, stats: CleanStats) -> None:
    if not as_bool(get_cfg(cfg, "reminder.dedupe", True), True):
        return
    keep_latest = as_bool(get_cfg(cfg, "reminder.keep_latest", True), True)

    last_user = -1
    for i, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            last_user = i

    start = _cache_start(cfg)
    for i, message in enumerate(messages):
        if i < start:
            continue
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        is_current = i == last_user
        content = message.get("content")
        if isinstance(content, str):
            cleaned, removed = _strip_reminder_text(content, keep_latest and is_current)
            stats.reminder_blocks_removed += removed
            if cleaned != content:
                _set_content(message, cleaned)
        elif isinstance(content, list):
            kept = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in TEXT_TYPES:
                    original = str(part.get("text", ""))
                    cleaned, removed = _strip_reminder_text(original, keep_latest and is_current)
                    stats.reminder_blocks_removed += removed
                    if cleaned.strip():
                        new_part = dict(part)
                        new_part["text"] = cleaned
                        kept.append(new_part)
                else:
                    kept.append(part)
            if kept != content:
                _set_content(message, kept)


def _old_cutoff(messages: list[dict], cfg: Any) -> int:
    keep_turns = max(0, as_int(get_cfg(cfg, "basic.keep_recent_turns", get_cfg(cfg, "keep_recent_turns", 12)), 12))
    return max(0, len(messages) - keep_turns * 2)


def _cache_start(cfg: Any) -> int:
    """First index that may be rewritten when cache-friendly mode is on."""
    if not as_bool(get_cfg(cfg, "compression.cache_friendly", True), True):
        return 0
    return max(0, as_int(get_cfg(cfg, "compression.cache_prefix_messages", 24), 24))


def dedupe_old(messages: list[dict], cfg: Any, stats: CleanStats) -> None:
    """Remove exact duplicates only outside the recent window.

    User duplicates are safe to remove as standalone old turns.  Tool-result
    duplicates are removed only as a complete one-call/one-result pair; a
    complex/mixed assistant message is left untouched to preserve protocol.
    """
    cutoff = _old_cutoff(messages, cfg)
    if cutoff <= 1:
        return
    start = _cache_start(cfg)

    # User messages: keep the latest occurrence, remove earlier exact copies.
    last_user: dict[str, int] = {}
    for i, message in enumerate(messages[:cutoff]):
        if i < start:
            continue
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = text_of(message).strip()
        if text:
            last_user[text] = i
    for i, message in enumerate(messages[:cutoff]):
        if i < start:
            continue
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = text_of(message).strip()
        if text and last_user.get(text) != i:
            message["_ctx_hygiene_drop"] = True
            stats.duplicate_user_messages_removed += 1
            # Avoid leaving an assistant answer without its question.
            if i + 1 < cutoff:
                nxt = messages[i + 1]
                if (
                    isinstance(nxt, dict)
                    and nxt.get("role") == "assistant"
                    and not nxt.get("tool_calls")
                ):
                    nxt["_ctx_hygiene_drop"] = True

    # Tool results: duplicate complete one-call pairs.
    call_to_assistant: dict[str, int] = {}
    call_count: dict[str, int] = {}
    for i, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            ids = _call_ids(message)
            for cid in ids:
                call_to_assistant[cid] = i
                call_count[cid] = call_count.get(cid, 0) + 1

    results: dict[str, list[tuple[int, str]]] = {}
    tool_indices: dict[str, list[int]] = {}
    for i, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        cid = str(message.get("tool_call_id") or "")
        if not cid:
            continue
        tool_indices.setdefault(cid, []).append(i)
        if i < start or i >= cutoff:
            continue
        content = text_of(message).strip()
        if content and call_count.get(cid, 0) == 1:
            results.setdefault(content, []).append((i, cid))
    for entries in results.values():
        if len(entries) < 2:
            continue
        # Keep the newest copy; remove older complete pairs.
        for _i, cid in entries[:-1]:
            ai = call_to_assistant.get(cid)
            if ai is None or ai < start or ai >= cutoff:
                continue
            assistant = messages[ai]
            calls = assistant.get("tool_calls") or []
            if len(calls) != 1:
                continue
            assistant["_ctx_hygiene_drop"] = True
            for j in tool_indices.get(cid, []):
                messages[j]["_ctx_hygiene_drop"] = True
            stats.duplicate_tool_pairs_removed += 1

    cleaned = []
    for message in messages:
        if isinstance(message, dict) and message.pop("_ctx_hygiene_drop", False):
            continue
        cleaned.append(message)
    messages[:] = cleaned


def _call_ids(message: dict) -> list[str]:
    ids = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            value = call.get("id") or call.get("tool_call_id")
            if value:
                ids.append(str(value))
    return ids


def _message_ts(message: dict) -> float | None:
    for key in ("created_at", "timestamp", "time", "ts"):
        value = message.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return float(value)
        try:
            text = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            continue
    return None


def _truncate_tool_result(message: dict, max_chars: int, stats: CleanStats) -> None:
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = text_of(message)
    else:
        return
    if "[工具结果中间已截断]" in text or len(text) <= max_chars:
        return
    # Keep the beginning and end: tool errors/exit codes often live at the tail.
    head_len = max(80, max_chars * 2 // 3)
    tail_len = max(40, max_chars - head_len - 24)
    message["content"] = (
        text[:head_len].rstrip()
        + "\n...[工具结果中间已截断]...\n"
        + text[-tail_len:].lstrip()
    )
    stats.tool_results_summarized += 1


def cap_current_tool_results(messages: list[dict], cfg: Any, stats: CleanStats) -> None:
    """Cap only the current turn's tool results; old cached turns stay untouched."""
    limit = max(500, as_int(get_cfg(cfg, "tools.max_current_result_chars", 12000), 12000))
    last_user = -1
    for i, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            last_user = i
    for i in range(last_user + 1, len(messages)):
        message = messages[i]
        if isinstance(message, dict) and message.get("role") == "tool":
            _truncate_tool_result(message, limit, stats)


def clean_tools(messages: list[dict], cfg: Any, stats: CleanStats) -> None:
    keep_recent = max(1, as_int(get_cfg(cfg, "tools.keep_recent_pairs", 3), 3))
    ttl_minutes = max(0, as_int(get_cfg(cfg, "tools.ttl_minutes", 65), 65))
    remove_orphan = as_bool(get_cfg(cfg, "tools.remove_orphan", True), True)
    summarize = as_bool(get_cfg(cfg, "tools.summarize_long", True), True)
    max_result_chars = max(200, as_int(get_cfg(cfg, "tools.max_result_chars", 6000), 6000))
    keep_turns = max(0, as_int(get_cfg(cfg, "basic.keep_recent_turns", get_cfg(cfg, "keep_recent_turns", 12)), 12))
    # Only clean tool traffic older than this many turns; keep recent turns
    # byte-stable so the prompt cache prefix is not disturbed.
    tool_keep_turns = max(1, as_int(get_cfg(cfg, "tools.keep_recent_turns", 30), 30))
    tool_recent_cutoff = max(0, len(messages) - tool_keep_turns * 2)
    recent_cutoff = max(0, len(messages) - keep_turns * 2)
    cutoff = datetime.now(timezone.utc).timestamp() - ttl_minutes * 60 if ttl_minutes else None

    calls: list[tuple[int, str, bool, bool]] = []
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for cid in _call_ids(message):
                ts = _message_ts(message)
                old = bool(cutoff and ts is not None and ts < cutoff)
                calls.append((i, cid, old, ts is not None))

    start = _cache_start(cfg)
    recent_ids = {cid for _, cid, _, _ in calls[-keep_recent:]}
    keep_ids = set(recent_ids)
    # Preserve the cache prefix, including its tool calls.
    for idx, cid, _old, _has_ts in calls:
        if idx < start:
            keep_ids.add(cid)
    if cutoff:
        for _, cid, old, has_ts in calls:
            # A missing timestamp must not make every call look recent.
            if has_ts and not old:
                keep_ids.add(cid)

    # Remove old assistant tool_calls (and their results) beyond the recent set.
    for i, message in enumerate(messages):
        if i < start or i >= tool_recent_cutoff or not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            continue
        kept_calls = []
        for call in tool_calls:
            cid = str(call.get("id") or call.get("tool_call_id") or "") if isinstance(call, dict) else ""
            if cid and cid in keep_ids:
                kept_calls.append(call)
            else:
                stats.tool_calls_removed += 1
        if len(kept_calls) != len(tool_calls):
            message["tool_calls"] = kept_calls
            if not kept_calls:
                message.pop("tool_calls", None)

    # Remove tool messages without a surviving call; optionally remove all orphans.
    known_ids = {cid for _, cid, _, _ in calls}
    for i, message in enumerate(messages):
        if i < start or i >= tool_recent_cutoff or not isinstance(message, dict) or message.get("role") != "tool":
            continue
        cid = str(message.get("tool_call_id") or "")
        if cid and cid in keep_ids:
            if summarize:
                _truncate_tool_result(message, max_result_chars, stats)
            continue
        if (not cid or cid not in known_ids) and not remove_orphan:
            continue
        message["_ctx_hygiene_drop"] = True
        stats.tool_messages_removed += 1
        if not cid or cid not in known_ids:
            stats.orphan_tool_messages_removed += 1

    # Drop marked tool messages and empty assistant shells.
    cleaned = []
    for message in messages:
        if isinstance(message, dict) and message.pop("_ctx_hygiene_drop", False):
            continue
        if isinstance(message, dict) and message.get("role") == "assistant":
            if not message.get("tool_calls") and _is_empty_content(message.get("content")):
                stats.empty_messages_removed += 1
                continue
        cleaned.append(message)
    messages[:] = cleaned


def _image_label(part: dict, max_chars: int) -> str:
    """Return a tiny 2-4 character label if the image part carries one."""
    candidates = [part]
    nested = part.get("image_url")
    if isinstance(nested, dict):
        candidates.append(nested)
    for obj in candidates:
        for key in ("alt", "caption", "description", "label", "emotion", "tag", "name"):
            value = obj.get(key)
            if isinstance(value, str):
                text = re.sub(r"\s+", "", value).strip()
                if text:
                    return text[:max_chars]
    return ""


def _image_has_inline_base64(part: dict) -> bool:
    nested = part.get("image_url")
    values = [part, nested] if isinstance(nested, dict) else [part]
    for obj in values:
        for key in ("url", "image_url", "data", "file"):
            value = obj.get(key)
            if isinstance(value, str) and ("base64," in value or value.startswith("data:image")):
                return True
    return False


def clean_images(messages: list[dict], cfg: Any, stats: CleanStats) -> None:
    if not as_bool(get_cfg(cfg, "images.clean_old", True), True):
        return
    keep_turns = max(0, as_int(get_cfg(cfg, "basic.keep_recent_turns", get_cfg(cfg, "keep_recent_turns", 12)), 12))
    cutoff = max(0, len(messages) - keep_turns * 2)
    start = _cache_start(cfg)
    placeholder = str(get_cfg(cfg, "images.placeholder", "[图片已省略]"))
    keep_labels = as_bool(get_cfg(cfg, "images.keep_labels", True), True)
    only_base64 = as_bool(get_cfg(cfg, "images.only_base64", True), True)
    max_label = max(2, min(4, as_int(get_cfg(cfg, "images.max_label_chars", 4), 4)))
    for i, message in enumerate(messages):
        if i < start or i >= cutoff or not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        changed = False
        new_parts = []
        image_seen = False
        image_count = 0
        labels: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in IMAGE_TYPES:
                if only_base64 and not _image_has_inline_base64(part):
                    new_parts.append(part)
                    continue
                stats.images_replaced += 1
                image_count += 1
                changed = True
                if keep_labels:
                    label = _image_label(part, max_label)
                    if label and label not in labels:
                        labels.append(label)
                if not image_seen:
                    new_parts.append({"type": "text", "text": placeholder})
                    image_seen = True
            else:
                new_parts.append(part)
        # Keep a tiny semantic label when available, plus the image count.
        if changed:
            if labels:
                text = f"[图片:{'/'.join(labels[:2])}]"
            else:
                text = placeholder
            if image_count > 1:
                text += f" x{image_count}"
            new_parts[0] = {"type": "text", "text": text}
            _set_content(message, new_parts)


TOOL_IMAGE_RE = re.compile(r"^\[Image from tool\b.*\]$", re.I | re.S)
TOOL_IMAGE_PLACEHOLDER = "[图片工具结果已省略]"


def strip_tool_image_artifacts(messages: list[dict]) -> tuple[int, int]:
    """Drop tool-image marker text and its inline base64 image parts.

    These are stored as normal user messages, so clean_images() does not see
    them as image parts when the marker sits beside the image payload.
    """
    changed = 0
    saved = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        before = message_chars(message)
        if isinstance(content, str):
            if "Image from tool" not in content:
                continue
            new = re.sub(r"\[Image from tool\b[^\]]*\]", TOOL_IMAGE_PLACEHOLDER, content)
            if new != content:
                _set_content(message, new)
        elif isinstance(content, list):
            has_marker = any(
                isinstance(part, dict)
                and isinstance(part.get("text"), str)
                and TOOL_IMAGE_RE.match(part["text"].strip())
                for part in content
            )
            if not has_marker:
                continue
            new_parts = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and TOOL_IMAGE_RE.match(text.strip()):
                        continue
                    if part.get("type") in IMAGE_TYPES:
                        continue
                new_parts.append(part)
            if not new_parts:
                new_parts = [{"type": "text", "text": TOOL_IMAGE_PLACEHOLDER}]
            _set_content(message, new_parts)
        else:
            continue
        after = message_chars(message)
        if after != before:
            changed += 1
            saved += max(0, before - after)
    return changed, saved


OVERSIZE_MARKER = "\n[内容过长已截断]\n"


def truncate_oversized_messages(messages: list[dict], limit: int = 10000) -> tuple[int, int]:
    """Hard safety cap: structural garbage is cut even inside cache protection."""
    limit = max(1000, int(limit))
    changed = 0
    saved = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        before = message_chars(message)
        if before <= limit:
            continue
        content = message.get("content")
        budget = max(200, limit // 2)
        if isinstance(content, str):
            new = content[:budget].rstrip() + OVERSIZE_MARKER + content[-budget:].lstrip()
        elif isinstance(content, list):
            new_parts = []
            remaining = limit
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text = part["text"]
                    if len(text) > remaining:
                        keep = max(100, remaining // 2)
                        text = text[:keep].rstrip() + OVERSIZE_MARKER + text[-keep:].lstrip()
                    part = dict(part)
                    part["text"] = text
                    remaining = max(0, remaining - len(text))
                new_parts.append(part)
            new = new_parts
        else:
            continue
        _set_content(message, new)
        after = message_chars(message)
        if after != before:
            changed += 1
            saved += max(0, before - after)
    return changed, saved


COMPRESS_MARKER = "...[历史内容已压缩]..."


def _compress_text(text: str, ratio: float) -> str:
    if not text or ratio >= 1 or COMPRESS_MARKER in text:
        return text
    target = max(80, int(len(text) * max(0.05, min(ratio, 0.9))))
    if len(text) <= target:
        return text
    head = max(30, target * 2 // 3)
    tail = max(20, target - head - 12)
    return f"{text[:head].rstrip()}\n{COMPRESS_MARKER}\n{text[-tail:].lstrip()}"


def compress_old(messages: list[dict], cfg: Any, stats: CleanStats, before_chars: int) -> None:
    if not as_bool(get_cfg(cfg, "compression.enabled", True), True):
        return
    # 阈值只统计实际会被压缩的内容：user/assistant，且不含 tool_calls。
    compressible_chars = sum(
        message_chars(m)
        for m in messages
        if isinstance(m, dict)
        and m.get("role") in {"user", "assistant"}
        and not m.get("tool_calls")
    )
    threshold_chars = max(500, as_int(get_cfg(cfg, "threshold_chars", 126500), 126500))
    threshold_tokens = max(500, as_int(get_cfg(cfg, "threshold_tokens", 75000), 75000))
    if compressible_chars <= threshold_chars and estimate_tokens(compressible_chars) <= threshold_tokens:
        return
    min_chars = max(500, as_int(get_cfg(cfg, "compression.min_chars", 16000), 16000))
    if compressible_chars < min_chars:
        return
    ratio = max(0.05, min(0.9, as_float(get_cfg(cfg, "compression.keep_ratio", 0.333), 0.333)))
    keep_turns = max(1, as_int(get_cfg(cfg, "basic.keep_recent_turns", get_cfg(cfg, "keep_recent_turns", 12)), 12))
    cutoff = max(0, len(messages) - keep_turns * 2)
    cache_friendly = as_bool(get_cfg(cfg, "compression.cache_friendly", True), True)
    cache_prefix = max(0, as_int(get_cfg(cfg, "compression.cache_prefix_messages", 24), 24))
    start = cache_prefix if cache_friendly else 0
    for i, message in enumerate(messages):
        if i < start or i >= cutoff or not isinstance(message, dict):
            continue
        if message.get("role") not in {"user", "assistant"}:
            continue
        if message.get("tool_calls") or message.get("role") == "tool":
            continue
        content = message.get("content")
        if isinstance(content, str):
            new = _compress_text(content, ratio)
            if new != content:
                _set_content(message, new)
                stats.messages_compressed += 1
        elif isinstance(content, list):
            # Only compress pure text messages; leave multimodal messages alone.
            if any(isinstance(p, dict) and p.get("type") not in TEXT_TYPES for p in content):
                continue
            text = text_of(message)
            new = _compress_text(text, ratio)
            if new != text:
                _set_content(message, [{"type": "text", "text": new}])
                stats.messages_compressed += 1


MEMORY_MARKER = "以下记忆是用第三人称写的"
MEMORY_PLACEHOLDER = "[早期记忆块已省略]"
_MEMORY_SEEN: dict[str, float] = {}
_MEMORY_SEEN_LOCK = threading.Lock()


def _memory_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in value
            if isinstance(part, dict) and part.get("type") in TEXT_TYPES
        )
    return ""


def _memory_seen_path() -> Path:
    data_root = Path(__file__).resolve().parents[2]
    return data_root / "plugin_data" / "astrbot_plugin_context_hygiene" / "memory_block_seen.json"


def _load_memory_seen() -> dict[str, float]:
    global _MEMORY_SEEN
    with _MEMORY_SEEN_LOCK:
        if _MEMORY_SEEN:
            return dict(_MEMORY_SEEN)
        try:
            data = json.loads(_memory_seen_path().read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _MEMORY_SEEN = {str(k): float(v) for k, v in data.items()}
        except Exception:
            _MEMORY_SEEN = {}
        return dict(_MEMORY_SEEN)


def _save_memory_seen(data: dict[str, float]) -> None:
    global _MEMORY_SEEN
    cutoff = time.time() - 30 * 86400
    data = {k: v for k, v in data.items() if v >= cutoff}
    with _MEMORY_SEEN_LOCK:
        _MEMORY_SEEN = dict(data)
        try:
            path = _memory_seen_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def _memory_message_time(message: dict) -> float | None:
    for key in ("timestamp", "ts", "created_at", "time"):
        value = message.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, (int, float)):
                return float(value)
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            return datetime.fromisoformat(text).timestamp()
        except Exception:
            continue
    return None


def clean_memory_blocks(messages: list[dict], cfg: Any, stats: CleanStats) -> None:
    """Keep the newest memory injections and protect blocks younger than min_age."""
    if not as_bool(get_cfg(cfg, "memory_blocks.enabled", True), True):
        return
    keep = max(0, as_int(get_cfg(cfg, "memory_blocks.keep_latest", 5), 5))
    try:
        min_age_hours = max(0.0, float(get_cfg(cfg, "memory_blocks.min_age_hours", 12) or 12))
    except (TypeError, ValueError):
        min_age_hours = 12.0
    min_age = min_age_hours * 3600
    now = time.time()
    seen = _load_memory_seen()
    changed_seen = False
    hits: list[tuple[int, str]] = []
    for i, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _memory_text(message.get("content"))
        if MEMORY_MARKER not in text:
            continue
        hits.append((i, text))
        key = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
        if key not in seen:
            seen[key] = now
            changed_seen = True
    if changed_seen:
        _save_memory_seen(seen)
    if len(hits) <= keep:
        return
    drop = hits[: len(hits) - keep] if keep else hits
    for i, text in drop:
        message = messages[i]
        age_start = _memory_message_time(message) or seen.get(
            hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
        )
        if age_start is not None and now - age_start < min_age:
            continue
        content = message.get("content")
        if isinstance(content, str):
            _set_content(message, MEMORY_PLACEHOLDER)
        elif isinstance(content, list):
            new_parts = []
            for part in content:
                if (
                    isinstance(part, dict)
                    and part.get("type") in TEXT_TYPES
                    and MEMORY_MARKER in str(part.get("text", ""))
                ):
                    part = dict(part)
                    part["text"] = MEMORY_PLACEHOLDER
                new_parts.append(part)
            _set_content(message, new_parts)
        stats.memory_blocks_removed += 1


def clean_history(messages: Any, cfg: Any) -> tuple[list[dict], CleanStats]:
    """Return a cleaned copy and statistics; never mutates the input list."""
    if not isinstance(messages, list):
        return [], CleanStats()
    working = [_new_message(m) for m in messages if isinstance(m, dict)]
    stats = CleanStats(
        before_chars=sum(message_chars(m) for m in working),
        messages_before=len(working),
    )
    clean_reminders(working, cfg, stats)
    dedupe_old(working, cfg, stats)
    if as_bool(get_cfg(cfg, "tools.cleanup_tools", False), False):
        clean_tools(working, cfg, stats)
    strip_tool_image_artifacts(working)
    clean_images(working, cfg, stats)
    cap_current_tool_results(working, cfg, stats)
    clean_memory_blocks(working, cfg, stats)
    compress_old(working, cfg, stats, stats.before_chars)
    # Drop empty assistant/think shells, and old user turns that consisted
    # only of a system reminder.  Keep the recent window intact.
    old_cutoff = _old_cutoff(working, cfg)
    cleaned = []
    for i, message in enumerate(working):
        if not isinstance(message, dict):
            cleaned.append(message)
            continue
        role = message.get("role")
        if role == "assistant" and not message.get("tool_calls") and _is_empty_content(message.get("content")):
            stats.empty_messages_removed += 1
            continue
        if role == "user" and i < old_cutoff and _is_empty_content(message.get("content")):
            stats.empty_messages_removed += 1
            continue
        cleaned.append(message)
    working = cleaned
    stats.messages_after = len(working)
    stats.after_chars = sum(message_chars(m) for m in working)
    stats.changed = stats.before_chars != stats.after_chars or stats.messages_before != stats.messages_after
    return working, stats
