"""Conservative tool-definition cleanup and optional official-tool gating.

Two separate features:

1. ``clean_tool_definitions``
   Safely cleans definitions from every tool.  It never changes handlers or
   parameter schemas, and strong mode only trims overlong descriptions.

2. ``gate_official_tools``
   Content-based gating for *official/common* tools only.  Third-party plugin
   tools are always passed through unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

try:
    from .cleaner import as_bool, as_int, get_cfg
except ImportError:  # local import fallback
    from cleaner import as_bool, as_int, get_cfg  # type: ignore[no-redef]

DEFAULT_OFFICIAL_TOOLS = [
    "astrbot_execute_shell",
    "astrbot_execute_python",
    "astrbot_execute_ipython",
    "astrbot_execute_browser",
    "astrbot_execute_browser_batch",
]

_URL_RE = re.compile(r"https?://|www\.", re.I)
_CODE_RE = re.compile(
    r"```|`[^`]+`|\bpython\b|\b代码\b|\b脚本\b|\b报错\b|\btraceback\b|\b运行\b|\b执行\b|\b计算\b",
    re.I,
)
_SHELL_RE = re.compile(r"\bshell\b|终端|命令行|命令|ssh|bash|powershell|cmd", re.I)
_BROWSER_RE = re.compile(r"浏览器|网页|打开链接|访问网站|browser|url", re.I)


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [x.strip() for x in value.replace("，", ",").split(",") if x.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def official_names(cfg: Any) -> set[str]:
    configured = _names(get_cfg(cfg, "tools.gating.official_tool_names", DEFAULT_OFFICIAL_TOOLS))
    return set(configured)


def is_dynamic_official(tool: Any) -> bool:
    """Best-effort detection for AstrBot core tools, without a version table.

    Core tools carry a handler module such as ``astrbot.core.tools...``.
    Third-party plugin tools usually carry ``astrbot_plugin_*`` or
    ``data.plugins...``; MCP tools normally have no module path.
    """
    module = str(getattr(tool, "handler_module_path", "") or "").strip()
    if not module:
        return False
    prefixes = (
        "astrbot.core.",
        "astrbot.builtin_stars.",
        "astrbot.astrbot.",
    )
    return module.startswith(prefixes)


def official_tool_names(tools: Iterable[Any], cfg: Any) -> set[str]:
    """Combine configured names with module-path discovery.

    This adapts to official tools added/renamed by newer AstrBot builds without
    requiring a hard-coded name for every release.  It deliberately does not
    infer official status from a bare name prefix.
    """
    configured = official_names(cfg)
    discovered = {
        str(getattr(t, "name", "") or "")
        for t in tools
        if str(getattr(t, "name", "") or "") and is_dynamic_official(t)
    }
    return configured | discovered


def _clean_description(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text or "")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_tool_definitions(tools: list[Any], cfg: Any) -> tuple[list[Any], int]:
    """Safely clean all tool definitions.

    Weak mode:
    - remove inactive tools
    - deduplicate by name
    - normalize description whitespace

    Strong mode adds:
    - trim descriptions over ``strong.max_description_chars``

    Handlers and parameter schemas are never changed.
    """
    if not as_bool(get_cfg(cfg, "tools.cleanup.enabled", True), True):
        return tools, 0

    mode = str(get_cfg(cfg, "tools.cleanup.mode", "weak") or "weak").lower()
    strong = mode == "strong"
    remove_inactive = as_bool(get_cfg(cfg, "tools.cleanup.remove_inactive", True), True)
    normalize = as_bool(get_cfg(cfg, "tools.cleanup.normalize_whitespace", True), True)
    deduplicate = as_bool(get_cfg(cfg, "tools.cleanup.deduplicate", True), True)
    max_desc = max(
        200,
        as_int(get_cfg(cfg, "tools.cleanup.strong.max_description_chars", 1200), 1200),
    )
    # Strong cleanup is intentionally limited to official descriptions.  This
    # is not configurable to "false" because mutating third-party descriptions
    # can silently change another plugin's behaviour.
    official_only_desc = True
    protected = [
        x.lower()
        for x in _names(
            get_cfg(
                cfg,
                "tools.cleanup.strong.protected_tool_patterns",
                ["memory", "remember", "记忆", "日记", "rag"],
            )
        )
    ]
    configured_official = official_names(cfg)

    changed = 0
    kept: list[Any] = []
    seen: dict[str, Any] = {}
    for tool in tools:
        try:
            name = str(getattr(tool, "name", "") or "")
            if not name:
                kept.append(tool)
                continue
            if remove_inactive and not bool(getattr(tool, "active", True)):
                changed += 1
                continue
            if deduplicate and name in seen:
                # Keep the first active tool; never drop the only copy.
                changed += 1
                continue
            seen[name] = tool
            if normalize:
                old = str(getattr(tool, "description", "") or "")
                new = _clean_description(old)
                is_official = name in configured_official or is_dynamic_official(tool)
                protected_tool = any(p in name.lower() or p in old.lower() for p in protected)
                should_trim = (
                    strong
                    and not protected_tool
                    and (not official_only_desc or is_official)
                    and len(new) > max_desc
                )
                if should_trim:
                    new = new[:max_desc].rstrip() + "\n...[工具描述已截断]"
                if new != old:
                    try:
                        tool.description = new
                        changed += 1
                    except Exception:  # noqa: BLE001 - frozen/validated object
                        pass
            kept.append(tool)
        except Exception:  # noqa: BLE001 - one malformed tool must not break all
            kept.append(tool)
    return kept, changed


def categories_for_text(text: str) -> set[str]:
    text = str(text or "")
    categories: set[str] = set()
    if _URL_RE.search(text) or _BROWSER_RE.search(text):
        categories.add("browser")
    if _CODE_RE.search(text):
        categories.add("code")
    if _SHELL_RE.search(text):
        categories.add("shell")
    return categories


def _categories_for_tool(name: str) -> set[str]:
    if name == "astrbot_execute_shell":
        return {"shell", "code"}
    if name in {"astrbot_execute_python", "astrbot_execute_ipython"}:
        return {"code"}
    if name in {"astrbot_execute_browser", "astrbot_execute_browser_batch"}:
        return {"browser"}
    return set()


def select_official_names(
    available: Iterable[str],
    text: str,
    pending: Iterable[str],
    recent: Iterable[str],
    cfg: Any,
    official: set[str] | None = None,
) -> set[str]:
    """Choose official tools for this request.

    Unknown/empty text keeps all official tools when ``fallback_all`` is true.
    Third-party tools are never part of this selection.
    """
    official = official if official is not None else official_names(cfg)
    available_set = {str(n) for n in available}
    selected: set[str] = set()

    if not as_bool(get_cfg(cfg, "tools.gating.fallback_all", True), True):
        selected = set()
    else:
        selected = set(available_set & official)

    categories = categories_for_text(text)
    if categories:
        selected = set()
        for name in available_set & official:
            tool_categories = _categories_for_tool(name)
            # Unknown official category: preserve it.  Strong gating must not
            # silently disable an official tool added by a newer AstrBot.
            if not tool_categories or tool_categories & categories:
                selected.add(name)

    selected.update(str(x) for x in pending if str(x) in (available_set & official))
    selected.update(str(x) for x in recent if str(x) in (available_set & official))
    return selected


def gate_official_tools(
    tools: list[Any],
    text: str,
    pending: Iterable[str],
    recent: Iterable[str],
    cfg: Any,
) -> tuple[list[Any], int, int]:
    """Gate only official/common tools; third-party tools always stay."""
    if not as_bool(get_cfg(cfg, "tools.gating.enabled", False), False):
        return tools, 0, 0
    names = [str(getattr(t, "name", "") or "") for t in tools]
    official = official_tool_names(tools, cfg)
    selected = select_official_names(names, text, pending, recent, cfg, official=official)
    kept: list[Any] = []
    removed = 0
    official_kept = 0
    for tool, name in zip(tools, names):
        if name in official:
            if name in selected:
                kept.append(tool)
                official_kept += 1
            else:
                removed += 1
        else:
            # Third-party plugin tool: always pass through.
            kept.append(tool)
    if not kept and tools:
        return tools, 0, 0
    return kept, removed, official_kept
