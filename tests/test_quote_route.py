"""QQ intake to real disposable tmux, with only network delivery replaced."""

import importlib.util
import asyncio
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

if importlib.util.find_spec("gateway") is None:
    raise unittest.SkipTest("QQ runtime absent: use Docker for the full suite; see docs/verification.md")

from gateway.config import PlatformConfig
from tmux_bot import app, owner
from tmux_bot.bridge import Tmux
from tmux_bot.multi_relay import Channel, MultiGateway
from tmux_bot.multiplex import MultiRelay
from tmux_bot.terminal_relay import format_screen

SOURCE = {"chat_id": "owner", "user_id": "owner", "chat_type": "dm"}
GROUP = {"chat_id": "group", "user_id": "member", "chat_type": "group"}


class QuoteRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"HERMES_HOME": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        owner.write_private(self.root / "owner.json", {"openid": "owner"})
        owner.write_private(self.root / "group.json", {"owner_openid": "owner",
                            "group_openid": "group", "member_openid": "member"})
        self.tmux = Tmux(str(self.root / "quote.sock"))
        script = 'import sys; print("READY",flush=True); [print("ECHO:"+line.rstrip("\\n"),flush=True) for line in sys.stdin]'
        command = shlex.join([sys.executable, "-u", "-c", script])
        self.tmux.run("new-session", "-d", "-s", "quote-test", "-x", "160", "-y", "30", command)
        self.addCleanup(lambda: self.tmux.run("kill-server"))
        self.tmux.run("new-window", "-t", "quote-test", command)
        self.multi = MultiRelay(self.tmux, self.root / "tmux-relay")
        self.addCleanup(self.multi.close)
        self.multi.listing()
        for number in ("001", "002"):
            self.multi.route(f"/tmux sel {number} ent", "enter-" + number, SOURCE)
        self.adapter = app.TerminalAdapter(PlatformConfig(extra={
            "app_id": "123", "dm_policy": "allowlist", "allow_from": ["owner"]}))
        self.counter = 0

        async def delivered(*args, **kwargs):
            self.counter += 1
            return SimpleNamespace(success=True, message_id=f"sent-{self.counter}")

        self.adapter.send = AsyncMock(side_effect=delivered)
        self.adapter.send_document = AsyncMock(side_effect=delivered)
        self.gateway = self.adapter.gateway
        self.gateway.request = AsyncMock(side_effect=lambda path, body: self.multi.api(path, body))
        self.addAsyncCleanup(self.gateway.stop)
        # Observer behavior has its own tests. Here exercise the real input path
        # without background output racing the route assertions.
        update = patch.object(Channel, "update", AsyncMock())
        update.start()
        self.addCleanup(update.stop)

    async def output(self, number, source=SOURCE):
        result = self.multi._decorate(number, self.multi._child(number)._response(""))
        message = format_screen(result["target"], "Previous terminal output " + number)
        await self.gateway.send(source, message, channel=number, token=result["selection_token"])
        return f"sent-{self.counter}", message

    async def incoming(self, content, raw=None, source=SOURCE, message_id="incoming-1"):
        data = {"id": message_id, "content": content, **(raw or {})}
        if source["chat_type"] == "dm":
            data["author"] = {"user_openid": source["user_id"]}
            event_type = "C2C_MESSAGE_CREATE"
        else:
            data.update(group_openid=source["chat_id"], author={"member_openid": source["user_id"]})
            event_type = "GROUP_AT_MESSAGE_CREATE"
        await self.adapter._on_message(event_type, data)

    def routes(self):
        return [call.args[1] for call in self.gateway.request.call_args_list if call.args[0] == "/v2/route"]

    async def wait_echo(self, number, expected):
        pane = self.multi._row(number)[1]["pane_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            screen = self.tmux.capture(pane)
            if "ECHO:" + expected in screen:
                return screen
            await asyncio.sleep(.02)
        self.fail("literal quoted input did not reach tmux")

    async def test_real_qq_quote_routes_only_new_text_to_the_correct_pane(self):
        _, first = await self.output("001")
        _, second = await self.output("002")
        self.gateway.request.reset_mock()
        body = "中文输入 $(echo literal); '保持原样'"
        await self.incoming(body, {"message_type": 103, "msg_elements": [{"content": second}]})
        self.assertEqual(self.routes()[0]["text"], "/tmux sel 002 send " + body)
        await self.wait_echo("002", body)
        self.assertNotIn(body, self.tmux.capture(self.multi._row("001")[1]["pane_id"]))
        self.assertNotIn(first, self.routes()[0]["text"])
        self.assertNotIn("Previous terminal output", self.routes()[0]["text"])

    async def test_quote_by_id_and_restart_preserve_literal_slash_and_reserved_words(self):
        message_id, _ = await self.output("001")
        self.gateway.reply_targets = type(self.gateway.reply_targets)()
        self.gateway.request.reset_mock()
        for index, body in enumerate(("/model", "/goal resume", "ext", "key enter")):
            await self.incoming(body, {"message_reference": {"message_id": message_id}},
                                message_id=f"quoted-{index}")
            self.assertEqual(self.routes()[-1]["text"], "/tmux sel 001 send " + body)
            await self.wait_echo("001", body)
        self.assertTrue(self.multi._child("001").selected)

    async def test_group_at_quotes_work_without_crossing_private_chat(self):
        private_id, _ = await self.output("001")
        self.multi.route("/tmux sel 002 ext", "switch-out", SOURCE)
        self.multi.route("/tmux sel 002 ent", "switch-in", GROUP)
        group_id, _ = await self.output("002", GROUP)
        self.gateway.request.reset_mock()
        await self.incoming("@123 群引用输入", {"message_reference": {"message_id": group_id}}, GROUP)
        self.assertEqual(self.routes()[0]["text"], "/tmux sel 002 send 群引用输入")
        await self.wait_echo("002", "群引用输入")
        self.gateway.request.reset_mock()
        await self.incoming("不应跨聊天", {"message_reference": {"message_id": private_id}}, GROUP,
                            message_id="cross-chat")
        self.assertEqual(self.routes(), [])

    async def test_unauthorized_member_cannot_use_a_quote(self):
        message_id, _ = await self.output("001")
        self.gateway.request.reset_mock()
        await self.incoming("拒绝输入", {"message_reference": {"message_id": message_id}},
                            {**GROUP, "user_id": "outsider"})
        self.assertEqual(self.routes(), [])

    async def test_bot_commands_win_and_quote_attachments_are_not_downloaded(self):
        _, message = await self.output("001")
        self.gateway.request.reset_mock()
        with patch.object(self.adapter, "_process_attachments", wraps=self.adapter._process_attachments) as download:
            await self.incoming("/help", {"message_type": 103, "msg_elements": [{"content": message,
                "attachments": [{"url": "https://example.org/old-file.zip"}]}]})
        self.assertEqual(self.routes(), [])
        self.assertIn("QQbot-Tmux", self.adapter.send.call_args.args[1])
        self.assertEqual(download.call_args_list[0].args, (None,))
        self.assertEqual(download.call_count, 1)
        await self.incoming("/tmux sel 002 send 明确选002", {"message_type": 103,
                            "msg_elements": [{"content": message}]}, message_id="explicit")
        self.assertEqual(self.routes()[-1]["text"], "/tmux sel 002 send 明确选002")

    async def test_unknown_empty_and_closed_quotes_do_not_submit(self):
        message_id, _ = await self.output("001")
        self.gateway.request.reset_mock()
        await self.incoming("unknown", {"message_reference": {"message_id": "not-our-message"}})
        await self.incoming("", {"message_type": 103, "msg_elements": [{"message_id": message_id}]},
                            message_id="empty")
        self.assertEqual(self.routes(), [])
        self.multi.route("/tmux sel 001 ext", "close", SOURCE)
        await self.incoming("closed", {"message_reference": {"message_id": message_id}}, message_id="closed")
        self.assertIn("先 /tmux sel 001 ent", self.adapter.send.call_args.args[1])
        self.assertNotIn("ECHO:closed", self.tmux.capture(self.multi._row("001")[1]["pane_id"]))

    async def test_repeated_quote_event_is_not_submitted_twice(self):
        message_id, _ = await self.output("001")
        data = {"message_reference": {"message_id": message_id}}
        for _ in range(2):
            await self.incoming("duplicate-input", data, message_id="same-inbound")
        screen = await self.wait_echo("001", "duplicate-input")
        self.assertEqual(screen.count("ECHO:duplicate-input"), 1)

    async def test_snapshot_and_long_text_file_receipts_are_quotable(self):
        for number, body in (("001", "snapshot context"), ("002", "long text " * 1000)):
            meta = self.multi._decorate(number, self.multi._child(number)._response(""))
            channel = Channel(self.gateway, meta)
            self.assertTrue(await channel.frame(body))
            message_id = f"sent-{self.counter}"
            await self.incoming("来自快照或附件", {"message_reference": {"message_id": message_id}},
                                message_id="from-file-" + number)
            self.assertEqual(self.routes()[-1]["text"], f"/tmux sel {number} send 来自快照或附件")

    async def test_index_save_failure_does_not_resend_accepted_output(self):
        with patch.object(self.gateway.reply_targets, "note", side_effect=PermissionError), \
                self.assertLogs("qq_tmuxbot.multi", level="WARNING"):
            self.assertTrue(await self.gateway.send(SOURCE, "accepted once", channel="001"))
        self.assertEqual(self.adapter.send.await_count, 1)

    async def test_unquoted_text_still_requires_an_explicit_target(self):
        await self.output("001")
        self.gateway.request.reset_mock()
        await self.incoming("ordinary unquoted message")
        self.assertEqual(self.routes(), [])

    async def test_permission_revoked_while_queued_prevents_delivery(self):
        await self.gateway.send_lock.acquire()
        queued = asyncio.create_task(self.gateway.send(GROUP, "must stay private"))
        try:
            await asyncio.sleep(0)
            owner.unbind_group()
        finally:
            self.gateway.send_lock.release()
        self.assertFalse(await queued)
        self.adapter.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
