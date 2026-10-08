import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

from test_relay import TmuxFixture, relay_module


class ExclusiveTests(TmuxFixture):
    def bridge(self, name, durable=False):
        locks = relay_module.PaneLocks(Path(self.temp.name) / "locks", self.socket, name)
        self.addCleanup(locks.release_except)
        return relay_module.Relay(self.tmux, pane_locks=locks,
                                  state_file=Path(self.temp.name) / (name + ".sqlite3") if durable else None)

    def test_other_bot_cannot_select_pick_input_or_steal_on_reconnect(self):
        first, second = self.bridge("winter"), self.bridge("atri")
        first.route("#tmux select %0")
        listing = second.route("#tmux ls")
        self.assertIn("占用：winter", listing["message"])
        for command in ("#tmux select %0", "#tmux pick " + listing["pane_shortcuts"][0]["token"]):
            result = second.route(command)
            self.assertFalse(result["active"])
            self.assertIn("已由 winter 连接", result["message"])
        self.assertFalse(second.route("not-forwarded")["handled"])
        self.assertNotIn("not-forwarded", self.tmux.capture("%0"))
        first.route("#tmux exit")
        self.assertTrue(second.route("#tmux select %0")["active"])

    def test_failed_switch_keeps_original_selection_and_lease(self):
        self.tmux.run("new-window", "-t", "relay-test", "sleep 60")
        first, second = self.bridge("winter"), self.bridge("atri")
        first.route("#tmux select %0")
        second.route("#tmux select %1")
        token = first.selection_token
        result = first.route("#tmux select %1")
        self.assertIn("已由 atri 连接", result["message"])
        self.assertEqual(first.selected["pane_id"], "%0")
        self.assertEqual(first.selection_token, token)
        self.assertFalse(second.route("#tmux select %0").get("initial_context", False))
        first.route("#tmux exit")
        self.assertTrue(second.route("#tmux select %0")["initial_context"])

    def test_failed_capture_keeps_old_lease_and_releases_candidate(self):
        self.tmux.run("new-window", "-t", "relay-test", "sleep 60")
        first, second = self.bridge("winter"), self.bridge("atri")
        first.route("#tmux select %0")
        with patch.object(first.tmux, "capture", side_effect=relay_module.RelayError("capture failed")):
            self.assertIn("capture failed", first.route("#tmux select %1")["message"])
        self.assertEqual(first.selected["pane_id"], "%0")
        self.assertTrue(second.route("#tmux select %1")["active"])
        self.assertIn("已由 winter 连接", second.route("#tmux select %0")["message"])

    def test_idle_release_and_reconnect_cannot_take_other_bot_lease(self):
        first, second = self.bridge("winter"), self.bridge("atri")
        first.route("#tmux select %0")
        first.touched -= 1801
        notice = first.state()
        self.assertEqual(notice["reason"], "idle")
        self.assertTrue(second.route("#tmux select %0")["active"])
        denied = first.route("#tmux reconnect " + notice["reconnect_token"])
        self.assertFalse(denied["active"])
        self.assertIn("已由 atri 连接", denied["message"])
        second.route("#tmux exit")
        self.assertTrue(first.route("#tmux reconnect " + notice["reconnect_token"])["active"])

    def test_restore_conflict_drops_selection_without_executing_input(self):
        first = self.bridge("winter", durable=True)
        first.route("#tmux select %0")
        first.db.close()
        first.pane_locks.release_except()
        second = self.bridge("atri")
        second.route("#tmux select %0")
        restored = self.bridge("winter", durable=True)
        self.addCleanup(restored.db.close)
        self.assertEqual(restored.state()["reason"], "claimed")
        self.assertFalse(restored.route("never-sent")["handled"])
        self.assertNotIn("never-sent", self.tmux.capture("%0"))

    def test_concurrent_selection_has_exactly_one_winner(self):
        first, second = self.bridge("winter"), self.bridge("atri")
        barrier, results = threading.Barrier(2), []

        def select(bridge):
            barrier.wait(timeout=5)
            results.append(bridge.route("#tmux select %0"))

        threads = [threading.Thread(target=select, args=(bridge,)) for bridge in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(result["active"] for result in results), 1)

    def test_process_crash_releases_kernel_lock(self):
        pane = self.tmux.panes()[0]
        code = ("import json,pathlib,sys,time; sys.path.insert(0,sys.argv[1]); "
                "from tmux_bot.bridge import PaneLocks; "
                "locks=PaneLocks(pathlib.Path(sys.argv[2]),sys.argv[3],'child'); "
                "locks.acquire(json.loads(sys.argv[4])); print('READY',flush=True); time.sleep(30)")
        process = subprocess.Popen([sys.executable, "-B", "-c", code,
            str(Path(relay_module.__file__).parents[1]), str(Path(self.temp.name) / "locks"),
            self.socket, json.dumps(pane)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            bridge = self.bridge("atri")
            self.assertFalse(bridge.route("#tmux select %0")["active"])
            process.kill()
            process.wait(timeout=5)
            self.assertTrue(bridge.route("#tmux select %0")["active"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stdout.close()


if __name__ == "__main__":
    unittest.main()
