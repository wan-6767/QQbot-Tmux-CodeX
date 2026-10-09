"""QQ quota command; only the host quota service holds the admin key."""

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

logger = logging.getLogger("qq_tmuxbot.sub2api")
DEFAULT_USAGE_URL = "http://127.0.0.1:18014/v1/sub2api/usage"


def usage_url():
    """Return the configured loopback endpoint without exposing credentials."""
    value = os.environ.get("SUB2API_USAGE_URL", DEFAULT_USAGE_URL).strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid Sub2API usage URL") from exc
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.username is not None or parsed.password is not None
            or port is None or not 1024 <= port <= 65535
            or parsed.path != "/v1/sub2api/usage" or parsed.query or parsed.fragment):
        raise ValueError("Sub2API usage URL must be a fixed 127.0.0.1 HTTP endpoint")
    return value


def fetch_usage():
    try:
        token_file = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "sub2api-usage/token"
        request = Request(usage_url(), data=b"{}", headers={
            "Authorization": "Bearer " + token_file.read_text().strip(),
            "Content-Type": "application/json"}, method="POST")
        with build_opener(ProxyHandler({})).open(request, timeout=110) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError("quota response too large")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError("invalid quota response")
            return result
    except HTTPError as exc:
        return {"ok": False, "error": "桥接 HTTP " + str(exc.code)}
    except (URLError, OSError, ValueError):
        return {"ok": False, "error": "额度刷新连接失败或超时"}


def local_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M")
    except (ValueError, AttributeError):
        return "未知"


def safe_name(value):
    return " ".join(str(value).replace("`", "'").replace("*", "").replace("#", "").split())[:80]


def quota_line(label, value):
    if not value:
        return label + " 未知"
    percent = max(0, min(100, value["remaining_percent"]))
    filled = round(percent * 8 / 100)
    bar = "[" + "=" * filled + "-" * (8 - filled) + "]"
    reset = " · " + local_time(value["resets_at"]) if value.get("resets_at") else ""
    return f"{label} `{bar}` {percent:g}%{reset}"


def window_label(value, fallback):
    minutes = value.get("window_minutes")
    if type(minutes) is not int or minutes <= 0:
        return fallback
    if minutes % 1440 == 0:
        return str(minutes // 1440) + "d"
    if minutes % 60 == 0:
        return str(minutes // 60) + "h"
    return str(minutes) + "m"


def credits_label(row):
    if not row.get("credits_fresh"):
        error = row.get("credits_error")
        return "积分 未刷新（" + error + "）" if error else "积分 未知"
    value = row.get("credits")
    if not value:
        return "积分 未提供"
    if value.get("unlimited"):
        return "积分 不限"
    if not value.get("has_credits"):
        return "积分 0"
    return "积分 " + (value.get("balance") or "可用（数值未提供）")


def format_usage(data):
    if not data.get("ok"):
        return "## Sub2API 额度\n\n**刷新失败**\n" + data.get("error", "未知错误")
    rows = data.get("accounts", [])
    parts = ["## Sub2API 额度", local_time(data.get("checked_at")) + " 更新 · 剩余额度 · 北京时间"]
    if not rows:
        parts.append("暂无账号。")
    for row in sorted(rows, key=lambda row: row["id"]):
        title = "**" + safe_name(row.get("name", row["id"])) + "** · " + credits_label(row)
        if not row.get("fresh"):
            parts.append(title + " · 未刷新\n" + row.get("error", "未取得新快照")
                         + " · 上次 " + local_time(row.get("updated_at")))
            continue
        exhausted = any(row.get(key) and row[key]["remaining_percent"] == 0 for key in ("five_hour", "seven_day"))
        label = " · 已耗尽" if exhausted else "" if row.get("status") == "active" and row.get("schedulable") else " · 不可调度"
        lines = [title + label]
        for field, default in (("five_hour", "5h"), ("seven_day", "7d")):
            value = row.get(field)
            if value:
                lines.append(quota_line(window_label(value, default), value))
        if len(lines) == 1:
            lines.append("上游未提供窗口额度")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


class UsageCommand:
    def __init__(self, authorized=None):
        self.task = None
        self.seen = set()
        self.authorized = authorized or (lambda event: True)

    async def handle(self, event, adapter):
        if not self.authorized(event):
            return
        if event.message_id in self.seen:
            return
        if self.task and not self.task.done():
            await adapter.send(event.source.chat_id, "额度正在刷新。", reply_to=event.message_id)
            return
        self.seen.add(event.message_id)
        if len(self.seen) > 1024:
            self.seen = {event.message_id}
        await adapter.send(event.source.chat_id, "刷新额度中…", reply_to=event.message_id)
        self.task = asyncio.create_task(self.deliver(event, adapter))

    async def deliver(self, event, adapter):
        try:
            data = await asyncio.to_thread(fetch_usage)
            if not self.authorized(event):
                return
            result = await adapter.send(event.source.chat_id, format_usage(data), reply_to=event.message_id)
            logger.info("Usage command completed: fresh=%s accounts=%s delivered=%s",
                        sum(bool(x.get("fresh")) for x in data.get("accounts", [])),
                        len(data.get("accounts", [])), bool(result.success))
        except Exception as exc:
            logger.warning("Usage command failed (%s)", type(exc).__name__)
            await adapter.send(event.source.chat_id, "## Sub2API 额度\n\n刷新失败，请稍后再试。", reply_to=event.message_id)
