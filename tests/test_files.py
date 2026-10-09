import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from gateway.config import Platform, PlatformConfig
from tmux_bot import app, files, owner, terminal_files
from tmux_bot.bridge import RelayError
from tmux_bot.multi_relay import MultiGateway


class HostFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_download_is_a_private_snapshot_and_release_is_scoped(self):
        source = self.home / "源文件.txt"
        source.write_bytes(b"original content")
        result = files.api(self.home, "prepare", {"path": str(source)})
        spool = self.home / result["relative"]
        self.assertEqual(spool.read_bytes(), source.read_bytes())
        self.assertEqual(spool.stat().st_mode & 0o777, 0o600)
        source.write_bytes(b"new content")
        self.assertEqual(spool.read_bytes(), b"original content")
        with self.assertRaises(RelayError):
            files.api(self.home, "release", {"token": "../源文件.txt"})
        files.api(self.home, "release", {"token": result["token"]})
        self.assertFalse(spool.exists())
        self.assertTrue(source.exists())

    def test_directory_device_fifo_symlink_relative_and_oversized_files_rejected(self):
        source = self.home / "source"
        source.write_bytes(b"1234")
        link = self.home / "link"
        link.symlink_to(source)
        fifo = self.home / "fifo"
        os.mkfifo(fifo)
        for path in (str(self.home), str(link), str(fifo), "/dev/zero", "relative", "/missing-file"):
            with self.subTest(path=path), self.assertRaises(RelayError):
                files.api(self.home, "prepare", {"path": path})
        with patch.object(files, "MAX_FILE_BYTES", 3), self.assertRaises(RelayError):
            files.api(self.home, "prepare", {"path": str(source)})
        self.assertFalse(list((self.home / "cache/outgoing-files").iterdir()))

    def test_cache_receipt_verified_by_host_inode_and_cleanup_preserves_other_files(self):
        root = self.home / "tmux-relay"
        root.mkdir()
        (root / "client.json").write_text(json.dumps({"host_data_root": str(self.home)}))
        cache = self.home / "cache/documents"
        cache.mkdir(parents=True)
        source, unrelated = cache / "upload.txt", cache / "other.txt"
        source.write_text("uploaded")
        unrelated.write_text("preserve")
        event = SimpleNamespace(metadata={"qqbot_cached_attachments": [{"path": str(source)}]})
        def validate(relative, device, inode):
            return files.api(self.home, "validate", {"relative": relative, "device": device, "inode": inode})["path"]
        message = terminal_files.receipt(event, root, validate)
        self.assertIn(str(source), message)
        self.assertIn("1", terminal_files.clear(root))
        self.assertFalse(source.exists())
        self.assertTrue(unrelated.exists())
        with self.assertRaises(RelayError):
            files.api(self.home, "validate", {"relative": "../other", "device": 1, "inode": 2})

    def test_specific_cache_cleanup_preserves_other_received_and_project_files(self):
        root = self.home / "tmux-relay"
        root.mkdir()
        (root / "client.json").write_text(json.dumps({"host_data_root": str(self.home)}))
        directory = self.home / "cache/documents"
        directory.mkdir(parents=True)
        first, second = directory / "first.txt", directory / "second.txt"
        first.write_text("first")
        second.write_text("second")
        event = SimpleNamespace(metadata={"qqbot_cached_attachments": [{"path": str(first)}, {"path": str(second)}]})
        terminal_files.receipt(event, root)
        self.assertIn("只能删除", terminal_files.clear(root, "/etc/passwd"))
        self.assertIn("已清理 1", terminal_files.clear(root, str(first)))
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())


class FileGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {"HERMES_HOME": self.temp.name})
        env.start(); self.addCleanup(env.stop)
        owner.write_private(owner.home() / "owner.json", {"openid": "owner"})
        owner.write_private(owner.home() / "group.json", {"owner_openid": "owner", "group_openid": "group", "member_openid": "member"})
        self.adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)),
            send_document=AsyncMock(return_value=SimpleNamespace(success=True)), _chat_type_map={})
        self.gateway = MultiGateway(self.adapter)
        self.gateway.request = AsyncMock()

    def event(self, text="", user="owner", chat="owner", kind="dm", metadata=None):
        return SimpleNamespace(source=SimpleNamespace(platform=Platform.QQBOT, user_id=user, chat_id=chat, chat_type=kind),
            text=text, raw_message={"content": text}, metadata=metadata, message_id="file-event")

    async def test_upload_does_not_send_metadata_or_caption_to_any_terminal(self):
        with patch.object(terminal_files, "receipt", return_value="接收完成\n/path"):
            await self.gateway.dispatch(self.event("/tmux sel 001 dangerous", metadata={"qqbot_cached_attachments": [{"path": "/cache/file"}]}))
        self.gateway.request.assert_not_awaited()
        self.assertIn("接收完成", self.adapter.send.call_args.args[1])

    async def test_download_private_and_group_and_cleanup_on_qq_failure(self):
        for event, success in ((self.event("/file dl /srv/a.zip"), True),
                (self.event("/file dl /srv/a.zip", "member", "group", "group"), False)):
            self.gateway.request.reset_mock()
            self.gateway.request.side_effect = [{"relative": "cache/outgoing-files/abc", "token": "a" * 32, "name": "a.zip"}, {"released": True}]
            self.adapter.send_document.return_value = SimpleNamespace(success=success, error="HTTP 429")
            await self.gateway.dispatch(event)
            self.assertEqual(self.gateway.request.call_args_list[-1].args[0], "/v2/files/release")
            self.assertEqual(self.adapter.send_document.call_args.args[0], event.source.chat_id)
        self.assertIn("HTTP 429", self.adapter.send.call_args.args[1])

    async def test_clear_and_unauthorized_requests_never_reach_terminal(self):
        with patch.object(terminal_files, "clear", return_value="已清理 1 个上传文件。") as clear:
            await self.gateway.dispatch(self.event("/file rm"))
            clear.assert_called_once_with(Path(self.temp.name) / "tmux-relay", None)
        for value in ("/file dl /srv/private.zip", "/file rm"):
            await self.gateway.dispatch(self.event(value, user="outsider", chat="outsider"))
        self.gateway.request.assert_not_awaited()
        self.adapter.send_document.assert_not_awaited()

    async def test_file_help_and_invalid_operations_never_reach_terminal(self):
        for value in ("/file", "/file dl", "/file help", "/file delete /etc/passwd"):
            await self.gateway.dispatch(self.event(value))
            self.assertIn("/file help", self.adapter.send.call_args.args[1])
        self.gateway.request.assert_not_awaited()
        self.adapter.send_document.assert_not_awaited()

    async def test_global_and_module_help_are_distinct_and_read_only(self):
        await self.gateway.dispatch(self.event("/help"))
        global_help = self.adapter.send.call_args.args[1]
        for command in ("/tmux help", "/file help", "/sub2api usage"):
            self.assertIn(command, global_help)
        await self.gateway.dispatch(self.event("/tmux help"))
        self.assertIn("key enter", self.adapter.send.call_args.args[1])
        await self.gateway.dispatch(self.event("/file help"))
        self.assertEqual(self.adapter.send.call_args.args[1], terminal_files.HELP)
        self.gateway.request.assert_not_awaited()
        self.adapter.send_document.assert_not_awaited()

    async def test_selected_cache_delete_preserves_argument_and_never_reaches_terminal(self):
        with patch.object(terminal_files, "clear", return_value="已清理 1 个上传文件。") as clear:
            await self.gateway.dispatch(self.event("/file rm /srv/cache/documents/文件 a.txt"))
            clear.assert_called_once_with(Path(self.temp.name) / "tmux-relay", "/srv/cache/documents/文件 a.txt")
        self.gateway.request.assert_not_awaited()

    async def test_voice_is_cached_as_file_without_transcription(self):
        adapter = app.TerminalAdapter(PlatformConfig())
        with patch.object(app.QQAdapter, "_process_attachments", AsyncMock(return_value={})) as process:
            await adapter._process_attachments([{"content_type": "audio/silk", "filename": "voice.silk", "url": "https://example.org/v"}])
        self.assertEqual(process.call_args.args[0][0]["content_type"], "application/octet-stream")
        self.assertEqual(process.call_args.args[0][0]["filename"], "qq-voice.bin")
