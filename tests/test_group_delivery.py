import asyncio
import os
import tempfile
import unittest
import importlib.util
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

if importlib.util.find_spec("gateway") is None:
    raise unittest.SkipTest("QQ runtime absent: use Docker for the full suite; see docs/verification.md")

from gateway.config import PlatformConfig
from tmux_bot import app, group_delivery


class GroupDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"HERMES_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        clock = patch.object(group_delivery.time, "time", return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)
        self.adapter = app.TerminalAdapter(PlatformConfig(extra={"markdown_support": True}))

    async def test_text_and_media_share_real_reply_window(self):
        self.adapter.group_replies.note("group", "real-inbound", 999)
        with patch.object(self.adapter, "_api_request", AsyncMock(return_value={"id": "sent"})) as api:
            await self.adapter._send_group_text("group", "terminal text")
            await self.adapter._post_message("/v2/groups/group/messages", {
                "msg_type": 7, "media": {"file_info": "uploaded-file"}})
        bodies = [call.args[2] for call in api.call_args_list]
        self.assertEqual([body["msg_id"] for body in bodies], ["real-inbound"] * 2)
        self.assertEqual([body["msg_seq"] for body in bodies], [1, 2])
        self.assertEqual(bodies[1]["media"]["file_info"], "uploaded-file")

    async def test_native_document_upload_gets_passive_reply(self):
        self.adapter._chat_type_map["group"] = "group"
        self.adapter.group_replies.note("group", "real-inbound", 999)
        with patch.object(self.adapter, "_ensure_connected", AsyncMock(return_value=True)), \
                patch.object(self.adapter, "_upload_local_file", AsyncMock(return_value={"file_info": "file"})), \
                patch.object(self.adapter, "_api_request", AsyncMock(return_value={"id": "sent"})) as api:
            result = await self.adapter._send_media("group", "/tmp/screen.txt", 4, "document")
        self.assertTrue(result.success)
        self.assertEqual(api.call_args.args[2]["msg_id"], "real-inbound")
        self.assertEqual(api.call_args.args[2]["msg_seq"], 1)

    async def test_five_replies_then_active_without_fabricated_id(self):
        self.adapter.group_replies.note("group", "real-inbound", 999)
        with patch.object(self.adapter, "_api_request", AsyncMock(return_value={"id": "sent"})) as api:
            await asyncio.gather(*(self.adapter._send_group_text("group", "reply") for _ in range(6)))
        bodies = [call.args[2] for call in api.call_args_list]
        self.assertEqual([body["msg_seq"] for body in bodies[:5]], list(range(1, 6)))
        self.assertNotIn("msg_id", bodies[-1])

    async def test_duplicate_and_restart_do_not_reset_quota(self):
        replies = self.adapter.group_replies
        replies.note("group", "inbound", 999)
        self.assertEqual(replies.reserve("group"), ("inbound", 1))
        replies.note("group", "inbound", 1000)
        restarted = group_delivery.GroupReplies()
        self.assertEqual(restarted.reserve("group"), ("inbound", 2))
        self.assertEqual(restarted.path.stat().st_mode & 0o777, 0o600)

    async def test_expired_wrong_group_and_future_timestamp(self):
        replies = self.adapter.group_replies
        replies.note("group", "expired", 710)
        self.assertIsNone(replies.reserve("group"))
        replies.note("group", "new-inbound", 10000)
        self.assertEqual(replies.state["group"]["at"], 1000)
        self.assertIsNone(replies.reserve("other-group"))
        self.assertEqual(replies.reserve("group"), ("new-inbound", 1))

    async def test_denied_active_warns_once_and_new_at_can_reply(self):
        async def qq_api(method, path, body):
            if "msg_id" not in body:
                raise RuntimeError("QQ Bot API error [400]: 主动消息失败, 无权限")
            return {"id": "sent"}

        with patch.object(self.adapter, "_api_request", AsyncMock(side_effect=qq_api)) as api, \
                patch.object(app.owner, "owner_id", return_value="owner"), \
                patch.object(self.adapter, "_send_c2c_text", AsyncMock()) as warning:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    await self.adapter._send_group_text("group", "retained output")
            api.assert_awaited_once()
            warning.assert_awaited_once()
            self.adapter.group_replies.note("group", "new-at", 1000)
            result = await self.adapter._send_group_text("group", "retained output")
            self.assertTrue(result.success)
            self.assertEqual(api.call_args.args[2]["msg_id"], "new-at")

    async def test_uncertain_failed_post_keeps_sequence_reserved(self):
        self.adapter.group_replies.note("group", "inbound", 999)
        with patch.object(self.adapter, "_api_request", AsyncMock(side_effect=TimeoutError)):
            with self.assertRaises(TimeoutError):
                await self.adapter._send_group_text("group", "uncertain")
        self.assertEqual(group_delivery.GroupReplies().reserve("group"), ("inbound", 2))

    async def test_private_media_does_not_consume_group_reply(self):
        self.adapter.group_replies.note("group", "inbound", 999)
        with patch.object(self.adapter, "_api_request", AsyncMock(return_value={"id": "sent"})) as api:
            await self.adapter._post_message("/v2/users/owner/messages", {
                "msg_type": 7, "media": {"file_info": "file"}})
        self.assertNotIn("msg_id", api.call_args.args[2])
        self.assertEqual(self.adapter.group_replies.reserve("group"), ("inbound", 1))

    async def test_recovery_after_active_permission_enabled(self):
        self.adapter.group_replies.denied("group")
        with patch.object(group_delivery.time, "time", return_value=1061), \
                patch.object(self.adapter, "_api_request", AsyncMock(return_value={"id": "sent"})):
            result = await self.adapter._send_group_text("group", "retained output")
        self.assertTrue(result.success)
        self.assertFalse(self.adapter.group_replies.blocked("group"))
        self.assertFalse(self.adapter.group_replies.state["group"]["notified"])


if __name__ == "__main__":
    unittest.main()
