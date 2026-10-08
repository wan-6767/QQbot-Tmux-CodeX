import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for name in ("manage", "check_release"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    globals()[name] = module


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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
            for name in ("backups/private.txt", "site/node_modules/package/index.js",
                         "site/test-results/screenshot.png", "site/playwright-report/index.html"):
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
