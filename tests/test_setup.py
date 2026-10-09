import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
for name in ("manage", "check_release"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    globals()[name] = module

from tmux_bot.bridge import RelayError, resolve_tmux_binary, serve
from tmux_bot.multiplex import MultiRelay


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tmux_binary = self.root / "tmux"
        self.tmux_binary.write_text("#!/bin/sh\nexit 0\n")
        self.tmux_binary.chmod(0o700)
        environment = patch.dict(os.environ, {"TMUX_BINARY": str(self.tmux_binary)})
        environment.start()
        self.addCleanup(environment.stop)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.root / "tmux.sock"))
        self.addCleanup(self.sock.close)

    def test_initialization_permissions_and_no_credential_copy(self):
        with patch.object(manage, "ROOT", self.root), patch.dict(os.environ, {}, clear=False):
            instance = manage.initialize("first", 18210, self.root / "tmux.sock")
        self.assertEqual(instance.stat().st_mode & 0o777, 0o700)
        for path in (instance / "bot.env", instance / "compose.env", instance / "data/tmux-relay/token",
                     instance / "data/pairing-instruction.json"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((instance / "bot.env").read_text(), "QQ_APP_ID=\nQQ_CLIENT_SECRET=\n")
        self.assertGreaterEqual(len((instance / "data/tmux-relay/token").read_text().strip()), 32)
        config = json.loads((instance / "data/tmux-relay/client.json").read_text())
        self.assertEqual(config["url"], "http://127.0.0.1:18210")
        self.assertIn(str(self.root / "instances/pane-locks"),
                      (instance / "qq-tmux-bridge-first.service").read_text())
        self.assertIn("WorkingDirectory=" + str(self.root) + "\n",
                      (instance / "qq-tmux-bridge-first.service").read_text())
        bridge = json.loads((instance / "data/tmux-relay/bridge.json").read_text())
        self.assertEqual(bridge["port"], 18210)
        self.assertEqual(bridge["idle_seconds"], 1800)
        self.assertEqual(bridge["usage_port"], 18014)
        self.assertIn("http://127.0.0.1:18014/v1/sub2api/usage",
                      (instance / "compose.env").read_text())
        self.assertIn("--tmux-binary", (instance / "qq-tmux-bridge-first.service").read_text())
        self.assertIn("RestartPreventExitStatus=2", (instance / "qq-tmux-bridge-first.service").read_text())

    def test_existing_instance_is_never_overwritten(self):
        with patch.object(manage, "ROOT", self.root), patch.dict(os.environ, {}, clear=False):
            instance = manage.initialize("first", 18210, self.root / "tmux.sock")
            token = (instance / "data/tmux-relay/token").read_bytes()
            with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
                manage.initialize("first", 18211, self.root / "tmux.sock")
        self.assertEqual((instance / "data/tmux-relay/token").read_bytes(), token)

    def test_bad_instance_names_ports_and_non_socket_are_rejected(self):
        with patch.object(manage, "ROOT", self.root):
            for name in ("../other", "/absolute", "with space", "a\nvalue"):
                with self.assertRaises(ValueError):
                    manage.initialize(name, 18210, self.root / "tmux.sock")
            for port in (22, 65536):
                with self.assertRaises(ValueError):
                    manage.initialize("first", port, self.root / "tmux.sock")
            with self.assertRaises(ValueError):
                manage.initialize("first", 18210, self.root / "not-a-socket")

    def test_two_instances_have_distinct_tokens_and_shared_lock_directory(self):
        with patch.object(manage, "ROOT", self.root), patch.dict(os.environ, {}, clear=False):
            a = manage.initialize("first", 18210, self.root / "tmux.sock")
            b = manage.initialize("second", 18211, self.root / "tmux.sock")
        self.assertNotEqual((a / "data/tmux-relay/token").read_text(), (b / "data/tmux-relay/token").read_text())
        for path in (a / "qq-tmux-bridge-first.service", b / "qq-tmux-bridge-second.service"):
            self.assertIn(str(self.root / "instances/pane-locks"), path.read_text())

    def test_occupied_or_configured_port_is_rejected_before_writing_instance(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        occupied = listener.getsockname()[1]
        with patch.object(manage, "ROOT", self.root):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                manage.initialize("occupied", occupied, self.root / "tmux.sock")
            self.assertFalse((self.root / "instances/occupied").exists())
            manage.initialize("first", 18210, self.root / "tmux.sock")
            with self.assertRaisesRegex(ValueError, "already assigned"):
                manage.initialize("duplicate", 18210, self.root / "tmux.sock")
            self.assertFalse((self.root / "instances/duplicate").exists())

    def test_configure_preserves_credentials_pairing_and_state(self):
        with patch.object(manage, "ROOT", self.root), patch.dict(os.environ, {}, clear=False):
            instance = manage.initialize("first", 18210, self.root / "tmux.sock")
            token = (instance / "data/tmux-relay/token").read_bytes()
            bot_env = (instance / "bot.env").read_bytes()
            pairing = (instance / "data/pairing-instruction.json").read_bytes()
            state = instance / "data/tmux-relay/state-fixture"
            state.write_bytes(b"retained terminal state")
            manage.configure("first", idle_seconds=2400, usage_port=18214)
        self.assertEqual((instance / "data/tmux-relay/token").read_bytes(), token)
        self.assertEqual((instance / "bot.env").read_bytes(), bot_env)
        self.assertEqual((instance / "data/pairing-instruction.json").read_bytes(), pairing)
        config = json.loads((instance / "data/tmux-relay/bridge.json").read_text())
        self.assertEqual(config["idle_seconds"], 2400)
        self.assertEqual(config["usage_port"], 18214)
        self.assertEqual(state.read_bytes(), b"retained terminal state")
        self.assertIn("http://127.0.0.1:18214/v1/sub2api/usage",
                      (instance / "compose.env").read_text())
        self.assertIn('"2400"', (instance / "qq-tmux-bridge-first.service").read_text())

    def test_usage_port_must_be_valid_and_separate_from_bridge(self):
        with patch.object(manage, "ROOT", self.root):
            for port in (22, 65536, 18210):
                with self.assertRaisesRegex(ValueError, "usage port"):
                    manage.initialize("first", 18210, self.root / "tmux.sock", usage_port=port)
                self.assertFalse((self.root / "instances/first").exists())

    def test_display_name_survives_reconfigure_and_legacy_migration(self):
        with patch.object(manage, "ROOT", self.root), patch.dict(os.environ, {}, clear=False):
            instance = manage.initialize("first", 18210, self.root / "tmux.sock", bot_name="Terminal Helper")
            manage.configure("first", idle_seconds=2400)
            bridge_path = instance / "data/tmux-relay/bridge.json"
            self.assertEqual(json.loads(bridge_path.read_text())["bot_name"], "Terminal Helper")
            bridge_path.unlink()
            manage.configure("first", socket_path=self.root / "tmux.sock")
            self.assertEqual(json.loads(bridge_path.read_text())["bot_name"], "Terminal Helper")
            self.assertIn('BOT_NAME="Terminal Helper"', (instance / "compose.env").read_text())

    def test_invalid_json_configuration_has_a_readable_error(self):
        path = self.root / "invalid.json"
        path.write_text("[]")
        with self.assertRaisesRegex(ValueError, "expected a JSON object"):
            manage.read_json(path)

    def test_legacy_instance_requires_socket_then_migrates(self):
        with patch.object(manage, "ROOT", self.root), patch.dict(os.environ, {}, clear=False):
            instance = manage.initialize("first", 18210, self.root / "tmux.sock")
            (instance / "data/tmux-relay/bridge.json").unlink()
            with self.assertRaisesRegex(ValueError, "requires --socket"):
                manage.configure("first")
            manage.configure("first", socket_path=self.root / "tmux.sock")
        self.assertTrue((instance / "data/tmux-relay/bridge.json").is_file())


class HostRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_tmux_binary_supports_path_discovery_and_explicit_path(self):
        executable = self.root / "custom-tmux"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        self.assertEqual(resolve_tmux_binary(str(executable)), str(executable.resolve()))
        with patch.dict(os.environ, {"PATH": str(self.root)}):
            path_name = self.root / "tmux"
            path_name.write_text("#!/bin/sh\nexit 0\n")
            path_name.chmod(0o700)
            self.assertEqual(resolve_tmux_binary(), str(path_name.resolve()))
        with self.assertRaisesRegex(RelayError, "--tmux-binary"):
            resolve_tmux_binary(str(self.root / "missing"))

    def test_bridge_bind_error_is_actionable(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        with self.assertRaisesRegex(RelayError, "无法监听.*端口占用"):
            serve(object(), "x" * 32, port)

    def test_relocated_instance_starts_from_generated_configuration(self):
        if shutil.which("tmux") is None:
            self.skipTest("tmux required for real relocation test")
        old_root, new_root = self.root / "old workspace", self.root / "new workspace"
        tmux_binary = resolve_tmux_binary("tmux")
        tmux_socket = str(self.root / "migration.sock")
        subprocess.run([tmux_binary, "-S", tmux_socket, "new-session", "-d",
                        "-s", "migration-test", "cat"], check=True, capture_output=True)
        self.addCleanup(lambda: subprocess.run([tmux_binary, "-S", tmux_socket, "kill-server"],
                                              capture_output=True))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with patch.object(manage, "ROOT", old_root), patch.dict(os.environ, {}, clear=False):
            old_instance = manage.initialize("migrated", port, tmux_socket, tmux_binary)
        token_before = (old_instance / "data/tmux-relay/token").read_bytes()
        shutil.copytree(old_root / "instances", new_root / "instances")
        shutil.copytree(ROOT / "src", new_root / "src", ignore=shutil.ignore_patterns("__pycache__"))
        with patch.object(manage, "ROOT", new_root), patch.dict(os.environ, {}, clear=False):
            instance = manage.configure("migrated", socket_path=tmux_socket, tmux_binary=tmux_binary)
        config = json.loads((instance / "data/tmux-relay/bridge.json").read_text())
        self.assertEqual(config["lock_dir"], str(new_root / "instances/pane-locks"))
        self.assertEqual((instance / "data/tmux-relay/token").read_bytes(), token_before)
        unit = (instance / "qq-tmux-bridge-migrated.service").read_text()
        self.assertNotIn(str(old_root), unit)
        self.assertNotIn(str(old_root), (instance / "compose.env").read_text())
        command = shlex.split(next(line.split("=", 1)[1] for line in unit.splitlines()
                                   if line.startswith("ExecStart=")))
        if shutil.which("systemd-analyze"):
            verified = subprocess.run(["systemd-analyze", "--user", "verify",
                                      str(instance / "qq-tmux-bridge-migrated.service")],
                                      text=True, capture_output=True, timeout=5)
            self.assertEqual(verified.returncode, 0, verified.stderr)
        with tempfile.TemporaryFile(mode="w+") as output:
            process = subprocess.Popen(command, cwd=new_root, stdout=output, stderr=output)
            try:
                request = Request("http://127.0.0.1:" + str(port) + "/v2/route",
                    data=json.dumps({"text": "/tmux ls", "message_id": "migration-list",
                        "source": {"chat_type": "dm", "chat_id": "test", "user_id": "test"}}).encode(),
                    headers={"Authorization": "Bearer " + token_before.decode().strip(),
                             "Content-Type": "application/json"}, method="POST")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with build_opener(ProxyHandler({})).open(request, timeout=1) as response:
                            result = json.load(response)
                        break
                    except (URLError, OSError):
                        if process.poll() is not None:
                            output.seek(0)
                            self.fail("relocated bridge startup failed: " + output.read())
                        time.sleep(.05)
                else:
                    self.fail("relocated bridge did not become ready")
                self.assertIn("migration-test", result["message"])
                self.assertIn("001", result["message"])
            finally:
                process.terminate()
                process.wait(timeout=5)

    def test_direct_file_startup_has_readable_errors_not_tracebacks(self):
        token = self.root / "token"
        token.write_text("x" * 32)
        command = [os.sys.executable, str(ROOT / "src/tmux_bot/bridge.py"),
                   "--token-file", str(token), "--tmux-binary", str(self.root / "missing")]
        result = subprocess.run(command, text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--tmux-binary", result.stderr)
        self.assertIn("桥接启动失败", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        executable = self.root / "tmux"
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o700)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        result = subprocess.run(command[:-1] + [str(executable), "--port",
                                str(listener.getsockname()[1])],
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)
        self.assertIn("端口占用", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_terminal_listing_is_sorted_by_stable_numeric_id(self):
        class FakeTmux:
            errors = {}

            def panes(self):
                return [
                    {"identity": "third", "target": "third:0.0", "pane_id": "%3"},
                    {"identity": "first", "target": "first:0.0", "pane_id": "%1"},
                ]

        relay = MultiRelay(FakeTmux(), self.root / "relay")
        self.addCleanup(relay.close)
        relay.db.executemany("INSERT INTO terminals VALUES (?,?,?,NULL)", [
            ("003", "third", json.dumps({"identity": "third", "target": "third:0.0", "pane_id": "%3"})),
            ("001", "first", json.dumps({"identity": "first", "target": "first:0.0", "pane_id": "%1"})),
        ])
        relay.db.commit()
        message = relay.listing()["message"]
        self.assertLess(message.index("`001`"), message.index("`003`"))


class ReleaseTests(unittest.TestCase):
    def test_obvious_keys_and_credential_fields_are_rejected_without_echo(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret = "sk-" + "x" * 32
            (root / "config.py").write_text("key = " + repr(secret))
            result = check_release.check(root)
            self.assertTrue(result)
            self.assertNotIn(secret, " ".join(result))
            (root / "config.py").write_text("config = " + repr({"client_secret": "A" * 32}))
            self.assertTrue(check_release.check(root))

    def test_force_tracked_private_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            path = root / "instances/default/token"
            path.parent.mkdir(parents=True)
            path.write_text("fixture")
            subprocess.run(["git", "add", str(path)], cwd=root, check=True)
            self.assertTrue(check_release.check(root, indexed=True))

    def test_index_scan_reads_staged_blob_not_clean_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            path = root / "config.py"
            path.write_text("key = " + repr("sk-" + "x" * 32))
            subprocess.run(["git", "add", str(path)], cwd=root, check=True)
            path.write_text("key = None")
            self.assertFalse(check_release.check(root))
            self.assertTrue(check_release.check(root, indexed=True))
            path.unlink()
            self.assertTrue(check_release.check(root, indexed=True))

    def test_real_env_files_are_rejected_but_example_is_allowed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env.example").write_text("QQ_CLIENT_SECRET=\n")
            self.assertFalse(check_release.check(root))
            for name in (".env.production", "private.env"):
                path = root / name
                path.write_text("QQ_CLIENT_SECRET=fixture")
                self.assertTrue(check_release.check(root))
                path.unlink()

    def test_generated_site_and_backup_files_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            for name in ("backups/private.txt", "docs/guide/node_modules/package/index.js",
                         "docs/guide/test-results/screenshot.png", "docs/guide/playwright-report/index.html"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
                self.assertFalse(check_release.check(root))
                subprocess.run(["git", "add", str(path)], cwd=root, check=True)
                self.assertTrue(check_release.check(root, indexed=True))
                subprocess.run(["git", "rm", "--cached", str(path)], cwd=root,
                               check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
