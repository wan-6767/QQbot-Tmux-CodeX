import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tmux_bot import reply_targets

SOURCE = {"chat_id": "owner", "user_id": "owner", "chat_type": "dm"}
GROUP = {"chat_id": "group", "user_id": "member", "chat_type": "group"}
MESSAGE = "[001 · local · work:0.0]\n```text\n你好！\n```"


class ReplyTargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"HERMES_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.store = reply_targets.ReplyTargets()
        self.store.note(SimpleNamespace(success=True, message_id="sent-1",
            raw_response={"id": "sent-1", "msg_idx": "index-1"}), SOURCE, "001", MESSAGE)

    def resolve(self, raw, source=SOURCE):
        return self.store.resolve(raw, source)

    def test_id_and_opaque_index_resolve_successful_output(self):
        for raw in ({"message_reference": {"message_id": "sent-1"}},
                    {"message_scene": {"ext": ["ref_msg_idx=index-1"]}},
                    {"message_type": 103, "msg_elements": [{"message_id": "sent-1"}]}):
            self.assertEqual(self.resolve(raw), (True, "001"))

    def test_displayed_quote_and_unknown_opaque_index_match_without_fences(self):
        for quote in (MESSAGE, "[001 · local · work:0.0]\n你好！"):
            raw = {"message_type": "103", "msg_elements": [{"content": quote}],
                   "message_scene": {"ext": ["ref_msg_idx=opaque-client-index"]}}
            self.assertEqual(self.resolve(raw), (True, "001"))

    def test_unquoted_text_or_copied_header_does_not_choose_a_terminal(self):
        for raw in ({"content": MESSAGE}, {"msg_elements": [{"content": MESSAGE}]}):
            self.assertEqual(self.resolve(raw), (False, None))
        self.assertEqual(self.resolve({"message_type": 103,
            "msg_elements": [{"content": "[001 · local · work:0.0]\nunrecognized"}]}), (True, None))

    def test_failed_send_other_chat_and_other_member_do_not_match(self):
        raw = {"message_reference": {"message_id": "sent-1"}}
        for source in (GROUP, {**SOURCE, "user_id": "outsider"}, {**SOURCE, "chat_id": "other"}):
            self.assertEqual(self.resolve(raw, source), (True, None))
        self.store.note(SimpleNamespace(success=False, message_id="failed"), SOURCE, "002", "failed")
        self.assertEqual(self.resolve({"message_reference": {"message_id": "failed"}}), (True, None))

    def test_explicit_unknown_id_cannot_fall_back_to_copied_text(self):
        self.assertEqual(self.resolve({"message_reference": {"message_id": "other-bot"},
            "message_type": 103, "msg_elements": [{"content": MESSAGE}]}), (True, None))

    def test_multiple_quoted_terminals_are_rejected(self):
        other = "[002 · local · another:0.0]\nsecond output"
        self.store.note(SimpleNamespace(success=True, message_id="sent-2"), SOURCE, "002", other)
        self.assertEqual(self.resolve({"message_type": 103,
            "msg_elements": [{"content": MESSAGE}, {"content": other}]}), (True, None))

    def test_restart_keeps_targets_without_storing_terminal_content(self):
        restored = reply_targets.ReplyTargets()
        self.assertEqual(restored.resolve({"message_reference": {"message_id": "sent-1"}}, SOURCE), (True, "001"))
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("你好", self.store.path.read_text())

    def test_index_has_a_fixed_upper_bound(self):
        with patch.object(reply_targets, "MAX_RECORDS", 3):
            for number in range(5):
                self.store.note(SimpleNamespace(success=True, message_id=str(number)), SOURCE, "001", str(number))
            self.assertEqual(len(json.loads(self.store.path.read_text())["records"]), 3)
            self.assertEqual(self.resolve({"message_reference": {"message_id": "0"}}), (True, None))

    def test_corrupted_or_unsupported_index_does_not_prevent_startup(self):
        for value in ("not json", "[]", '{"version":2,"records":[]}', '{"version":1,"records":null}'):
            with patch.object(type(self.store.path), "read_text", return_value=value), \
                    self.assertLogs("qq_tmuxbot.quotes", level="WARNING"):
                restored = reply_targets.ReplyTargets()
            self.assertEqual(restored.records, [])

    def test_malformed_records_are_ignored_without_discarding_valid_receipts(self):
        good = self.store.records[0]
        records = [good, None, {}, {**good, "channel": "000"}, {**good, "keys": 42},
                   {**good, "digest": None}, {**good, "source": "bad"}]
        reply_targets.owner.write_private(self.store.path, {"version": 1, "records": records})
        restored = reply_targets.ReplyTargets()
        self.assertEqual(restored.records, [good])
        self.assertEqual(restored.resolve({"message_reference": {"message_id": "sent-1"}}, SOURCE), (True, "001"))

    def test_unreadable_index_does_not_prevent_startup(self):
        with patch.object(type(self.store.path), "read_text", side_effect=PermissionError), \
                self.assertLogs("qq_tmuxbot.quotes", level="WARNING"):
            self.assertEqual(reply_targets.ReplyTargets().records, [])


if __name__ == "__main__":
    unittest.main()
