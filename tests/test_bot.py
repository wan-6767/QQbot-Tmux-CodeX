import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.qqbot.adapter import QQAdapter
from tmux_bot import app, owner


class OwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"HERMES_HOME": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)

    def code(self):
        owner.prepare_pairing()
        return json.loads((owner.home() / "pairing-instruction.json").read_text())["command"]

    def test_unknown_message_never_claims_owner(self):
        self.code()
        for user, text in (("outsider", "hello"), ("outsider", "/bind wrong"), ("*", self.code())):
            self.assertFalse(owner.bind(user, text))
        self.assertEqual(owner.owner_id(), "")

    def test_one_time_pairing_and_private_storage(self):
        code = self.code()
        self.assertTrue(owner.bind("owner", code))
        self.assertEqual(owner.owner_id(), "owner")
        self.assertFalse(owner.bind("other", code))
        self.assertFalse((owner.home() / "pairing-instruction.json").exists())
        self.assertFalse((owner.home() / "pairing.json").exists())
        self.assertEqual((owner.home() / "owner.json").stat().st_mode & 0o777, 0o600)

    def test_expired_pairing_is_rejected(self):
        code = self.code()
        path = owner.home() / "pairing.json"
        data = json.loads(path.read_text())
        data["expires_at"] = time.time() - 1
        owner.write_private(path, data)
        self.assertFalse(owner.bind("owner", code))

    def test_live_code_is_not_rotated_on_restart(self):
        first = self.code()
        self.assertEqual(first, self.code())

    def test_group_requires_private_owner_and_one_time_ticket(self):
        with self.assertRaises(ValueError):
            owner.prepare_group_pairing()
        self.assertTrue(owner.bind("dm-owner", self.code()))
        command = owner.prepare_group_pairing()
        self.assertFalse(owner.bind_group("group", "outsider", "/group bind wrong"))
        self.assertTrue(owner.bind_group("group", "member-owner", command))
        self.assertTrue(owner.group_allowed("group", "member-owner"))
        self.assertFalse(owner.group_allowed("group", "dm-owner"))
        self.assertFalse(owner.group_allowed("other-group", "member-owner"))
        self.assertFalse(owner.bind_group("group", "outsider", command))
        self.assertEqual((owner.home() / "group.json").stat().st_mode & 0o777, 0o600)

    def test_expired_rotated_unbound_and_changed_owner_group_tickets(self):
        owner.bind("dm-owner", self.code())
        old = owner.prepare_group_pairing()
        command = owner.prepare_group_pairing()
        self.assertFalse(owner.bind_group("group", "member", old))
        path = owner.home() / "group-pairing.json"
        data = json.loads(path.read_text())
        data["expires_at"] = time.time() - 1
        owner.write_private(path, data)
        self.assertFalse(owner.bind_group("group", "member", command))
        command = owner.prepare_group_pairing()
        self.assertTrue(owner.bind_group("group", "member", command))
        owner.unbind_group()
        self.assertFalse(owner.group_allowed("group", "member"))
        command = owner.prepare_group_pairing()
        owner.write_private(owner.home() / "owner.json", {"openid": "new-owner"})
        self.assertFalse(owner.bind_group("group", "member", command))

    def test_application_data_isolated(self):
        owner.bind("winter-owner", self.code())
        owner.bind_group("winter-group", "winter-member", owner.prepare_group_pairing())
        with tempfile.TemporaryDirectory() as other, patch.dict(os.environ, {"HERMES_HOME": other}):
            self.assertEqual(owner.owner_id(), "")
            self.assertFalse(owner.group_allowed("winter-group", "winter-member"))
            owner.bind("atri-owner", self.code())
            owner.bind_group("atri-group", "atri-member", owner.prepare_group_pairing())
            self.assertFalse(owner.group_allowed("winter-group", "winter-member"))
        self.assertEqual(owner.owner_id(), "winter-owner")
        self.assertTrue(owner.group_allowed("winter-group", "winter-member"))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        environment = patch.dict(os.environ, {"HERMES_HOME": self.temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        app.terminal_relay._session.clear()
        app.terminal_relay._checkpoint_root = None
        self.owner_patch = patch.object(owner, "owner_id", return_value="owner")
        self.owner_patch.start()
        self.addCleanup(self.owner_patch.stop)
        self.adapter = SimpleNamespace(send=AsyncMock(), send_with_keyboard=AsyncMock())
        self.gateway = app.TerminalGateway(self.adapter)

    def event(self, user="owner", chat="owner", kind="dm", text="hello"):
        return SimpleNamespace(source=SimpleNamespace(platform=Platform.QQBOT, user_id=user, chat_id=chat, chat_type=kind),
                               text=text, raw_message={"content": text}, message_id="fixture-message")

    async def test_unauthorized_and_group_events_never_reach_bridge(self):
        with patch.object(app.terminal_relay, "handle") as handle, patch.object(
                app.terminal_relay, "record_activity", AsyncMock()) as activity:
            for event in (self.event(user="other"), self.event(chat="other"), self.event(kind="group")):
                await self.gateway.dispatch(event)
        handle.assert_not_called()
        activity.assert_not_awaited()
        self.adapter.send.assert_not_awaited()

    async def test_bound_group_member_reaches_bridge_but_other_members_do_not(self):
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        with patch.object(app.terminal_relay, "handle", return_value=True) as handle:
            await self.gateway.dispatch(self.event(user="test-member", chat="test-group", kind="group"))
            handle.assert_called_once()
            handle.reset_mock()
            for user, chat in (("other", "test-group"), ("test-member", "other-group"), ("owner", "test-group")):
                await self.gateway.dispatch(self.event(user=user, chat=chat, kind="group"))
            handle.assert_not_called()

    async def test_group_intake_authorizes_before_attachments_and_normalizes_raw_text(self):
        adapter = app.TerminalAdapter(PlatformConfig(extra={"app_id": "123"}))
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        data = {"group_openid": "test-group", "attachments": [{"url": "https://example.org/file"}]}
        with patch.object(adapter, "_ingest", AsyncMock()) as ingest:
            await adapter._handle_group_message(data, "id", "@bot /tmux ls", {"member_openid": "other"}, "")
            ingest.assert_not_awaited()
            data = {"group_openid": "test-group"}
            for text, expected in (("@" + app.bot_name() + " /tmux ls", "/tmux ls"), ("<@!123> /model", "/model"),
                                    ("<@123> @someone hello", "@someone hello"),
                                    ("@someone hello", "@someone hello"), ("/tmux ls", "/tmux ls")):
                await adapter._handle_group_message(data, "id", text, {"member_openid": "test-member"}, "")
                self.assertEqual(ingest.call_args.args[0]["content"], expected)
                self.assertEqual(ingest.call_args.kwargs["qq_chat_type"], "group")

    async def test_terminal_rejects_owner_upload_before_download(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        adapter.send = AsyncMock()
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        with patch.object(adapter, "_process_attachments", AsyncMock()) as download, \
                patch.object(app.terminal_relay, "handle") as handle:
            await adapter._on_message("GROUP_AT_MESSAGE_CREATE", {
                "id": "owner-upload", "group_openid": "test-group",
                "author": {"member_openid": "test-member"},
                "attachments": [{"url": "https://example.org/sample.zip"}]})
            await adapter._on_message("C2C_MESSAGE_CREATE", {
                "id": "private-upload", "author": {"user_openid": "owner"},
                "attachments": [{"url": "https://example.org/sample.zip"}]})
        download.assert_not_awaited()
        handle.assert_not_called()
        self.assertEqual(adapter.send.await_count, 2)
        self.assertIn("仅接收文字", adapter.send.call_args.args[1])

    async def test_download_and_clear_do_not_reach_terminal(self):
        with patch.object(app.terminal_relay, "handle") as handle:
            for text in ("/download /srv/example.zip", "/files clear", "/tmux files clear"):
                await self.gateway.dispatch(self.event(text=text))
        handle.assert_not_called()
        self.assertEqual(self.adapter.send.await_count, 3)

    async def test_private_group_bind_never_enters_selected_terminal(self):
        with patch.object(app.terminal_relay, "handle") as handle:
            await self.gateway.dispatch(self.event(text="/group bind"))
        handle.assert_not_called()
        self.assertIn("/group bind ", self.adapter.send.call_args.args[1])

    async def test_group_pairing_consumes_ticket_without_running_terminal(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        command = owner.prepare_group_pairing()
        adapter.send = AsyncMock()
        with patch.object(adapter, "_ingest", AsyncMock()) as ingest:
            await adapter._handle_group_message({"group_openid": "test-group"}, "id", command,
                                                {"member_openid": "test-member"}, "")
            ingest.assert_not_awaited()
        self.assertTrue(owner.group_allowed("test-group", "test-member"))
        adapter.send.assert_awaited_once()

    async def test_real_group_event_pipeline_preserves_text_and_exit_command(self):
        adapter = app.TerminalAdapter(PlatformConfig(extra={"app_id": "123"}))
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        # Exercise SDK _on_message and _ingest, not a fabricated gateway event.
        with patch.object(app.terminal_relay, "handle", return_value=True) as handle:
            for index, text in enumerate(("/tmux exit", "/tmux key enter", "@someone literal terminal input")):
                await adapter._on_message("GROUP_AT_MESSAGE_CREATE", {
                    "id": "incoming-" + str(index), "group_openid": "test-group",
                    "author": {"member_openid": "test-member"}, "content": text,
                    "timestamp": "2026-10-07T06:00:00Z"})
                event = handle.call_args.args[0]
                self.assertEqual(event.raw_message["content"], text)
                self.assertEqual(event.source.chat_type, "group")
                self.assertEqual(event.source.user_id, "test-member")
            self.assertEqual(handle.call_count, 3)
        self.assertEqual(adapter._guess_chat_type("test-group"), "group")

    async def test_real_group_event_pipeline_rejects_other_member_before_download(self):
        adapter = app.TerminalAdapter(PlatformConfig(extra={"app_id": "123"}))
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        with patch.object(adapter, "_process_attachments", AsyncMock()) as attachments, \
                patch.object(app.terminal_relay, "handle") as handle:
            await adapter._on_message("GROUP_AT_MESSAGE_CREATE", {
                "id": "outsider-event", "group_openid": "test-group",
                "author": {"member_openid": "other"}, "content": "/tmux exit",
                "attachments": [{"url": "https://example.org/file"}]})
        attachments.assert_not_awaited()
        handle.assert_not_called()

    async def test_other_chat_cannot_steal_active_delivery_progress(self):
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        app.terminal_relay._session.update(chat_id="owner", user_id="owner", chat_type="dm")
        with patch.object(app.terminal_relay, "active", return_value=True), \
                patch.object(app.terminal_relay, "handle") as handle, \
                patch.object(app.terminal_relay, "record_activity", AsyncMock()) as activity:
            await self.gateway.dispatch(self.event(user="test-member", chat="test-group", kind="group", text="/model"))
        handle.assert_not_called()
        activity.assert_awaited_once_with("", "user:fixture-message")
        self.assertIn("原聊天 /tmux exit", self.adapter.send.call_args.args[1])
        self.assertEqual(app.terminal_relay._session["chat_id"], "owner")

    async def test_group_recovery_preserves_group_route_and_rejects_other_identity(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        session = {"chat_id": "test-group", "user_id": "test-member", "chat_type": "group"}
        source = adapter.gateway._terminal_recovery_source(session)
        self.assertEqual(source.chat_type, "group")
        self.assertEqual(adapter._guess_chat_type("test-group"), "group")
        session["user_id"] = "other"
        self.assertIsNone(adapter.gateway._terminal_recovery_source(session))

    async def test_group_outbound_counts_only_current_chat(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        app.terminal_relay._session.update(chat_id="test-group", chat_type="group", selection_token="selection")
        with patch.object(app.terminal_relay, "activity_scope", return_value="selection"), \
                patch.object(app.terminal_relay, "record_activity", AsyncMock()) as activity, \
                patch.object(QQAdapter, "send", AsyncMock(return_value=SimpleNamespace(success=True, message_id="id"))):
            await adapter.send("owner", "private status")
            activity.assert_awaited_once_with("", "bot:id")
            activity.reset_mock()
            await adapter.send("test-group", "terminal output")
            activity.assert_awaited_once_with("selection", "bot:id")

    async def test_group_output_uses_official_group_endpoint(self):
        adapter = app.TerminalAdapter(PlatformConfig(extra={"markdown_support": True}))
        with patch.object(adapter, "_api_request", AsyncMock(return_value={"id": "accepted"})) as api:
            result = await adapter._send_group_text("test-group", "group terminal reply")
        self.assertTrue(result.success)
        self.assertEqual(api.call_args.args[:2], ("POST", "/v2/groups/test-group/messages"))
        self.assertNotIn("msg_id", api.call_args.args[2])

    async def test_listing_from_other_chat_does_not_move_existing_observer(self):
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        app.terminal_relay._session.update(chat_id="owner", user_id="owner", chat_type="dm")
        with patch.object(app.terminal_relay, "active", return_value=True), \
                patch.object(app.terminal_relay, "handle") as handle, \
                patch.object(app.terminal_relay, "request", return_value={"message": "pane listing", "pane_shortcuts": []}) as request:
            await self.gateway.dispatch(self.event(user="test-member", chat="test-group", kind="group", text="/tmux ls"))
        handle.assert_not_called()
        request.assert_called_once()
        self.adapter.send.assert_awaited_once()
        self.adapter.send_with_keyboard.assert_not_awaited()
        self.assertEqual(app.terminal_relay._session["chat_id"], "owner")

    async def test_unbind_or_rebind_blocked_while_group_terminal_is_active(self):
        app.terminal_relay._session.update(chat_id="test-group", user_id="test-member", chat_type="group")
        with patch.object(app.terminal_relay, "active", return_value=True), \
                patch.object(owner, "unbind_group") as unbind, \
                patch.object(owner, "prepare_group_pairing") as pairing:
            await self.gateway.dispatch(self.event(text="/group unbind"))
            await self.gateway.dispatch(self.event(text="/group bind"))
        unbind.assert_not_called()
        pairing.assert_not_called()
        self.assertIn("先在群里 /tmux exit", self.adapter.send.call_args.args[1])

    def test_instance_bridge_url_is_pinned_and_loopback_only(self):
        path = app.terminal_relay.root()
        path.mkdir(parents=True)
        for url in ("http://127.0.0.1:18011", "http://127.0.0.1:18011?x=1", "http://localhost:18011",
                    "http://127.0.0.1:18011/", "http://bad@127.0.0.1:18011", "https://example.org"):
            owner.write_private(path / "client.json", {"url": url})
            if url == "http://127.0.0.1:18011":
                with patch.dict(os.environ, {"TMUX_BRIDGE_URL": "http://127.0.0.1:18010"}), self.assertRaises(RuntimeError):
                    app.terminal_relay.bridge_url()
            with patch.dict(os.environ, {"TMUX_BRIDGE_URL": url}):
                if url == "http://127.0.0.1:18011":
                    self.assertEqual(app.terminal_relay.bridge_url(), url)
                else:
                    with self.assertRaises(RuntimeError):
                        app.terminal_relay.bridge_url()

    async def test_help_uses_terminal_help_not_agent(self):
        with patch.object(app.terminal_relay, "handle") as handle:
            await self.gateway.dispatch(self.event(text="/help"))
        handle.assert_not_called()
        self.assertIn("/tmux", self.adapter.send.call_args.args[1])
        self.assertNotIn("返回 Hermes", self.adapter.send.call_args.args[1])

    async def test_unselected_text_returns_selection_not_llm(self):
        with patch.object(app.terminal_relay, "handle", return_value=False) as handle:
            await self.gateway.dispatch(self.event())
        handle.assert_called_once()
        self.adapter.send.assert_awaited_once()
        self.adapter.send_with_keyboard.assert_not_awaited()

    async def test_selected_message_uses_existing_relay(self):
        with patch.object(app.terminal_relay, "handle", return_value=True) as handle:
            await self.gateway.dispatch(self.event(text="/model"))
        handle.assert_called_once()
        self.adapter.send.assert_not_awaited()
        self.adapter.send_with_keyboard.assert_not_awaited()

    async def test_user_upload_help_and_quota_count_as_activity(self):
        for text in ("uploaded attachment", "/help", "/sub2api usage", "/model"):
            with self.subTest(text=text), patch.object(app.terminal_relay, "activity_scope", return_value="selection"), \
                    patch.object(app.terminal_relay, "record_activity", AsyncMock()) as activity, \
                    patch.object(app.terminal_relay, "handle", return_value=True):
                await self.gateway.dispatch(self.event(text=text))
                activity.assert_awaited_once_with("selection", "user:fixture-message")

    async def test_successful_plain_keyboard_and_document_messages_count_as_activity(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        accepted = SimpleNamespace(success=True, message_id="accepted-message")
        for method in ("send", "send_with_keyboard", "send_document"):
            args = ("owner", "body", SimpleNamespace()) if method == "send_with_keyboard" else ("owner", "body")
            native_method = "send" if method == "send_with_keyboard" else method
            with self.subTest(method=method), patch.object(app.terminal_relay, "activity_scope", return_value="selection"), \
                    patch.object(app.terminal_relay, "record_activity", AsyncMock()) as activity, \
                    patch.object(QQAdapter, native_method, AsyncMock(return_value=accepted)):
                self.assertIs(await getattr(adapter, method)(*args), accepted)
                activity.assert_awaited_once_with("selection", "bot:accepted-message")

    async def test_failed_bot_send_does_not_count_as_activity(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        for method in ("send", "send_with_keyboard", "send_document"):
            args = ("owner", "body", SimpleNamespace()) if method == "send_with_keyboard" else ("owner", "body")
            native_method = "send" if method == "send_with_keyboard" else method
            with self.subTest(method=method), patch.object(app.terminal_relay, "activity_scope", return_value="selection"), \
                    patch.object(app.terminal_relay, "record_activity", AsyncMock()) as activity, \
                    patch.object(QQAdapter, native_method, AsyncMock(return_value=SimpleNamespace(success=False, error="HTTP 503"))):
                self.assertFalse((await getattr(adapter, method)(*args)).success)
                activity.assert_not_awaited()

    async def test_send_ack_uses_pre_send_selection_not_newly_selected_pane(self):
        adapter = app.TerminalAdapter(PlatformConfig())

        async def send(*args, **kwargs):
            return SimpleNamespace(success=True, message_id="late-message")

        with patch.object(app.terminal_relay, "activity_scope", side_effect=["original", "new-selection"]) as scope, \
                patch.object(app.terminal_relay, "record_activity", AsyncMock()) as activity, \
                patch.object(QQAdapter, "send", side_effect=send):
            await adapter.send("owner", "late output")
        scope.assert_called_once()
        activity.assert_awaited_once_with("original", "bot:late-message")

    async def test_quota_is_intercepted_before_selected_terminal(self):
        with patch.object(app.terminal_relay, "handle") as handle:
            await self.gateway.dispatch(self.event(text="/sub2api usage"))
        handle.assert_not_called()
        self.assertIn("不提供额度查询", self.adapter.send.call_args.args[1])
        self.assertFalse(hasattr(self.gateway, "usage"))

    async def test_unauthorized_quota_never_fetches(self):
        with patch.object(app.terminal_relay, "handle") as handle:
            for event in (self.event(user="other", text="/sub2api usage"),
                          self.event(kind="group", text="/sub2api usage")):
                await self.gateway.dispatch(event)
        handle.assert_not_called()
        self.adapter.send.assert_not_awaited()

    async def test_hash_commands_are_not_typed_into_terminal(self):
        with patch.object(app.terminal_relay, "handle") as handle:
            for text in ("#tmux select 1", "#help", "#tmux button old-token enter"):
                await self.gateway.dispatch(self.event(text=text))
        handle.assert_not_called()
        self.assertEqual(self.adapter.send.await_count, 3)
        self.assertIn("/tmux help", self.adapter.send.call_args.args[1])

    async def test_group_tmux_help_has_all_slash_controls_without_buttons(self):
        owner.write_private(owner.home() / "group.json", {
            "owner_openid": "owner", "group_openid": "test-group", "member_openid": "test-member"})
        adapter = app.TerminalAdapter(PlatformConfig())
        adapter.send = AsyncMock()
        await adapter._on_message("GROUP_AT_MESSAGE_CREATE", {
            "id": "help-in-group", "group_openid": "test-group",
            "author": {"member_openid": "test-member"}, "content": "/tmux help"})
        text = adapter.send.call_args.args[1]
        for command in ("select", "key", "type", "send", "list100", "screen", "reconnect", "exit", "help"):
            self.assertIn("/tmux " + command, text)
        self.assertNotIn("#tmux", text)
        self.assertNotIn("#help", text)

    async def test_legacy_keyboard_send_emits_plain_text_only(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        accepted = SimpleNamespace(success=True, message_id="plain")
        with patch.object(QQAdapter, "send", AsyncMock(return_value=accepted)) as send, \
                patch.object(QQAdapter, "send_with_keyboard", AsyncMock()) as keyed:
            result = await adapter.send_with_keyboard("owner", "terminal response", SimpleNamespace())
        self.assertIs(result, accepted)
        keyed.assert_not_awaited()
        send.assert_awaited_once()
        self.assertFalse(adapter.terminal_keyboards_enabled)

    async def test_adapter_bypasses_agent_busy_pipeline(self):
        adapter = app.TerminalAdapter(PlatformConfig(extra={"app_id": "fixture", "client_secret": "fixture"}))
        adapter.gateway.dispatch = AsyncMock()
        with patch.object(QQAdapter, "handle_message", side_effect=AssertionError("agent pipeline entered")):
            await adapter.handle_message(self.event())
        adapter.gateway.dispatch.assert_awaited_once()

    async def test_unknown_sender_is_rejected_before_attachment_processing(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        with patch.object(QQAdapter, "_handle_c2c_message", AsyncMock()) as process:
            await adapter._handle_c2c_message({"attachments": [{"url": "https://example.org/large"}]}, "id", "hello",
                                              {"user_openid": "other"}, "")
        process.assert_not_awaited()

    async def test_voice_never_calls_a_paid_stt(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        self.assertIsNone(await adapter._stt_voice_attachment("https://example.org/voice", "audio/wav", "voice"))

    def test_only_service_messages_are_renamed(self):
        self.assertEqual(app.service_message("已退出终端，返回 Hermes。"), "已退出终端。")
        text = "```text\nCodex reply: 返回 Hermes\n```"
        self.assertEqual(app.service_message(text), text)


if __name__ == "__main__":
    unittest.main()
