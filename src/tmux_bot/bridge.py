"""Authenticated loopback bridge for the owner's existing tmux server."""

from __future__ import annotations

import argparse
from collections import OrderedDict
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time


HELP = """## 终端直连

### 窗格与会话

- `/tmux ls` · 列出全部窗格
- `/tmux select 1` · 进入上次列表中的第 1 个窗格
- `/tmux select %4` · 按稳定窗格 ID 进入
- `/tmux select 会话:窗口.窗格` · 按完整终端位置进入
- `/tmux exit` · 返回 Hermes，终端任务继续运行
- `/tmux reconnect` · 重连上次因闲置断开的窗格

### 文字输入

进入后，**普通消息原样输入终端并回车**。`/model` 等斜杠命令直接交给终端里的程序；`/help` 和本页列出的 `/tmux` 快捷命令由机器人处理。

- `/tmux type 文字` · 只填入，不提交；可补充文字或用退格修改
- `/tmux send 文字` · 输入文字并回车；例如 `/tmux send /help` 将保留命令原样交给终端
- 多行文字支持粘贴；图片、语音不作为终端输入
- 不接收文件，不自动把附件路径输入终端

### 全部按键

格式为 `/tmux key 按键名`，**一次发送一个按键**：

- `enter` · 回车，提交或确认
- `esc` · Esc，取消或返回
- `up` / `down` / `left` / `right` · 四个方向键，移动选项或光标
- `tab` · Tab，补全或切换焦点
- `space` · 空格，输入空格或勾选选项
- `backspace` · 退格，删除光标前的字符
- `delete` · Delete，删除光标后的字符
- `home` / `end` · Home / End，移到行首或行尾
- `pgup` / `pgdn` · PageUp / PageDown，发送翻页键，实际行为取决于程序
- `ctrl-c` · Ctrl+C，中断当前操作
- `ctrl-d` · Ctrl+D，结束输入；部分程序会因此退出

### 菜单操作示例

先发 `/model` 打开模型菜单，再发 `/tmux key up` 或 `/tmux key down` 移动，最后 `/tmux key enter` 确认；`/tmux key esc` 取消。权限菜单的命令以终端程序实际支持为准。

### 屏幕与回显

- `/tmux list100` · 完整查看最近 100 行，并重置自动追加进度
- `/tmux screen` · 查看最近 40 行快照
- `/tmux screen 50` · 指定快照行数，范围 10–100
- `/tmux screen100` · `/tmux list100` 的兼容别名
- `/tmux next` · 确认继续自动接收；正常情况下无需手动调用
- 每次选择进入窗格立即返回最近约 100 行完整上下文，不做增量；历史不足 100 行则全部返回
- 进入后只自动追加自然语言说明和回答，完整一段一个 text 文本框
- 识别到菜单时完整返回当前可见对话框，保留选项、光标、警告和操作提示
- Ran、Explored、Edited 等工具操作记录及其子项默认隐藏，使用 `/tmux list100` 查看
- 未完成的末段暂存；完整段落、代码块、首次上下文和菜单不拆体
- 选中后持续自动发送新增文本体，无需点击继续；暂时发送失败会保留进度并重试
- 过滤输入框、时钟和状态栏重绘；按键成功直接回显结果，不另发“已输入”

### 帮助与边界

- `/tmux help` · 查看全部终端指令及教程；群里每次都需要 @对应机器人
- `/help` · Hermes 全局帮助，不退出当前终端连接；需向终端输入该命令时发送 `/tmux send /help`
- 双方连续 30 分钟没有消息才断开；bot 成功发送新内容也重新计时，用 `/tmux reconnect` 重连
- 重启自动恢复原窗格和发送进度；提交超时会核对记录，不盲目重复输入
- `/tmux` 指令由机器人处理，其余 `/` 命令交给终端；消息下方不附加快捷按钮
- 目前不支持重复次数、组合按键、创建或关闭 tmux 窗格的专用命令

> 直连时机器人只传话，不调用 Hermes 模型。输入按所选终端的现有权限执行；退出直连不会停止其任务。"""
KEYS = {
    "enter": "Enter", "ctrl-c": "C-c", "ctrl-d": "C-d", "esc": "Escape",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "tab": "Tab", "space": "Space", "backspace": "BSpace", "delete": "DC",
    "home": "Home", "end": "End", "pgup": "PPage", "pgdn": "NPage",
}


class RelayError(Exception):
    pass


class PaneLocks:
    """Keep kernel locks on stable pane identities; never unlink their inodes."""

    def __init__(self, directory: Path, namespace: str, label: str):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory, self.namespace, self.label = directory, namespace, label
        self.held = {}

    def _open(self, pane):
        namespace = "ssh" if pane.get("server") not in {None, "local"} else self.namespace
        key = hashlib.sha256((namespace + "\0" + pane["identity"]).encode()).hexdigest()
        return os.open(self.directory / (key + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)

    def holder(self, pane):
        if pane["identity"] in self.held:
            return self.label
        fd = self._open(pane)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return ""
            except BlockingIOError:
                label = os.pread(fd, 256, 0).decode("utf-8", errors="replace").strip()
                return label or "另一个 bot"
        finally:
            os.close(fd)

    def acquire(self, pane):
        identity = pane["identity"]
        if identity in self.held:
            return
        fd = self._open(pane)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                label = os.pread(fd, 256, 0).decode("utf-8", errors="replace").strip() or "另一个 bot"
                raise RelayError(f"窗格已由 {label} 连接；请先在该 bot 退出，或选择其他窗格。") from exc
            os.ftruncate(fd, 0)
            os.write(fd, self.label.encode("utf-8")[:240])
            self.held[identity] = fd
        except BaseException:
            os.close(fd)
            raise

    def release_except(self, identity=None):
        for key in list(self.held):
            if key != identity:
                os.close(self.held.pop(key))

    def release(self, identity):
        fd = self.held.pop(identity, None)
        if fd is not None:
            os.close(fd)


class Tmux:
    def __init__(self, socket: str | None = None):
        self.prefix = ["/usr/bin/tmux"] + (["-S", socket] if socket else [])
        self.text_sent_at: dict[str, float] = {}

    def run(self, *args: str, input: bytes | None = None) -> str:
        try:
            result = subprocess.run(
                self.prefix + list(args), input=input, capture_output=True, timeout=3,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise RelayError("tmux 操作失败或超时，请重新列出窗格。") from exc
        if result.returncode:
            raise RelayError("tmux 窗格不存在、已关闭，或当前没有 tmux 服务。")
        return result.stdout.decode("utf-8", errors="replace")

    def panes(self) -> list[dict]:
        fields = ["pane_id", "pane_pid", "pane_tty", "session_name", "window_index",
                  "pane_index", "pane_current_command", "pane_current_path", "pane_dead"]
        delimiter = "\x1f"
        server = self.run("display-message", "-p", "#{pid}").strip()
        output = self.run("list-panes", "-a", "-F", delimiter.join("#{" + f + "}" for f in fields))
        panes = []
        for line in output.splitlines():
            # Newer tmux escapes separators; older releases emit the literal byte.
            values = line.split("\\037") if "\\037" in line else line.split(delimiter)
            if len(values) != len(fields) or values[-1] != "0":
                continue
            item = dict(zip(fields, values))
            try:
                # A pane id can be reused after the tmux server restarts.
                stat = Path(f"/proc/{item['pane_pid']}/stat").read_text()
                started = stat.rsplit(")", 1)[1].split()[19]
            except (OSError, IndexError):
                continue
            item["identity"] = f"{server}:{item['pane_pid']}:{started}:{item['pane_tty']}"
            item["target"] = f"{item['session_name']}:{item['window_index']}.{item['pane_index']}"
            panes.append(item)
        return panes

    def capture(self, pane: str, lines: int = 40) -> str:
        output = self.run("capture-pane", "-p", "-J", "-t", pane, "-S", f"-{lines}")
        output = "\n".join(output.rstrip().splitlines()[-lines:])
        return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", output)

    def viewport(self, pane: str) -> str:
        output = self.run("capture-pane", "-p", "-J", "-t", pane)
        return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", output.rstrip())

    def send(self, pane: str, text: str, enter: bool = True) -> None:
        if len(text.encode("utf-8")) > 16000 or any(ord(c) < 32 and c not in "\n\t" for c in text):
            raise RelayError("输入过长或包含终端控制字符；请使用 /tmux key 发送按键。")
        if "\n" in text or "\t" in text:
            # Bracketed paste preserves multiline prompts in TUI applications.
            name = f"hermes-relay-{os.getpid()}-{time.monotonic_ns()}"
            self.run("load-buffer", "-b", name, "-", input=text.encode("utf-8"))
            try:
                self.run("paste-buffer", "-p", "-d", "-b", name, "-t", pane)
            finally:
                try:
                    self.run("delete-buffer", "-b", name)
                except RelayError:
                    pass
        else:
            self.run("send-keys", "-t", pane, "-l", "--", text)
        self.text_sent_at[pane] = time.monotonic()
        if enter:
            self.key(pane, "enter")

    def key(self, pane: str, name: str) -> None:
        key = KEYS.get(name.lower())
        if not key:
            raise RelayError("支持的按键：" + "、".join(KEYS))
        if key == "Enter":
            # Separate submission from the TUI's fast-paste detection window.
            remaining = 0.25 - (time.monotonic() - self.text_sent_at.get(pane, 0.0))
            if remaining > 0:
                time.sleep(remaining)
        self.run("send-keys", "-t", pane, key)


def command(text: str) -> tuple[str, str] | None:
    value = text.strip()
    if value == "#help":
        return "help", ""
    if value.lower() in {"#tmux", "/tmux"}:
        return "help", ""
    if value.lower().startswith(("#tmux ", "/tmux ")):
        rest = value[6:].strip()
        verb, _, arg = rest.partition(" ")
        verb = {"button": "button", "pick": "pick", "help": "help", "ls": "list", "select": "enter", "screen": "screen", "list100": "screen100", "screen100": "screen100", "next": "next", "key": "key",
                "type": "type", "send": "send", "exit": "exit", "reconnect": "reconnect"}.get(verb.lower(), "help")
        return verb, arg
    return None


class Relay:
    def __init__(self, tmux: Tmux, idle_seconds: int = 1800, usage_file: Path | None = None,
                 state_file: Path | None = None, pane_locks: PaneLocks | None = None):
        self.tmux = tmux
        self.pane_locks = pane_locks
        self.idle_seconds = idle_seconds
        self.selected: dict | None = None
        self.listed: list[dict] = []
        self.touched = 0.0
        self.epoch = 0
        self.selection_token = ""
        self.input_serial = 0
        self.disconnected: dict | None = None
        self.cache: OrderedDict[str, dict] = OrderedDict()
        self.activity_ids: OrderedDict[str, None] = OrderedDict()
        self.shortcuts: OrderedDict[str, dict] = OrderedDict()
        self.usage_file = usage_file
        self.usage = self._load_usage()
        self.lock = threading.RLock()
        self.db = None
        if state_file is not None:
            fd = os.open(state_file, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            os.chmod(state_file, 0o600)
            self.db = sqlite3.connect(state_file, check_same_thread=False)
            self.db.execute("CREATE TABLE IF NOT EXISTS runtime (id INTEGER PRIMARY KEY, data TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, status TEXT NOT NULL, data TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS activity_receipts (id TEXT PRIMARY KEY)")
            saved = self.db.execute("SELECT data FROM runtime WHERE id=1").fetchone()
            if saved:
                state = json.loads(saved[0])
                for field in ("selected", "listed", "epoch", "selection_token", "disconnected", "input_serial"):
                    setattr(self, field, state[field])
                self.shortcuts = OrderedDict(state["shortcuts"])
                self.touched = time.monotonic() - max(0, time.time() - state["touched_at"])
            # A process may die after tmux accepted input but before its receipt committed.
            self.db.execute("UPDATE receipts SET status='unknown' WHERE status='pending'")
            self.db.commit()
        if self.selected and self.pane_locks:
            if not self._expire():
                try:
                    self._selected()
                    self.pane_locks.acquire(self.selected)
                except RelayError as exc:
                    if self.selected:
                        self.disconnected = {"pane": self.selected.copy(), "epoch": self.epoch,
                                             "reason": "claimed", "message": str(exc)}
                        self.selected = None
                        self.selection_token = ""
                        self.epoch += 1
                        self._persist()

    def _persist(self) -> None:
        if self.db is None:
            return
        state = {field: getattr(self, field) for field in (
            "selected", "listed", "epoch", "selection_token", "disconnected", "input_serial")}
        state.update(shortcuts=list(self.shortcuts.items()),
                     touched_at=time.time() - max(0, time.monotonic() - self.touched))
        self.db.execute("INSERT OR REPLACE INTO runtime VALUES (1, ?)", (json.dumps(state),))
        self.db.commit()

    def receipt(self, message_id: str) -> dict:
        with self.lock:
            if message_id in self.cache:
                return {"status": "complete", "result": self.cache[message_id]}
            if self.db is not None:
                row = self.db.execute("SELECT status, data FROM receipts WHERE id=?", (message_id,)).fetchone()
                if row:
                    return {"status": row[0], "result": json.loads(row[1]) if row[1] else None}
            return {"status": "missing"}

    def state(self) -> dict:
        with self.lock:
            notice = self._expire()
            if notice:
                return notice
            if self.selected:
                return {**self.screen(self.epoch), "epoch": self.epoch, "input_serial": self.input_serial}
            return self._disconnect_notice() if self.disconnected else self._response("")

    def activity(self, selection_token: str, event_id: str = "") -> dict:
        """Count a QQ input or accepted bot message, never a screen poll."""
        with self.lock:
            # Late sends from an exited/replaced selection cannot renew a new one.
            if not self.selected or not selection_token or not secrets.compare_digest(selection_token, self.selection_token):
                return {"recorded": False}
            if event_id:
                if event_id in self.activity_ids or (self.db is not None and self.db.execute(
                        "SELECT 1 FROM activity_receipts WHERE id=?", (event_id,)).fetchone()):
                    return {"recorded": False, "duplicate": True}
                self.activity_ids[event_id] = None
                while len(self.activity_ids) > 1024:
                    self.activity_ids.popitem(last=False)
                if self.db is not None:
                    self.db.execute("INSERT INTO activity_receipts VALUES (?)", (event_id,))
                    self.db.execute("DELETE FROM activity_receipts WHERE rowid NOT IN "
                                    "(SELECT rowid FROM activity_receipts ORDER BY rowid DESC LIMIT 1024)")
            self.touched = time.monotonic()
            self._persist()
            return {"recorded": True}

    def _load_usage(self) -> dict:
        if self.usage_file is None:
            return {}
        try:
            data = json.loads(self.usage_file.read_text())
            return {target: item for target, item in data.get("panes", {}).items()
                    if isinstance(target, str) and isinstance(item, dict)
                    and type(item.get("count")) is int and item["count"] >= 0
                    and isinstance(item.get("last_used"), (int, float))}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, AttributeError):
            logging.warning("Unable to load tmux usage statistics")
            return {}

    def _record_usage(self, pane: dict) -> None:
        target = pane["target"]
        self.usage[target] = {"count": self.usage.get(target, {}).get("count", 0) + 1,
                              "last_used": time.time()}
        if self.usage_file is None:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.usage_file.parent,
                                             prefix=".usage-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"version": 1, "panes": self.usage}, stream, ensure_ascii=False)
            temporary.replace(self.usage_file)
        except OSError:
            logging.warning("Unable to persist tmux usage statistics")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _pane_shortcuts(self) -> list[dict]:
        ranked = sorted(self.listed, key=lambda pane: (
            -self.usage.get(pane["target"], {}).get("count", 0),
            -self.usage.get(pane["target"], {}).get("last_used", 0),
        ))
        result = []
        for pane in ranked[:8]:
            token = secrets.token_urlsafe(12)
            self.shortcuts[token] = pane.copy()
            label = pane["session_name"]
            if sum(item["session_name"] == label for item in self.listed) > 1:
                label = pane["target"]
            if len(label) > 20:
                label = label[:19] + "…"
            result.append({"label": label, "token": token})
        # Keep recent lists usable without ever reinterpreting a button as a new pane id.
        while len(self.shortcuts) > 256:
            self.shortcuts.popitem(last=False)
        return result

    def _selected(self) -> dict:
        if self.selected is None:
            raise RelayError("尚未进入终端。发送 /tmux ls 选择窗格。")
        candidate = self.selected
        if hasattr(self.tmux, "resolve"):
            resolved = self.tmux.resolve(candidate)
            if resolved is not None:
                return resolved
            candidates = []
        else:
            candidates = self.tmux.panes()
        for item in candidates:
            if item["pane_id"] == candidate["pane_id"] and item["identity"] == candidate["identity"]:
                return item
        self.disconnected = {"pane": candidate.copy(), "epoch": self.epoch,
                             "reason": "closed", "message": "选中的窗格已关闭或被替换，已退出直连。请重新 /tmux ls。"}
        self.selected = None
        if self.pane_locks:
            self.pane_locks.release_except()
        self.selection_token = ""
        self.epoch += 1
        self._persist()
        raise RelayError("选中的窗格已关闭或被替换，已退出直连。请重新 /tmux ls。")

    def _response(self, message: str, watch: bool = False) -> dict:
        return {"handled": True, "active": self.selected is not None, "message": message,
                "epoch": self.epoch, "watch": watch,
                "input_serial": self.input_serial,
                "selection_token": self.selection_token if self.selected is not None else ""}

    def _expire(self) -> dict | None:
        if self.selected is None or time.monotonic() - self.touched < self.idle_seconds:
            return None
        self.disconnected = {"pane": self.selected.copy(), "epoch": self.epoch,
                             "token": secrets.token_urlsafe(12)}
        self.selected = None
        if self.pane_locks:
            self.pane_locks.release_except()
        self.selection_token = ""
        self.epoch += 1
        self._persist()
        return self._disconnect_notice()

    def _disconnect_notice(self) -> dict:
        if self.disconnected.get("reason") in {"closed", "claimed"}:
            return {**self._response(self.disconnected["message"]), "reason": self.disconnected["reason"]}
        target = self.disconnected["pane"]["target"].replace("`", "'")
        result = self._response(f"`{target}` · 双方 30 分钟无消息，已断开。\n"
                                "任务继续运行；/tmux reconnect 重连。")
        result.update(reason="idle", reconnect_token=self.disconnected["token"])
        return result

    def screen(self, epoch: int, lines: int = 1000) -> dict:
        with self.lock:
            if self.disconnected and not self.selected and epoch == self.disconnected["epoch"]:
                return self._disconnect_notice()
            if epoch != self.epoch or not self.selected:
                return {"active": False}
            notice = self._expire()
            if notice:
                return notice
            try:
                pane = self._selected()
                screen = self.tmux.capture(pane["pane_id"], lines)
                viewport = self.tmux.viewport(pane["pane_id"])
                normalized = lambda text: "\n".join(line.rstrip() for line in text.splitlines()).rstrip()
                return {"active": True, "target": pane["target"], "screen_key": pane["identity"],
                        "screen": screen, "viewport": viewport,
                        "redrawing": not normalized(screen).endswith(normalized(viewport)),
                        "input_serial": self.input_serial,
                        "selection_token": self.selection_token}
            except RelayError as exc:
                if self.selected is not None:
                    # A transient capture failure is not proof that the pane closed.
                    raise
                return {"active": False, "reason": "closed", "message": str(exc)}

    def route(self, text: str, message_id: str = "") -> dict:
        with self.lock:
            saved = self.receipt(message_id) if message_id else {"status": "missing"}
            if saved["status"] != "missing":
                # Never retransmit commands on QQ redelivery or an HTTP retry.
                if saved["status"] == "complete":
                    return {**saved["result"], "duplicate": True, "active": self.selected is not None,
                            "epoch": self.epoch, "selection_token": self.selection_token}
                return {**self._response("上次提交状态不确定，请先用 List100 核对；未重复输入。"), "uncertain": True}
            if message_id and self.db is not None:
                self.db.execute("INSERT INTO receipts VALUES (?, 'pending', NULL)", (message_id,))
                self.db.commit()
            parsed = command(text)
            expired = self._expire()
            if parsed is None and self.selected is None:
                if expired:
                    result = expired
                    result["message"] += "\n这条消息未输入终端。"
                else:
                    result = {"handled": False, "active": False}
            else:
                try:
                    result = self._route(text, parsed)
                    if self.selected:
                        self.touched = time.monotonic()
                except RelayError as exc:
                    result = self._disconnect_notice() if self.disconnected and not self.selected else self._response(str(exc))
                    result["message"] = str(exc)
                    result["error"] = True
            if message_id:
                self.cache[message_id] = result
                while len(self.cache) > 1024:
                    self.cache.popitem(last=False)
                if self.db is not None:
                    self.db.execute("UPDATE receipts SET status='complete', data=? WHERE id=?",
                                    (json.dumps(result), message_id))
                    self.db.execute("DELETE FROM receipts WHERE rowid NOT IN (SELECT rowid FROM receipts ORDER BY rowid DESC LIMIT 1024)")
            self._persist()
            return result

    def _route(self, text: str, parsed: tuple[str, str] | None) -> dict:
        if parsed:
            verb, arg = parsed
            if verb == "pick":
                candidate = self.shortcuts.get(arg)
                if candidate is None:
                    raise RelayError("原选择已过期，请 /tmux ls 刷新。")
                if not any(p["identity"] == candidate["identity"] and p["pane_id"] == candidate["pane_id"]
                           for p in self.tmux.panes()):
                    self.shortcuts.pop(arg, None)
                    raise RelayError("原窗格已关闭或被替换，请 /tmux ls 刷新。")
                return self._enter(candidate)
            if verb == "reconnect":
                previous = self.disconnected
                if self.selected or not previous or (arg and not secrets.compare_digest(arg, previous.get("token", ""))):
                    raise RelayError("重连已过期；请使用 /tmux ls 选择窗格。")
                candidate = previous["pane"]
                if not any(p["identity"] == candidate["identity"] and p["pane_id"] == candidate["pane_id"]
                           for p in self.tmux.panes()):
                    self.disconnected = None
                    raise RelayError("原窗格已关闭或被替换，无法重连。请 /tmux ls。")
                return self._enter(candidate)
            if verb == "button":
                token, _, action = arg.partition(" ")
                if not self.selected or not secrets.compare_digest(token, self.selection_token):
                    raise RelayError("按钮已过期；请 /tmux ls 重新选择窗格。")
                self._selected()
                if action == "list100":
                    action = "screen100"
                if action in KEYS:
                    return self._route(text, ("key", action))
                if action in {"next", "screen", "screen100", "exit"}:
                    return self._route(text, (action, ""))
                raise RelayError("不支持的快捷按钮操作。")
            if verb == "exit":
                if self.pane_locks:
                    self.pane_locks.release_except()
                self.selected = None
                self.disconnected = None
                self.selection_token = ""
                self.epoch += 1
                return {**self._response("已退出终端，返回 Hermes。"), "exited": True}
            if verb == "help":
                return self._response(HELP)
            if verb == "list":
                self.listed = self.tmux.panes()
                lines = ["## tmux 窗格", ""]
                for index, pane in enumerate(self.listed, 1):
                    mark = " · **当前**" if self.selected and pane["identity"] == self.selected["identity"] else ""
                    if not mark and self.pane_locks:
                        holder = self.pane_locks.holder(pane)
                        if holder:
                            mark = " · **占用：" + holder + "**"
                    # Pane names are user-controlled; keep them inside escaped inline code.
                    target = pane['target'].replace('`', "'")
                    program = pane['pane_current_command'].replace('`', "'")
                    lines.append(f"`{index}` · `{target}` · `{pane['pane_id']}` · {program}{mark}")
                lines.extend(["", "`/tmux select 编号` 进入；`/tmux help` 查看指令。"])
                result = self._response("\n".join(lines))
                result["pane_shortcuts"] = self._pane_shortcuts()
                return result
            if verb == "enter":
                if arg.isdigit():
                    index = int(arg) - 1
                    if index < 0 or index >= len(self.listed):
                        raise RelayError("编号无效，请先 /tmux ls，再使用本次列表中的编号。")
                    candidate = self.listed[index]
                else:
                    candidate = next((p for p in self.tmux.panes() if p["pane_id"] == arg or p["target"] == arg), None)
                    if not candidate:
                        raise RelayError("未找到窗格。请 /tmux ls。")
                if not any(p["identity"] == candidate["identity"] and p["pane_id"] == candidate["pane_id"] for p in self.tmux.panes()):
                    raise RelayError("列表中的窗格已关闭或被替换，请重新 /tmux ls。")
                return self._enter(candidate)
            if verb in {"screen", "screen100"}:
                try:
                    lines = 100 if verb == "screen100" else int(arg or "40")
                except ValueError as exc:
                    raise RelayError("格式：/tmux screen 10到100") from exc
                if not 10 <= lines <= 100:
                    raise RelayError("屏幕行数范围：10 到 100。")
                pane = self._selected()
                return self._snapshot(pane, "", lines, initial_context=lines == 100)
            if verb == "next":
                pane = self._selected()
                return self._response("已启用自动接收；新增内容会自动发送。", watch=True)
            if verb not in {"key", "send", "type"}:
                return self._response(HELP)
            pane = self._selected()
            self.epoch += 1
            if verb == "key":
                self.tmux.key(pane["pane_id"], arg.strip())
            else:
                if not arg:
                    raise RelayError("请输入要转发的文字。")
                self.tmux.send(pane["pane_id"], arg, enter=verb == "send")
        else:
            pane = self._selected()
            self.epoch += 1
            self.tmux.send(pane["pane_id"], text)
        submitted = parsed is None or parsed[0] == "send" or (parsed[0] == "key" and parsed[1].strip().lower() == "enter")
        if submitted:
            self.input_serial += 1
        action = "已填入，**未回车**；`/tmux key enter` 提交。" if parsed and parsed[0] == "type" else "已输入，等待新增输出。"
        target = pane["target"].replace("`", "'")
        result = self._response(f"`{target}` · {action}", watch=True)
        result["submitted"] = submitted
        result["quiet"] = not (parsed and parsed[0] == "type")
        return result

    def _enter(self, candidate: dict) -> dict:
        if self.pane_locks:
            self.pane_locks.acquire(candidate)
        previous = (self.selected, self.disconnected, self.selection_token, self.epoch)
        self.selected = candidate
        self.disconnected = None
        self.selection_token = secrets.token_urlsafe(12)
        self.epoch += 1
        try:
            pane = self._selected()
            result = self._snapshot(pane, "已连接 · 最近 100 行", lines=100, initial_context=True)
        except RelayError:
            self.selected, self.disconnected, self.selection_token, self.epoch = previous
            if self.pane_locks:
                self.pane_locks.release_except(self.selected["identity"] if self.selected else None)
                if self.selected:
                    self.pane_locks.acquire(self.selected)
            raise
        if self.pane_locks:
            self.pane_locks.release_except(pane["identity"])
        self._record_usage(pane)
        return result

    def _snapshot(self, pane: dict, message: str, lines: int = 40, initial_context: bool = False) -> dict:
        baseline = self.tmux.capture(pane["pane_id"], 1000)
        result = self._response(message)
        result.update(target=pane["target"], screen_key=pane["identity"], baseline=baseline,
                      screen="\n".join(baseline.splitlines()[-lines:]),
                      viewport=self.tmux.viewport(pane["pane_id"]), initial_context=initial_context)
        return result


def serve(relay: Relay, token: str, port: int) -> None:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.connection.settimeout(10)
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid size")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("invalid body")
                if getattr(relay, "multiplex", False):
                    result = relay.api(self.path, body)
                elif self.path == "/v1/route":
                    text = body["text"]
                    message_id = body.get("message_id", "")
                    if not isinstance(text, str) or not isinstance(message_id, str):
                        raise ValueError("invalid message")
                    result = relay.route(text, message_id)
                elif self.path == "/v1/screen":
                    result = relay.screen(int(body["epoch"]))
                elif self.path == "/v1/state":
                    result = relay.state()
                elif self.path == "/v1/activity":
                    selection_token = body["selection_token"]
                    event_id = body.get("event_id", "")
                    if (not isinstance(selection_token, str) or not selection_token or len(selection_token) > 256
                            or not isinstance(event_id, str) or len(event_id) > 256):
                        raise ValueError("invalid activity")
                    result = relay.activity(selection_token, event_id)
                elif self.path == "/v1/receipt":
                    message_id = body["message_id"]
                    if not isinstance(message_id, str) or not message_id:
                        raise ValueError("invalid message id")
                    result = relay.receipt(message_id)
                elif self.path == "/healthz":
                    result = {"ok": True}
                else:
                    self.send_error(404)
                    return
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                self.send_error(400)
                return
            payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    class Server(ThreadingHTTPServer):
        def service_actions(self):
            # Release idle leases even if the QQ container is offline and no longer polls.
            with relay.lock:
                relay._expire()

    server = Server(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.serve_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--socket")
    parser.add_argument("--port", type=int, default=18010)
    parser.add_argument("--lock-dir", type=Path)
    parser.add_argument("--bot-name", default="终端 bot")
    parser.add_argument("--hosts-file", type=Path)
    args = parser.parse_args()
    token = args.token_file.read_text().strip()
    if len(token) < 32:
        parser.error("bridge token must be at least 32 characters")
    # The ordinary-user systemd service also supports launching this file directly.
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tmux_bot.bridge import Tmux as RuntimeTmux, PaneLocks as RuntimePaneLocks
    from tmux_bot.multiplex import MultiRelay
    from tmux_bot.remote import TmuxFleet, load_hosts
    pane_locks = RuntimePaneLocks(args.lock_dir, args.socket or "default", args.bot_name) if args.lock_dir else None
    hosts_file = args.hosts_file or args.token_file.with_name("hosts.json")
    fleet = TmuxFleet(RuntimeTmux(args.socket), load_hosts(hosts_file), args.token_file.parent / "ssh")
    serve(MultiRelay(fleet, args.token_file.parent,
                     pane_locks=pane_locks), token, args.port)


if __name__ == "__main__":
    main()
