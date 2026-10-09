"""Stable terminal IDs and durable, independently leased multi-pane connections."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading

from .bridge import Relay, RelayError
from . import files

HELP = """## 多终端转发

`/tmux ls` 查看编号、终端名和连接状态。

| 指令 | 用途 |
| --- | --- |
| `/tmux sel 001 ent` | 接入，返回最近100行并持续追加 |
| `/tmux sel 001 文字` | 输入文字并回车 |
| `/tmux sel 001 /model` | 输入终端程序的命令 |
| `/tmux sel 001 key enter` | 回车确认 |
| `/tmux sel 001 key up` | 上方向键 |
| `/tmux sel 001 type 文字` | 只输入，不回车 |
| `/tmux sel 001 send ent` | 原样发送保留词 ent |
| `/tmux sel 001 tail 100` | 最近N行原始快照，重置这一连接的追加进度 |
| `/tmux sel 001 ext` | 仅断开这一连接，任务继续运行 |

目标与操作可以互换：`/tmux tail 100 sel 001`、`/tmux ent sel 001`、`/tmux key up sel 001`。
tail行数1–5000；不足时返回已有内容。tail100也可识别，旧list100已移除。
按键：enter、esc、方向键、tab、space、backspace、delete、insert、home、end、pageup、pagedown、F1–F24、字母/数字/符号。
组合键：ctrl、alt、shift，可用+或-连接，例如ctrl+c、shift+tab、alt+enter、ctrl+shift+left。
ctrl-c 可能中断任务；ent 是接入，不是回车。

编号001–999固定对应实际窗格，不是当前列表排序。窗格关闭后旧编号失效，不会自动指向另一个终端。
每个连接独立追加、重试和恢复；双方30分钟无消息仅断开该连接。
所有输入都要带编号；不再使用旧版 select/exit 或无编号输入。
群里每条指令都需 @本 bot，只有绑定本人可操作，输出全群可见。
私聊 /group bind、/group status、/group unbind 管理群绑定。
文件/图片/语音上传后返回本机绝对路径，不自动输入终端。
`/file dl /绝对路径` 下载本机普通文件（最多100 MiB）；`/file rm` 只清理本bot接收的缓存。
路径仅属于本机，远端需自行scp；群收发仍要求绑定本人的@消息。
`/sub2api usage` 刷新并查看本机账号额度及积分（可选插件）。
不调用模型，不提供Hermes秘书功能。"""


def parse(text):
    value = text.strip()
    if value.lower() in {"/help", "/tmux", "/tmux help"}:
        return "help", "", ""
    if value.lower() == "/tmux ls":
        return "list", "", ""
    guidance = "格式：/tmux sel 001 ent｜文字｜key enter｜tail 100｜ext；也可 /tmux tail 100 sel 001。"
    match = re.fullmatch(r"/tmux\s+sel\s+([0-9]{3})\s+([\s\S]+)", value, re.I)
    if match:
        channel, body = match[1], match[2].strip()
    else:
        match = re.fullmatch(r"/tmux\s+([\s\S]+?)\s+sel\s+([0-9]{3})", value, re.I)
        if not match:
            raise RelayError(guidance)
        body, channel = match[1].strip(), match[2]
        # Only explicit operations can precede the target. Never search arbitrary
        # prose for target tokens or silently switch the destination of a prompt.
        if not re.match(r"(?:ent|ext|key|type|send|tail\d*|list100)(?:\s|$)", body, re.I):
            raise RelayError(guidance)
    if channel == "000":
        raise RelayError(guidance)
    if body.lower() in {"ent", "ext"}:
        return body.lower(), channel, ""
    if re.match(r"list100(?:\s|$)", body, re.I):
        raise RelayError("list100已移除，请用 /tmux sel " + channel + " tail 100；行数可自定。")
    if re.match(r"tail(?:\s|\d|$)", body, re.I):
        tail = re.fullmatch(r"tail\s*([0-9]{1,4})", body, re.I)
        if not tail or not 1 <= int(tail[1]) <= 5000:
            raise RelayError("格式：/tmux sel " + channel + " tail N，N为1–5000的行数。")
        return "tail", channel, str(int(tail[1]))
    operation = re.match(r"(key|type|send)\s+([\s\S]*)", body, re.I)
    if operation:
        if not operation[2]:
            raise RelayError(guidance)
        return operation[1].lower(), channel, operation[2]
    if re.match(r"(?:ent|ext|key|type|send)(?:\s|$)", body, re.I):
        raise RelayError(guidance + " 保留词作为文字请加send。")
    return "send", channel, body


def safe_name(name):
    return re.sub(r"[\x00-\x1f\x7f]", "", name).replace("`", "'")


class PaneLease:
    """Limit the existing single-pane relay's release operation to its own lease."""

    def __init__(self, shared):
        self.shared, self.identity = shared, None

    def acquire(self, pane):
        self.shared.acquire(pane)
        self.identity = pane["identity"]

    def holder(self, pane):
        return self.shared.holder(pane)

    def release_except(self, identity=None):
        if self.identity is not None and self.identity != identity:
            self.shared.release(self.identity)
            self.identity = None


class MultiRelay:
    multiplex = True

    def __init__(self, tmux, root: Path, pane_locks=None, idle_seconds=1800):
        self.tmux, self.root, self.pane_locks = tmux, root, pane_locks
        self.idle_seconds = idle_seconds
        self.lock = threading.RLock()
        self.channels = {}
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = root / "multi.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS terminals (id TEXT PRIMARY KEY, identity TEXT UNIQUE, pane TEXT, source TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, status TEXT, channel TEXT, data TEXT)")
        self.db.execute("UPDATE receipts SET status='unknown' WHERE status='pending'")
        self.db.commit()
        for channel, in self.db.execute("SELECT id FROM terminals WHERE source IS NOT NULL").fetchall():
            self._child(channel)

    def close(self):
        for child in self.channels.values():
            if child.pane_locks:
                child.pane_locks.release_except()
            if child.db:
                child.db.close()
        self.db.close()

    def _row(self, channel):
        row = self.db.execute("SELECT identity,pane,source FROM terminals WHERE id=?", (channel,)).fetchone()
        if row is None:
            raise RelayError("编号不存在，请 /tmux ls 查看。")
        return row[0], json.loads(row[1]), json.loads(row[2]) if row[2] else None

    def _child(self, channel):
        if channel not in self.channels:
            self._row(channel)
            directory = self.root / "channels" / channel
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.channels[channel] = Relay(self.tmux, idle_seconds=self.idle_seconds,
                state_file=directory / "bridge.sqlite3",
                pane_locks=PaneLease(self.pane_locks) if self.pane_locks else None)
        return self.channels[channel]

    def _source(self, channel, source):
        self.db.execute("UPDATE terminals SET source=? WHERE id=?",
                        (json.dumps(source) if source else None, channel))
        self.db.commit()

    def _refresh(self):
        panes = self.tmux.panes()
        known = {identity: channel for channel, identity in self.db.execute("SELECT id,identity FROM terminals")}
        serial = self.db.execute("SELECT COALESCE(MAX(CAST(id AS INTEGER)),0) FROM terminals").fetchone()[0]
        result, seen = [], set()
        for pane in panes:
            identity = pane["identity"]
            if identity in seen:
                continue
            seen.add(identity)
            channel = known.get(identity)
            if channel is None:
                if serial >= 999:
                    raise RelayError("001–999 编号已用尽；请备份后维护编号库，不会复用旧编号。")
                serial += 1
                channel = f"{serial:03d}"
                self.db.execute("INSERT INTO terminals VALUES (?,?,?,NULL)", (channel, identity, json.dumps(pane)))
            else:
                self.db.execute("UPDATE terminals SET pane=? WHERE id=?", (json.dumps(pane), channel))
            result.append((channel, pane))
        self.db.commit()
        return result

    def _decorate(self, channel, result):
        _, pane, source = self._row(channel)
        target = safe_name(result.get("target", pane["target"]))
        label = f"{channel} · {target}"
        result = {**result, "channel": channel, "target": label, "pane_target": target, "source": source}
        if result.get("message"):
            if result.get("reason") == "idle":
                result["message"] = f"[{label}] 双方30分钟无消息，已断开。\n/tmux sel {channel} ent 重连；任务继续运行。"
            elif result.get("exited"):
                result["message"] = f"[{label}] 已断开，任务继续运行。"
            else:
                message = result["message"].replace("/tmux key enter", f"/tmux sel {channel} key enter")
                result["message"] = f"[{label}] {message}"
        return result

    def _expire(self):
        for child in self.channels.values():
            with child.lock:
                child._expire()

    @staticmethod
    def source(value):
        if (not isinstance(value, dict) or value.get("chat_type") not in {"dm", "group"}
                or any(not isinstance(value.get(k), str) or not 0 < len(value[k]) <= 256
                       for k in ("chat_id", "user_id"))):
            raise ValueError("invalid source")
        return {k: value[k] for k in ("chat_id", "user_id", "chat_type")}

    def listing(self):
        self._expire()
        lines = ["## tmux 终端", ""]
        server = None
        for channel, pane in self._refresh():
            if pane.get("server") != server:
                server = pane.get("server")
                if server is not None:
                    lines.extend(["", "### " + safe_name(server)])
            child = self.channels.get(channel)
            status = "已连接" if child and child.selected else "未连接"
            if self.pane_locks and not (child and child.selected):
                holder = self.pane_locks.holder(pane)
                if holder:
                    status = "占用：" + safe_name(holder)
            lines.append(f"`{channel}` · `{safe_name(pane['target'])}` · {status}")
        if len(lines) == 2:
            lines.append("没有运行中的 tmux 窗格。")
        for host, error in getattr(self.tmux, "errors", {}).items():
            lines.extend(["", "### " + safe_name(host) + " · 连接异常", safe_name(error),
                          "原编号保留；本地及其他服务器仍可操作。"])
        lines.extend(["", "`/tmux sel 001 ent` 接入；`/tmux help` 查看指令。"])
        return {"handled": True, "message": "\n".join(lines), "protocol": 2}

    def receipt(self, key):
        with self.lock:
            row = self.db.execute("SELECT status,channel,data FROM receipts WHERE id=?", (key,)).fetchone()
            if row is None:
                return {"status": "missing"}
            status, channel, data = row
            if status != "complete" and channel and self.db.execute("SELECT 1 FROM terminals WHERE id=?", (channel,)).fetchone():
                saved = self._child(channel).receipt(key)
                if saved["status"] == "complete":
                    result = self._decorate(channel, saved["result"])
                    self._finish(key, result)
                    return {"status": "complete", "result": result}
            return {"status": status, "result": json.loads(data) if status == "complete" else None}

    @staticmethod
    def receipt_key(source, message_id):
        return hashlib.sha256((json.dumps(source, sort_keys=True) + "\0" + message_id).encode()).hexdigest()

    def _finish(self, key, result):
        if key:
            self.db.execute("UPDATE receipts SET status='complete',data=? WHERE id=?", (json.dumps(result), key))
            self.db.commit()

    def route(self, text, message_id, source):
        source = self.source(source)
        if not isinstance(text, str) or not isinstance(message_id, str) or len(message_id) > 256:
            raise ValueError("invalid message")
        with self.lock:
            key = self.receipt_key(source, message_id) if message_id else ""
            if key:
                saved = self.receipt(key)
                if saved["status"] == "complete":
                    return {**saved["result"], "duplicate": True}
                if saved["status"] != "missing":
                    return {"handled": True, "uncertain": True, "message": "上次输入状态不明，未重复发送。请用 /tmux sel 编号 tail 100 核对。"}
            try:
                verb, channel, body = parse(text)
                if key:
                    self.db.execute("INSERT INTO receipts VALUES (?,'pending',?,NULL)", (key, channel))
                    self.db.commit()
                if verb == "help":
                    result = {"handled": True, "message": HELP}
                elif verb == "list":
                    result = self.listing()
                else:
                    identity, pane, bound = self._row(channel)
                    if bound and bound != source:
                        raise RelayError(f"{channel} 已连接另一个聊天，请在原聊天 /tmux sel {channel} ext。")
                    child = self._child(channel)
                    child._expire()
                    if verb == "ent":
                        current = next((p for p in self.tmux.panes() if p["identity"] == identity), None)
                        if current is None:
                            if pane.get("server") in getattr(self.tmux, "errors", {}):
                                raise RelayError("远程服务器暂不可达，原编号保留；请稍后重试。")
                            raise RelayError("原窗格已关闭或替换，旧编号失效。请 /tmux ls 查看新编号。")
                        self._source(channel, source)
                        result = child.route("#tmux select " + current["pane_id"], key)
                        if not result.get("active"):
                            self._source(channel, None)
                    elif verb == "ext":
                        result = child.route("#tmux exit", key)
                        self._source(channel, None)
                    else:
                        if not child.selected or bound != source:
                            raise RelayError(f"尚未连接，请先 /tmux sel {channel} ent。")
                        command = "#tmux " + {"tail": "tail", "send": "send", "type": "type", "key": "key"}[verb]
                        if body:
                            command += " " + body
                        result = child.route(command, key)
                    result = self._decorate(channel, result)
                    if not result.get("active") and not result.get("exited"):
                        result["error"] = True
                result["protocol"] = 2
            except RelayError as exc:
                result = {"handled": True, "message": str(exc), "protocol": 2, "error": True}
            self._finish(key, result)
            return result

    def state(self):
        with self.lock:
            self._expire()
            connections = []
            for channel, in self.db.execute("SELECT id FROM terminals WHERE source IS NOT NULL").fetchall():
                child = self._child(channel)
                if child.selected or child.disconnected:
                    connections.append(self._decorate(channel, child._response("") if child.selected else child.state()))
            return {"protocol": 2, "channels": connections}

    def screen(self, channel, epoch):
        with self.lock:
            return self._decorate(channel, self._child(channel).screen(epoch))

    def activity(self, channel, token, event_id):
        with self.lock:
            return self._child(channel).activity(token, event_id)

    def acknowledge_close(self, channel, epoch):
        with self.lock:
            child = self._child(channel)
            if not child.selected and child.epoch == epoch:
                self._source(channel, None)
                return {"acknowledged": True}
            return {"acknowledged": False}

    def api(self, path, body):
        try:
            return self._api(path, body)
        except RelayError as exc:
            return {"handled": True, "active": False, "error": True, "message": str(exc), "protocol": 2}

    def _api(self, path, body):
        if path == "/healthz":
            return {"ok": True, "protocol": 2}
        if path == "/v2/route":
            return self.route(body["text"], body.get("message_id", ""), body["source"])
        if path == "/v2/state":
            return self.state()
        if path == "/v2/receipt":
            return self.receipt(self.receipt_key(self.source(body["source"]), body["message_id"]))
        if path.startswith("/v2/files/"):
            return files.api(self.root.parent, path.removeprefix("/v2/files/"), body)
        channel = body.get("channel", "")
        if not isinstance(channel, str) or not re.fullmatch(r"[0-9]{3}", channel) or channel == "000":
            raise ValueError("invalid channel")
        if path == "/v2/screen":
            return self.screen(channel, int(body["epoch"]))
        if path == "/v2/activity":
            token, event_id = body["selection_token"], body.get("event_id", "")
            if not isinstance(token, str) or not token or len(token) > 256 or not isinstance(event_id, str) or len(event_id) > 256:
                raise ValueError("invalid activity")
            return self.activity(channel, token, event_id)
        if path == "/v2/ack-close":
            return self.acknowledge_close(channel, int(body["epoch"]))
        raise ValueError("unsupported multi-terminal endpoint")
