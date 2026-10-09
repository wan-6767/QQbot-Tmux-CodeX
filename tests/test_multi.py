import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from gateway.config import Platform
from tmux_bot import owner
from tmux_bot.bridge import PaneLocks, Relay, RelayError, Tmux, normalize_key, KEY_NAMES
from tmux_bot.multiplex import MultiRelay, parse, HELP as TMUX_HELP
from tmux_bot.multi_relay import Channel, MultiGateway, HELP as BOT_HELP
from tmux_bot.remote import load_hosts, RemoteTmux, TmuxFleet, worker_source
from test_relay import TmuxFixture, MENU, TOOL_RECORDS

SOURCE = {"chat_id": "owner", "user_id": "owner", "chat_type": "dm"}


class ModularCommandTests(unittest.TestCase):
    def test_send_is_visible_in_help_and_invalid_bare_command_guidance(self):
        for help_text in (TMUX_HELP, BOT_HELP):
            self.assertIn("/tmux sel 001 send 文字", help_text)
        self.assertIn("/tmux sel 001 send /goal resume", TMUX_HELP)
        with self.assertRaises(RelayError) as caught:
            parse("/goal resume")
        self.assertIn("/tmux sel 001 send 内容", str(caught.exception))

    def test_send_preserves_terminal_slash_commands_and_multiline_body(self):
        for body in ("/goal resume", "/model", "/permissions", "两行\n/goal resume"):
            with self.subTest(body=body):
                self.assertEqual(parse("/tmux sel 008 send " + body), ("send", "008", body))
                self.assertEqual(parse("/tmux send " + body + " sel 008"), ("send", "008", body))

    def test_target_and_operation_modules_can_exchange_order(self):
        for body, expected in (("ent", ("ent", "001", "")), ("ext", ("ext", "001", "")),
                ("tail 250", ("tail", "001", "250")), ("tail100", ("tail", "001", "100")),
                ("key ctrl+shift+left", ("key", "001", "ctrl+shift+left")),
                ("type 两行\n文字", ("type", "001", "两行\n文字")),
                ("send /model", ("send", "001", "/model"))):
            for command in (f"/tmux sel 001 {body}", f"/tmux {body} sel 001"):
                with self.subTest(command=command):
                    self.assertEqual(parse(command), expected)

    def test_literals_are_not_searched_for_embedded_modules(self):
        self.assertEqual(parse("/tmux sel 002 说明 sel 001 tail 100"), ("send", "002", "说明 sel 001 tail 100"))
        self.assertEqual(parse("/tmux sel 002 send list100"), ("send", "002", "list100"))
        self.assertEqual(parse("/tmux send 文本 sel 003 sel 002"), ("send", "002", "文本 sel 003"))

    def test_old_snapshot_and_malformed_reserved_operations_are_rejected(self):
        for body in ("list100", "LIST100", "list100 sel 002", "tail", "tail 0", "tail -1", "tail 5001",
                     "tail 100 more", "tail 100 sel 002", "ent sel 002", "ext more", "key", "type", "send"):
            with self.subTest(body=body), self.assertRaises(RelayError):
                parse("/tmux sel 001 " + body)
        for command in ("/tmux ent sel 000", "/tmux tail 100 sel 1", "/tmux sel 001", "/tmux blah sel 001"):
            with self.assertRaises(RelayError):
                parse(command)

    def test_keyboard_mapping_families_and_notation(self):
        examples = {"Enter": "Enter", "esc": "Escape", "delete": "DC", "backspace": "BSpace", "insert": "IC",
                    "pageup": "PPage", "pagedown": "NPage", "F24": "F24", "A": "A", "1": "1", "-": "-", "+": "+",
                    "ctrl+c": "C-c", "C-c": "C-c", "alt-enter": "M-Enter", "shift+tab": "BTab",
                    "ctrl+shift+left": "C-S-Left", "Ctrl+Alt+Delete": "C-M-DC", "shift+a": "A", "shift+1": "!",
                    "ctrl+space": "C-Space", "ctrl+plus": "C-+", "ctrl+-": "C--"}
        for value, expected in examples.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_key(value), expected)
        for value in KEY_NAMES:
            self.assertIsInstance(normalize_key(value), str)
        for value in ("unknown", "ctrl+ctrl+a", "ctrl+", "a b", "F25", "--", "\n", "中文", "mouse1"):
            with self.subTest(value=value), self.assertRaises(RelayError):
                normalize_key(value)


class MultiBridgeTests(TmuxFixture):
    def setUp(self):
        super().setUp()
        script = 'import sys; print("SECOND_READY", flush=True); [print("SECOND:"+line.rstrip("\\n"), flush=True) for line in sys.stdin]'
        self.tmux.run("new-window", "-t", "relay-test", f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}")
        self.root = Path(self.temp.name) / "multi"
        self.locks = PaneLocks(Path(self.temp.name) / "locks", self.socket, "winter")
        self.multi = MultiRelay(self.tmux, self.root, self.locks)
        self.route("/tmux ls")

    def tearDown(self):
        self.multi.close()
        super().tearDown()

    def route(self, text, message="", source=None):
        return self.multi.route(text, message, source or SOURCE)

    def test_two_connected_panes_receive_only_their_numbered_input(self):
        a, b = self.route("/tmux sel 001 ent"), self.route("/tmux sel 002 ent")
        self.assertTrue(a["active"] and b["active"])
        self.assertEqual(len(self.locks.held), 2)
        self.route("/tmux sel 001 first-随机中文", "input-a")
        self.route("/tmux sel 002 second-随机中文", "input-b")
        self.wait_for("ECHO:first-随机中文")
        deadline = time.monotonic() + 3
        while "SECOND:second-随机中文" not in self.tmux.capture("%1") and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertIn("SECOND:second-随机中文", self.tmux.capture("%1"))
        self.assertNotIn("second-随机中文", self.tmux.capture("%0"))
        self.assertNotIn("first-随机中文", self.tmux.capture("%1"))
        self.assertEqual([c["channel"] for c in self.multi.state()["channels"]], ["001", "002"])

    def test_exit_one_keeps_other_lease_and_tasks_alive(self):
        self.route("/tmux sel 001 ent")
        self.route("/tmux sel 002 ent")
        self.route("/tmux sel 001 ext")
        self.assertEqual(len(self.locks.held), 1)
        self.assertTrue(self.multi.channels["002"].selected)
        self.assertEqual(len(self.tmux.panes()), 2)
        other = Relay(self.tmux, pane_locks=PaneLocks(Path(self.temp.name) / "locks", self.socket, "other"))
        try:
            self.assertTrue(other.route("#tmux select %0")["active"])
            self.assertFalse(other.route("#tmux select %1").get("initial_context", False))
        finally:
            other.pane_locks.release_except()

    def test_numbers_persist_through_reorder_rename_and_restart(self):
        original = {p["identity"]: n for n, p in self.multi._refresh()}
        panes = self.tmux.panes()
        with patch.object(self.tmux, "panes", return_value=list(reversed(panes))):
            self.assertEqual({p["identity"]: n for n, p in self.multi._refresh()}, original)
        self.tmux.run("rename-session", "-t", "relay-test", "renamed")
        self.route("/tmux sel 001 ent")
        self.route("/tmux sel 002 ent")
        self.multi.close()
        self.multi = MultiRelay(self.tmux, self.root, self.locks)
        self.assertEqual({p["identity"]: n for n, p in self.multi._refresh()}, original)
        self.assertEqual(len(self.multi.state()["channels"]), 2)
        self.assertIn("renamed", self.route("/tmux ls")["message"])

    def test_replaced_pane_never_reuses_the_old_number(self):
        self.tmux.run("respawn-pane", "-k", "-t", "%0", "sleep 60")
        self.assertTrue(self.route("/tmux sel 001 ent")["error"])
        self.route("/tmux ls")
        self.assertEqual(self.multi._row("003")[1]["pane_id"], "%0")
        self.assertTrue(self.route("/tmux sel 003 ent")["active"])

    def test_new_panes_only_increment_after_closed_panes_and_registry_restart(self):
        for expected in range(3, 7):
            native = self.tmux.run("new-window", "-P", "-F", "#{pane_id}", "-t", "relay-test", "sleep 60").strip()
            assigned = {p["pane_id"]: n for n, p in self.multi._refresh()}
            self.assertEqual(assigned[native], f"{expected:03d}")
            self.tmux.run("kill-pane", "-t", native)
        self.multi.close()
        self.multi = MultiRelay(self.tmux, self.root, self.locks)
        native = self.tmux.run("new-window", "-P", "-F", "#{pane_id}", "-t", "relay-test", "sleep 60").strip()
        assigned = {p["pane_id"]: n for n, p in self.multi._refresh()}
        self.assertEqual(assigned[native], "007")
        self.assertEqual(assigned["%0"], "001")
        self.assertEqual(assigned["%1"], "002")
        self.assertEqual(self.multi.db.execute("SELECT count(*) FROM terminals").fetchone()[0], 7)

    def test_numbers_never_wrap_after_999(self):
        self.multi.db.execute("INSERT INTO terminals VALUES ('999','retired-identity','{}',NULL)")
        self.multi.db.commit()
        self.tmux.run("new-window", "-t", "relay-test", "sleep 60")
        response = self.route("/tmux ls")
        self.assertTrue(response["error"])
        self.assertIn("编号已用尽", response["message"])
        self.assertEqual(self.multi._row("001")[1]["pane_id"], "%0")

    def test_idle_and_activity_are_independent_and_late_acks_do_not_renew(self):
        a = self.route("/tmux sel 001 ent")
        b = self.route("/tmux sel 002 ent")
        self.multi.channels["001"].touched -= 1801
        self.assertTrue(self.multi.activity("002", b["selection_token"], "bot:message")["recorded"])
        state = self.multi.state()["channels"]
        self.assertFalse(state[0]["active"])
        self.assertEqual(state[0]["reason"], "idle")
        self.assertTrue(state[1]["active"])
        self.assertFalse(self.multi.activity("001", a["selection_token"], "late")["recorded"])
        self.assertTrue(self.multi.acknowledge_close("001", state[0]["epoch"])["acknowledged"])
        self.assertEqual(len(self.multi.state()["channels"]), 1)

    def test_deduplication_is_global_across_channels_and_persistent(self):
        self.route("/tmux sel 001 ent")
        self.route("/tmux sel 002 ent")
        self.route("/tmux sel 001 exactly-once-fixture", "same-message")
        self.assertTrue(self.route("/tmux sel 002 must-not-be-sent", "same-message")["duplicate"])
        self.wait_for("ECHO:exactly-once-fixture")
        self.multi.close()
        self.multi = MultiRelay(self.tmux, self.root, self.locks)
        self.assertTrue(self.route("/tmux sel 002 must-not-be-sent", "same-message")["duplicate"])
        self.assertNotIn("must-not-be-sent", self.tmux.capture("%1"))
        self.assertEqual(self.tmux.capture("%0").count("ECHO:exactly-once-fixture"), 1)

    def test_unknown_submission_is_not_retried_and_child_receipt_can_recover(self):
        self.route("/tmux sel 001 ent")
        key = self.multi.receipt_key(SOURCE, "crash-input")
        self.multi.db.execute("INSERT INTO receipts VALUES (?,'unknown','001',NULL)", (key,))
        self.multi.db.commit()
        self.assertTrue(self.route("/tmux sel 001 never-repeat", "crash-input")["uncertain"])
        self.assertNotIn("never-repeat", self.tmux.capture("%0"))
        self.multi.channels["001"].route("#tmux send recovered-accepted", key)
        self.assertEqual(self.multi.receipt(key)["status"], "complete")

    def test_chat_sources_cannot_steal_a_connection_or_progress(self):
        self.route("/tmux sel 001 ent")
        group = {"chat_id": "group", "user_id": "member", "chat_type": "group"}
        for body in ("ent", "ext", "tail 100", "secret"):
            self.assertTrue(self.route("/tmux sel 001 " + body, source=group)["error"])
        self.assertTrue(self.route("/tmux sel 002 ent", source=group)["active"])
        self.assertEqual(self.multi._row("001")[2], SOURCE)
        self.assertEqual(self.multi._row("002")[2], group)

    def test_unselected_text_and_old_commands_never_reach_a_terminal(self):
        self.route("/tmux sel 001 ent")
        for command in ("ordinary-text", "/model", "/tmux exit", "/tmux select 1", "#tmux ls", "/tmux sel 000 ent", "/tmux sel 1 ent"):
            self.assertTrue(self.route(command)["error"])
        self.assertNotIn("ordinary-text", self.tmux.capture("%0"))
        self.assertTrue(self.route("/tmux sel 002 not-connected")["error"])

    def test_type_key_reserved_literals_and_100_line_snapshot(self):
        self.route("/tmux sel 001 ent")
        self.route("/tmux sel 001 type draft")
        self.assertNotIn("ECHO:draft", self.tmux.capture("%0"))
        self.route("/tmux sel 001 key enter")
        self.wait_for("ECHO:draft")
        self.route("/tmux sel 001 send ext")
        self.wait_for("ECHO:ext")
        snap = self.route("/tmux sel 001 tail 100")
        self.assertTrue(snap["initial_context"])
        self.assertLessEqual(len(snap["screen"].splitlines()), 100)
        self.assertIn("001 · relay-test", snap["target"])

    def test_send_slash_command_reaches_only_the_target_as_literal_text(self):
        self.route("/tmux sel 001 ent")
        self.route("/tmux sel 002 ent")
        result = self.route("/tmux sel 001 send /goal resume", "send-goal-fixture")
        self.assertTrue(result["submitted"])
        self.wait_for("ECHO:/goal resume")
        self.assertNotIn("/goal resume", self.tmux.capture("%1"))

    def test_reversed_modules_work_on_real_tmux_and_tail_is_raw(self):
        self.assertTrue(self.route("/tmux ent sel 001")["active"])
        self.route("/tmux type modular-中文 sel 001")
        submitted = self.route("/tmux key return sel 001")
        self.assertTrue(submitted["submitted"])
        self.wait_for("ECHO:modular-中文")
        snap = self.route("/tmux tail 1 sel 001")
        self.assertTrue(snap["initial_context"])
        self.assertLessEqual(len(snap["screen"].splitlines()), 1)
        self.assertTrue(self.route("/tmux sel 001 list100")["error"])
        self.assertTrue(self.route("/tmux ext sel 001")["exited"])

    def test_tail_over_1000_lines_requests_enough_history(self):
        self.route("/tmux sel 001 ent")
        history = "\n".join(f"raw-log-{n}" for n in range(1600))
        with patch.object(self.tmux, "capture", return_value=history) as capture:
            snap = self.route("/tmux sel 001 tail 1500")
        capture.assert_called_once_with("%0", 1500)
        self.assertEqual(len(snap["screen"].splitlines()), 1500)
        self.assertIn("raw-log-100", snap["screen"])
        self.assertTrue(snap["initial_context"])

    def test_invalid_key_is_never_sent_as_literal_text(self):
        self.route("/tmux sel 001 ent")
        with patch.object(self.tmux, "run", wraps=self.tmux.run) as run:
            self.assertTrue(self.route("/tmux sel 001 key invalid-key")["error"])
        self.assertFalse(any(call.args[0] == "send-keys" for call in run.call_args_list))

    def test_real_tmux_keyboard_bytes_not_just_mapping_strings(self):
        script = "import os,sys,tty; tty.setraw(0); print('KEYS_READY',flush=True);\nwhile True:\n data=os.read(0,1024); print('BYTES:'+data.hex(),flush=True)"
        native = self.tmux.run("new-window", "-P", "-F", "#{pane_id}", "-t", "relay-test",
                              shlex.join([sys.executable, "-u", "-c", script])).strip()
        deadline = time.monotonic() + 3
        while "KEYS_READY" not in self.tmux.capture(native, 100) and time.monotonic() < deadline:
            time.sleep(.02)
        for key, expected in (("A", b"A"), ("shift+1", b"!"), ("-", b"-"), ("+", b"+"),
                ("ctrl+c", b"\x03"), ("enter", b"\r"), ("shift+tab", b"\x1b[Z"),
                ("alt+x", b"\x1bx"), ("F1", b"\x1bOP"), ("F12", b"\x1b[24~"),
                ("up", b"\x1b[A"), ("delete", b"\x1b[3~"), ("insert", b"\x1b[2~")):
            with self.subTest(key=key):
                self.tmux.key(native, key)
                deadline = time.monotonic() + 3
                while "BYTES:" + expected.hex() not in self.tmux.capture(native, 100) and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertIn("BYTES:" + expected.hex(), self.tmux.capture(native, 100))

    def test_api_rejects_legacy_protocol_and_invalid_ids_without_side_effects(self):
        with self.assertRaises(ValueError):
            self.multi.api("/v1/route", {"text": "#tmux select 1"})
        for channel in ("../../root", "000", "1", 1):
            with self.assertRaises(ValueError):
                self.multi.api("/v2/screen", {"channel": channel, "epoch": 1})
        self.assertTrue(self.multi.api("/v2/screen", {"channel": "999", "epoch": 1})["error"])


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": self.temp.name})
        env.start(); self.addCleanup(env.stop)
        self.adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True, message_id="accepted")),
            send_document=AsyncMock(return_value=SimpleNamespace(success=True, message_id="file")), MAX_MESSAGE_LENGTH=4000, _chat_type_map={})
        self.gateway = MultiGateway(self.adapter)
        self.gateway.request = AsyncMock(return_value={"recorded": True})
        self.gateway.authorized = lambda source: source == SOURCE
        self.a = Channel(self.gateway, self.result("001", "OLD_A"))
        self.b = Channel(self.gateway, self.result("002", "OLD_B"))

    def result(self, number, screen, **extra):
        return {"channel": number, "target": number + " · work:0.0", "source": SOURCE,
                "selection_token": "token-" + number, "epoch": 1, "input_serial": 0,
                "screen_key": "identity-" + number, "screen": screen, "viewport": screen,
                "active": True, **extra}

    async def test_interleaved_channels_have_independent_baselines_and_labels(self):
        for channel, number, baseline in ((self.a, "001", "OLD_A"), (self.b, "002", "OLD_B")):
            await channel.deliver(self.result(number, baseline, initial_context=True), snapshot=True)
        self.adapter.send.reset_mock()
        await asyncio.gather(self.a.deliver(self.result("001", "OLD_A\n\nAnswer A")),
                             self.b.deliver(self.result("002", "OLD_B\n\nAnswer B")))
        messages = [c.args[1] for c in self.adapter.send.call_args_list]
        self.assertEqual(len(messages), 2)
        self.assertTrue(any("[001 ·" in m and "Answer A" in m and "Answer B" not in m for m in messages))
        self.assertTrue(any("[002 ·" in m and "Answer B" in m and "Answer A" not in m for m in messages))
        await self.a.deliver(self.result("001", "RAW_A", initial_context=True), snapshot=True)
        self.assertEqual(self.b.screen, "OLD_B\n\nAnswer B")

    async def test_tools_hidden_errors_preserved_and_menu_complete(self):
        self.a.seed(self.result("001", "› question\n"))
        await self.a.deliver(self.result("001", "› question\n\n" + TOOL_RECORDS + "\n\n• HTTP 503 upstream unavailable\n\n• Real answer here\n"))
        messages = "\n".join(c.args[1] for c in self.adapter.send.call_args_list)
        self.assertIn("HTTP 503", messages)
        self.assertIn("Real answer here", messages)
        self.assertNotIn("operation-output", messages)
        self.adapter.send.reset_mock()
        await self.a.deliver(self.result("001", MENU))
        self.assertIn(MENU, self.adapter.send.call_args.args[1])

    async def test_failed_batch_resumes_after_restart_without_replaying_accepted_body(self):
        self.a.seed(self.result("001", "› question\n"))
        self.adapter.send.side_effect = [SimpleNamespace(success=True, message_id="first"), SimpleNamespace(success=False, error="HTTP 429")]
        after = self.result("001", "› question\n\n• First paragraph\n\nSecond paragraph\n")
        self.assertFalse(await self.a.deliver(after))
        self.assertEqual(self.a.pending["next"], 1)
        restored = Channel(self.gateway, after)
        self.adapter.send.side_effect = None
        self.adapter.send.reset_mock()
        self.assertTrue(await restored.deliver(after))
        messages = "\n".join(c.args[1] for c in self.adapter.send.call_args_list)
        self.assertIn("Second paragraph", messages)
        self.assertNotIn("First paragraph", messages)
        self.assertEqual(self.b.screen, None)

    async def test_failed_send_does_not_renew_and_success_renews_only_its_channel(self):
        self.adapter.send.return_value = SimpleNamespace(success=False, error="HTTP 503")
        self.assertFalse(await self.a.frame("failure"))
        self.gateway.request.assert_not_awaited()
        self.adapter.send.return_value = SimpleNamespace(success=True, message_id="new")
        self.assertTrue(await self.b.frame("success"))
        self.gateway.request.assert_awaited_once_with("/v2/activity", {"channel": "002", "selection_token": "token-002", "event_id": "bot:new"})

    async def test_large_snapshot_is_one_attachment_with_id_and_temp_cleanup(self):
        self.assertTrue(await self.a.frame("长内容" * 3000))
        self.adapter.send_document.assert_awaited_once()
        self.adapter.send.assert_not_awaited()
        self.assertIn("001", self.adapter.send_document.call_args.kwargs["caption"])
        self.assertFalse(list((self.a.path.parent / "outgoing").glob("*.txt")))

    async def test_update_never_cancels_an_inflight_send_or_another_channel(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_send(*args, **kwargs):
            entered.set(); await release.wait()
            return SimpleNamespace(success=True, message_id="slow")
        self.adapter.send.side_effect = slow_send
        self.a.inflight = asyncio.create_task(self.a.frame("Old frame"))
        await entered.wait()
        with patch.object(self.a, "watch", AsyncMock()):
            update = asyncio.create_task(self.a.update(self.result("001", "next", epoch=2), "key", "up"))
            await asyncio.sleep(.02)
            self.assertFalse(update.done())
            release.set()
            await update
            await self.a.task
        self.assertTrue(self.a.inflight.result())
        self.assertEqual(self.b.meta["epoch"], 1)

    async def test_both_watchers_append_without_any_new_qq_input(self):
        for channel, number, baseline in ((self.a, "001", "OLD_A"), (self.b, "002", "OLD_B")):
            channel.seed(self.result(number, baseline))
        unavailable = {"001"}
        async def capture(path, payload):
            if path == "/v2/activity":
                return {"recorded": True}
            number = payload["channel"]
            if number in unavailable:
                unavailable.remove(number)
                return {"active": False, "error": True, "message": "transient SSH failure"}
            return self.result(number, {"001": "OLD_A\n\nAnswer A", "002": "OLD_B\n\nAnswer B"}[number])
        self.gateway.request.side_effect = capture
        self.a.task = asyncio.create_task(self.a.watch())
        self.b.task = asyncio.create_task(self.b.watch())
        self.gateway.channels = {"001": self.a, "002": self.b}
        try:
            deadline = asyncio.get_running_loop().time() + 8
            while self.adapter.send.await_count < 2 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(.05)
            self.assertEqual(self.adapter.send.await_count, 2)
            messages = [call.args[1] for call in self.adapter.send.call_args_list]
            self.assertTrue(any("[001 ·" in m and "Answer A" in m for m in messages))
            self.assertTrue(any("[002 ·" in m and "Answer B" in m for m in messages))
            self.assertFalse(any("OLD_" in m for m in messages))
            self.assertTrue(all(call.args[0] in {"/v2/screen", "/v2/activity"} for call in self.gateway.request.call_args_list))
        finally:
            await self.gateway.stop()

    async def test_attachment_does_not_bypass_revoked_chat_authorization(self):
        self.gateway.authorized = lambda source: False
        self.assertFalse(await self.a.frame("private" * 1000))
        self.adapter.send_document.assert_not_awaited()


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key, self.known = self.root / "key", self.root / "known_hosts"
        self.key.write_text("fixture key"); self.key.chmod(0o600)
        self.known.write_text("fixture known_hosts")
        self.item = {"name": "remote", "host": "server.example.com", "port": 2222, "user": "user",
                     "identity_file": str(self.key), "known_hosts_file": str(self.known)}
        self.path = self.root / "hosts.json"

    def config(self, item):
        self.path.write_text(json.dumps({"version": 1, "servers": [item]})); self.path.chmod(0o600)
        return load_hosts(self.path)

    def test_host_configuration_permissions_and_injection_validation(self):
        self.assertEqual(self.config(self.item)[0]["port"], 2222)
        for changes in ({"host": "server;touch /tmp/no"}, {"user": "-oProxyCommand=bad"}, {"port": True},
                        {"name": "local"}, {"socket": "relative/socket"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.config({**self.item, **changes})
        self.key.chmod(0o644)
        with self.assertRaises(ValueError): self.config(self.item)
        self.key.chmod(0o600)
        self.config(self.item); self.path.chmod(0o644)
        with self.assertRaises(ValueError): load_hosts(self.path)

    def test_ssh_strict_verification_and_data_only_stdin(self):
        remote = RemoteTmux(self.config(self.item)[0], self.root / "control")
        with patch("tmux_bot.remote.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout=b'{"ok":true,"result":null}')) as run:
            remote.send("%1", "$(touch /tmp/never); '\" 中文\nline")
        command = run.call_args.args[0]
        self.assertIn("StrictHostKeyChecking=yes", command)
        self.assertIn("IdentitiesOnly=yes", command)
        self.assertEqual(command[1:3], ["-F", "/dev/null"])
        self.assertNotIn("$(touch", " ".join(command))
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertIn("$(touch", payload["args"][1])

    def test_long_installation_paths_use_private_instance_scoped_short_sockets(self):
        directory = self.root / ("long-installation-" * 7) / "control"
        remote = RemoteTmux(self.config(self.item)[0], directory)
        control = Path(next(x.split("=", 1)[1] for x in remote.command if x.startswith("ControlPath=")))
        self.assertLessEqual(len(str(control).encode()), 85)
        self.assertEqual(control.parent.stat().st_mode & 0o777, 0o700)
        other = RemoteTmux(self.item, directory.parent / "other")
        self.assertNotEqual(remote.command, other.command)

    def test_shared_or_symlinked_control_directory_is_rejected(self):
        control = self.root / "control"
        control.mkdir(mode=0o755)
        with self.assertRaisesRegex(ValueError, "unsafe SSH"):
            RemoteTmux(self.item, control)
        control.chmod(0o700)
        link = self.root / "link"
        link.symlink_to(control)
        with self.assertRaisesRegex(ValueError, "unsafe SSH"):
            RemoteTmux(self.item, link)

    def test_remote_worker_runs_the_real_tmux_class_not_a_mock(self):
        socket = str(self.root / "worker.sock")
        tmux = Tmux(socket)
        try:
            tmux.run("new-session", "-d", "-s", "worker", "cat")
            def rpc(method, *args):
                result = subprocess.run([sys.executable, "-c", worker_source()], input=json.dumps({"socket": socket, "method": method, "args": args}).encode(), capture_output=True, check=True)
                data = json.loads(result.stdout)
                self.assertTrue(data["ok"], data)
                return data["result"]
            self.assertEqual(rpc("panes")[0]["pane_id"], "%0")
            rpc("send", "%0", "SSH-worker-真实中文")
            deadline = time.monotonic() + 3
            while "SSH-worker-真实中文" not in rpc("capture", "%0", 100) and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertIn("SSH-worker-真实中文", rpc("capture", "%0", 100))
        finally:
            tmux.run("kill-server")

    def test_unreachable_remote_does_not_hide_local_windows(self):
        with patch.object(RemoteTmux, "panes", side_effect=Exception("not used")):
            fleet = TmuxFleet(SimpleNamespace(panes=lambda: [{"pane_id": "%0", "identity": "local-identity", "target": "work:0.0"}]), self.config(self.item), self.root / "control")
        from tmux_bot.bridge import RelayError
        with patch.object(fleet.backends["remote"], "panes", side_effect=RelayError("unreachable")):
            panes = fleet.panes()
        self.assertEqual(panes[0]["pane_id"], "local/%0")
        self.assertEqual(panes[0]["identity"], "local-identity")
        self.assertIn("remote", fleet.errors)

    def test_renaming_a_server_does_not_change_or_break_pane_identity(self):
        item = self.config(self.item)[0]
        before = TmuxFleet(SimpleNamespace(panes=lambda: []), [item], self.root / "control")
        after = TmuxFleet(SimpleNamespace(panes=lambda: []), [{**item, "name": "renamed"}], self.root / "control")
        pane = {"pane_id": "%1", "target": "work:0.0", "identity": "server-pid:proc:tty"}
        with patch.object(before.backends["remote"], "panes", return_value=[pane]):
            selected = before.panes()[0]
        with patch.object(after.backends["renamed"], "panes", return_value=[pane]):
            resolved = after.resolve(selected)
        self.assertEqual(selected["identity"], resolved["identity"])
        self.assertEqual(resolved["pane_id"], "renamed/%1")

    def test_every_listing_refreshes_offline_status_and_recovery_keeps_numbers(self):
        local = SimpleNamespace(panes=lambda: [{"pane_id": "%0", "identity": "local-identity", "target": "local-work:0.0"}])
        fleet = TmuxFleet(local, self.config(self.item), self.root / "control")
        remote_pane = {"pane_id": "%1", "identity": "remote-identity", "target": "remote-work:0.0"}
        from tmux_bot.bridge import RelayError
        relay = MultiRelay(fleet, self.root / "registry")
        try:
            with patch.object(fleet.backends["remote"], "panes", side_effect=[[remote_pane], RelayError("SSH连接失败"), [remote_pane]]):
                online = relay.listing()["message"]
                offline = relay.listing()["message"]
                restored = relay.listing()["message"]
            self.assertIn("`002`", online)
            self.assertIn("remote · 连接异常", offline)
            self.assertIn("SSH连接失败", offline)
            self.assertIn("`001`", offline)
            self.assertIn("原编号保留", offline)
            self.assertIn("`002`", restored)
            self.assertNotIn("连接异常", restored)
            self.assertEqual(relay.db.execute("SELECT count(*) FROM terminals").fetchone()[0], 2)
        finally:
            relay.close()


if __name__ == "__main__":
    unittest.main()
