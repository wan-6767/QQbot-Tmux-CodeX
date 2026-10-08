import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "src/tmux_bot/bridge.py"
spec = importlib.util.spec_from_file_location("tmux_relay", MODULE)
relay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay_module)
from tmux_bot import terminal_relay

MENU = """  Select Model and Effort
  Warning: OpenAI base URL is overridden. Selecting models may
  not be supported or work properly.
  1. Model A
  2. Model B
  3. Model C
› 4. Model D (current)
  5. Model E
  enter select · esc back"""

TOOL_RECORDS = """• Explored
  └ Search Hermes|tmux|private|boundary in MEMORY.md
    Read terminal_relay.py, test_relay.py
    Search codex|Ran|Edited|Explored in tmux_relay.py

• Ran printf 'operation-output'
  └ operation-output
    more output after an empty line

• Edited example.py (+2 -1)
    - old_value
    + new_value

◦ Browsing the web

• Opened https://www.bilibili.com/video/BV126eG6AEM2/
  └ video-page output

• Searched for '1.5'
  └ search-result output
    continuation after search

• Failed (exit 1) node --input-type=module -e 'import { bilibiliExpand } from "./dsh/tools/publi...'
  └ {"error":"公开数据源返回 HTTP 412"}
    + Show details

Worked for 6s • 14:30"""


class TmuxFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket = str(Path(self.temp.name) / "tmux.sock")
        self.tmux = relay_module.Tmux(self.socket)
        script = 'import sys; print("READY", flush=True); [print("ECHO:"+line.rstrip("\\n"), flush=True) for line in sys.stdin]'
        self.tmux.run("new-session", "-d", "-s", "relay-test", "-x", "100", "-y", "24",
                      f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}")
        self.relay = relay_module.Relay(self.tmux)
        self.wait_for("READY")

    def tearDown(self):
        try:
            self.tmux.run("kill-server")
        except relay_module.RelayError:
            pass
        self.temp.cleanup()

    def wait_for(self, text):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            screen = self.tmux.capture("%0")
            if text in screen:
                return screen
            time.sleep(0.03)
        self.fail(f"tmux did not show expected fixture text {text!r}")

    def enter(self):
        self.relay.route("#tmux ls")
        result = self.relay.route("#tmux select 1")
        self.assertTrue(result["active"])
        self.assertIn("READY", result["screen"])
        self.assertIn("READY", result["baseline"])
        self.assertEqual(result["screen_key"], self.relay.selected["identity"])


class RealTmuxTests(TmuxFixture):
    def test_unicode_literal_input_screen_and_exit(self):
        self.assertFalse(self.relay.route("普通聊天")["handled"])
        self.enter()
        self.relay.route("中文消息 $(echo nope); '引号'", "qq-1")
        self.wait_for("ECHO:中文消息 $(echo nope); '引号'")
        self.assertIn("中文消息", self.relay.route("#tmux screen")["screen"])
        self.assertFalse(self.relay.route("#tmux exit")["active"])
        self.assertFalse(self.relay.route("回到 Hermes")["handled"])

    def test_type_without_enter_then_key_and_deduplication(self):
        self.enter()
        self.relay.route("#tmux type delayed")
        time.sleep(0.05)
        self.assertNotIn("ECHO:delayed", self.tmux.capture("%0"))
        self.relay.route("#tmux key enter", "qq-key")
        screen = self.wait_for("ECHO:delayed")
        self.assertEqual(screen.count("ECHO:delayed"), 1)
        self.relay.route("again", "qq-input")
        self.wait_for("ECHO:again")
        self.assertTrue(self.relay.route("again", "qq-input")["duplicate"])
        self.assertEqual(self.tmux.capture("%0").count("ECHO:again"), 1)

    def test_exit_command_and_button_offer_list_without_reentering(self):
        for use_button in (False, True):
            with self.subTest(use_button=use_button):
                self.enter()
                text = (f"#tmux button {self.relay.selection_token} exit"
                        if use_button else "#tmux exit")
                result = self.relay.route(text)
                self.assertTrue(result["exited"])
                self.assertFalse(result["active"])
                self.assertEqual(result["message"], "已退出终端，返回 Hermes。")
                self.assertEqual(self.relay.selection_token, "")
                listing = self.relay.route("#tmux ls")
                self.assertTrue(listing["pane_shortcuts"])
                self.assertFalse(listing["active"])
                self.assertFalse(self.relay.route("普通聊天")["handled"])
                self.assertIn("READY", self.tmux.capture("%0"))

    def test_durable_selection_receipts_and_uncertain_submission_survive_restart(self):
        path = Path(self.temp.name) / "bridge.sqlite3"
        relay = relay_module.Relay(self.tmux, state_file=path)
        selected = relay.route("#tmux select %0", "enter-receipt")
        accepted = relay.route("persistent-input", "input-receipt")
        self.wait_for("ECHO:persistent-input")
        epoch, touched = relay.epoch, relay.touched
        self.assertEqual(relay.state()["epoch"], epoch)
        self.assertEqual(relay.touched, touched)
        relay.db.execute("INSERT INTO receipts VALUES ('interrupted-input', 'pending', NULL)")
        relay.db.commit()
        relay.db.close()
        resumed = relay_module.Relay(self.tmux, state_file=path)
        try:
            self.assertEqual(resumed.state()["selection_token"], selected["selection_token"])
            self.assertEqual(resumed.state()["epoch"], epoch)
            self.assertEqual(resumed.receipt("input-receipt")["result"], accepted)
            self.assertTrue(resumed.route("persistent-input", "input-receipt")["duplicate"])
            self.assertTrue(resumed.route("never-repeat-unknown", "interrupted-input")["uncertain"])
            screen = self.tmux.capture("%0")
            self.assertEqual(screen.count("ECHO:persistent-input"), 1)
            self.assertNotIn("never-repeat-unknown", screen)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            resumed.db.close()

    def test_closed_notice_remains_available_after_poll_retry_and_restart(self):
        path = Path(self.temp.name) / "bridge.sqlite3"
        relay = relay_module.Relay(self.tmux, state_file=path)
        epoch = relay.route("#tmux select %0")["epoch"]
        self.tmux.run("respawn-pane", "-k", "-t", "%0", "sleep 30")
        notice = relay.screen(epoch)
        self.assertEqual(notice["reason"], "closed")
        self.assertEqual(relay.screen(epoch)["message"], notice["message"])
        relay.db.close()
        resumed = relay_module.Relay(self.tmux, state_file=path)
        try:
            self.assertEqual(resumed.screen(epoch)["reason"], "closed")
            self.assertEqual(resumed.state()["reason"], "closed")
        finally:
            resumed.db.close()

    def test_fast_text_is_separate_from_automatic_and_explicit_submission(self):
        script = '''import os, sys, time, tty
tty.setraw(sys.stdin.fileno())
print('READY', flush=True)
draft = bytearray()
last_character = 0.0
while True:
    key = os.read(sys.stdin.fileno(), 1)
    now = time.monotonic()
    if key == b'\\r':
        if now - last_character < 0.18:
            print('UNSUBMITTED: Enter treated as pasted newline', flush=True)
            draft.extend(b'\\n')
        else:
            print('SUBMITTED:' + draft.decode('utf-8'), flush=True)
            draft.clear()
    else:
        draft.extend(key)
        last_character = now
'''
        self.tmux.run("respawn-pane", "-k", "-t", "%0",
                      f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}")
        self.wait_for("READY")
        self.enter()
        self.relay.route("回复我你好", "automatic-submit")
        self.wait_for("SUBMITTED:回复我你好")
        self.relay.route("#tmux type manual-enter")
        self.relay.route("#tmux key enter", "explicit-submit")
        screen = self.wait_for("SUBMITTED:manual-enter")
        self.assertNotIn("UNSUBMITTED", screen)
        self.assertEqual(screen.count("SUBMITTED:回复我你好"), 1)
        self.assertEqual(screen.count("SUBMITTED:manual-enter"), 1)

    def test_scoped_button_executes_key_and_old_selection_button_is_rejected(self):
        self.enter()
        token = self.relay.selection_token
        self.relay.route("#tmux type button-test")
        result = self.relay.route(f"#tmux button {token} enter")
        self.assertTrue(result["watch"])
        self.wait_for("ECHO:button-test")
        self.relay.route("#tmux select 1")
        self.assertNotEqual(token, self.relay.selection_token)
        result = self.relay.route(f"#tmux button {token} ctrl-c")
        self.assertIn("过期", result["message"])
        self.assertTrue(result["active"])

    def test_next_does_not_write_to_terminal(self):
        self.enter()
        epoch = self.relay.epoch
        result = self.relay.route("#tmux next")
        self.assertTrue(result["watch"])
        self.assertEqual(result["epoch"], epoch)
        self.assertNotIn("ECHO:", self.tmux.capture("%0"))

    def test_multiline_paste_and_send_literal_reserved_command(self):
        self.enter()
        self.relay.route("第一行\n第二行", "qq-multiline")
        self.wait_for("第二行")
        self.relay.route("#tmux send #help", "qq-literal")
        self.wait_for("ECHO:#help")
        self.assertIsNotNone(self.relay.selected)

    def test_closed_or_replaced_pane_cannot_receive_input(self):
        self.enter()
        self.tmux.run("respawn-pane", "-k", "-t", "%0", "sleep 30")
        result = self.relay.route("must-not-send")
        self.assertFalse(result["active"])
        self.assertIn("被替换", result["message"])

    def test_epoch_prevents_old_watcher_after_exit_or_new_input(self):
        self.enter()
        previous = self.relay.route("first")["epoch"]
        self.relay.route("second")
        self.assertFalse(self.relay.screen(previous)["active"])
        current = self.relay.epoch
        self.relay.route("#tmux exit")
        self.assertFalse(self.relay.screen(current)["active"])

    def test_expiry_consumes_message_before_resuming_agent(self):
        self.enter()
        self.relay.touched -= 1801
        result = self.relay.route("do not execute")
        self.assertTrue(result["handled"])
        self.assertFalse(result["active"])
        self.assertNotIn("do not execute", self.tmux.capture("%0"))

    def test_idle_poll_disconnects_without_input_and_reconnect_is_bound_to_same_pane(self):
        self.enter()
        old_epoch, old_button = self.relay.epoch, self.relay.selection_token
        touched = self.relay.touched
        self.assertTrue(self.relay.screen(old_epoch)["active"])
        self.assertEqual(self.relay.touched, touched)  # polling alone is not a QQ message
        self.relay.touched -= 1800
        notice = self.relay.screen(old_epoch)
        self.assertFalse(notice["active"])
        self.assertEqual(notice["reason"], "idle")
        self.assertIn("/tmux reconnect", notice["message"])
        self.assertEqual(self.relay.screen(old_epoch)["reconnect_token"], notice["reconnect_token"])
        self.assertIn("READY", self.tmux.capture("%0"))
        resumed = self.relay.route("#tmux reconnect " + notice["reconnect_token"])
        self.assertTrue(resumed["active"])
        self.assertTrue(resumed["initial_context"])
        self.assertNotEqual(resumed["selection_token"], old_button)
        self.assertIn("过期", self.relay.route(f"#tmux button {old_button} enter")["message"])
        self.assertIn("过期", self.relay.route("#tmux reconnect " + notice["reconnect_token"])["message"])

    def test_bot_messages_keep_selection_alive_without_user_input(self):
        self.enter()
        epoch, token = self.relay.epoch, self.relay.selection_token
        self.relay.touched -= 1790
        self.assertTrue(self.relay.activity(token, "bot:first")["recorded"])
        self.relay.touched -= 1790
        self.assertTrue(self.relay.screen(epoch)["active"])
        self.assertTrue(self.relay.activity(token, "bot:second")["recorded"])
        self.relay.touched -= 1799
        self.assertTrue(self.relay.screen(epoch)["active"])
        self.relay.touched -= 2
        notice = self.relay.screen(epoch)
        self.assertEqual(notice["reason"], "idle")
        self.assertIn("双方 30 分钟无消息", notice["message"])
        self.assertIn("READY", self.tmux.capture("%0"))

    def test_unforwarded_terminal_output_and_polls_do_not_renew_activity(self):
        self.enter()
        epoch, touched = self.relay.epoch, self.relay.touched
        self.tmux.run("display-message", "-p", "background output")
        self.relay.screen(epoch)
        self.relay.state()
        self.assertEqual(self.relay.touched, touched)

    def test_activity_is_selection_scoped_and_cannot_reopen_expired_connection(self):
        self.enter()
        token = self.relay.selection_token
        touched = self.relay.touched
        self.assertFalse(self.relay.activity("old-selection", "bot:late")["recorded"])
        self.assertEqual(self.relay.touched, touched)
        self.relay.route("#tmux exit")
        self.assertFalse(self.relay.activity(token, "bot:late")["recorded"])
        self.enter()
        touched = self.relay.touched
        self.assertFalse(self.relay.activity(token, "bot:late")["recorded"])
        self.assertEqual(self.relay.touched, touched)
        self.relay.touched -= 1801
        self.relay.screen(self.relay.epoch)
        self.assertFalse(self.relay.activity(self.relay.selection_token, "bot:late")["recorded"])
        self.assertIsNone(self.relay.selected)

    def test_activity_receipts_and_timer_survive_bridge_restart(self):
        path = Path(self.temp.name) / "activity.sqlite3"
        relay = relay_module.Relay(self.tmux, state_file=path)
        selection = relay.route("#tmux select %0")
        token = selection["selection_token"]
        self.assertTrue(relay.activity(token, "user:unique")["recorded"])
        relay.touched -= 100
        touched = relay.touched
        self.assertTrue(relay.activity(token, "user:unique")["duplicate"])
        self.assertEqual(relay.touched, touched)
        self.assertTrue(relay.activity(token, "bot:delivered")["recorded"])
        relay.touched -= 120
        relay._persist()
        relay.db.close()
        resumed = relay_module.Relay(self.tmux, state_file=path)
        try:
            age = time.monotonic() - resumed.touched
            self.assertGreaterEqual(age, 120)
            self.assertLess(age, 122)
            touched = resumed.touched
            self.assertTrue(resumed.activity(token, "bot:delivered")["duplicate"])
            self.assertEqual(resumed.touched, touched)
            self.assertTrue(resumed.activity(token, "bot:new")["recorded"])
            self.assertTrue(resumed.screen(selection["epoch"])["active"])
        finally:
            resumed.db.close()

    def test_reconnect_cannot_attach_to_replaced_pane(self):
        self.enter()
        self.relay.touched -= 1801
        notice = self.relay.screen(self.relay.epoch)
        self.tmux.run("respawn-pane", "-k", "-t", "%0", "sleep 30")
        result = self.relay.route("#tmux reconnect " + notice["reconnect_token"])
        self.assertFalse(result["active"])
        self.assertIn("被替换", result["message"])

    def test_invalid_key_and_control_characters_rejected(self):
        self.enter()
        self.assertIn("支持的按键", self.relay.route("#tmux key ; ls")["message"])
        self.assertIn("控制字符", self.relay.route("bad\x03input")["message"])

    def test_stale_numeric_list_cannot_select_replacement(self):
        self.relay.route("#tmux ls")
        self.tmux.run("respawn-pane", "-k", "-t", "%0", "sleep 30")
        result = self.relay.route("#tmux select 1")
        self.assertFalse(result["active"])

    def test_list_shortcuts_rank_successful_selections_and_persist_across_restart(self):
        path = Path(self.temp.name) / "usage.json"
        relay = relay_module.Relay(self.tmux, usage_file=path)
        for index in range(1, 10):
            self.tmux.run("new-window", "-d", "-t", "relay-test", "-n", f"fixture-{index}", "sleep 30")
        panes = self.tmux.panes()
        frequent, recent = panes[-1], panes[-2]
        relay.route("#tmux select " + frequent["pane_id"], "first-entry")
        relay.route("#tmux select " + frequent["pane_id"], "first-entry")  # QQ redelivery
        relay.route("#tmux select " + recent["pane_id"], "second-entry")
        choices = relay.route("#tmux ls")["pane_shortcuts"]
        self.assertEqual(choices[0]["label"], recent["target"])
        picked = relay.route("#tmux pick " + choices[1]["token"], "third-entry")
        self.assertEqual(picked["target"], frequent["target"])
        self.assertTrue(picked["initial_context"])
        self.assertEqual(len(relay.route("#tmux ls")["pane_shortcuts"]), 8)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        resumed = relay_module.Relay(self.tmux, usage_file=path)
        self.assertEqual(resumed.usage[frequent["target"]]["count"], 2)
        self.assertEqual(resumed.route("#tmux ls")["pane_shortcuts"][0]["label"], frequent["target"])
        before = dict(resumed.usage)
        resumed.route("#tmux select %99999")
        self.assertEqual(resumed.usage, before)

    def test_list_button_remains_bound_after_refresh_and_rejects_replaced_pane(self):
        token = self.relay.route("#tmux ls")["pane_shortcuts"][0]["token"]
        self.relay.route("#tmux ls")
        self.assertTrue(self.relay.route("#tmux pick " + token)["initial_context"])
        before = dict(self.relay.usage)
        self.tmux.run("respawn-pane", "-k", "-t", "%0", "sleep 30")
        result = self.relay.route("#tmux pick " + token)
        self.assertIn("被替换", result["message"])
        self.assertEqual(self.relay.usage, before)

    def test_hash_help_works_inside_and_outside_terminal_without_typing(self):
        self.assertIn("/tmux select", self.relay.route("#help")["message"])
        self.enter()
        self.assertTrue(self.relay.route("#help")["active"])
        self.assertNotIn("ECHO:#help", self.tmux.capture("%0"))

    def test_slash_commands_are_literal_terminal_input(self):
        self.enter()
        self.relay.route("/model")
        self.wait_for("ECHO:/model")

    def test_only_hash_meta_commands_are_recognized(self):
        self.assertEqual(relay_module.command("#tmux ls"), ("list", ""))
        self.assertEqual(relay_module.command("#tmux select 2"), ("enter", "2"))
        self.assertEqual(relay_module.command("#tmux exit"), ("exit", ""))
        self.assertEqual(relay_module.command("#help"), ("help", ""))
        self.assertIsNone(relay_module.command("/term list"))
        self.assertIsNone(relay_module.command("#screen"))

    def test_help_and_list_use_markdown(self):
        help_text = self.relay.route("#help")["message"]
        self.assertIn("## 终端直连", help_text)
        self.assertIn("`/tmux type 文字`", help_text)
        self.assertIn("只填入，不提交", help_text)
        listing = self.relay.route("#tmux ls")["message"]
        self.assertIn("## tmux 窗格", listing)
        self.assertIn(f"`{self.relay.listed[0]['target']}`", listing)
        self.assertEqual(self.relay.route("#tmux help")["message"], help_text)
        for key in relay_module.KEYS:
            self.assertIn(f"`{key}`", help_text)
        self.assertLess(len(help_text), 3600)

    def test_public_slash_commands_work_on_real_tmux_without_keyboard(self):
        listing = self.relay.route("/tmux ls")
        self.assertIn("/tmux select", listing["message"])
        self.assertNotIn("#tmux", listing["message"])
        result = self.relay.route("/tmux select 1")
        self.assertTrue(result["initial_context"])
        self.relay.route("/tmux type slash-input")
        self.assertNotIn("ECHO:slash-input", self.tmux.capture("%0"))
        self.relay.route("/tmux key enter")
        self.wait_for("ECHO:slash-input")
        self.relay.route("/tmux send /tmux literal")
        self.wait_for("ECHO:/tmux literal")
        self.assertIn("ECHO:slash-input", self.relay.route("/tmux list100")["screen"])
        self.assertFalse(self.relay.route("/tmux exit")["active"])

    def test_public_command_mapping_preserves_multiline_and_literal_body(self):
        from tmux_bot.qq_commands import terminal_command
        for text in ("/tmux select %3", "/tmux key down", "/tmux type 保留  空格\n第二行",
                     "/tmux send #tmux exit", "/tmux reconnect", "/tmux screen 55"):
            self.assertEqual(terminal_command(text), "#tmux " + text[6:])
        self.assertEqual(terminal_command("/model"), "/model")

    def test_real_viewport_excludes_old_scrollback_and_preserves_complete_menu(self):
        script = "import time; print('OLD CHAT\\n'*80, end='', flush=True); print('\\033[2J\\033[H'+" + repr(MENU) + ", flush=True); time.sleep(30)"
        self.tmux.run("respawn-pane", "-k", "-t", "%0",
                      f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}")
        self.wait_for("Select Model and Effort")
        viewport = self.tmux.viewport("%0")
        block = terminal_relay.dialog_block(viewport)
        self.assertIsNotNone(block)
        self.assertIn("5. Model E", block)
        self.assertNotIn("OLD CHAT", block)

    def test_every_selection_returns_one_hundred_lines_not_incremental(self):
        script = "import time; [print('context-'+str(i), flush=True) for i in range(200)]; print('READY', flush=True); time.sleep(30)"
        self.tmux.run("respawn-pane", "-k", "-t", "%0",
                      f"{shlex.quote(sys.executable)} -u -c {shlex.quote(script)}")
        self.wait_for("context-199")
        self.relay.route("#tmux ls")
        first = self.relay.route("#tmux select 1")
        self.assertEqual(len(first["screen"].splitlines()), 100)
        self.assertTrue(first["initial_context"])
        self.assertIn("context-199", first["screen"])
        self.relay.route("#tmux exit")
        second = self.relay.route("#tmux select 1")
        self.assertEqual(first["screen"], second["screen"])
        self.assertEqual(len(self.relay.route("#tmux screen")["screen"].splitlines()), 40)
        token = self.relay.selection_token
        for command in ["#tmux list100", "#tmux List100", "#tmux screen100",
                        "#tmux button " + token + " list100", "#tmux button " + token + " screen100"]:
            result = self.relay.route(command)
            self.assertEqual(len(result["screen"].splitlines()), 100)
            self.assertTrue(result["initial_context"])

    def test_rendered_list100_button_returns_snapshot_and_rejects_stale_scope(self):
        self.enter()
        token = self.relay.selection_token
        expected = self.relay.route("#tmux list100")["screen"]
        for compact in (False, True):
            rows = terminal_relay.TerminalKeyboard(token, "owner", compact=compact).to_dict()["content"]["rows"]
            button = next(button for row in rows for button in row["buttons"] if button["id"] == "list100")
            command = button["action"]["data"]
            snapshot = self.relay.route(command)
            self.assertTrue(snapshot["initial_context"])
            self.assertEqual(snapshot["screen"], expected)
        self.relay.route("#tmux exit")
        self.enter()
        self.assertNotEqual(self.relay.selection_token, token)
        self.assertIn("过期", self.relay.route(command)["message"])


class ScreenDeltaTests(unittest.TestCase):
    def test_paragraphs_keep_wrapped_lines_and_fenced_code_together(self):
        text = "第一段\n同段续行\n\n第二段\n\n```python\nprint(1)\n\nprint(2)\n```\n\n第四段"
        self.assertEqual(terminal_relay.paragraph_bodies(text), [
            "第一段\n同段续行", "第二段", "```python\nprint(1)\n\nprint(2)\n```", "第四段",
        ])
    def test_terminal_padding_is_removed_but_indentation_preserved(self):
        text = "  历史文字     \n  新的一行     "
        self.assertEqual(terminal_relay.format_screen("test", text), "[test]\n```text\n  历史文字\n  新的一行\n```")

    def test_reconnect_keyboard_remains_one_row_with_one_bound_button(self):
        rows = terminal_relay.TerminalKeyboard("reconnect-scope", "owner", reconnect=True).to_dict()["content"]["rows"]
        self.assertEqual([len(row["buttons"]) for row in rows], [1])
        button = rows[0]["buttons"][0]
        self.assertEqual(button["render_data"]["label"], "Reconnect")
        self.assertEqual(button["action"]["data"], "#tmux reconnect reconnect-scope")
        self.assertEqual(button["action"]["permission"], {"type": 2})

    def test_full_controls_keep_three_rows_when_explicitly_requested(self):
        rows = terminal_relay.TerminalKeyboard("scope", "owner").to_dict()["content"]["rows"]
        self.assertEqual([len(row["buttons"]) for row in rows], [4, 4, 4])
        self.assertEqual([[b["render_data"]["label"] for b in row["buttons"]] for row in rows], [
            ["↑", "↓", "←", "→"], ["Enter", "Esc", "Tab", "Space"],
            ["Backspace", "Ctrl+C", "List100", "Exit"],
        ])

    def test_list100_label_and_action_agree_and_preserve_mobile_permission(self):
        for compact in (False, True):
            with self.subTest(compact=compact):
                rows = terminal_relay.TerminalKeyboard("scope", "owner", compact=compact).to_dict()["content"]["rows"]
                buttons = [button for row in rows for button in row["buttons"]]
                button = next(button for button in buttons if button["id"] == "list100")
                self.assertEqual(button["render_data"]["label"], "List100")
                self.assertEqual(button["render_data"]["visited_label"], "List100")
                self.assertEqual(button["action"]["data"], "#tmux button scope list100")
                self.assertEqual(button["action"]["permission"], {"type": 2})
                self.assertNotIn("screen100", [button["id"] for button in buttons])
                self.assertNotIn("screen", [button["render_data"]["label"] for button in buttons])

    def test_nested_code_fences_are_not_mutated_or_split(self):
        text = "说明\n\n```python\nprint('hello')\n\nprint('world')\n```\n\n尾注"
        formatted = terminal_relay.format_screen("test", text)
        self.assertEqual(formatted, "[test]\n````text\n" + text + "\n````")
        self.assertEqual(terminal_relay.message_bodies(text), [
            "说明", "```python\nprint('hello')\n\nprint('world')\n```", "尾注",
        ])
        long_code = "```python\n" + "x" * 2000 + "\n```"
        self.assertEqual(terminal_relay.message_bodies("短句\n\n" + long_code), ["短句", long_code])

    def test_dialog_keeps_all_options_and_warning_but_removes_chat_prefix(self):
        self.assertEqual(terminal_relay.dialog_block("我怎么给你cookie\n" + MENU + "\n\n"), MENU)

    def test_old_menu_followed_by_chat_is_not_a_live_dialog(self):
        self.assertIsNone(terminal_relay.dialog_block(MENU + "\n新回复\n输入提示"))

    def test_reasoning_permissions_and_generic_dialogs(self):
        for title in ["Select Reasoning Effort", "Configure Permissions", "Choose Theme"]:
            menu = f"  {title}\n› 1. option one\n  2. option two\n  enter confirm · esc cancel"
            self.assertEqual(terminal_relay.dialog_block(menu), menu)

    def test_slash_command_selector_and_text_input_are_live_dialogs(self):
        for menu in [
            "› /\n\n  /model Select model\n› /permissions Change permissions\nenter select · esc close",
            "Enter a name\n\n› draft\n\nenter submit · esc cancel",
        ]:
            with self.subTest(menu=menu):
                self.assertEqual(terminal_relay.dialog_block("旧回答\n\n" + menu), menu)
                self.assertIsNone(terminal_relay.dialog_block(menu + "\n\n• 新回答。\n\n› "))

    def test_append_only_new_lines(self):
        self.assertEqual(terminal_relay.screen_delta("历史\n上一轮回复", "历史\n上一轮回复\n新回复"), "新回复")

    def test_scrolling_does_not_repeat_overlap(self):
        self.assertEqual(terminal_relay.screen_delta("旧行\n上一轮\n共同行", "上一轮\n共同行\n新回复"), "新回复")

    def test_tui_footer_repaint_does_not_repeat_conversation(self):
        self.assertEqual(terminal_relay.screen_delta(
            "对话历史\n上一轮回复\n等待输入\n快捷键帮助",
            "对话历史\n上一轮回复\n本轮回复\n等待输入\n快捷键帮助",
        ), "本轮回复")

    def test_unchanged_deleted_and_space_padded_lines_are_silent(self):
        for previous, current in [("原文", "原文"), ("历史\n等待中", "历史"), ("原文  ", "原文")]:
            self.assertEqual(terminal_relay.screen_delta(previous, current), "")

    def test_repeated_identical_lines_are_not_lost(self):
        self.assertEqual(terminal_relay.screen_delta("回复\n回复", "回复\n回复\n回复"), "回复")

    def test_replaced_line_reports_new_value_only(self):
        self.assertEqual(terminal_relay.screen_delta("历史\n进度 10%", "历史\n进度 90%"), "进度 90%")

    def test_codex_tool_records_and_all_children_are_hidden(self):
        screen = "› 检查功能\n\n• 我先检查配置。\n\n" + TOOL_RECORDS + "\n\n• 已完成修改。\n\n› \n"
        self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 我先检查配置。\n\n• 已完成修改。")
        self.assertEqual(terminal_relay.clean_terminal_content(screen, complete_only=True), "• 我先检查配置。")

    def test_tool_record_proves_preceding_short_commentary_is_complete(self):
        screen = "› 检查\n\n• 正在检查。\n\n" + TOOL_RECORDS + "\n\n• Working (3s · esc to interrupt)\n\n› \n"
        self.assertEqual(terminal_relay.clean_terminal_content(screen, complete_only=True), "• 正在检查。")

    def test_tool_names_inside_assistant_code_fences_are_preserved(self):
        screen = "› 演示\n\n• 示例格式：\n\n```text\n• Ran example\n\n  output\n• Explored\n```\n\n• 这只是示例。\n\n› \n"
        self.assertEqual(terminal_relay.clean_terminal_content(screen),
                         "• 示例格式：\n\n```text\n• Ran example\n\n  output\n• Explored\n```\n\n• 这只是示例。")

    def test_browser_status_link_visits_and_failed_commands_are_not_prose(self):
        examples = [
            "• Opened https://www.bilibili.com/video/BV126eG6AEM2/",
            "• Opened https://github.com/inikulin/parse5",
            "◦ Browsing the web",
            "◦ Browsing the web• Opened https://github.com/inikulin/parse5",
            "◦ Browsing the web (3s · esc to interrupt)",
            "• Failed (exit 1) node --input-type=module -e 'example'\n  └ {\"error\":\"公开数据源返回 HTTP 412\"}\n    + Show details",
            "• Completed (exit 0) node example.js\n  └ command-output",
            "• Clicked https://example.com/\n  └ link-result",
            "• Viewed https://example.com/\n  └ page-output",
        ]
        for operation in examples:
            with self.subTest(operation=operation):
                screen = "› 检查\n\n• 正在检查来源。\n\n" + operation + "\n\n› \n"
                self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 正在检查来源。")
                self.assertEqual(terminal_relay.clean_terminal_content(screen, complete_only=True), "• 正在检查来源。")

    def test_prose_urls_and_literal_operation_examples_are_preserved(self):
        prose = ("• 视频地址：https://www.bilibili.com/video/BV126eG6AEM2/\n\n"
                 "  I opened https://github.com/inikulin/parse5 to verify it.\n\n"
                 "  Failed to fetch the public data; please try again.\n\n"
                 "```text\n◦ Browsing the web\n• Opened https://example.com/\n"
                 "• Failed (exit 1) example\n  └ example-error\n    + Show details\n```")
        self.assertEqual(terminal_relay.clean_terminal_content("› 演示\n\n" + prose + "\n\n› \n"), prose)

    def test_rate_limit_and_provider_failure_survive_hidden_tool_blocks(self):
        examples = ["■ unexpected status 429 Too Many Requests: retry later",
                    "■ unexpected status 503 Service Unavailable: upstream unavailable",
                    "■ unexpected status 401 Unauthorized",
                    '  └ {"error":"HTTP 503 Service Unavailable"}',
                    "• Reconnecting 1/5 (stream disconnected: HTTP 429; esc to interrupt)",
                    "• Error: status code: 502 gateway failure"]
        for error in examples:
            with self.subTest(error=error):
                screen = "› 检查\n\n• Ran provider request\n  └ hidden output\n" + error + "\n    + 3 lines (ctrl+t to expand)\n\n› \n"
                for complete_only in (False, True):
                    content = terminal_relay.clean_terminal_content(screen, complete_only=complete_only)
                    self.assertIn(error, content)
                    self.assertNotIn("Ran provider", content)
                    self.assertNotIn("hidden output", content)
                    self.assertNotIn("ctrl+t", content)

    def test_numbers_are_not_errors_and_credentials_in_real_errors_are_redacted(self):
        screen = "› 检查\n\n• Ran import\n  └ processed 429 rows and 503 values\n\n› \n"
        self.assertEqual(terminal_relay.clean_terminal_content(screen), "")
        token = "sk-" + "a" * 40
        error = "■ HTTP 503 failed; key=" + token + " Bearer secret-value https://api.example/?token=private-value"
        content = terminal_relay.clean_terminal_content("› 检查\n\n" + error + "\n\n› \n")
        self.assertIn("HTTP 503", content)
        for secret in (token, "secret-value", "private-value"):
            self.assertNotIn(secret, content)

    def test_literal_status_examples_in_assistant_code_stay_unchanged(self):
        text = "• 示例：\n\n```text\nHTTP 503 Service Unavailable\nHTTP 429 Too Many Requests\n```\n\n• 不是真实失败。"
        self.assertEqual(terminal_relay.clean_terminal_content("› 演示\n\n" + text + "\n\n› \n"), text)

    def test_searched_for_records_and_their_children_are_hidden(self):
        for header in ("• Searched for '1.5'", '• Searched for "Fast mode"',
                       "◦ Searching for 'priority'", "• Searched the web for a term"):
            with self.subTest(header=header):
                operation = header + "\n  └ result\n\n    continued result"
                screen = "› 检查\n\n• 我先核对资料。\n\n" + operation + "\n\n• 核对完成。\n\n› \n"
                self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 我先核对资料。\n\n• 核对完成。")
                self.assertEqual(terminal_relay.clean_terminal_content(screen, complete_only=True), "• 我先核对资料。")

    def test_search_text_and_code_examples_are_not_removed(self):
        prose = ("• I searched for the correct rate and found a difference.\n\n"
                 "  日志里的 Searched for 是检索记录。\n\n"
                 "```text\n• Searched for '1.5'\n  └ literal example\n◦ Searching for 'priority'\n```")
        self.assertEqual(terminal_relay.clean_terminal_content("› 示例\n\n" + prose + "\n\n› \n"), prose)

    def test_details_toggle_is_chrome_even_when_detached_from_tool_header(self):
        screen = "› 测试\n\n• 正常正文。\n\n    + Show details\n\n› \n"
        self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 正常正文。")

    def test_nested_file_diff_children_and_numbered_code_are_hidden(self):
        details = ("  └ extensions/plugins/planning/planning/plugin.yaml (+12 -0)\n"
                   "     1 +name: planning\n     2 +version: 1.0.0\n"
                   "  └ extensions/plugins/planning/planning/store.py (+475 -0)\n"
                   "      1 +\"\"\"Transactional plan snapshots.\"\"\"\n      2 +")
        screen = "› 修改\n\n• 开始修改。\n\n" + details + "\n\n• 修改完成。\n\n› \n"
        self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 开始修改。\n\n• 修改完成。")
        self.assertIn(details, terminal_relay.format_screen("test", screen))

    def test_detached_folded_tool_output_is_not_a_natural_reply(self):
        cases = ["OK\n    + 3 lines (ctrl+t to expand)",
                 "• Ran tests\n  └ OK\n    + 3 lines (ctrl+t to expand)",
                 "• OK\n    + 3 lines (ctrl+t to expand)",
                 "  └ tool output\n    + 3 more lines (ctrl+o to expand)"]
        for details in cases:
            with self.subTest(details=details):
                screen = "› 测试\n\n" + details + "\n\n• 检查完成。\n\n› \n"
                self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 检查完成。")

    def test_wrapped_rename_diff_and_cropped_gutter_rows_are_hidden(self):
        records = [
            "  └ extensions/services/tmux-bot/tests/smoke_usage.py → extensions/services/sub2api-usage/\n"
            "    tests/smoke_usage.py (+8 -6)\n    11 +from pathlib import Path\n    12 +import yaml\n"
            "    12 -from tmux_bot import app, owner, sub2api",
            "  └ extensions/services/very/long/\n    path/to/source.py\n    (+8 -6)\n    11 +import yaml",
            "    11 +from pathlib import Path\n    12 -from old_module import value",
        ]
        for record in records:
            with self.subTest(record=record):
                screen = "› 修改\n\n• 正在修改。\n\n" + record + "\n\n• 修改完成。\n\n› \n"
                self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 正在修改。\n\n• 修改完成。")
                self.assertIn(record, terminal_relay.format_screen("test", screen))

    def test_rename_examples_in_code_and_plain_directory_tree_remain_visible(self):
        text = "• 示例：\n\n```text\n  └ old/path.py → new/\n    path.py (+8 -6)\n    11 +literal\n```"
        self.assertEqual(terminal_relay.clean_terminal_content("› 示例\n\n" + text + "\n\n› \n"), text)
        tree = "• 文件布局：\n\nproject/\n  ├ src/\n  └ docs/"
        self.assertEqual(terminal_relay.clean_terminal_content("› 示例\n\n" + tree + "\n\n› \n"), tree)

    def test_real_http_failure_kept_but_diff_error_literal_hidden(self):
        screen = "› 修改\n\n• 检查中。\n\n    12 +raise RuntimeError('HTTP 503')\n\n■ HTTP 429 Too Many Requests\n\n• 稍后重试。\n\n› \n"
        result = terminal_relay.clean_terminal_content(screen)
        self.assertNotIn("RuntimeError", result)
        self.assertIn("HTTP 429", result)

    def test_fold_and_diff_examples_in_code_and_natural_ok_are_preserved(self):
        text = ("• OK，已经处理。\n\n```text\n"
                "  └ demo.py (+12 -0)\n    1 +literal\n"
                "OK\n    + 3 lines (ctrl+t to expand)\n```")
        self.assertEqual(terminal_relay.clean_terminal_content("› 示例\n\n" + text + "\n\n› \n"), text)

    def test_detached_fold_does_not_remove_preceding_complete_prose(self):
        screen = "› 测试\n\n• 正在验证。\n\nOK\n    + 3 lines (ctrl+t to expand)\n\n• 验证完成。\n\n› \n"
        self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 正在验证。\n\n• 验证完成。")

    def test_historical_browser_status_is_not_a_current_running_task(self):
        screen = "◦ Browsing the web\n\n• 已完成。\n\nWorked for 6s • 14:30\n\n› \n"
        self.assertFalse(terminal_relay.terminal_busy(screen))
        self.assertTrue(terminal_relay.terminal_busy(screen + "\n◦ Browsing the web\n"))

    def test_cropped_tool_output_without_header_is_not_assistant_prose(self):
        screen = "    unanchored-tool-output\n\n  command output\n\n• 完成检查。\n\n› \n"
        self.assertEqual(terminal_relay.clean_terminal_content(screen), "• 完成检查。")

    def test_changed_prose_body_is_delivered_whole_not_as_changed_lines(self):
        self.assertEqual(terminal_relay.screen_delta(
            "• 保留正文。\n\n下一段的第一行\n尚未结束",
            "• 保留正文。\n\n下一段的第一行\n现在已结束",
            paragraphs=True,
        ), "下一段的第一行\n现在已结束")


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        terminal_relay._screens.clear()
        terminal_relay._content_screens.clear()
        terminal_relay._dialogs.clear()
        terminal_relay._pending_bodies.clear()
        terminal_relay._ui_states.clear()
        terminal_relay._turn_states.clear()
        terminal_relay._session.clear()
        terminal_relay._closing_notice = None
        terminal_relay._checkpoint_root = None
        terminal_relay._checkpoint_payload = None
        terminal_relay._keyboard_available = True
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"HERMES_HOME": self.temp.name, "QQ_ALLOWED_USERS": "owner"})
        self.env.start()
        root = terminal_relay.root()
        root.mkdir()
        (root / "client.json").write_text('{"url":"http://127.0.0.1:18010"}')
        self.messages = []
        self.documents = []

        async def send(chat, text, reply_to=None):
            self.messages.append((chat, text, reply_to))
            return SimpleNamespace(success=True)

        async def send_document(chat, path, caption=None, file_name=None, reply_to=None):
            self.documents.append((chat, Path(path).read_text(), caption, file_name, reply_to))
            return SimpleNamespace(success=True)

        self.gateway = SimpleNamespace(
            _is_user_authorized_for_source=lambda source: source.user_id == "owner",
            _adapter_for_source=lambda source: SimpleNamespace(send=send, send_document=send_document),
        )
        self.event = SimpleNamespace(
            source=SimpleNamespace(platform=SimpleNamespace(value="qqbot"), chat_type="dm",
                                   chat_id="owner", user_id="owner"),
            text="#tmux ls", raw_message={"content": "#tmux ls"}, message_id="inbound-1",
        )

    async def asyncTearDown(self):
        for task in (terminal_relay._watcher, terminal_relay._delivery, terminal_relay._watch_delivery):
            if task and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        terminal_relay._watcher = terminal_relay._delivery = None
        terminal_relay._watch_delivery = None
        terminal_relay._screens.clear()
        terminal_relay._content_screens.clear()
        terminal_relay._dialogs.clear()
        terminal_relay._pending_bodies.clear()
        terminal_relay._ui_states.clear()
        terminal_relay._turn_states.clear()
        terminal_relay._session.clear()
        terminal_relay._closing_notice = None
        terminal_relay._checkpoint_root = None
        terminal_relay._checkpoint_payload = None
        self.env.stop()
        self.temp.cleanup()

    async def test_disabled_keyboards_cover_replies_snapshots_menus_and_idle(self):
        from unittest.mock import AsyncMock
        adapter = self.gateway._adapter_for_source(self.event.source)
        adapter.terminal_keyboards_enabled = False
        adapter.send_with_keyboard = AsyncMock(side_effect=AssertionError("keyboard must not be emitted"))
        self.gateway._adapter_for_source = lambda source: adapter
        self.assertTrue(await terminal_relay.send(self.event, self.gateway, "ordinary", scope="active"))
        self.assertTrue(await terminal_relay.send(self.event, self.gateway, "idle /tmux reconnect", scope="idle", reconnect=True))
        self.assertTrue(await terminal_relay.send_terminal(self.event, self.gateway, "test:0.0", MENU,
                                                        scope="active", full_controls=True))
        adapter.send_with_keyboard.assert_not_awaited()
        self.assertEqual(len(self.messages), 3)

    async def test_oversized_snapshot_does_not_send_empty_shortcut_message(self):
        from unittest.mock import AsyncMock
        adapter = self.gateway._adapter_for_source(self.event.source)
        adapter.terminal_keyboards_enabled = False
        adapter.send_with_keyboard = AsyncMock(side_effect=AssertionError("keyboard must not be emitted"))
        self.gateway._adapter_for_source = lambda source: adapter
        self.assertTrue(await terminal_relay.send_terminal(self.event, self.gateway, "test:0.0", "x" * 5000,
                                                        scope="active", full_controls=True))
        self.assertEqual(len(self.documents), 1)
        self.assertEqual(len(self.messages), 0)
        adapter.send_with_keyboard.assert_not_awaited()











    async def test_bridge_outage_keeps_terminal_message_out_of_model(self):
        terminal_relay.save_active(True)
        self.event.raw_message["content"] = "危险命令"
        with patch.object(terminal_relay, "request", side_effect=OSError("offline")):
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            await terminal_relay._delivery
        self.assertIn("状态暂无法确认", self.messages[-1][1])
        self.assertIn("勿重复发送", self.messages[-1][1])
        self.assertNotIn("未转发", self.messages[-1][1])


    async def test_lost_http_response_checks_receipt_without_retyping(self):
        terminal_relay.save_active(True)
        terminal_relay.seed_screen({"screen_key": "identity", "screen": "旧回答"})
        self.event.raw_message["content"] = "仅提交一次"
        accepted = {"handled": True, "active": True, "quiet": True, "epoch": 2,
                    "input_serial": 1, "submitted": True, "selection_token": "scope"}
        calls = []
        def request(path, payload):
            calls.append(path)
            if path == "/v1/route":
                raise TimeoutError("response lost after tmux submission")
            return {"status": "complete", "result": accepted}
        with patch.object(terminal_relay, "request", side_effect=request), \
             patch.object(terminal_relay, "watch"):
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            await terminal_relay._delivery
            await terminal_relay._watcher
        self.assertEqual(calls, ["/v1/route", "/v1/receipt"])
        self.assertEqual(self.messages, [])
        self.assertTrue(terminal_relay.active())

    async def test_bridge_restart_consumes_first_message_and_exits(self):
        terminal_relay.save_active(True)
        self.event.raw_message["content"] = "原本发给终端"
        with patch.object(terminal_relay, "request", return_value={"handled": False, "active": False}):
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            await terminal_relay._delivery
        self.assertFalse(terminal_relay.active())
        self.assertIn("连接已重置", self.messages[-1][1])

    async def test_normal_chat_falls_through(self):
        self.event.raw_message["content"] = "你好"
        with patch.object(terminal_relay, "request", return_value={"handled": False, "active": False}):
            self.assertFalse(terminal_relay.handle(self.event, self.gateway))

    async def test_exit_during_outage_restores_chat_and_clears_remote_on_recovery(self):
        terminal_relay.save_active(True)
        self.event.raw_message["content"] = "#tmux exit"
        with patch.object(terminal_relay, "request", side_effect=OSError("offline")):
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            await terminal_relay._delivery
        self.assertFalse(terminal_relay.active())
        self.assertTrue(terminal_relay.pending_exit())
        self.event.raw_message["content"] = "普通聊天"
        with patch.object(terminal_relay, "request", return_value={"handled": False, "active": False}) as request:
            self.assertFalse(terminal_relay.handle(self.event, self.gateway))
            self.assertEqual(request.call_args_list[0].args[1]["text"], "#tmux exit")

    async def test_media_in_terminal_is_not_forwarded_or_sent_to_agent(self):
        terminal_relay.save_active(True)
        self.event.raw_message = {"content": "图片标题", "attachments": [{"content_type": "image/jpeg"}]}
        with patch.object(terminal_relay, "request") as request:
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            request.assert_not_called()
            await terminal_relay._delivery



    async def test_cleanup_button_after_exit_does_not_delete_uploads(self):
        self.event.raw_message = {"content": "#tmux files clear"}
        with patch.object(terminal_relay, "request") as request, \
             patch.object(terminal_relay.terminal_files, "clear") as clear:
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            await terminal_relay._delivery
            request.assert_not_called()
            clear.assert_not_called()
        self.assertEqual(self.messages[-1][1], "仅限 tmux 模式。")


    async def test_selection_immediately_sends_text_block_and_sets_baseline(self):
        with patch.object(terminal_relay, "request", return_value={
            "handled": True, "active": True, "message": "**已进入终端直连**", "epoch": 1,
            "target": "test:0.0", "screen_key": "pane-identity", "screen": "已有上下文",
            "baseline": "更早历史\n已有上下文",
        }):
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            await terminal_relay._delivery
        self.assertIn("```text\n已有上下文\n```", self.messages[-1][1])
        self.assertEqual(terminal_relay._screens["pane-identity"], "更早历史\n已有上下文")

    async def test_restart_reads_baseline_before_input_and_only_sends_new_reply(self):
        terminal_relay.save_active(True)
        self.event.raw_message["content"] = "新问题"
        before = {"active": True, "screen_key": "identity", "target": "test:0.0", "screen": "旧上下文"}
        accepted = {"handled": True, "active": True, "quiet": True, "epoch": 2}
        with patch.object(terminal_relay, "request", side_effect=[before, accepted]) as request, \
             patch.object(terminal_relay, "watch"):
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            await terminal_relay._delivery
            await terminal_relay._watcher
        self.assertEqual([call.args[0] for call in request.call_args_list], ["/v1/state", "/v1/route"])
        self.assertEqual(request.call_args_list[-1].args[1]["text"], "新问题")
        self.assertEqual(self.messages, [])
        await terminal_relay.deliver_screen(self.event, self.gateway,
            {**before, "screen": "旧上下文\n\n真正的新回答"})
        self.assertEqual(len(self.messages), 1)
        self.assertIn("真正的新回答", self.messages[0][1])
        self.assertNotIn("旧上下文", self.messages[0][1])

    async def test_queue_banner_is_status_once_and_real_answer_remains_incremental(self):
        old = "› 旧问题\n\n• 旧回答\n\n› Ask Codex to do anything\n\nGPT-6-Luna max · ~/fixture"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old, "viewport": old}
        terminal_relay.seed_screen(result)
        queued = old.replace("› Ask", "• Queued follow-up inputs\n  ↳ 新问题\n    shift+← edit last queued message\n\n› Ask")
        result.update(screen=queued, viewport=queued)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        await terminal_relay.deliver_ui(self.event, self.gateway, result)
        await terminal_relay.deliver_ui(self.event, self.gateway, result)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("消息已排队，尚未开始", self.messages[0][1])
        self.assertNotIn("shift+", self.messages[0][1])
        self.assertNotIn("Queued follow-up", terminal_relay.clean_terminal_content(queued))
        completed = old.replace("› Ask", "› 新问题\n\n• 真正的回答\n  同段第二行\n\n› Ask")
        result.update(screen=completed, viewport=completed)
        await terminal_relay.deliver_screen(self.event, self.gateway, result)
        await terminal_relay.deliver_ui(self.event, self.gateway, result)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("• 真正的回答同段第二行", self.messages[-1][1])
        self.assertNotIn("旧回答", self.messages[-1][1])

    async def test_backspace_echoes_changed_composer_without_old_history(self):
        old = "› 旧问题\n\n• 旧回答\n\n› /\n\nGPT-6-Luna max · ~/fixture"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old, "viewport": old}
        terminal_relay.seed_screen(result)
        changed = old.replace("› /", "› Ask Codex to do anything")
        result.update(screen=changed, viewport=changed)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        await terminal_relay.deliver_ui(self.event, self.gateway, result, show_editor=True)
        await terminal_relay.deliver_ui(self.event, self.gateway, result, show_editor=True)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0][1], "[test:0.0]\n```text\n› （空）\n```")

    async def test_incremental_delivery_across_separate_messages(self):
        result = {"screen_key": "identity", "target": "test:0.0", "screen": "历史"}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
        result["screen"] += "\n第一轮回复"
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertNotIn("历史", self.messages[-1][1])
        result["screen"] += "\n第二轮回复"
        self.event.message_id = "inbound-2"
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertNotIn("第一轮回复", self.messages[-1][1])
        self.assertIn("第二轮回复", self.messages[-1][1])
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 3)

    async def test_each_complete_paragraph_has_one_text_body(self):
        terminal_relay._screens["identity"] = "历史"
        result = {"screen_key": "identity", "target": "test:0.0",
                  "screen": "历史\n第一段\n同段续行\n\n第二段"}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 2)
        self.assertIn("```text\n第一段同段续行\n```", self.messages[0][1])
        self.assertIn("```text\n第二段\n```", self.messages[1][1])
        self.assertNotIn("历史", self.messages[0][1])

    async def test_each_text_body_has_mobile_compatible_scoped_native_keyboard(self):
        buttons = []
        adapter = self.gateway._adapter_for_source(self.event.source)

        async def send_with_keyboard(chat, message, keyboard, reply_to=None):
            buttons.append(keyboard.to_dict())
            return await adapter.send(chat, message, reply_to=reply_to)

        adapter.send_with_keyboard = send_with_keyboard
        self.gateway._adapter_for_source = lambda source: adapter
        terminal_relay._screens["identity"] = "历史"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": "历史\n第一段\n\n第二段",
                  "selection_token": "fixture-scope"}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(buttons), 2)
        self.assertEqual(buttons[0], buttons[1])
        rows = buttons[0]["content"]["rows"]
        self.assertEqual([len(row["buttons"]) for row in rows], [2])
        self.assertEqual([[button["render_data"]["label"] for button in row["buttons"]] for row in rows], [
            ["List100", "Exit"],
        ])
        actions = [button["action"] for row in buttons[0]["content"]["rows"] for button in row["buttons"]]
        self.assertTrue(all(action["permission"] == {"type": 2} for action in actions))
        self.assertTrue(all(action["type"] == 2 and action["enter"] for action in actions))
        labels = [button["render_data"]["label"] for row in buttons[0]["content"]["rows"] for button in row["buttons"]]
        self.assertTrue({"Enter", "Esc", "Backspace", "Ctrl+C"}.isdisjoint(labels))
        self.assertNotIn("Delete", labels)
        self.assertIn("#tmux button fixture-scope list100", [action["data"] for action in actions])
        self.assertIn("List100", labels)
        self.assertNotIn("Refresh", labels)
        self.assertFalse(any("\u4e00" <= char <= "\u9fff" for label in labels for char in label))
        self.assertNotIn("#tmux button fixture-scope next", [action["data"] for action in actions])

    async def test_list_reply_has_only_two_rows_of_ranked_owner_selection_buttons(self):
        keyboards = []
        adapter = self.gateway._adapter_for_source(self.event.source)
        async def send_with_keyboard(chat, message, keyboard, reply_to=None):
            keyboards.append(keyboard.to_dict())
            return await adapter.send(chat, message, reply_to=reply_to)
        adapter.send_with_keyboard = send_with_keyboard
        self.gateway._adapter_for_source = lambda source: adapter
        choices = [{"label": f"Project-{index}", "token": f"pane-binding-{index}"} for index in range(10)]
        with patch.object(terminal_relay, "request", return_value={
            "handled": True, "active": False, "message": "tmux 窗格", "pane_shortcuts": choices,
        }):
            self.assertTrue(terminal_relay.handle(self.event, self.gateway))
            await terminal_relay._delivery
        rows = keyboards[0]["content"]["rows"]
        self.assertEqual([len(row["buttons"]) for row in rows], [4, 4])
        buttons = [button for row in rows for button in row["buttons"]]
        self.assertEqual([b["render_data"]["label"] for b in buttons], [c["label"] for c in choices[:8]])
        for index, button in enumerate(buttons):
            self.assertEqual(button["action"]["data"], f"#tmux pick pane-binding-{index}")
            self.assertEqual(button["action"]["permission"], {"type": 2})
            self.assertTrue(button["action"]["enter"])

    async def test_screen100_restores_full_keyboard_after_compact_prose(self):
        keyboards = []
        adapter = self.gateway._adapter_for_source(self.event.source)
        async def send_with_keyboard(chat, message, keyboard, reply_to=None):
            keyboards.append(keyboard.to_dict())
            return await adapter.send(chat, message, reply_to=reply_to)
        adapter.send_with_keyboard = send_with_keyboard
        self.gateway._adapter_for_source = lambda source: adapter
        result = {"screen_key": "identity", "target": "test:0.0", "screen": "› 问题\n\n• 回复。\n\n› \n",
                  "selection_token": "fixture-scope", "initial_context": True}
        await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True)
        self.assertEqual([len(row["buttons"]) for row in keyboards[-1]["content"]["rows"]], [4, 4, 4])
        result["screen"] = "› 问题\n\n• 回复。\n\n• 新段落。\n\n› \n"
        await terminal_relay.deliver_screen(self.event, self.gateway, result)
        self.assertEqual([len(row["buttons"]) for row in keyboards[-1]["content"]["rows"]], [2])

    async def test_live_menus_and_editor_replies_have_full_keyboard_without_screen100(self):
        keyboards = []
        adapter = self.gateway._adapter_for_source(self.event.source)
        async def send_with_keyboard(chat, message, keyboard, reply_to=None):
            keyboards.append(keyboard.to_dict())
            return await adapter.send(chat, message, reply_to=reply_to)
        adapter.send_with_keyboard = send_with_keyboard
        self.gateway._adapter_for_source = lambda source: adapter
        old = "› 问题\n\n• 回答。\n\n› \n"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old, "viewport": old,
                  "selection_token": "fixture-scope"}
        terminal_relay.seed_screen(result)
        for menu in [MENU, "› /\n\n  /model Select model\n› /permissions Change permissions\nenter select · esc close",
                     "Enter a name\n\n› draft\n\nenter submit · esc cancel"]:
            result.update(screen=old + "\n" + menu, viewport=menu)
            self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
            self.assertEqual([len(row["buttons"]) for row in keyboards[-1]["content"]["rows"]], [4, 4, 4])
        result.update(screen=old.replace("› \n", "› /mode\n"), viewport="› /mode\n")
        await terminal_relay.deliver_ui(self.event, self.gateway, result, show_editor=True)
        self.assertEqual([len(row["buttons"]) for row in keyboards[-1]["content"]["rows"]], [4, 4, 4])
        result.update(screen=old.replace("› \n", "• 新回复。\n\n› \n"), viewport="› \n")
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual([len(row["buttons"]) for row in keyboards[-1]["content"]["rows"]], [2])

    async def test_keyboard_permission_denial_falls_back_without_losing_body(self):
        adapter = self.gateway._adapter_for_source(self.event.source)

        async def send_with_keyboard(*args, **kwargs):
            return SimpleNamespace(success=False, error="304057 not allowd custom keyborad")

        adapter.send_with_keyboard = send_with_keyboard
        self.gateway._adapter_for_source = lambda source: adapter
        self.assertTrue(await terminal_relay.send(self.event, self.gateway, "完整文本体", scope="fixture"))
        self.assertEqual(self.messages[-1][1], "完整文本体")
        self.assertFalse(terminal_relay._keyboard_available)

    async def test_more_than_five_bodies_push_automatically_without_another_input(self):
        terminal_relay._screens["identity"] = "历史"
        bodies = [f"完整段落-{i}\n" + str(i) * 1500 for i in range(8)]
        result = {"screen_key": "identity", "target": "test:0.0",
                  "screen": "历史\n" + "\n\n".join(bodies)}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 8)
        self.assertTrue(all(message[2] is None for message in self.messages))
        for index, body in enumerate(bodies):
            self.assertIn(body.replace("\n", " "), self.messages[index][1])
        self.assertNotIn("identity", terminal_relay._pending_bodies)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))

    async def test_codex_menu_close_and_status_redraw_do_not_resend_history_or_single_line_chrome(self):
        old = "› 上个问题\n\n• 已有回复\n\n  11:09\n\n› /\n\n  GPT-6-Luna max · ~/project\n  ⚠ 4 warnings · f2 to view"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old,
                  "viewport": old, "selection_token": "fixture-scope"}
        await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True)
        result.update(screen=old + "\n" + MENU, viewport=MENU)
        await terminal_relay.deliver_screen(self.event, self.gateway, result)
        result.update(screen=old.replace("› /", "• Model changed to gpt-6-luna max\n\n› /"), viewport="› /\nGPT-6-Luna max · ~/project")
        await terminal_relay.deliver_screen(self.event, self.gateway, result)
        self.assertEqual(self.messages[-1][1], "[test:0.0]\n```text\n• Model changed to gpt-6-luna max\n```")
        before = len(self.messages)
        result["screen"] = result["screen"].replace("4 warnings", "5 warnings").replace("11:09", "11:10")
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), before)

    async def test_streaming_tail_is_buffered_until_complete_and_keeps_lines_in_one_body(self):
        old = "› 上个问题\n\n• 已有回复\n\n› \n\nGPT-6-Luna max · ~/project"
        terminal_relay._screens["identity"] = old
        terminal_relay._content_screens["identity"] = terminal_relay.clean_terminal_content(old)
        prefix = "› 上个问题\n\n• 已有回复\n\n"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": prefix + "• 回答的第"}
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        result["screen"] = prefix + "• 回答的第一行\n  同段第二行"
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        self.assertEqual(self.messages, [])
        result["screen"] += "\n\n› \n\nGPT-6-Luna max · ~/project"
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 1)
        self.assertIn("• 回答的第一行同段第二行", self.messages[0][1])

    async def test_complete_short_paragraphs_send_while_unfinished_tail_waits(self):
        composer = "\n\n› Ask Codex to do anything\n\nGPT-6.1-Sol max · ~/fixture"
        old = "› 旧问题\n\n• 旧回答\n\nWorked for 4s • 14:28" + composer
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old, "viewport": old}
        terminal_relay.seed_screen(result)
        terminal_relay._turn_states["identity"].update(pending=True, completion_before=terminal_relay.completion_marker(old))
        partial = old.removesuffix(composer) + "\n\n› 新问题\n\n• 第一段\n\n第二段\n\n第三段尚未完整" + composer
        result.update(screen=partial, viewport=partial)
        # No spinner is visible, but there is still no completion for this input.
        for attempt in range(12):
            terminal_relay.observe_turn(result)
            self.assertFalse(terminal_relay.terminal_settled(result))
            self.assertEqual(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False),
                             attempt == 0)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("• 第一段", self.messages[0][1])
        self.assertIn("第二段", self.messages[1][1])
        self.assertTrue(all("第三段" not in message[1] for message in self.messages))
        done = partial.replace("第三段尚未完整", "第三段完整\n\nWorked for 4s • 14:28")
        result.update(screen=done, viewport=done)
        terminal_relay.observe_turn(result)
        self.assertTrue(terminal_relay.terminal_settled(result))
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 4)
        self.assertIn("第三段完整", self.messages[2][1])
        self.assertEqual(self.messages[3][1], terminal_relay.format_screen("test:0.0", "Worked for 4s • 14:28"))

    async def test_entering_running_task_appends_reply_without_new_user_input(self):
        composer = "\n\n› Ask Codex to do anything\n\nGPT-6.1-Sol max · ~/fixture"
        history = "› 已有问题\n\n• 原有历史\n\nWorked for 4s • 14:28"
        running = history + "\n\n› 本机已经提交的问题\n\n• Working (1s · esc to interrupt)" + composer
        entry = {"screen_key": "identity", "target": "test:0.0", "screen": running,
                 "viewport": running, "initial_context": True, "input_serial": 0}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, entry, snapshot=True))
        self.assertTrue(terminal_relay._turn_states["identity"]["pending"])
        done = history + "\n\n› 本机已经提交的问题\n\n• 运行完成后的新增正文\n\nWorked for 5s • 14:29" + composer
        result = {**entry, "screen": done, "viewport": done}
        terminal_relay.observe_turn(result)
        self.assertTrue(terminal_relay.terminal_settled(result))
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 3)
        self.assertIn("原有历史", self.messages[0][1])
        self.assertIn("运行完成后的新增正文", self.messages[1][1])
        self.assertNotIn("原有历史", self.messages[1][1])
        self.assertEqual(self.messages[2][1], terminal_relay.format_screen("test:0.0", "Worked for 5s • 14:29"))
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))

    async def test_long_stream_emits_complete_paragraphs_and_keeps_unfinished_tail(self):
        terminal_relay._screens["identity"] = "历史"
        a, b = "甲" * 1200, "乙" * 1200
        result = {"screen_key": "identity", "target": "test:0.0",
                  "screen": "历史\n\n" + a + "\n\n" + b + "\n\n未完成的尾段"}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        self.assertEqual(len(self.messages), 2)
        self.assertIn(a, self.messages[0][1])
        self.assertNotIn(b, self.messages[0][1])
        self.assertIn(b, self.messages[1][1])
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 3)
        self.assertIn("未完成的尾段", self.messages[-1][1])
        self.assertNotIn(b, self.messages[-1][1])

    async def test_tool_only_updates_are_silent_and_do_not_repeat_commentary(self):
        prefix = "› 检查\n\n"
        composer = "\n\n› \n\nGPT-6.1-Sol medium · ~/fixture"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": prefix + composer}
        terminal_relay.seed_screen(result)
        commentary = prefix + "• 我先检查配置。\n\n" + TOOL_RECORDS
        result.update(screen=commentary + "\n\n• Working (3s · esc to interrupt)" + composer)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        self.assertEqual(len(self.messages), 1)
        self.assertIn("我先检查配置。", self.messages[0][1])
        result.update(screen=commentary.replace("new_value", "another_value") + composer)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        # A turn with only commentary and tools must not replay that commentary at completion.
        result.update(screen=commentary + "\n\nWorked for 8s • 14:30" + composer)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.messages[-1][1], terminal_relay.format_screen("test:0.0", "Worked for 8s • 14:30"))
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertFalse(terminal_relay._turn_states["identity"]["pending"])
        final = commentary + "\n\n• 配置检查完成。\n  无需变更。\n\nWorked for 8s • 14:30" + composer
        result.update(screen=final)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 3)
        self.assertIn("• 配置检查完成。无需变更。", self.messages[-1][1])
        for _, message, _ in self.messages:
            for hidden in ("Explored", "Ran", "Edited", "Search", "Read", "operation-output", "old_value",
                           "Browsing the web", "Opened", "Failed (exit", "HTTP 412", "Show details"):
                self.assertNotIn(hidden, message)

    async def test_watcher_sends_complete_paragraph_while_tail_keeps_streaming(self):
        clock = [0.0]
        original_sleep = asyncio.sleep
        prefix = "› 任务\n\n• 先发送的完整段落。\n\n"
        suffix = "\n\n• Working (3s · esc to interrupt)\n\n› \n\nGPT-6.1-Sol medium · ~/fixture"
        terminal_relay.seed_screen({"screen_key": "identity", "screen": "› \n"})
        frames = iter([{"active": True, "screen_key": "identity", "target": "test:0.0",
                        "screen": prefix + "正在生成" + "续" * size + suffix}
                       for size in range(7)] + [{"active": False}])

        async def tick(_):
            clock[0] += 1
            await original_sleep(0)

        with patch.object(terminal_relay, "request", side_effect=lambda *_: next(frames)), \
             patch.object(terminal_relay.asyncio, "sleep", side_effect=tick), \
             patch.object(terminal_relay.asyncio, "get_running_loop", return_value=SimpleNamespace(time=lambda: clock[0])):
            await terminal_relay.watch(self.event, self.gateway, 1)
        self.assertEqual(len(self.messages), 1)
        self.assertIn("先发送的完整段落。", self.messages[0][1])
        self.assertNotIn("正在生成", self.messages[0][1])

    async def test_live_provider_errors_append_once_without_tool_noise(self):
        prefix = "› 请求\n\n• Ran request\n  └ tool log\n"
        suffix = "\n\n› \n"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": prefix + suffix}
        terminal_relay.seed_screen(result)
        for code, cause in ((429, "Too Many Requests"), (503, "Service Unavailable")):
            result["screen"] = result["screen"].replace(suffix, f"\n■ HTTP {code} {cause}\n" + suffix)
            self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
            self.assertIn(str(code), self.messages[-1][1])
            self.assertNotIn("Ran request", self.messages[-1][1])
            self.assertNotIn("tool log", self.messages[-1][1])
            self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        self.assertEqual(len(self.messages), 2)

    async def test_screen100_keeps_tool_records_and_resets_automatic_progress(self):
        prefix = "› 检查\n\n• 已显示的正文。\n\n"
        initial = {"screen_key": "identity", "target": "test:0.0", "screen": prefix + TOOL_RECORDS + "\n\n› \n",
                   "initial_context": True}
        terminal_relay.seed_screen({**initial, "screen": "› \n"})
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, initial, snapshot=True))
        self.assertIn(TOOL_RECORDS, self.messages[-1][1])
        self.assertNotIn("Explored", terminal_relay._content_screens["identity"])
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, initial))
        updated = {**initial, "screen": initial["screen"].replace("› \n", "• 新增的自然语言。\n\n› \n")}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, updated))
        self.assertIn("新增的自然语言。", self.messages[-1][1])
        self.assertNotIn("已显示的正文。", self.messages[-1][1])
        self.assertNotIn("Explored", self.messages[-1][1])

    async def test_legacy_pending_migration_filters_tool_children_and_keeps_unsent_prose(self):
        key = "identity"
        bodies = ["• 已确认送达。", "• Explored\n  └ Search private-data", "    Read secret-output", "• 未发送的正文。"]
        baseline = "› 检查\n\n" + "\n\n".join(bodies) + "\n\n› \n"
        data = {"version": 1, "session": {}, "screens": {key: "› 检查\n\n• 已确认送达。\n\n› \n"},
                "content": {key: bodies[0]}, "dialogs": {}, "ui": {}, "turns": {},
                "pending": {key: {"target": "test:0.0", "baseline": baseline,
                                  "content": "\n\n".join(bodies), "bodies": bodies, "next": 2, "scope": ""}}}
        (terminal_relay.root() / "delivery.json").write_text(json.dumps(data))
        terminal_relay.load_checkpoint()
        self.assertEqual(terminal_relay._pending_bodies[key]["bodies"], [bodies[-1]])
        self.assertTrue(await terminal_relay.flush_bodies(self.event, self.gateway, key))
        self.assertEqual(len(self.messages), 1)
        self.assertIn("未发送的正文。", self.messages[-1][1])
        self.assertNotIn("Search", self.messages[-1][1])
        self.assertNotIn("Read", self.messages[-1][1])

    async def test_version_six_pending_rename_noise_is_removed_without_replaying_prose(self):
        key = "identity"
        acknowledged = "• 已送达正文。"
        noise = "  └ old/path.py → new/\n    path.py (+8 -6)\n    11 +import yaml"
        bodies = [noise, "• 待发送正文。"]
        content = acknowledged + "\n\n" + "\n\n".join(bodies)
        data = {"version": 6, "session": {}, "screens": {key: acknowledged}, "content": {key: acknowledged},
                "dialogs": {}, "ui": {}, "turns": {},
                "pending": {key: {"target": "test:0.0", "baseline": content, "content": content,
                                  "bodies": bodies, "next": 0, "scope": ""}}}
        (terminal_relay.root() / "delivery.json").write_text(json.dumps(data))
        terminal_relay.load_checkpoint()
        self.assertEqual(terminal_relay._pending_bodies[key]["bodies"], [bodies[-1]])
        self.assertTrue(await terminal_relay.flush_bodies(self.event, self.gateway, key))
        self.assertEqual(len(self.messages), 1)
        self.assertNotIn("import yaml", self.messages[0][1])
        self.assertNotIn(acknowledged, self.messages[0][1])

    async def test_legacy_pending_children_with_header_in_previous_baseline_stay_hidden(self):
        key = "identity"
        previous = "• 已送达正文。\n\n• Ran prior-command\n  └ acknowledged-output"
        bodies = ["    late-operation-output", "• 待发送正文。"]
        content = previous + "\n\n" + "\n\n".join(bodies)
        data = {"version": 1, "session": {}, "screens": {key: previous}, "content": {key: previous},
                "dialogs": {}, "ui": {}, "turns": {},
                "pending": {key: {"target": "test:0.0", "baseline": content, "content": content,
                                  "bodies": bodies, "next": 0, "scope": ""}}}
        (terminal_relay.root() / "delivery.json").write_text(json.dumps(data))
        terminal_relay.load_checkpoint()
        self.assertEqual(terminal_relay._pending_bodies[key]["bodies"], [bodies[-1]])
        self.assertTrue(await terminal_relay.flush_bodies(self.event, self.gateway, key))
        self.assertEqual(len(self.messages), 1)
        self.assertNotIn("late-operation-output", self.messages[-1][1])

    async def test_version_two_checkpoint_filters_pending_browser_and_failure_records(self):
        key = "identity"
        previous = "• 已发送正文。\n\n• Opened https://example.com/"
        bodies = ["  └ late-browser-output", "• Failed (exit 1) node example.js\n  └ failure-output\n    + Show details",
                  "• 待发送的自然语言。"]
        content = previous + "\n\n" + "\n\n".join(bodies)
        data = {"version": 2, "session": {}, "screens": {key: previous}, "content": {key: previous},
                "dialogs": {}, "ui": {}, "turns": {},
                "pending": {key: {"target": "test:0.0", "baseline": content, "content": content,
                                  "bodies": bodies, "next": 0, "scope": ""}}}
        (terminal_relay.root() / "delivery.json").write_text(json.dumps(data))
        terminal_relay.load_checkpoint()
        self.assertEqual(terminal_relay._pending_bodies[key]["bodies"], [bodies[-1]])
        self.assertEqual(terminal_relay._content_screens[key], "• 已发送正文。")
        self.assertEqual(json.loads((terminal_relay.root() / "delivery.json").read_text())["version"], 7)
        self.assertTrue(await terminal_relay.flush_bodies(self.event, self.gateway, key))
        self.assertEqual(len(self.messages), 1)
        self.assertIn("待发送的自然语言。", self.messages[-1][1])

    async def test_version_four_checkpoint_filters_pending_search_records(self):
        key = "identity"
        previous = "• 已发送正文。\n\n• Searched for '1.5'"
        bodies = ["  └ late-search-result", "◦ Searching for 'priority'\n  └ another-result", "• 待发送的正文。"]
        content = previous + "\n\n" + "\n\n".join(bodies)
        data = {"version": 4, "session": {}, "screens": {key: previous}, "content": {key: previous},
                "dialogs": {}, "ui": {}, "turns": {},
                "pending": {key: {"target": "test:0.0", "baseline": content, "content": content,
                                  "bodies": bodies, "next": 0, "scope": ""}}}
        (terminal_relay.root() / "delivery.json").write_text(json.dumps(data))
        terminal_relay.load_checkpoint()
        self.assertEqual(terminal_relay._pending_bodies[key]["bodies"], [bodies[-1]])
        self.assertEqual(terminal_relay._content_screens[key], "• 已发送正文。")
        self.assertEqual(json.loads((terminal_relay.root() / "delivery.json").read_text())["version"], 7)
        self.assertTrue(await terminal_relay.flush_bodies(self.event, self.gateway, key))
        self.assertEqual(len(self.messages), 1)
        self.assertIn("待发送的正文。", self.messages[0][1])
        self.assertNotIn("search-result", self.messages[0][1])

    async def test_search_only_update_is_silent_but_list100_keeps_it(self):
        result = {"screen_key": "identity", "target": "test:0.0", "screen": "› 检查\n\n› \n"}
        terminal_relay.seed_screen(result)
        operation = "• Searched for '1.5'\n  └ search-output\n\n    continued-search-output"
        result["screen"] = "› 检查\n\n" + operation + "\n\n› \n"
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(self.messages, [])
        result["screen"] = "› 检查\n\n" + operation + "\n\n• 这是正文解释。\n\n› \n"
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 1)
        self.assertIn("这是正文解释。", self.messages[0][1])
        self.assertNotIn("Searched for", self.messages[0][1])
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
        self.assertIn(operation, self.messages[-1][1])

    async def test_version_five_does_not_replay_old_errors_or_absorb_buffered_prose(self):
        key = "identity"
        old_error = "■ HTTP 429 Too Many Requests"
        previous = "› 任务\n\n• 已发送正文。\n\n" + old_error + "\n\n• 还在生成\n\n› \n"
        data = {"version": 5, "session": {}, "screens": {key: previous},
                "content": {key: "• 已发送正文。"}, "dialogs": {}, "ui": {}, "turns": {}, "pending": {}}
        (terminal_relay.root() / "delivery.json").write_text(json.dumps(data))
        terminal_relay.load_checkpoint()
        self.assertIn(old_error, terminal_relay._content_screens[key])
        self.assertNotIn("还在生成", terminal_relay._content_screens[key])
        result = {"screen_key": key, "target": "test:0.0", "screen": previous}
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        result["screen"] = previous.replace("• 还在生成", "• 生成完成。\n\n■ HTTP 503 Service Unavailable")
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        joined = "\n".join(item[1] for item in self.messages)
        self.assertIn("生成完成", joined)
        self.assertIn("HTTP 503", joined)
        self.assertNotIn("HTTP 429", joined)
        self.assertNotIn("已发送正文", joined)

    async def test_version_five_pending_errors_are_recovered_without_repeating_sent_body(self):
        key = "identity"
        previous = "› 任务\n\n• 已发送正文。\n\n› \n"
        bodies = ["• 已发批次。", "• 未发批次。"]
        raw = "› 任务\n\n• 已发送正文。\n\n• 已发批次。\n\n• Ran request\n  └ HTTP 503 Service Unavailable\n\n• 未发批次。\n\n› \n"
        data = {"version": 5, "session": {}, "screens": {key: previous},
                "content": {key: "• 已发送正文。"}, "dialogs": {}, "ui": {}, "turns": {},
                "pending": {key: {"target": "test:0.0", "baseline": raw,
                                  "content": "• 已发送正文。\n\n" + "\n\n".join(bodies),
                                  "bodies": bodies, "next": 1, "scope": ""}}}
        (terminal_relay.root() / "delivery.json").write_text(json.dumps(data))
        terminal_relay.load_checkpoint()
        self.assertTrue(await terminal_relay.flush_bodies(self.event, self.gateway, key))
        joined = "\n".join(item[1] for item in self.messages)
        self.assertIn("HTTP 503", joined)
        self.assertIn("未发批次", joined)
        self.assertNotIn("已发批次", joined)
        self.assertNotIn("已发送正文", joined)
        self.assertNotIn("Ran request", joined)

    async def test_version_three_checkpoint_filters_pending_diff_and_fold_widgets(self):
        key = "identity"
        previous = "› 开发\n\n• 已发正文。\n\n› \n"
        bodies = ["  └ path/plugin.yaml (+12 -0)\n    1 +name: planning", "OK\n    + 3 lines (ctrl+t to expand)", "• 还没发的正文。"]
        content = previous + "\n\n" + "\n\n".join(bodies)
        data = {"version": 3, "session": {}, "screens": {key: previous}, "content": {key: "• 已发正文。"},
                "dialogs": {}, "ui": {}, "turns": {},
                "pending": {key: {"target": "test:0.0", "baseline": content, "content": content,
                                  "bodies": bodies, "next": 0, "scope": "fixture-scope"}}}
        (terminal_relay.root() / "delivery.json").write_text(json.dumps(data))
        terminal_relay.load_checkpoint()
        self.assertEqual(terminal_relay._pending_bodies[key]["bodies"], [bodies[-1]])
        self.assertTrue(await terminal_relay.flush_bodies(self.event, self.gateway, key))
        self.assertNotIn("plugin.yaml", self.messages[-1][1])
        self.assertNotIn("ctrl+t", self.messages[-1][1])

    async def test_browsing_status_marks_current_turn_as_running(self):
        screen = "› 查网页\n\n• 正在生成\n\n◦ Browsing the web\n\n› \n"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": screen, "viewport": screen}
        terminal_relay.observe_turn(result)
        self.assertFalse(terminal_relay.terminal_settled(result))

    async def test_repeated_question_returns_answer_then_completion_once_per_turn(self):
        turn = "› 回复我你好\n\n• 你好！\n\n  Worked for 6s • 13:25"
        composer = "\n\n› Ask Codex to do anything\n\nGPT-6-Luna max · ~/fixture"
        result = {"screen_key": "identity", "target": "test:0.0",
                  "screen": turn + composer, "viewport": turn + composer}
        terminal_relay.seed_screen(result)
        running = turn + "\n\n› 回复我你好\n\n• Working (1s · esc to interrupt)" + composer
        result.update(screen=running, viewport=running)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
        done = turn + "\n\n" + turn.replace("13:25", "13:26") + composer
        result.update(screen=done, viewport=done)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        expected = [terminal_relay.format_screen("test:0.0", text)
                    for text in ("• 你好！", "Worked for 6s • 13:26")]
        self.assertEqual([message[1] for message in self.messages], expected)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        # Another full turn with exactly the same text and minute is still new.
        done = done.removesuffix(composer) + "\n\n" + turn.replace("13:25", "13:26") + composer
        result.update(screen=done, viewport=done)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual([message[1] for message in self.messages], expected * 2)

    async def test_full_tui_redraw_does_not_replay_old_answer_or_hide_repeated_turn(self):
        old = "› 回复我你好\n\n• 你好！\n\n  Worked for 6s • 13:25"
        composer = "\n\n› Ask Codex to do anything\n\nGPT-6-Luna max · ~/fixture\n⚠ 4 warnings · f2 to view"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old + composer}
        terminal_relay.seed_screen(result)
        # capture-pane contains an erased full frame followed by its replacement.
        result.update(screen=old + composer + "\n\n" + old + composer, viewport=old + composer)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        current = old + "\n\n" + old.replace("13:25", "13:26") + composer
        result.update(screen=old + composer + "\n\n" + current, viewport=current)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual([message[1] for message in self.messages],
                         [terminal_relay.format_screen("test:0.0", text)
                          for text in ("• 你好！", "Worked for 6s • 13:26")])

    async def test_duration_footer_only_redraw_is_silent(self):
        old = "› 问题\n\n• 正文\n\n  13:26\n\n› \n\nGPT-6-Luna max · ~/fixture"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old}
        terminal_relay.seed_screen(result)
        for footer in ["Worked for 6s • 13:26", "Worked for 1m 2s • 13:26", "Worked for 8s • Sep 28 at 16:03"]:
            result["screen"] = old.replace("13:26", footer)
            self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(self.messages, [])

    async def test_cropped_identical_answers_deliver_once_per_observed_turn(self):
        composer = "\n\n› Ask Codex to do anything\n\nGPT-6-Luna max · ~/fixture"
        old = "› 回复我你好\n\n• 你好！\n\nWorked for 6s • 13:26" + composer
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old, "viewport": old}
        terminal_relay.seed_screen(result)
        for _ in range(2):
            running = "› 回复我你好\n\n• Working (1s · esc to interrupt)" + composer
            result.update(screen=running, viewport=running)
            self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, settled=False))
            # Entire screen, including the timestamp, matches the previous answer.
            result.update(screen=old, viewport=old)
            self.assertTrue(terminal_relay.terminal_settled(result))
            self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
            self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual([message[1] for message in self.messages],
                         [terminal_relay.format_screen("test:0.0", text)
                          for text in ("• 你好！", "Worked for 6s • 13:26")] * 2)

    async def test_duration_arrives_after_answer_without_replaying_prose(self):
        composer = "\n\n› \n\nGPT-6.1-Sol medium · ~/fixture"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": composer}
        terminal_relay.seed_screen(result)
        running = "› 完成任务\n\n• Working (3s · esc to interrupt)" + composer
        result.update(screen=running, viewport=running)
        terminal_relay.observe_turn(result)
        answer = "› 完成任务\n\n• 任务完成。\n\n08:57" + composer
        result.update(screen=answer, viewport=answer)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(terminal_relay._turn_states["identity"]["completion_armed"])
        footer = "Worked for 34m 23s • 08:57"
        done = answer.replace("08:57", footer)
        result.update(screen=done, viewport=done)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual([message[1] for message in self.messages],
                         [terminal_relay.format_screen("test:0.0", text) for text in ("• 任务完成。", footer)])
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        # Even a changed duration on the same finished turn is a redraw, not a new completion.
        result.update(screen=done.replace("34m 23s", "34m 24s"), viewport=done.replace("34m 23s", "34m 24s"))
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 2)

    async def test_tool_only_task_finishes_with_one_compact_duration_notice(self):
        composer = "\n\n› \n\nGPT-6.1-Sol medium · ~/fixture"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": composer,
                  "selection_token": "fixture-scope"}
        terminal_relay.seed_screen(result)
        running = "› 执行任务\n\n• Working (3s · esc to interrupt)" + composer
        result.update(screen=running, viewport=running)
        terminal_relay.observe_turn(result)
        done = "› 执行任务\n\n• Ran true\n  └ no output\n\nWorked for 3s • 08:57" + composer
        result.update(screen=done, viewport=done)
        keyboards = []
        adapter = self.gateway._adapter_for_source(self.event.source)

        async def send_with_keyboard(chat, message, keyboard, reply_to=None):
            keyboards.append(keyboard.to_dict())
            return await adapter.send(chat, message, reply_to=reply_to)

        adapter.send_with_keyboard = send_with_keyboard
        self.gateway._adapter_for_source = lambda _source: adapter
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(self.messages, [("owner", terminal_relay.format_screen("test:0.0", "Worked for 3s • 08:57"), None)])
        buttons = [button for row in keyboards[0]["content"]["rows"] for button in row["buttons"]]
        self.assertEqual([button["render_data"]["label"] for button in buttons], ["List100", "Exit"])
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))

    async def test_watcher_waits_for_stable_duration_after_prose_is_already_delivered(self):
        composer = "\n\n› \n\nGPT-6.1-Sol medium · ~/fixture"
        running = "› 完成任务\n\n• Working (3s · esc to interrupt)" + composer
        result = {"screen_key": "identity", "target": "test:0.0", "screen": running}
        terminal_relay.seed_screen(result)
        answer = "› 完成任务\n\n• 正文已发送。\n\n08:57" + composer
        result.update(screen=answer, viewport=answer)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        clock = [0.0]
        original_sleep = asyncio.sleep
        accepted_at = []
        adapter = self.gateway._adapter_for_source(self.event.source)

        async def send(chat, message, reply_to=None):
            accepted_at.append(clock[0])
            return await adapter.send(chat, message, reply_to=reply_to)

        self.gateway._adapter_for_source = lambda _source: SimpleNamespace(send=send)
        times = ["34m 22s", "34m 23s", "34m 23s", "34m 23s", "34m 23s"]
        frames = iter([{**result, "active": True, "screen": answer.replace("08:57", f"Worked for {duration} • 08:57"),
                        "viewport": answer.replace("08:57", f"Worked for {duration} • 08:57")}
                       for duration in times] + [{"active": False}])

        async def tick(_):
            clock[0] += 1
            await original_sleep(0)

        with patch.object(terminal_relay, "request", side_effect=lambda *_: next(frames)), \
             patch.object(terminal_relay.asyncio, "sleep", side_effect=tick), \
             patch.object(terminal_relay.asyncio, "get_running_loop", return_value=SimpleNamespace(time=lambda: clock[0])):
            await terminal_relay.watch(self.event, self.gateway, 1)
        self.assertEqual(accepted_at, [4.0])
        self.assertEqual(self.messages[-1][1], terminal_relay.format_screen("test:0.0", "Worked for 34m 23s • 08:57"))
        self.assertEqual(len(self.messages), 2)

    async def test_completion_rejection_survives_restart_without_replaying_answer(self):
        composer = "\n\n› \n\nGPT-6.1-Sol medium · ~/fixture"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": composer}
        terminal_relay.seed_screen(result)
        footer = "Worked for 34m 23s • 08:57"
        done = "› 新任务\n\n• 已完成。\n\n" + footer + composer
        result.update(screen=done, viewport=done)
        original = terminal_relay.send_terminal

        async def reject_completion(*args, **kwargs):
            return False if args[3] == footer else await original(*args, **kwargs)

        with patch.object(terminal_relay, "send_terminal", side_effect=reject_completion):
            self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        saved = json.loads((terminal_relay.root() / "delivery.json").read_text())
        self.assertEqual(saved["pending"]["identity"]["next"], 1)
        self.assertEqual(saved["pending"]["identity"]["completion"], footer)
        for mapping in (terminal_relay._screens, terminal_relay._content_screens,
                        terminal_relay._pending_bodies, terminal_relay._turn_states):
            mapping.clear()
        terminal_relay._checkpoint_root = None
        terminal_relay.load_checkpoint()
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual([message[1] for message in self.messages],
                         [terminal_relay.format_screen("test:0.0", text) for text in ("• 已完成。", footer)])
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        terminal_relay._turn_states.clear()
        terminal_relay._checkpoint_root = None
        terminal_relay.load_checkpoint()
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))

    async def test_menu_navigation_does_not_emit_a_historical_completion(self):
        old = "› 旧任务\n\n• 已完成。\n\nWorked for 6s • 08:57\n\n› \n"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": old, "viewport": old}
        terminal_relay.seed_screen(result)
        opened = old + "\n\n" + MENU
        result.update(screen=opened, viewport=MENU)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        result.update(screen=old, viewport=old)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 1)
        # Entering while the menu is already open must also ignore its cursor as a user prompt.
        result.update(screen=opened, viewport=MENU)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
        result.update(screen=old, viewport=old)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 2)

    async def test_literal_duration_in_code_or_tool_output_is_not_a_completion(self):
        composer = "\n\n› \n\nGPT-6.1-Sol medium · ~/fixture"
        for example in ("• 字段示例：\n\n```text\nWorked for 34m 23s • 08:57\n```",
                        "• Ran printf example\n  └ Worked for 34m 23s • 08:57\n    + Show details",
                        "• Ran printf example\n    Worked for 34m 23s • 08:57"):
            with self.subTest(example=example):
                self.assertEqual(terminal_relay.worked_footer("› 新任务\n\n" + example + composer), "")
        actual = "› 新任务\n\n• 完成。\n\nWorked for 34m 23s • 08:57" + composer
        self.assertEqual(terminal_relay.worked_footer(actual), "Worked for 34m 23s • 08:57")

    async def test_restart_restores_baseline_and_sends_answer_completed_offline(self):
        before = {"screen_key": "identity", "target": "test:0.0", "screen": "原有历史"}
        terminal_relay.seed_screen(before)
        terminal_relay.checkpoint(self.event, {"epoch": 2, "selection_token": "scope"})
        terminal_relay._screens.clear()
        terminal_relay._content_screens.clear()
        terminal_relay._session.clear()
        terminal_relay._checkpoint_root = None
        terminal_relay.load_checkpoint()
        self.assertEqual(terminal_relay._screens["identity"], "原有历史")
        self.assertEqual(terminal_relay._session["epoch"], 2)
        after = {**before, "screen": "原有历史\n\n重启期间完成的回答"}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, after))
        self.assertIn("重启期间完成的回答", self.messages[-1][1])
        self.assertNotIn("原有历史", self.messages[-1][1])
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, after))
        self.assertEqual((terminal_relay.root() / "delivery.json").stat().st_mode & 0o777, 0o600)

    async def test_closed_notice_retries_without_repolling_lost_reason(self):
        original_sleep = asyncio.sleep
        async def tick(_):
            await original_sleep(0)
        notice = {"active": False, "reason": "closed", "message": "窗格已关闭。"}
        with patch.object(terminal_relay, "request", return_value=notice) as request, \
             patch.object(terminal_relay, "send", side_effect=[False, True]) as send, \
             patch.object(terminal_relay.asyncio, "sleep", side_effect=tick):
            await terminal_relay.watch(self.event, self.gateway, 1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(send.await_count, 2)
        self.assertIsNone(terminal_relay._closing_notice)

    async def test_watcher_survives_three_minutes_and_sends_idle_reconnect_notice(self):
        clock = [0.0]
        original_sleep = asyncio.sleep
        outputs = iter([{"active": True, "redrawing": True, "screen_key": "identity",
                         "target": "test:0.0", "screen": "历史\n\n未完成的碎片"}] + [
            {"active": True, "screen_key": "identity", "target": "test:0.0", "screen": "历史\n\n完整新段落"},
        ] * 4 + [{"active": False, "reason": "idle", "message": "30 分钟无操作，已断开。",
                  "reconnect_token": "reconnect-scope"}])
        terminal_relay._screens["identity"] = "历史"
        keyboards = []
        adapter = self.gateway._adapter_for_source(self.event.source)
        async def send_keyboard(chat, content, keyboard, reply_to=None):
            keyboards.append(keyboard.to_dict())
            return await adapter.send(chat, content, reply_to=reply_to)
        adapter.send_with_keyboard = send_keyboard
        self.gateway._adapter_for_source = lambda source: adapter
        async def tick(_):
            clock[0] += 65
            await original_sleep(0)
        with patch.object(terminal_relay, "request", side_effect=lambda *args: next(outputs)), \
             patch.object(terminal_relay.asyncio, "sleep", side_effect=tick), \
             patch.object(terminal_relay.asyncio, "get_running_loop", return_value=SimpleNamespace(time=lambda: clock[0])):
            await terminal_relay.watch(self.event, self.gateway, 1)
        self.assertGreater(clock[0], 180)
        self.assertEqual(len(self.messages), 2)
        self.assertIn("完整新段落", self.messages[0][1])
        self.assertFalse(terminal_relay.active())
        button = keyboards[-1]["content"]["rows"][0]["buttons"][0]
        self.assertEqual(button["render_data"]["label"], "Reconnect")
        self.assertEqual(button["action"]["data"], "#tmux reconnect reconnect-scope")
        self.assertEqual(button["action"]["permission"], {"type": 2})

    async def test_select_starts_watcher_and_keys_do_not_send_acknowledgement(self):
        with patch.object(terminal_relay, "request", return_value={
            "handled": True, "active": True, "message": "已输入", "quiet": True, "watch": True, "epoch": 1,
        }), patch.object(terminal_relay, "watch") as watch:
            terminal_relay.handle(self.event, self.gateway)
            await terminal_relay._delivery
            await terminal_relay._watcher
            watch.assert_awaited_once()
        self.assertEqual(self.messages, [])

    async def test_paragraph_failure_retry_skips_already_delivered_body(self):
        terminal_relay._screens["identity"] = "历史"
        result = {"screen_key": "identity", "target": "test:0.0",
                  "screen": "历史\n第一段" + "甲" * 1500 + "\n\n第二段" + "乙" * 1500}
        original = terminal_relay.send_terminal
        attempts = 0

        async def send_terminal(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            return False if attempts == 2 else await original(*args, **kwargs)

        with patch.object(terminal_relay, "send_terminal", side_effect=send_terminal):
            self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        # Simulate process loss after the first body was accepted.
        terminal_relay._pending_bodies.clear()
        terminal_relay._screens.clear()
        terminal_relay._content_screens.clear()
        terminal_relay._checkpoint_root = None
        terminal_relay.load_checkpoint()
        self.assertEqual(terminal_relay._pending_bodies["identity"]["next"], 1)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(len(self.messages), 2)
        self.assertIn("第一段", self.messages[0][1])
        self.assertIn("第二段", self.messages[1][1])

    async def test_menu_cursor_move_sends_whole_dialog_without_old_chat(self):
        terminal_relay._screens["identity"] = "我怎么给你cookie"
        result = {"screen_key": "identity", "target": "test:0.0",
                  "screen": "我怎么给你cookie\n" + MENU, "viewport": "我怎么给你cookie\n" + MENU}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        moved = MENU.replace("  3. Model C", "› 3. Model C").replace("› 4.", "  4.")
        result.update(screen="我怎么给你cookie\n" + moved, viewport="我怎么给你cookie\n" + moved)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        message = self.messages[-1][1]
        for line in moved.splitlines():
            self.assertIn(line, message)
        self.assertNotIn("cookie", message)
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))

    async def test_closing_menu_restores_incremental_chat(self):
        terminal_relay._screens["identity"] = "对话历史\n" + MENU
        terminal_relay._dialogs["identity"] = MENU
        result = {"screen_key": "identity", "target": "test:0.0", "screen": "对话历史\n本轮新回复",
                  "viewport": "对话历史\n本轮新回复"}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertIn("本轮新回复", self.messages[-1][1])
        self.assertNotIn("对话历史", self.messages[-1][1])
        self.assertNotIn("identity", terminal_relay._dialogs)

    async def test_menu_failed_delivery_can_retry_full_dialog(self):
        terminal_relay._screens["identity"] = "历史"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": MENU, "viewport": MENU}
        with patch.object(terminal_relay, "send", return_value=False):
            self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertNotIn("identity", terminal_relay._dialogs)
        self.assertEqual(terminal_relay._screens["identity"], "历史")
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))

    async def test_long_menu_is_sent_completely_in_one_attachment(self):
        menu = "Select Model and Effort\n" + "\n".join(f"  {i}. option " + "x" * 100 for i in range(1, 60)) + "\nenter select · esc back"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": menu, "viewport": menu}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
        self.assertEqual(self.messages, [])
        self.assertEqual(len(self.documents), 1)
        self.assertEqual(self.documents[0][1], menu)
        self.assertEqual(self.documents[0][3], "terminal-context.txt")
        self.assertEqual(list((terminal_relay.root().parent / "workspace/tmux-relay").glob("*.txt")), [])

    async def test_initial_context_keeps_one_hundred_lines_including_history_and_menu(self):
        text = "\n".join(f"history-{i} " + "x" * 70 for i in range(100 - len(MENU.splitlines()))) + "\n" + MENU
        result = {"screen_key": "identity", "target": "test:0.0", "screen": text,
                  "viewport": MENU, "initial_context": True}
        for _ in range(2):
            self.messages.clear()
            self.documents.clear()
            self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
            self.assertEqual(len(self.documents), 1)
            self.assertEqual(self.documents[0][1], text)
            self.assertEqual(len(self.documents[0][1].splitlines()), 100)
            self.assertEqual(self.messages, [])

    async def test_reported_context_and_menu_fit_one_message_after_padding_cleanup(self):
        history = "\n".join("conversation " + "x" * 100 for _ in range(25))
        padded_menu = "\n".join(line + " " * 50 for line in MENU.splitlines())
        result = {"screen_key": "identity", "target": "test:0.0",
                  "screen": history + "\n" + padded_menu,
                  "viewport": padded_menu, "initial_context": True}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.documents, [])
        self.assertIn(history, self.messages[0][1])
        for line in MENU.splitlines():
            self.assertIn(line, self.messages[0][1])

    async def test_failed_attachment_does_not_advance_baseline(self):
        terminal_relay._screens["identity"] = "历史"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": "新" * 4500,
                  "initial_context": True}
        self.gateway._adapter_for_source = lambda source: SimpleNamespace(
            send_document=lambda *args, **kwargs: None,
        )
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
        self.assertEqual(terminal_relay._screens["identity"], "历史")
        self.assertEqual(list((terminal_relay.root().parent / "workspace/tmux-relay").glob("*.txt")), [])

    async def test_visible_chat_not_old_menu_in_history_controls_rendering(self):
        terminal_relay._screens["identity"] = MENU
        result = {"screen_key": "identity", "target": "test:0.0", "screen": MENU + "\n新回复",
                  "viewport": "新回复\n提示符"}
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertNotIn("Select Model", self.messages[-1][1])

    async def test_failed_delivery_preserves_baseline_for_retry(self):
        terminal_relay._screens["identity"] = "历史"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": "历史\n新回复"}
        with patch.object(terminal_relay, "send", return_value=False):
            self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(terminal_relay._screens["identity"], "历史")
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertIn("新回复", self.messages[-1][1])

    async def test_new_pane_and_explicit_screen_reset_baseline(self):
        terminal_relay._screens["old-pane"] = "旧窗格历史"
        result = {"screen_key": "new-pane", "target": "test:0.1", "screen": "新窗格历史"}
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
        self.assertNotIn("old-pane", terminal_relay._screens)
        self.assertTrue(await terminal_relay.deliver_screen(self.event, self.gateway, result, snapshot=True))
        self.assertEqual(len(self.messages), 2)

    async def test_deletion_only_updates_baseline_without_delivery(self):
        terminal_relay._screens["identity"] = "历史\n输入提示"
        result = {"screen_key": "identity", "target": "test:0.0", "screen": "历史"}
        self.assertFalse(await terminal_relay.deliver_screen(self.event, self.gateway, result))
        self.assertEqual(terminal_relay._screens["identity"], "历史")
        self.assertEqual(self.messages, [])


class HttpBridgeTests(TmuxFixture):
    def setUp(self):
        super().setUp()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.token = "fixture-token-" + "x" * 40
        path = Path(self.temp.name) / "token"
        path.write_text(self.token)
        self.process = subprocess.Popen(
            [sys.executable, str(MODULE), "--token-file", str(path), "--socket", self.socket,
             "--port", str(port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                if self.post("/healthz", {})["ok"]:
                    return
            except OSError:
                time.sleep(0.02)
        self.process.terminate()
        self.process.wait()
        self.fail("fixture HTTP server did not start")

    def tearDown(self):
        self.process.terminate()
        self.process.wait(timeout=3)
        super().tearDown()

    def post(self, path, data, token=None):
        request = urllib.request.Request(
            self.url + path, data=json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + (self.token if token is None else token)},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    def test_authenticated_http_roundtrip_with_real_terminal(self):
        result = self.post("/v1/route", {"text": "#tmux ls"})
        self.assertIn("relay-test", result["message"])
        self.assertTrue(self.post("/v1/route", {"text": "#tmux select 1"})["active"])
        result = self.post("/v1/route", {"text": "真实中文 HTTP 输入", "message_id": "http-message"})
        self.wait_for("ECHO:真实中文 HTTP 输入")
        self.assertIn("真实中文", self.post("/v1/screen", {"epoch": result["epoch"]})["screen"])
        self.assertTrue(self.post("/v1/route", {"text": "真实中文 HTTP 输入", "message_id": "http-message"})["duplicate"])
        self.assertFalse(self.post("/v1/route", {"text": "#tmux exit"})["active"])

    def test_missing_or_bad_token_is_denied(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/v1/route", {"text": "#tmux ls"}, token="invalid")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_activity_endpoint_is_authenticated_scoped_and_deduplicated(self):
        entry = self.post("/v1/route", {"text": "#tmux select %0"})
        body = {"selection_token": entry["selection_token"], "event_id": "bot:accepted"}
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/v1/activity", body, token="invalid")
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        self.assertFalse(self.post("/v1/activity", {**body, "selection_token": "stale"})["recorded"])
        self.assertTrue(self.post("/v1/activity", body)["recorded"])
        self.assertTrue(self.post("/v1/activity", body)["duplicate"])
        self.post("/v1/route", {"text": "#tmux exit"})
        self.assertFalse(self.post("/v1/activity", {**body, "event_id": "bot:late"})["recorded"])

    def test_activity_payload_is_validated(self):
        for body in ({}, {"selection_token": ""}, {"selection_token": 1},
                     {"selection_token": "x", "event_id": []}, {"selection_token": "x" * 257}):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as caught:
                self.post("/v1/activity", body)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()

    def test_malformed_payload_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.post("/v1/route", {"text": ["not", "text"]})
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()


if __name__ == "__main__":
    unittest.main()
