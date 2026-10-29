import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cleaner import clean_history, _compress_text  # noqa: E402


def base_cfg(**extra):
    cfg = {
        "enabled": True,
        "threshold_chars": 50000,
        "threshold_tokens": 30000,
        "keep_recent_turns": 2,
        "reminder.dedupe": True,
        "reminder.keep_latest": True,
        "tools.ttl_minutes": 10,
        "tools.keep_recent_pairs": 2,
        "tools.remove_orphan": True,
        "tools.summarize_long": False,
        "images.clean_old": True,
        "images.placeholder": "[图片已省略]",
        "compression.enabled": False,
        "compression.cache_friendly": False,
        "compression.cache_prefix_messages": 0,
        "compression.keep_ratio": 0.3,
        "compression.min_chars": 100,
    }
    cfg.update(extra)
    return cfg


class CleanerTests(unittest.TestCase):
    def test_reminders_keep_current_and_drop_history(self):
        msgs = [
            {"role": "user", "content": "a<system_reminder>old1</system_reminder>"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c<system_reminder>new1</system_reminder><system_reminder>new2</system_reminder>"},
        ]
        out, stats = clean_history(msgs, base_cfg())
        self.assertEqual(stats.reminder_blocks_removed, 2)
        self.assertNotIn("old1", str(out[0]))
        self.assertIn("new2", str(out[-1]))
        self.assertNotIn("new1", str(out[-1]))

    def test_tool_pairs_and_orphans(self):
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a", "content": "old"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "b"}]},
            {"role": "tool", "tool_call_id": "b", "content": "old2"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c"}]},
            {"role": "tool", "tool_call_id": "c", "content": "recent"},
            {"role": "tool", "tool_call_id": "orphan", "content": "x"},
        ]
        out, stats = clean_history(
            msgs,
            base_cfg(**{"tools.keep_recent_pairs": 1, "tools.remove_orphan": True}),
        )
        text = str(out)
        self.assertNotIn('"a"', text)
        self.assertNotIn('"b"', text)
        self.assertNotIn("orphan", text)
        self.assertIn("recent", text)
        self.assertGreaterEqual(stats.tool_messages_removed, 2)

    def test_images_only_old_are_replaced(self):
        msgs = []
        for i in range(8):
            msgs.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"x{i}"}}]})
        out, stats = clean_history(
            msgs,
            base_cfg(**{"keep_recent_turns": 1, "images.clean_old": True}),
        )
        self.assertGreaterEqual(stats.images_replaced, 1)
        self.assertIn("image_url", str(out[-1]))
        self.assertNotIn("x0", str(out[0]))

    def test_old_duplicate_users_and_tool_pairs(self):
        msgs = [
            {"role": "user", "content": "重复"},
            {"role": "assistant", "content": "回答1"},
            {"role": "user", "content": "重复"},
            {"role": "assistant", "content": "回答2"},
            {"role": "assistant", "content": "old", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a", "content": "same result"},
            {"role": "assistant", "content": "old2", "tool_calls": [{"id": "b"}]},
            {"role": "tool", "tool_call_id": "b", "content": "same result"},
            {"role": "user", "content": "最近"},
            {"role": "assistant", "content": "最近回答"},
        ]
        out, stats = clean_history(msgs, base_cfg(**{"keep_recent_turns": 1}))
        self.assertGreaterEqual(stats.duplicate_user_messages_removed, 1)
        self.assertEqual(stats.duplicate_tool_pairs_removed, 1)
        text = str(out)
        self.assertEqual(text.count("same result"), 1)

    def test_empty_assistant_and_reminder_only_old_message_removed(self):
        msgs = [
            {"role": "assistant", "content": [{"type": "think", "think": ""}]},
            {"role": "user", "content": "<system_reminder>only</system_reminder>"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "最近"},
            {"role": "assistant", "content": "最近回答"},
        ]
        out, stats = clean_history(msgs, base_cfg(**{"keep_recent_turns": 1}))
        self.assertGreaterEqual(stats.empty_messages_removed, 2)
        self.assertNotIn("only", str(out))
        self.assertNotIn('"think"', str(out))

    def test_tool_result_tail_preserved(self):
        content = "A" * 1000 + "TAIL_ERROR"
        msgs = [
            {"role": "assistant", "content": "old", "tool_calls": [{"id": "a"}]},
            {"role": "tool", "tool_call_id": "a", "content": content},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "最近"},
            {"role": "assistant", "content": "最近回答"},
        ]
        out, stats = clean_history(
            msgs,
            base_cfg(
                **{
                    "keep_recent_turns": 1,
                    "tools.summarize_long": True,
                    "tools.max_result_chars": 200,
                }
            ),
        )
        self.assertGreaterEqual(stats.tool_results_summarized, 1)
        self.assertIn("TAIL_ERROR", str(out[1]))

    def test_old_image_keeps_tiny_label(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "a"}, "alt": "猫咪"},
                ],
            },
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "最近"},
            {"role": "assistant", "content": "最近回答"},
        ]
        out, stats = clean_history(msgs, base_cfg(**{"keep_recent_turns": 1}))
        self.assertEqual(stats.images_replaced, 1)
        self.assertIn("[图片:猫咪]", str(out[0]))

    def test_multiple_old_images_collapse(self):
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "a"}},
                    {"type": "image_url", "image_url": {"url": "b"}},
                ],
            },
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "最近"},
            {"role": "assistant", "content": "最近回答"},
        ]
        out, stats = clean_history(msgs, base_cfg(**{"keep_recent_turns": 1}))
        self.assertEqual(stats.images_replaced, 2)
        self.assertEqual(str(out[0]).count("[图片已省略]"), 1)

    def test_compression_is_deterministic_and_keeps_recent(self):
        old = "旧内容" * 200
        recent = "最近内容" * 10
        msgs = [
            {"role": "user", "content": old},
            {"role": "assistant", "content": old},
            {"role": "user", "content": recent},
        ]
        out, stats = clean_history(
            msgs,
            base_cfg(
                **{
                    "threshold_chars": 50,
                    "threshold_tokens": 10,
                    "compression.enabled": True,
                    "compression.min_chars": 10,
                    "compression.keep_ratio": 0.2,
                    "compression.cache_friendly": False,
                    "compression.cache_prefix_messages": 0,
                    "keep_recent_turns": 1,
                }
            ),
        )
        self.assertGreaterEqual(stats.messages_compressed, 1)
        self.assertIn("历史内容已压缩", str(out[0]))
        self.assertEqual(out[-1]["content"], recent)

    def test_compression_is_idempotent(self):
        text = "很长的旧内容" * 500
        once = _compress_text(text, 0.35)
        twice = _compress_text(once, 0.35)
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
