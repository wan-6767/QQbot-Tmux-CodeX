"""Private QQ command-panel registration and exact terminal command aliases."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

PANEL_REMARK = "qq-tmuxbot-shortcuts-v1"
COMMANDS = (
    ("/tmux ls", "查看本地和远端终端编号", "#tmux ls"),
    ("/tmux sel", "指定编号，再填ent、ext或消息", "#tmux sel"),
    ("/tmux list100", "查看最近100行完整上下文", "#tmux list100"),
    ("/tmux exit", "退出终端选择模式", "#tmux exit"),
    ("/tmux help", "查看终端操作帮助", "#tmux help"),
    ("/help", "查看终端机器人帮助", "/help"),
)
ALIASES = {name: target for name, _description, target in COMMANDS}
PANEL_COMMANDS = ("/tmux ls", "/tmux sel", "/tmux help")
LEGACY_ALIASES = {"/tmux": "#tmux ls", "/list100": "#tmux list100", "/exit": "#tmux exit"}


def terminal_command(text: str) -> str:
    """Translate the public /tmux namespace, leaving its argument body untouched."""
    value = text.strip()
    head, _, rest = value.partition(" ")
    if head.lower() == "/tmux" and rest.strip():
        verb, separator, argument = rest.lstrip().partition(" ")
        return "#tmux " + verb.lower() + (separator + argument if separator else "")
    return ALIASES.get(value.lower(), LEGACY_ALIASES.get(value.lower(), text))


def panel_payload(owner: str, commands=None) -> dict:
    if not owner or not owner.strip():
        raise ValueError("Private owner is missing")
    descriptions = {name: description for name, description, _target in COMMANDS}
    selected = tuple(PANEL_COMMANDS if commands is None else commands)
    if not selected or len(set(selected)) != len(selected) or any(name not in descriptions for name in selected):
        raise ValueError("Invalid private command selection")
    return {"scope": "c2c", "target_type": "specific", "user_openids": [owner],
            "panel": {"remark": PANEL_REMARK, "items": [
                {"type": "command", "name": name.removeprefix("/"), "desc": descriptions[name], "only_admin": False}
                for name in selected
            ]}}


class PanelAPIError(RuntimeError):
    def __init__(self, status: int, code=None):
        self.status, self.code = status, code
        super().__init__(f"QQ panel API HTTP {status}")


def write_private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def list_panels(api) -> list[dict]:
    records, cursor, seen = [], "", set()
    for _page in range(20):
        params = {"scope": "c2c", "limit": 50}
        if cursor:
            params["cursor"] = cursor
        data = await api("GET", "/v2/panels", params=params)
        # QQ omits records for a successfully completed empty listing.
        page = data.get("records", [] if data.get("is_end") is True else None)
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise ValueError("Invalid QQ panel list")
        records.extend(page)
        cursor = data.get("next_cursor") or ""
        if data.get("is_end") is True or not cursor:
            return records
        if not isinstance(cursor, str) or cursor in seen:
            raise ValueError("Invalid QQ panel cursor")
        seen.add(cursor)
    raise ValueError("QQ panel pagination exceeded its limit")


def verify_scope(detail: dict, owner: str) -> None:
    if (detail.get("scope") != "c2c" or detail.get("target_type") != "specific"
            or detail.get("user_openids") != [owner] or detail.get("group_openids")
            or detail.get("panel", {}).get("remark") != PANEL_REMARK):
        raise ValueError("Managed panel does not match the sole private owner")


def matches_panel(actual: dict, expected: dict) -> bool:
    if actual.get("remark") != expected["remark"]:
        return False
    items = actual.get("items")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        return False
    normalized = [{key: item.get(key, False if key == "only_admin" else None)
                   for key in ("type", "name", "desc", "only_admin")} for item in items]
    return normalized == expected["items"]


def selected_commands(state_dir: Path, owner: str, explicit=None):
    """Retain the last verified app-specific selection unless explicitly changed."""
    if explicit is not None:
        panel_payload(owner, explicit)
        return tuple(explicit)
    try:
        state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or not isinstance(state.get("payload"), dict):
        raise ValueError("Invalid saved command panel")
    payload = state["payload"]
    verify_scope(payload, owner)
    items = payload["panel"].get("items")
    if (not isinstance(items, list) or any(not isinstance(item, dict)
            or item.get("type") != "command" or not isinstance(item.get("name"), str) for item in items)):
        raise ValueError("Invalid saved command selection")
    selected = tuple("/" + item["name"] for item in items)
    panel_payload(owner, selected)
    return selected


async def sync_panel(api, owner: str, state_dir: Path, *, apply: bool = False, remove: bool = False, commands=None) -> dict:
    records = await list_panels(api)
    managed = [item for item in records if item.get("panel", {}).get("remark") == PANEL_REMARK]
    if len(managed) > 1:
        raise ValueError("Multiple managed panels; refusing an ambiguous update")
    payload = panel_payload(owner, commands)
    detail, panel_id, path = None, None, None
    if managed:
        panel_id = managed[0].get("panel_id")
        if not isinstance(panel_id, str) or not panel_id:
            raise ValueError("Missing QQ panel identity")
        path = "/v2/panels/" + quote(panel_id, safe="")
        detail = await api("GET", path)
        verify_scope(detail, owner)
    action = ("delete" if detail else "absent") if remove else (
        "create" if detail is None else "unchanged" if matches_panel(detail["panel"], payload["panel"]) else "update")
    report = {"action": action, "commands": ["/" + item["name"] for item in payload["panel"]["items"]], "private_owner_only": True,
              "foreign_panels_untouched": len(records) - len(managed), "applied": False}
    if not apply and not remove:
        return report
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    if action not in {"unchanged", "absent"}:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        write_private_json(state_dir / "backups" / f"before-{stamp}.json", {"records": records, "detail": detail})
        if action == "delete":
            await api("DELETE", path)
            if any(item.get("panel_id") == panel_id for item in await list_panels(api)):
                raise ValueError("Removed QQ panel is still present")
        elif action == "create":
            created = await api("POST", "/v2/panels", body=payload)
            panel_id = created.get("panel_id")
            if not isinstance(panel_id, str) or not panel_id:
                raise ValueError("QQ did not return the created panel identity")
            path = "/v2/panels/" + quote(panel_id, safe="")
        else:
            await api("PUT", path, body={"panel": payload["panel"]})
    if not remove:
        detail = await api("GET", path)
        verify_scope(detail, owner)
        if not matches_panel(detail["panel"], payload["panel"]):
            raise ValueError("QQ panel read-back differs from requested commands")
        write_private_json(state_dir / "state.json", {"panel_id": panel_id, "payload": payload,
                           "verified_at": datetime.now(timezone.utc).isoformat()})
    else:
        (state_dir / "state.json").unlink(missing_ok=True)
    return {**report, "applied": True, "read_back_verified": True}


async def run(options) -> dict:
    import httpx
    from gateway.config import PlatformConfig
    from gateway.platforms.qqbot.adapter import QQAdapter
    from gateway.platforms.qqbot.constants import API_BASE
    from .owner import owner_id

    owner = owner_id()
    if not owner:
        raise ValueError("Bind the private owner before registering its panel")
    qq = PlatformConfig(enabled=True, extra={
        "app_id": os.environ["QQ_APP_ID"], "client_secret": os.environ["QQ_CLIENT_SECRET"],
        "dm_policy": "allowlist", "allow_from": [owner], "group_policy": "allowlist"})
    adapter = QQAdapter(qq)
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as http:
        adapter._http_client = http

        async def api(method, path, *, body=None, params=None):
            response = await http.request(method, API_BASE + path, headers=await adapter._auth_headers(),
                                          json=body, params=params)
            try:
                data = response.json()
            except ValueError:
                raise PanelAPIError(response.status_code) from None
            if not isinstance(data, dict) or response.is_error or data.get("code") not in {None, 0}:
                code = data.get("code") if isinstance(data, dict) else None
                raise PanelAPIError(response.status_code, code if isinstance(code, int) else None)
            return data

        state_dir = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "qq-command-panel"
        if options.apply or options.remove:
            state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (state_dir / "sync.lock").open("a") as lock:
                os.chmod(lock.name, 0o600)
                fcntl.flock(lock, fcntl.LOCK_EX)
                return await sync_panel(api, owner, state_dir, apply=options.apply, remove=options.remove,
                                        commands=selected_commands(state_dir, owner, getattr(options, "commands", None)))
        return await sync_panel(api, owner, state_dir,
                                commands=selected_commands(state_dir, owner, getattr(options, "commands", None)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect or sync the owner-only QQ terminal command panel.")
    mutation = parser.add_mutually_exclusive_group()
    mutation.add_argument("--apply", action="store_true")
    mutation.add_argument("--remove", action="store_true")
    parser.add_argument("--commands", nargs="+", choices=PANEL_COMMANDS)
    options = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(options)), ensure_ascii=False))
    except Exception as exc:
        report = {"error_type": type(exc).__name__}
        if isinstance(exc, PanelAPIError):
            report.update(http_status=exc.status, api_code=exc.code)
        print(json.dumps(report), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
