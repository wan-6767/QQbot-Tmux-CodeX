"""Regression coverage for the independent Hermes quota reader."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from tmux_bot.sub2api.reader import UsageError, UsageReader, account_window, credits, window


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "config.json"
        self.config.write_text(json.dumps({"base_url": "http://127.0.0.1:8080",
                                         "admin_key_file": str(Path(self.temp.name) / "key")}))

    def reader(self, *, fresh=True, errors=None, failure=None, credit_data=None,
               credit_failure=None, credit_fresh=True):
        reader = UsageReader(self.config)
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        new = datetime.now(timezone.utc).isoformat() if fresh else old
        def account(stamp):
            return {"id": 1, "name": "fixture", "platform": "openai", "type": "oauth",
                    "status": "active", "schedulable": True,
                    "extra": {"codex_usage_updated_at": stamp, "codex_5h_window_minutes": 300,
                              "codex_7d_window_minutes": 10080, "private_secret": "must-not-leak"},
                    "credentials": {"access_token": "must-not-leak"}}
        calls = iter(([account(old)], [account(new)]))
        reader.accounts = lambda: next(calls)
        reader.calls = []
        def api(method, path, body, timeout):
            reader.calls.append((method, path, body))
            if path == "/openai/accounts/1/quota/refresh":
                if credit_failure:
                    raise UsageError(credit_failure)
                return {"fetched_at": datetime.now(timezone.utc).timestamp() if credit_fresh else 1,
                        "credits": credit_data if credit_data is not None else {
                            "has_credits": True, "unlimited": False, "balance": "1200.50",
                            "private_secret": "must-not-leak"}}
            if failure:
                raise UsageError(failure)
            return {"usage": {"1": {"updated_at": new, "five_hour": {"utilization": 25},
                                      "seven_day": {"utilization": 100}}}, "errors": errors or {}}
        reader.api = api
        return reader

    def test_native_force_refresh_and_credential_whitelist(self):
        reader = self.reader()
        result = reader.refresh()
        self.assertCountEqual(reader.calls, [
            ("POST", "/accounts/usage/batch", {"account_ids": [1], "force": True}),
            ("POST", "/openai/accounts/1/quota/refresh", {}),
        ])
        row = result["accounts"][0]
        self.assertTrue(row["fresh"])
        self.assertEqual(row["five_hour"]["remaining_percent"], 75)
        self.assertEqual(row["seven_day"]["remaining_percent"], 0)
        self.assertTrue(row["credits_fresh"])
        self.assertEqual(row["credits"]["balance"], "1200.50")
        self.assertNotIn("must-not-leak", json.dumps(result))
        self.assertNotIn("credentials", row)

    def test_credits_refresh_failure_does_not_hide_fresh_windows(self):
        row = self.reader(credit_failure="额度接口 HTTP 503").refresh()["accounts"][0]
        self.assertTrue(row["fresh"])
        self.assertFalse(row["credits_fresh"])
        self.assertIn("503", row["credits_error"])
        self.assertNotIn("credits", row)

    def test_old_credits_are_not_shown_as_current(self):
        row = self.reader(credit_fresh=False).refresh()["accounts"][0]
        self.assertFalse(row["credits_fresh"])
        self.assertNotIn("credits", row)
        self.assertIn("新快照", row["credits_error"])

    def test_absent_credits_are_unknown_not_zero(self):
        row = self.reader(credit_data={}).refresh()["accounts"][0]
        self.assertTrue(row["credits_fresh"])
        self.assertIsNone(row["credits"])

    def test_credit_decimal_precision_and_field_whitelist(self):
        value = {"has_credits": True, "unlimited": False,
                 "balance": "12345678901234567890.0123", "token": "must-not-leak"}
        parsed = credits(value)
        self.assertEqual(parsed["balance"], value["balance"])
        self.assertNotIn("token", parsed)
        for balance in ("NaN", "Infinity", "-1", "secret=must-not-leak", 42, True):
            self.assertIsNone(credits({**value, "balance": balance})["balance"])
        for value in (None, {}, {"has_credits": 1, "unlimited": False}):
            self.assertIsNone(credits(value))

    def test_credit_deadline_skips_queued_requests(self):
        reader = self.reader()
        result = reader.credit_snapshot(1, datetime.now(timezone.utc), time.monotonic() - 1)
        self.assertFalse(result["credits_fresh"])
        self.assertFalse(reader.calls)

    def test_http_success_with_old_snapshot_is_not_fresh(self):
        row = self.reader(fresh=False).refresh()["accounts"][0]
        self.assertFalse(row["fresh"])
        self.assertEqual(row["error"], "未确认新快照")

    def test_real_status_errors_are_preserved_but_bodies_are_not(self):
        row = self.reader(errors={"1": "secret=must-not-leak https://private/ HTTP 429"}).refresh()["accounts"][0]
        self.assertFalse(row["fresh"])
        self.assertEqual(row["error"], "HTTP 429")
        row = self.reader(failure="额度接口 HTTP 503").refresh()["accounts"][0]
        self.assertFalse(row["fresh"])
        self.assertIn("503", row["error"])

    def test_concurrent_refresh_rejected_without_upstream_request(self):
        reader = self.reader()
        reader.lock.acquire()
        try:
            self.assertFalse(reader.refresh()["ok"])
            self.assertFalse(reader.calls)
        finally:
            reader.lock.release()

    def test_missing_quota_is_unknown_and_invalid_utilization_rejected(self):
        for value in (None, {}, {"utilization": True}, {"utilization": float("nan")}, {"utilization": -1}):
            self.assertIsNone(window(value))
        self.assertEqual(window({"utilization": 101})["remaining_percent"], 0)

    def test_synthetic_zero_minute_quota_is_not_a_full_window(self):
        result = {"five_hour": {"utilization": 0}, "seven_day": {"utilization": 0}}
        self.assertIsNone(account_window({"codex_5h_window_minutes": 0}, result, "five_hour", "codex_5h"))
        self.assertIsNone(account_window({}, result, "five_hour", "codex_5h"))
        monthly = account_window({"codex_7d_window_minutes": 43200}, result, "seven_day", "codex_7d")
        self.assertEqual(monthly["window_minutes"], 43200)

    def test_public_origin_and_credentials_in_url_rejected(self):
        for url in ("https://example.org", "http://8.8.8.8", "http://u:p@127.0.0.1:8080", "http://127.0.0.1/api"):
            self.config.write_text(json.dumps({"base_url": url, "admin_key_file": "unused"}))
            with self.assertRaises(UsageError):
                UsageReader(self.config)


if __name__ == "__main__":
    unittest.main()
