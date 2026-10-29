import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tool_policy import clean_tool_definitions, gate_official_tools  # noqa: E402


class Tool:
    def __init__(self, name, description="d", active=True, module=""):
        self.name = name
        self.description = description
        self.active = active
        self.handler_module_path = module


CFG = {
    "tools.cleanup.enabled": True,
    "tools.cleanup.mode": "weak",
    "tools.cleanup.remove_inactive": True,
    "tools.cleanup.normalize_whitespace": True,
    "tools.cleanup.deduplicate": True,
    "tools.cleanup.strong.max_description_chars": 1200,
    "tools.cleanup.strong.official_only_descriptions": True,
    "tools.cleanup.strong.protected_tool_patterns": ["memory", "remember", "记忆", "日记", "rag"],
    "tools.gating.enabled": True,
    "tools.gating.fallback_all": True,
    "tools.gating.keep_recent_minutes": 10,
    "tools.gating.official_tool_names": [
        "astrbot_execute_shell",
        "astrbot_execute_python",
        "astrbot_execute_ipython",
        "astrbot_execute_browser",
        "astrbot_execute_browser_batch",
    ],
}


class ToolPolicyTests(unittest.TestCase):
    def test_weak_cleans_all_definitions(self):
        third = Tool("plugin_unique", "keep  this", True)
        inactive = Tool("astrbot_execute_shell", "d", False)
        normal = Tool("astrbot_execute_python", "a   b\n\n\n\nc", True)
        kept, changed = clean_tool_definitions([third, inactive, normal], CFG)
        self.assertNotIn(inactive, kept)
        self.assertIn(third, kept)
        self.assertEqual(third.description, "keep this")
        self.assertIn(normal, kept)
        self.assertGreaterEqual(changed, 3)
        self.assertEqual(normal.description, "a b\n\nc")

    def test_strong_trims_only_official_description(self):
        cfg = dict(CFG)
        cfg["tools.cleanup.mode"] = "strong"
        cfg["tools.cleanup.strong.max_description_chars"] = 200
        third = Tool("plugin_unique", "长" * 500, module="astrbot_plugin_unique.main")
        official = Tool(
            "astrbot_execute_python",
            "长" * 500,
            module="astrbot.core.tools.computer_tools.python",
        )
        kept, changed = clean_tool_definitions([third, official], cfg)
        self.assertEqual(third.description, "长" * 500)
        self.assertLess(len(official.description), 500)
        self.assertIn("工具描述已截断", official.description)
        self.assertGreaterEqual(changed, 1)

    def test_dynamic_official_is_preserved_unknown_category(self):
        dynamic = Tool(
            "astrbot_new_official_tool",
            "d",
            module="astrbot.core.tools.new.feature",
        )
        third = Tool("plugin_unique", module="astrbot_plugin_unique.main")
        kept, removed, _ = gate_official_tools(
            [dynamic, third], "帮我执行 python 代码", [], [], CFG
        )
        self.assertIn("astrbot_new_official_tool", {t.name for t in kept})
        self.assertIn("plugin_unique", {t.name for t in kept})

    def test_gating_keeps_third_party_and_prunes_official(self):
        third = Tool("plugin_unique")
        python = Tool("astrbot_execute_python")
        browser = Tool("astrbot_execute_browser")
        shell = Tool("astrbot_execute_shell")
        kept, removed, official_kept = gate_official_tools(
            [third, python, browser, shell], "帮我执行 python 代码", [], [], CFG
        )
        names = {t.name for t in kept}
        self.assertIn("plugin_unique", names)
        self.assertIn("astrbot_execute_python", names)
        self.assertNotIn("astrbot_execute_browser", names)
        self.assertGreaterEqual(removed, 1)
        self.assertGreaterEqual(official_kept, 1)

    def test_gating_fallback_all_with_empty_text(self):
        tools = [Tool("astrbot_execute_browser"), Tool("plugin_unique")]
        kept, removed, _ = gate_official_tools(tools, "", [], [], CFG)
        self.assertEqual(removed, 0)
        self.assertEqual({t.name for t in kept}, {t.name for t in tools})

    def test_gating_preserves_pending(self):
        tools = [Tool("astrbot_execute_browser"), Tool("astrbot_execute_python")]
        kept, removed, _ = gate_official_tools(
            tools, "普通聊天", ["astrbot_execute_browser"], [], CFG
        )
        names = {t.name for t in kept}
        self.assertIn("astrbot_execute_browser", names)
        self.assertGreaterEqual(removed, 0)


if __name__ == "__main__":
    unittest.main()
