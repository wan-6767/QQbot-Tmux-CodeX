"""One-time private pairing and persistent application-scoped QQ ownership."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import tempfile
import time


def home():
    return Path(os.environ.get("HERMES_HOME", "/opt/data"))


def write_private(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(value, file)
            file.flush()
            os.fsync(file.fileno())
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def owner_id():
    try:
        data = json.loads((home() / "owner.json").read_text())
    except FileNotFoundError:
        return ""
    value = data.get("openid")
    if not isinstance(value, str) or not value or value == "*":
        raise ValueError("Invalid terminal-bot owner")
    return value


def prepare_pairing():
    if owner_id():
        return
    path = home() / "pairing.json"
    if path.exists() and json.loads(path.read_text()).get("expires_at", 0) > time.time():
        return
    code = secrets.token_hex(12)
    write_private(path, {"digest": hashlib.sha256(code.encode()).hexdigest(),
                         "expires_at": time.time() + 86400})
    write_private(home() / "pairing-instruction.json", {"command": "/bind " + code})


def bind(user, text):
    if owner_id() or not user or user == "*":
        return False
    prefix = "/bind "
    if not text.startswith(prefix):
        return False
    try:
        data = json.loads((home() / "pairing.json").read_text())
    except FileNotFoundError:
        return False
    digest = hashlib.sha256(text[len(prefix):].strip().encode()).hexdigest()
    if data.get("expires_at", 0) <= time.time() or not hmac.compare_digest(data.get("digest", ""), digest):
        return False
    write_private(home() / "owner.json", {"openid": user, "bound_at": time.time()})
    (home() / "pairing.json").unlink(missing_ok=True)
    (home() / "pairing-instruction.json").unlink(missing_ok=True)
    return True


def group_binding():
    try:
        data = json.loads((home() / "group.json").read_text())
    except FileNotFoundError:
        return {}
    if (not owner_id() or data.get("owner_openid") != owner_id()
            or any(not isinstance(data.get(key), str) or data[key] in {"", "*"}
                   for key in ("group_openid", "member_openid"))):
        return {}
    return data


def group_allowed(group, member):
    data = group_binding()
    return bool(data and group == data["group_openid"] and member == data["member_openid"])


def prepare_group_pairing():
    if not owner_id():
        raise ValueError("Private owner must bind first")
    code = secrets.token_hex(12)
    write_private(home() / "group-pairing.json", {
        "digest": hashlib.sha256(code.encode()).hexdigest(), "expires_at": time.time() + 600,
        "owner_openid": owner_id(),
    })
    return "/group bind " + code


def bind_group(group, member, text):
    prefix = "/group bind "
    if (not owner_id() or not group or not member or "*" in {group, member}
            or not text.startswith(prefix)):
        return False
    try:
        data = json.loads((home() / "group-pairing.json").read_text())
    except FileNotFoundError:
        return False
    digest = hashlib.sha256(text[len(prefix):].strip().encode()).hexdigest()
    if (data.get("owner_openid") != owner_id() or data.get("expires_at", 0) <= time.time()
            or not hmac.compare_digest(data.get("digest", ""), digest)):
        return False
    write_private(home() / "group.json", {
        "owner_openid": owner_id(), "group_openid": group, "member_openid": member, "bound_at": time.time(),
    })
    (home() / "group-pairing.json").unlink(missing_ok=True)
    return True


def unbind_group():
    (home() / "group.json").unlink(missing_ok=True)
    (home() / "group-pairing.json").unlink(missing_ok=True)
