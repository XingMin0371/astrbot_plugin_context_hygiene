"""Pure context-cleaning logic for astrbot_plugin_context_hygiene.

No AstrBot imports here.  Keeping this module dependency-free makes it easy to
unit-test and keeps the plugin's data transformations reviewable.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
            "changed": self.changed,
        }


DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "threshold_chars": 60000,
    "threshold_tokens": 24000,
    "keep_recent_turns": 8,
    "reminder.dedupe": True,
    "reminder.keep_latest": True,
    "tools.ttl_minutes": 10,
    "tools.keep_recent_pairs": 3,
    "tools.remove_orphan": True,
    "tools.summarize_long": False,
    "tools.max_result_chars": 4000,
    "images.clean_old": True,
    "images.placeholder": "[图片已省略]",
    "compression.enabled": True,
    "compression.keep_ratio": 0.28,
    "compression.min_chars": 12000,
    "compression.cache_friendly": True,
    "compression.cache_prefix_messages": 4,
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
    return max(0, chars // 3)


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
    keep_turns = max(0, as_int(get_cfg(cfg, "keep_recent_turns", 8), 8))
    return max(0, len(messages) - keep_turns * 2)


def _cache_start(cfg: Any) -> int:
    """First index that may be rewritten when cache-friendly mode is on."""
    if not as_bool(get_cfg(cfg, "compression.cache_friendly", True), True):
        return 0
    return max(0, as_int(get_cfg(cfg, "compression.cache_prefix_messages", 4), 4))


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
    if len(text) <= max_chars:
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


def clean_tools(messages: list[dict], cfg: Any, stats: CleanStats) -> None:
    keep_recent = max(1, as_int(get_cfg(cfg, "tools.keep_recent_pairs", 3), 3))
    ttl_minutes = max(0, as_int(get_cfg(cfg, "tools.ttl_minutes", 10), 10))
    remove_orphan = as_bool(get_cfg(cfg, "tools.remove_orphan", True), True)
    summarize = as_bool(get_cfg(cfg, "tools.summarize_long", False), False)
    max_result_chars = max(200, as_int(get_cfg(cfg, "tools.max_result_chars", 4000), 4000))
    keep_turns = max(0, as_int(get_cfg(cfg, "keep_recent_turns", 8), 8))
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
    # Preserve the cache prefix untouched, including its tool calls.
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
        if i < start or not isinstance(message, dict) or message.get("role") != "assistant":
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
        if i < start or not isinstance(message, dict) or message.get("role") != "tool":
            continue
        cid = str(message.get("tool_call_id") or "")
        if cid and cid in keep_ids:
            if summarize and start <= i < recent_cutoff:
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


def clean_images(messages: list[dict], cfg: Any, stats: CleanStats) -> None:
    if not as_bool(get_cfg(cfg, "images.clean_old", True), True):
        return
    keep_turns = max(0, as_int(get_cfg(cfg, "keep_recent_turns", 8), 8))
    cutoff = max(0, len(messages) - keep_turns * 2)
    start = _cache_start(cfg)
    placeholder = str(get_cfg(cfg, "images.placeholder", "[图片已省略]"))
    keep_labels = as_bool(get_cfg(cfg, "images.keep_labels", True), True)
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


def _compress_text(text: str, ratio: float) -> str:
    if not text or ratio >= 1:
        return text
    target = max(80, int(len(text) * max(0.05, min(ratio, 0.9))))
    if len(text) <= target:
        return text
    head = max(30, target * 2 // 3)
    tail = max(20, target - head - 12)
    return f"{text[:head].rstrip()}\n...[历史内容已压缩]...\n{text[-tail:].lstrip()}"


def compress_old(messages: list[dict], cfg: Any, stats: CleanStats, before_chars: int) -> None:
    if not as_bool(get_cfg(cfg, "compression.enabled", True), True):
        return
    threshold_chars = max(500, as_int(get_cfg(cfg, "threshold_chars", 60000), 60000))
    threshold_tokens = max(500, as_int(get_cfg(cfg, "threshold_tokens", 24000), 24000))
    if before_chars <= threshold_chars and estimate_tokens(before_chars) <= threshold_tokens:
        return
    min_chars = max(500, as_int(get_cfg(cfg, "compression.min_chars", 12000), 12000))
    if before_chars < min_chars:
        return
    ratio = max(0.05, min(0.9, as_float(get_cfg(cfg, "compression.keep_ratio", 0.28), 0.28)))
    keep_turns = max(1, as_int(get_cfg(cfg, "keep_recent_turns", 8), 8))
    cutoff = max(0, len(messages) - keep_turns * 2)
    cache_friendly = as_bool(get_cfg(cfg, "compression.cache_friendly", True), True)
    cache_prefix = max(0, as_int(get_cfg(cfg, "compression.cache_prefix_messages", 4), 4))
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
    clean_tools(working, cfg, stats)
    clean_images(working, cfg, stats)
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
