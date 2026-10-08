"""Receipts and cleanup for direct uploads in owner-only terminal mode."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import tempfile


def _read_files(root: Path) -> dict:
    try:
        data = json.loads((root / "uploads.json").read_text())
    except FileNotFoundError:
        return {}
    if data.get("version") != 1 or not isinstance(data.get("files"), dict):
        raise ValueError("invalid terminal upload index")
    return data["files"]


def _write_files(root: Path, files: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root,
                                     prefix=".uploads-", delete=False) as stream:
        path = Path(stream.name)
        try:
            os.chmod(path, 0o600)
            json.dump({"version": 1, "files": files}, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
            path.replace(root / "uploads.json")
        finally:
            path.unlink(missing_ok=True)


def _cache_path(home: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("not an uploaded cache file")
    allowed = (home / "cache/documents", home / "cache/images",
               home / "document_cache", home / "image_cache")
    if not any(path.is_relative_to(directory) for directory in allowed):
        raise ValueError("outside upload caches")
    resolved = path.resolve(strict=True)
    if not any(resolved.is_relative_to(directory.resolve()) for directory in allowed):
        raise ValueError("cache path escaped through a symlink")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError("upload is not a regular file")
    return path


def receipt(event, root: Path) -> str:
    metadata = getattr(event, "metadata", None) or {}
    attachments = metadata.get("qqbot_cached_attachments") or []
    config = json.loads((root / "client.json").read_text())
    home = root.parent
    host_home = Path(config.get("host_data_root", str(home)))
    files, paths, failed = _read_files(root), [], 0
    for item in attachments:
        try:
            path = _cache_path(home, str(item.get("path") or ""))
            host_path = host_home / path.relative_to(home)
            # Both names must refer to the same bind-mounted file, not just exist.
            if not host_path.is_absolute() or not path.samefile(host_path):
                raise ValueError("host cache mapping does not match")
            identity = path.lstat()
        except (OSError, ValueError):
            failed += 1
            continue
        files[str(path)] = {"device": identity.st_dev, "inode": identity.st_ino}
        paths.append(str(host_path))
    if not paths:
        return "接收失败，请重新发送文件。"
    _write_files(root, files)
    text = "\n".join(dict.fromkeys(paths))
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    message = f"接收完成\n{fence}text\n{text}\n{fence}"
    if failed:
        message += f"\n{failed} 个文件接收失败，请重发。"
    return message


def clear(root: Path) -> str:
    files = _read_files(root)
    deleted, remaining = 0, {}
    for value, expected in files.items():
        try:
            path = _cache_path(root.parent, value)
            identity = path.lstat()
            if (identity.st_dev, identity.st_ino) != (expected["device"], expected["inode"]):
                raise ValueError("cached file was replaced")
            path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError):
            remaining[value] = expected
    _write_files(root, remaining)
    if remaining:
        return f"已清理 {deleted} 个上传文件；{len(remaining)} 个未能清理。"
    return f"已清理 {deleted} 个上传文件。" if deleted else "暂无上传文件。"
