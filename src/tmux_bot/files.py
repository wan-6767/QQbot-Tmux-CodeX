"""Authenticated host-side cache validation and bounded download snapshots."""

import os
from pathlib import Path
import re
import secrets
import stat

from .bridge import RelayError
from .terminal_files import _cache_path

MAX_FILE_BYTES = 100 * 1024 * 1024


def api(home: Path, action: str, body: dict) -> dict:
    try:
        if action == "validate":
            relative = body["relative"]
            if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
                raise ValueError("invalid cache path")
            path = _cache_path(home, str(home / relative))
            identity = path.stat()
            if (identity.st_dev, identity.st_ino) != (body["device"], body["inode"]):
                raise ValueError("cache mount mismatch")
            return {"path": str(path)}
        spool = home / "cache/outgoing-files"
        spool.mkdir(parents=True, exist_ok=True, mode=0o700)
        if spool.is_symlink() or spool.stat().st_uid != os.getuid() or stat.S_IMODE(spool.stat().st_mode) != 0o700:
            raise ValueError("unsafe outgoing directory")
        if action == "release":
            token = body["token"]
            if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token):
                raise ValueError("invalid download token")
            (spool / token).unlink(missing_ok=True)
            return {"released": True}
        if action != "prepare":
            raise ValueError("unsupported file operation")
        value = body["path"]
        if not isinstance(value, str) or not value.startswith("/") or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise ValueError("download requires an absolute file path")
        token, destination = secrets.token_hex(16), None
        fd = os.open(value, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise ValueError("download requires a regular file of at most 100 MiB")
            destination = spool / token
            try:
                output_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(output_fd, "wb") as output:
                    remaining = MAX_FILE_BYTES
                    while chunk := source.read(min(1024 * 1024, remaining + 1)):
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise ValueError("file grew beyond the download limit")
                        output.write(chunk)
                after = os.fstat(source.fileno())
                if (after.st_size, after.st_mtime_ns) != (info.st_size, info.st_mtime_ns):
                    raise ValueError("file changed during snapshot; retry when stable")
            except Exception:
                destination.unlink(missing_ok=True)
                raise
        return {"token": token, "relative": str(destination.relative_to(home)), "name": Path(value).name, "size": info.st_size}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RelayError("文件操作失败：请检查绝对路径、普通文件权限和100 MiB上限；缓存映射或文件变化时会拒绝操作。") from exc
