import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from tmux_bot.sub2api import command, service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.reader = Mock()
        self.reader.refresh.return_value = {"ok": True, "accounts": []}
        self.server = service.make_server(self.reader, "test-token", port=0)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, path, body=b"{}", token="test-token"):
        request = Request("http://127.0.0.1:" + str(self.server.server_port) + path,
                          data=body, headers={"Authorization": "Bearer " + token}, method="POST")
        return build_opener(ProxyHandler({})).open(request, timeout=2)

    def test_auth_and_fixed_operation(self):
        with self.post("/v1/sub2api/usage") as response:
            self.assertEqual(response.status, 200)
        self.reader.refresh.assert_called_once()
        with self.assertRaises(HTTPError) as error:
            self.post("/v1/sub2api/usage", token="wrong")
        self.assertEqual(error.exception.code, 403)
        self.reader.refresh.assert_called_once()

    def test_no_terminal_or_arbitrary_admin_endpoints(self):
        for path in ("/v1/route", "/v1/screen", "/api/v1/admin/users"):
            with self.assertRaises(HTTPError) as error:
                self.post(path)
            self.assertEqual(error.exception.code, 404)
        self.reader.refresh.assert_not_called()

    def test_invalid_requests_never_refresh(self):
        for body in (b'{"command":"whoami"}', b"[]", b"null", b"invalid", b"x" * 1025):
            with self.assertRaises(HTTPError) as error:
                self.post("/v1/sub2api/usage", body)
            self.assertEqual(error.exception.code, 400)
        self.reader.refresh.assert_not_called()

    def test_health_does_not_refresh(self):
        with self.post("/healthz") as response:
            self.assertEqual(response.status, 200)
        self.reader.refresh.assert_not_called()


class CommandTests(unittest.IsolatedAsyncioTestCase):
    def test_usage_endpoint_is_configurable_but_stays_loopback_only(self):
        with patch.dict(os.environ, {"SUB2API_USAGE_URL": "http://127.0.0.1:18214/v1/sub2api/usage"}):
            self.assertEqual(command.usage_url(), "http://127.0.0.1:18214/v1/sub2api/usage")
        for value in ("https://127.0.0.1:18214/v1/sub2api/usage",
                      "http://example.com:18214/v1/sub2api/usage",
                      "http://user@127.0.0.1:18214/v1/sub2api/usage",
                      "http://127.0.0.1:22/v1/sub2api/usage",
                      "http://127.0.0.1:18214/api/v1/admin",
                      "http://127.0.0.1:18214/v1/sub2api/usage?token=test"):
            with patch.dict(os.environ, {"SUB2API_USAGE_URL": value}):
                with self.assertRaises(ValueError):
                    command.usage_url()

    async def test_revoked_authorization_does_not_deliver_refreshed_data(self):
        authorized = True
        handler = command.UsageCommand(lambda event: authorized)
        adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
        event = SimpleNamespace(source=SimpleNamespace(chat_id="owner"), message_id="revoked")
        with patch.object(command, "fetch_usage", side_effect=lambda: {"ok": True, "accounts": []}):
            await handler.handle(event, adapter)
            authorized = False
            await handler.task
        self.assertEqual(adapter.send.await_count, 1)

    async def test_duplicate_and_concurrent_queries_do_not_double_refresh(self):
        handler = command.UsageCommand()
        adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
        event = SimpleNamespace(source=SimpleNamespace(chat_id="owner"), message_id="query")
        with patch.object(command, "fetch_usage", return_value={"ok": True, "accounts": []}) as fetch:
            await handler.handle(event, adapter)
            await handler.handle(event, adapter)
            await handler.task
        fetch.assert_called_once()
        self.assertIn("Sub2API", adapter.send.call_args.args[1])

    def test_stale_quota_not_presented_as_current_or_zero(self):
        text = command.format_usage({"ok": True, "accounts": [{"id": 1, "name": "fixture", "fresh": False,
                                      "error": "HTTP 503", "five_hour": {"remaining_percent": 50}}]})
        self.assertIn("503", text)
        self.assertIn("未刷新", text)
        self.assertNotIn("50%", text)

    def test_fixed_width_progress_and_reset(self):
        for percent in (0, 29, 82, 100):
            line = command.quota_line("5h", {"remaining_percent": percent, "resets_at": "2026-10-06T19:22:00Z"})
            bar = line.split("`", 2)[1]
            self.assertEqual(len(bar), 10)
            self.assertEqual(bar.count("=") + bar.count("-"), 8)
            self.assertIn(str(percent) + "%", line)
            self.assertIn("10-07 03:22", line)
            self.assertNotIn("\n", line)
        self.assertEqual(command.quota_line("7d", None), "7d 未知")

    def test_monthly_window_is_not_mislabeled(self):
        value = {"remaining_percent": 100, "window_minutes": 43200}
        text = command.format_usage({"ok": True, "accounts": [{"id": 6, "name": "go", "fresh": True,
                                   "status": "active", "schedulable": True, "five_hour": None, "seven_day": value}]})
        self.assertIn("30d", text)
        self.assertNotIn("7d", text)
        self.assertNotIn("5h", text)

    def test_credits_compact_on_account_heading_and_not_reset_cards(self):
        for credits, expected in (
            ({"has_credits": True, "unlimited": False, "balance": "12345678901234567890.0123"},
             "积分 12345678901234567890.0123"),
            ({"has_credits": False, "unlimited": False, "balance": "999"}, "积分 0"),
            ({"has_credits": False, "unlimited": True, "balance": None}, "积分 不限"),
            ({"has_credits": True, "unlimited": False, "balance": None}, "积分 可用（数值未提供）"),
            (None, "积分 未提供"),
        ):
            row = {"id": 1, "name": "fixture", "fresh": True, "status": "active", "schedulable": True,
                   "credits_fresh": True, "credits": credits,
                   "five_hour": {"remaining_percent": 75}, "seven_day": {"remaining_percent": 50}}
            text = command.format_usage({"ok": True, "accounts": [row]})
            self.assertIn("**fixture** · " + expected + "\n5h", text)
            self.assertEqual(text.count("积分"), 1)

    def test_credits_failure_does_not_show_cached_balance_or_hide_quota(self):
        text = command.format_usage({"ok": True, "accounts": [{"id": 1, "name": "fixture", "fresh": True,
            "status": "active", "schedulable": True, "credits_fresh": False, "credits_error": "HTTP 503",
            "credits": {"has_credits": True, "balance": "1234"}, "five_hour": {"remaining_percent": 75}}]})
        self.assertIn("积分 未刷新（HTTP 503）", text)
        self.assertNotIn("1234", text)
        self.assertIn("75%", text)


if __name__ == "__main__":
    unittest.main()
