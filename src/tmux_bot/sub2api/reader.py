"""Host-only quota adapter, exposing fresh fields without account credentials."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import ipaddress
import json
import math
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class UsageError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise UsageError("额度接口拒绝重定向")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def window(value):
    if not isinstance(value, dict):
        return None
    used = value.get("utilization")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
        return None
    reset = timestamp(value.get("resets_at"))
    return {"remaining_percent": round(max(0, 100 - used), 2),
            "resets_at": reset.isoformat() if reset else None}


def account_window(extra, result, key, prefix):
    minutes = extra.get(prefix + "_window_minutes")
    # Sub2API supplies a synthetic 0%-used window when the upstream has no such quota.
    if type(minutes) is not int or minutes <= 0:
        return None
    value = window(result.get(key))
    return {**value, "window_minutes": minutes} if value else None


def failure_label(value):
    # Do not relay upstream bodies, URLs, tokens or email addresses.
    match = re.search(r"\b(429|5\d\d|401|403)\b", str(value))
    return "HTTP " + match[1] if match else "上游额度刷新失败"


def credits(value):
    if (not isinstance(value, dict) or type(value.get("has_credits")) is not bool
            or type(value.get("unlimited")) is not bool):
        return None
    balance = value.get("balance")
    # Preserve the upstream decimal string, not a rounded floating-point value.
    if (not isinstance(balance, str) or len(balance) > 128
            or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", balance.strip())):
        balance = None
    return {"has_credits": value["has_credits"], "unlimited": value["unlimited"],
            "balance": balance.strip() if balance is not None else None}


class UsageReader:
    def __init__(self, config):
        data = json.loads(Path(config).read_text())
        self.base_url = data["base_url"].rstrip("/")
        parsed = urlsplit(self.base_url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError as exc:
            raise UsageError("额度接口必须是本机私有地址") from exc
        if (parsed.scheme not in {"http", "https"} or not address.is_private
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            raise UsageError("额度接口必须是本机私有地址")
        self.key_path = Path(data["admin_key_file"])
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.lock = threading.Lock()

    def api(self, method, path, body=None, timeout=15):
        key = self.key_path.read_text().strip()
        if not key:
            raise UsageError("额度接口鉴权未配置")
        request = Request(self.base_url + "/api/v1/admin" + path,
                          data=json.dumps(body).encode() if body is not None else None,
                          headers={"x-api-key": key, "Content-Type": "application/json"}, method=method)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise UsageError("额度接口返回过大")
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("code") not in {None, 0}:
                raise UsageError("额度接口返回异常")
            return value["data"]
        except HTTPError as exc:
            raise UsageError("额度接口 HTTP " + str(exc.code)) from None
        except (URLError, TimeoutError, OSError):
            raise UsageError("额度接口连接失败或超时") from None
        except (ValueError, KeyError, TypeError):
            raise UsageError("额度接口返回无效数据") from None

    def accounts(self):
        accounts = []
        for page in range(1, 11):
            data = self.api("GET", f"/accounts?page={page}&page_size=100")
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
                raise UsageError("账号列表格式错误")
            accounts.extend(items)
            if len(items) < 100:
                return accounts
        raise UsageError("账号数量超过安全查询上限")

    def refresh(self):
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "error": "额度正在刷新，请稍后再查"}
        try:
            return self._refresh()
        except UsageError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            self.lock.release()

    def credit_snapshot(self, account_id, start, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"credits_fresh": False, "credits_error": "积分刷新超时"}
        try:
            result = self.api("POST", f"/openai/accounts/{account_id}/quota/refresh", {},
                              timeout=min(15, remaining))
            fetched = result.get("fetched_at") if isinstance(result, dict) else None
            if (type(fetched) not in (int, float) or not math.isfinite(fetched)
                    or not start.timestamp() - 2 <= fetched <= datetime.now(timezone.utc).timestamp() + 5):
                raise UsageError("积分未确认新快照")
            return {"credits_fresh": True, "credits": credits(result.get("credits")),
                    "credits_updated_at": datetime.fromtimestamp(fetched, timezone.utc).isoformat()}
        except UsageError as exc:
            return {"credits_fresh": False, "credits_error": str(exc)}

    def _refresh(self):
        before = {a["id"]: a for a in self.accounts()}
        ids = list(before)
        start = datetime.now(timezone.utc)
        usage, errors = {}, {}
        refresh_error = None
        # Refresh credits alongside quota windows, with bounded concurrency and time.
        deadline = time.monotonic() + 25
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {account_id: pool.submit(self.credit_snapshot, account_id, start, deadline)
                       for account_id, account in before.items()
                       if account.get("platform") == "openai" and account.get("type") == "oauth"}
            if ids:
                try:
                    data = self.api("POST", "/accounts/usage/batch", {"account_ids": ids, "force": True}, timeout=65)
                    usage, errors = data.get("usage", {}), data.get("errors", {})
                    if not isinstance(usage, dict) or not isinstance(errors, dict):
                        raise UsageError("额度批量刷新返回无效数据")
                except UsageError as exc:
                    refresh_error = str(exc)
            credit_results = {account_id: future.result() for account_id, future in pending.items()}
        after = {a["id"]: a for a in self.accounts()}
        rows = []
        for account_id, old in before.items():
            current = after.get(account_id)
            if current is None:
                rows.append({"id": account_id, "name": str(old.get("name", account_id)),
                             "fresh": False, "error": "账号已移除"})
                continue
            extra = current.get("extra") or {}
            old_extra = old.get("extra") or {}
            updated = timestamp(extra.get("codex_usage_updated_at"))
            previous = timestamp(old_extra.get("codex_usage_updated_at"))
            # UsageInfo.updated_at is not evidence: Sub2API stamps it even on failed probes.
            supported = current.get("platform") == "openai" and current.get("type") == "oauth"
            result = usage.get(str(account_id)) or {}
            error = refresh_error
            if str(account_id) in errors:
                error = error or failure_label(errors[str(account_id)])
            fresh = bool(not error and supported and updated and updated.timestamp() >= start.timestamp() - 2
                         and (previous is None or updated > previous))
            rows.append({"id": account_id, "name": str(current.get("name", account_id)),
                         "platform": current.get("platform"), "status": current.get("status"),
                         "schedulable": current.get("schedulable") is True,
                         "fresh": fresh, "updated_at": updated.isoformat() if updated else None,
                         **credit_results.get(account_id, {"credits_fresh": False, "credits_error": "暂不支持"}),
                         "five_hour": account_window(extra, result, "five_hour", "codex_5h"),
                         "seven_day": account_window(extra, result, "seven_day", "codex_7d"),
                         "error": error or (None if fresh else "未确认新快照" if supported else "暂不支持此类账号额度核验")})
        return {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat(), "accounts": rows}
