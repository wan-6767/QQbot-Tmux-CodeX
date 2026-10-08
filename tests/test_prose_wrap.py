import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tmux_bot import terminal_relay


class WrapTests(unittest.TestCase):
    def test_chinese_soft_wraps_keep_paragraphs(self):
        text = "• 这里是终端宽度造成的\n  自动换行，我们应该把它\n  接成一个自然段。\n\n这是下一段。"
        self.assertEqual(terminal_relay.unwrap_prose(text), "• 这里是终端宽度造成的自动换行，我们应该把它接成一个自然段。\n\n这是下一段。")

    def test_english_spaces_and_wrapped_url(self):
        self.assertEqual(terminal_relay.unwrap_prose("• This is a terminal\n  wrapped sentence."), "• This is a terminal wrapped sentence.")
        self.assertEqual(terminal_relay.unwrap_prose("• See https://example.org/long/\n  address"), "• See https://example.org/long/address")
        self.assertEqual(terminal_relay.unwrap_prose("• See https://example.org/\n  for more details."), "• See https://example.org/ for more details.")

    def test_list_soft_continuations_join_but_items_stay_separate(self):
        text = "- 第一项因为终端宽度\n  自动换行\n- 第二项\n\n1. English words\n  continue here\n2. Next item"
        expected = "- 第一项因为终端宽度自动换行\n- 第二项\n\n1. English words continue here\n2. Next item"
        self.assertEqual(terminal_relay.unwrap_prose(text), expected)
        self.assertEqual(terminal_relay.unwrap_prose("  - 嵌套条目\n    续行\n  - 下一项"), "  - 嵌套条目续行\n  - 下一项")

    def test_lists_headings_quotes_tables_and_code_keep_newlines(self):
        text = "# Title\n\n- one\n- two\n\n1. first\n2. second\n\n> quoted\n> lines\n\n| a | b |\n|---|---|\n\n```python\nprint(1)\nprint(2)\n```\n\n    one = 1\n    two = 2"
        self.assertEqual(terminal_relay.unwrap_prose(text), text)

    def test_top_level_assistant_blocks_remain_separate(self):
        self.assertEqual(terminal_relay.unwrap_prose("• first\n• second"), "• first\n• second")

    def test_snapshots_remain_raw(self):
        raw = "one line\nnext row"
        self.assertEqual(terminal_relay.format_screen("test", raw), "[test]\n```text\n" + raw + "\n```")

    def test_actual_append_path_unwraps_without_changing_baseline(self):
        raw = "• 终端换行\n  应当合并。"
        batch = {"bodies": [raw], "next": 0, "target": "test", "scope": "test", "baseline": raw, "content": raw}
        sender = AsyncMock(return_value=True)
        with patch.dict(terminal_relay._pending_bodies, {"wrap-test": batch}, clear=True), \
             patch.dict(terminal_relay._screens, {}, clear=True), \
             patch.dict(terminal_relay._content_screens, {}, clear=True), \
             patch.object(terminal_relay, "send_terminal", sender), patch.object(terminal_relay, "checkpoint"):
            self.assertTrue(asyncio.run(terminal_relay.flush_bodies(None, None, "wrap-test")))
            self.assertEqual(sender.await_args.args[-1], "• 终端换行应当合并。")
            self.assertEqual(terminal_relay._screens["wrap-test"], raw)


if __name__ == "__main__":
    unittest.main()
