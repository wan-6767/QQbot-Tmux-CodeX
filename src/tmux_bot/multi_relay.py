"""One authenticated QQ bot, with isolated delivery state for every terminal."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import tempfile

from gateway.config import Platform
from gateway.session import SessionSource

from . import owner, terminal_relay as text, terminal_files
from .multiplex import HELP as TMUX_HELP, parse
from .bridge import RelayError
from .sub2api.command import UsageCommand

logger = logging.getLogger("qq_tmuxbot.multi")

HELP = """## QQbot-Tmux

**终端** · `/tmux ls` 查看编号，`/tmux help` 查看操作。
接入：`/tmux sel 001 ent`；发送：`/tmux sel 001 send 文字`；退出：`/tmux sel 001 ext`。

**文件** · 上传后返回绝对路径，`/file help` 查看详情。
下载：`/file dl /绝对路径`；清理上传缓存：`/file rm`。

**额度** · `/sub2api usage` 刷新本机账号额度及积分（可选插件）。

**群绑定** · 私聊 `/group bind`、`/group status`、`/group unbind`。
仅绑定本人可操作；群内需@本bot，回复全群可见。"""


class Channel:
    def __init__(self, gateway, result):
        self.gateway = gateway
        self.id = result["channel"]
        self.path = text.root() / "channels" / self.id / "delivery.json"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.meta = result.copy()
        self.screen = None
        self.content = ""
        self.dialog = None
        self.ui = {"queue": "", "editor": ""}
        self.turn = {}
        self.pending = None
        self.entry = None
        self.notice = None
        self.show_editor = False
        self.task = None
        self.inflight = None
        self.closed = False
        self.delivery_lock = asyncio.Lock()
        self._saved = None
        try:
            saved = json.loads(self.path.read_text())
        except FileNotFoundError:
            saved = None
        if saved and saved.get("version") == 1 and saved["meta"].get("selection_token") == result.get("selection_token"):
            for key in ("screen", "content", "dialog", "ui", "turn", "pending", "entry", "notice", "show_editor"):
                setattr(self, key, saved.get(key, getattr(self, key)))

    def checkpoint(self):
        data = {key: getattr(self, key) for key in ("meta", "screen", "content", "dialog", "ui", "turn", "pending", "entry", "notice", "show_editor")}
        payload = json.dumps(data, sort_keys=True)
        if payload != self._saved:
            owner.write_private(self.path, {"version": 1, **data})
            self._saved = payload

    @property
    def source(self):
        return self.meta["source"]

    def observe(self, result):
        if text.dialog_block(result.get("viewport", result["screen"])) is not None:
            return
        state = self.turn
        state.setdefault("running", False)
        state.setdefault("awaiting", False)
        state.setdefault("serial", 0)
        before = state.copy()
        baseline = result.get("baseline", result["screen"])
        previous = self.screen or ""
        state.setdefault("completion_armed", bool(state.get("pending") or state.get("awaiting") or state["running"]))
        signature = text.prompt_signature(baseline)
        old_signature = state.get("prompt_signature", text.prompt_signature(previous))
        new_prompt = bool(signature[0] and (signature[0] > old_signature[0] or signature[1] != old_signature[1]))
        busy = text.terminal_busy(result.get("viewport", result["screen"]))
        if (busy and not state["running"]) or new_prompt:
            state.update(awaiting=True, body_delivered=False, pending=True,
                         completion_before=text.completion_marker(previous), completion_cleared=False,
                         completion_armed=True, completion_generation=state.get("completion_generation", 0) + 1)
        if (state.get("pending") or state["completion_armed"]) and not text.completion_marker(baseline):
            state["completion_cleared"] = True
        state.update(prompt_signature=signature, running=busy)
        if state != before:
            self.checkpoint()

    def settled(self, result):
        viewport = result.get("viewport", result["screen"])
        if text.terminal_busy(viewport):
            return False
        if not self.turn.get("pending") or not re.search(r"(?im)^\s*GPT-[\w.-]+.*[·]", viewport):
            return True
        marker = text.completion_marker(result.get("baseline", result["screen"]))
        return bool(marker and (self.turn.get("completion_cleared") or marker != self.turn.get("completion_before")))

    def completion(self, result, settled):
        if not settled or self.turn.get("running") or not self.turn.get("completion_armed"):
            return ""
        baseline = result.get("baseline", result["screen"])
        if not self.turn.get("completion_cleared") and text.completion_marker(baseline) == self.turn.get("completion_before"):
            return ""
        return text.worked_footer(baseline)

    def seed(self, result):
        self.screen = result.get("baseline", result["screen"])
        self.content = text.clean_terminal_content(self.screen)
        viewport = result.get("viewport", result["screen"])
        self.ui = text.terminal_ui(viewport)
        self.dialog = text.dialog_block(viewport)
        busy = text.terminal_busy(viewport)
        self.turn = {"running": busy, "awaiting": busy, "pending": busy,
            "serial": result.get("input_serial", 0), "completion_before": text.completion_marker(self.screen),
            "completion_cleared": busy and not text.completion_marker(self.screen), "completion_armed": busy,
            "completion_generation": 0, "prompt_signature": text.prompt_signature(self.screen)}
        self.checkpoint()

    async def frame(self, body, prefix="", scope=None):
        if self.closed:
            return False
        label = self.meta["target"]
        message = text.format_screen(label, body)
        if prefix:
            message = prefix + "\n\n" + message
        adapter = self.gateway.adapter
        token = scope if scope is not None else self.meta.get("selection_token", "")
        if len(message) <= getattr(adapter, "MAX_MESSAGE_LENGTH", 4000):
            return await self.gateway.send(self.source, message, channel=self.id, token=token)
        directory = self.path.parent / "outgoing"
        directory.mkdir(exist_ok=True, mode=0o700)
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", dir=directory, delete=False) as stream:
                path = Path(stream.name)
                stream.write(body)
            async with self.gateway.send_lock:
                if self.closed or not self.gateway.authorized(self.source):
                    return False
                result = await adapter.send_document(self.source["chat_id"], str(path),
                    caption=f"[{label}] 完整终端文本（未拆分或截断）", file_name=f"terminal-{self.id}.txt", reply_to=None)
            return await self.gateway.accepted(result, self.id, token)
        except Exception:
            logger.warning("Terminal %s attachment failed; retaining frame", self.id)
            return False
        finally:
            if path:
                path.unlink(missing_ok=True)

    async def flush(self):
        batch = self.pending
        if batch is None:
            return True
        while batch["next"] < len(batch["bodies"]):
            if not await self.frame(text.unwrap_prose(batch["bodies"][batch["next"]]), scope=batch["scope"]):
                return False
            batch["next"] += 1
            self.checkpoint()
        if batch.get("completion"):
            if not await self.frame(batch["completion"], scope=batch["scope"]):
                return False
            if self.turn.get("completion_generation", 0) == batch.get("completion_generation", 0):
                self.turn["completion_armed"] = False
            batch.pop("completion")
            self.checkpoint()
        self.screen, self.content, self.dialog = batch["baseline"], batch["content"], None
        self.pending = None
        self.checkpoint()
        return True

    async def deliver(self, result, snapshot=False, settled=True):
        self.observe(result)
        delivered_pending = False
        if not snapshot and self.pending:
            if not await self.flush():
                return False
            delivered_pending = True
        baseline = result.get("baseline", result["screen"])
        content = text.clean_terminal_content(baseline, complete_only=not settled and not snapshot)
        dialog = text.dialog_block(result.get("viewport", result["screen"]))
        if snapshot and result.get("initial_context"):
            body = result["screen"]
        elif dialog is not None:
            if not snapshot and self.dialog == dialog:
                self.screen = baseline
                self.checkpoint()
                return False
            body = dialog
        elif snapshot:
            body = result["screen"]
        elif self.screen is None:
            return False
        else:
            body = text.screen_delta(self.content, content, paragraphs=bool(re.search(r"(?m)^›(?: |$)", baseline)))
            if not body and settled:
                old_prompt, old_answer = text.latest_turn(self.screen)
                new_prompt, new_answer = text.latest_turn(baseline)
                if new_answer and ((self.turn["awaiting"] and not self.turn.get("body_delivered"))
                                   or (old_prompt and new_prompt != old_prompt and old_answer == new_answer)):
                    body = new_answer
            completion = self.completion(result, settled)
            if not body and not completion:
                self.screen = baseline
                if settled:
                    self.turn.update(awaiting=False, pending=False)
                    self.content = content
                self.checkpoint()
                return delivered_pending
            if body:
                self.turn["body_delivered"] = True
            if settled:
                self.turn.update(awaiting=False, pending=False)
            self.pending = {"baseline": baseline, "content": content, "bodies": text.message_bodies(body),
                            "next": 0, "completion": completion,
                            "completion_generation": self.turn.get("completion_generation", 0),
                            "scope": result.get("selection_token", "")}
            self.checkpoint()
            return await self.flush()
        if not await self.frame(body):
            return False
        if snapshot:
            self.seed(result)
            self.pending, self.entry = None, None
        else:
            self.screen = baseline
            if dialog is None:
                self.content = content
            self.dialog = dialog
        self.checkpoint()
        return True

    async def deliver_ui(self, result):
        current = text.terminal_ui(result.get("viewport", result["screen"]))
        body = ""
        if current["queue"] and current["queue"] != self.ui["queue"]:
            body = "Codex · 消息已排队，尚未开始。"
        elif self.show_editor and current["editor"] and current["editor"] != self.ui["editor"]:
            body = current["editor"]
        if body and not await self.frame(body):
            return False
        self.ui = current
        self.checkpoint()
        return True

    async def update(self, result, verb, argument=""):
        # Finish an in-flight POST before changing this channel's generation.
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.inflight and not self.inflight.done():
            await asyncio.shield(self.inflight)
        async with self.delivery_lock:
            old_token = self.meta.get("selection_token")
            self.meta.update(result)
            if verb == "ent" and old_token != result.get("selection_token"):
                self.screen, self.content, self.dialog = None, "", None
                self.pending, self.turn, self.ui = None, {}, {"queue": "", "editor": ""}
            if "screen" in result:
                self.entry = result
            if result.get("submitted"):
                self.turn.update(serial=result.get("input_serial", 0), awaiting=bool(self.turn.get("running")))
                if self.dialog is None:
                    self.turn.update(pending=True, completion_armed=True,
                        completion_before=text.completion_marker(self.screen or ""), completion_cleared=False)
            self.show_editor = verb == "type" or (verb == "key" and argument.lower() in {"backspace", "delete", "space", "left", "right", "home", "end"})
            self.checkpoint()
        self.task = asyncio.create_task(self.watch(), name="tmux-watch-" + self.id)

    async def watch(self):
        candidate, candidate_since, retry_at, failures = None, 0.0, 0.0, 0
        loop = asyncio.get_running_loop()
        try:
            while not self.closed:
                if self.entry:
                    async def initial():
                        async with self.delivery_lock:
                            entry = self.entry
                            sent = await self.deliver(entry, snapshot=True, settled=self.settled(entry))
                            if sent:
                                await self.deliver_ui(entry)
                            return sent
                    self.inflight = asyncio.create_task(initial())
                    if not await asyncio.shield(self.inflight):
                        await asyncio.sleep(5)
                        continue
                if self.notice:
                    if not await self.gateway.send(self.source, self.notice["message"]):
                        await asyncio.sleep(5)
                        continue
                    await self.gateway.request("/v2/ack-close", {"channel": self.id, "epoch": self.notice["epoch"]})
                    self.closed = True
                    self.entry, self.pending, self.notice = None, None, None
                    self.checkpoint()
                    return
                try:
                    result = await self.gateway.request("/v2/screen", {"channel": self.id, "epoch": self.meta["epoch"]})
                except Exception:
                    failures += 1
                    logger.warning("Terminal %s capture unavailable; retrying", self.id)
                    await asyncio.sleep(min(60, 2 ** min(failures, 6)))
                    continue
                if result.get("error"):
                    failures += 1
                    logger.warning("Terminal %s temporarily unavailable; keeping its subscription", self.id)
                    await asyncio.sleep(min(60, 2 ** min(failures, 6)))
                    continue
                if not result.get("active"):
                    if result.get("reason") in {"idle", "closed", "claimed"}:
                        self.notice = result
                        self.checkpoint()
                        continue
                    return
                if result.get("redrawing"):
                    candidate = None
                    await asyncio.sleep(.5)
                    continue
                # Keep a renamed pane's latest label without changing its stable ID.
                self.meta["target"] = result["target"]
                self.observe(result)
                menu = text.dialog_block(result.get("viewport", ""))
                settled = self.settled(result)
                ui = text.terminal_ui(result.get("viewport", result["screen"]))
                fingerprint = (menu if menu is not None else text.clean_terminal_content(result["screen"], complete_only=not settled),
                               ui["queue"], ui["editor"] if self.show_editor else "", settled,
                               self.completion(result, settled) if menu is None else "")
                now = loop.time()
                if fingerprint != candidate:
                    candidate, candidate_since = fingerprint, now
                if now >= retry_at and now - candidate_since >= (.3 if menu is not None else 2.0):
                    async def deliver_frame():
                        async with self.delivery_lock:
                            if self.screen is None:
                                self.seed(result)
                                return True, True
                            sent = await self.deliver(result, settled=self.settled(result))
                            return sent, await self.deliver_ui(result)
                    self.inflight = asyncio.create_task(deliver_frame())
                    sent, ui_sent = await asyncio.shield(self.inflight)
                    if not ui_sent or (not sent and self.pending):
                        failures += 1
                        retry_at = now + min(60, 2 ** min(failures + 1, 6))
                    else:
                        failures, retry_at = 0, now + .5
                await asyncio.sleep(.5)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Terminal %s observer failed", self.id)
            if not self.closed:
                await asyncio.sleep(5)
                self.task = asyncio.create_task(self.watch(), name="tmux-watch-" + self.id)


class MultiGateway:
    def __init__(self, adapter):
        self.adapter = adapter
        self.channels = {}
        self.send_lock = asyncio.Lock()
        self.start_task = None
        self.usage_command = UsageCommand(lambda event: self._is_user_authorized_for_source(event.source))

    def _is_user_authorized_for_source(self, source):
        if source.platform != Platform.QQBOT:
            return False
        if source.chat_type == "dm":
            return bool(owner.owner_id()) and source.user_id == source.chat_id == owner.owner_id()
        return source.chat_type == "group" and owner.group_allowed(source.chat_id, source.user_id)

    def authorized(self, source):
        return self._is_user_authorized_for_source(SessionSource(platform=Platform.QQBOT, **source))

    def has_group(self):
        return any(not c.closed and c.source["chat_type"] == "group" for c in self.channels.values())

    async def request(self, path, payload):
        return await asyncio.to_thread(text.request, path, payload)

    async def accepted(self, result, channel="", token=""):
        success = bool(getattr(result, "success", False))
        if success and channel and token:
            try:
                await self.request("/v2/activity", {"channel": channel, "selection_token": token,
                    "event_id": "bot:" + str(getattr(result, "message_id", "") or "")})
            except Exception:
                logger.warning("Terminal %s delivery activity not recorded", channel)
        return success

    async def send(self, source, message, channel="", token="", reply_to=None):
        if not self.authorized(source):
            return False
        try:
            async with self.send_lock:
                result = await self.adapter.send(source["chat_id"], message, reply_to=reply_to)
            return await self.accepted(result, channel, token)
        except Exception:
            logger.warning("QQ multi-terminal send failed; preserving pending output")
            return False

    async def start(self):
        failures = 0
        while True:
            try:
                state = await self.request("/v2/state", {})
                if state.get("protocol") != 2:
                    raise RuntimeError("multi-terminal bridge required")
                for result in state["channels"]:
                    if not result.get("source") or not self.authorized(result["source"]):
                        continue
                    self.adapter._chat_type_map[result["source"]["chat_id"]] = "group" if result["source"]["chat_type"] == "group" else "c2c"
                    channel = Channel(self, result)
                    self.channels[channel.id] = channel
                    if not result.get("active") and result.get("reason"):
                        channel.notice = result
                    channel.task = asyncio.create_task(channel.watch(), name="tmux-watch-" + channel.id)
                logger.info("Multi-terminal delivery restored (%d connections)", len(self.channels))
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.warning("Multi-terminal recovery unavailable; retrying")
                await asyncio.sleep(min(60, 2 ** min(failures, 6)))

    async def stop_channel(self, channel):
        if channel.task and not channel.task.done():
            channel.task.cancel()
            await asyncio.gather(channel.task, return_exceptions=True)
        if channel.inflight and not channel.inflight.done():
            await asyncio.shield(channel.inflight)
        channel.closed = True
        async with channel.delivery_lock:
            channel.entry, channel.pending, channel.notice = None, None, None
            channel.checkpoint()

    async def stop(self):
        if self.usage_command.task and not self.usage_command.task.done():
            self.usage_command.task.cancel()
            await asyncio.gather(self.usage_command.task, return_exceptions=True)
        if self.start_task and not self.start_task.done():
            self.start_task.cancel()
            await asyncio.gather(self.start_task, return_exceptions=True)
        for channel in self.channels.values():
            if channel.task and not channel.task.done():
                channel.task.cancel()
        await asyncio.gather(*(c.task for c in self.channels.values() if c.task), return_exceptions=True)
        pending = [c.inflight for c in self.channels.values() if c.inflight and not c.inflight.done()]
        if pending:
            await asyncio.wait(pending, timeout=10)

    async def dispatch(self, event):
        if not self._is_user_authorized_for_source(event.source):
            return
        source = {k: getattr(event.source, k) for k in ("chat_id", "user_id", "chat_type")}
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        value = str(raw.get("content", event.text) or "").strip()
        reply_to = str(event.message_id or "")
        if value.lower() == "/sub2api usage":
            await self.usage_command.handle(event, self.adapter)
            return
        attachments = (getattr(event, "metadata", None) or {}).get("qqbot_cached_attachments")
        if attachments:
            try:
                def validate(relative, device, inode):
                    return text.request("/v2/files/validate", {"relative": relative, "device": device, "inode": inode})["path"]
                message = await asyncio.to_thread(terminal_files.receipt, event, text.root(), validate)
            except Exception:
                message = "文件接收失败：缓存与宿主映射未通过核验，请重发。"
            await self.send(source, message, reply_to=reply_to)
            return
        if value.lower() == "/help":
            await self.send(source, HELP, reply_to=reply_to)
            return
        file_command = re.fullmatch(r"/file(?:\s+(dl|rm|help)(?:\s+([\s\S]+))?)?", value, re.I)
        if (value.split(maxsplit=1) or [""])[0].lower() == "/file" and not file_command:
            await self.send(source, "文件指令：/file dl 绝对路径；/file rm 清理上传缓存；/file help 查看帮助。", reply_to=reply_to)
            return
        if file_command and (not file_command[1] or file_command[1].lower() == "dl" and not file_command[2]):
            await self.send(source, terminal_files.HELP, reply_to=reply_to)
            return
        if file_command and file_command[1].lower() == "help":
            await self.send(source, terminal_files.HELP, reply_to=reply_to)
            return
        if value.lower() == "/files clear" or (file_command and file_command[1].lower() == "rm"):
            try:
                message = await asyncio.to_thread(terminal_files.clear, text.root(), file_command[2] if file_command else None)
            except Exception:
                message = "缓存清理失败，未删除其他文件。"
            await self.send(source, message, reply_to=reply_to)
            return
        if (value.split(maxsplit=1) or [""])[0].lower() == "/download" or (file_command and file_command[1].lower() == "dl"):
            payload = None
            try:
                argument = (file_command[2] or "").strip() if file_command else value.partition(" ")[2].strip()
                payload = await self.request("/v2/files/prepare", {"path": argument})
                if payload.get("error"):
                    raise RelayError(payload["message"])
                if not self.authorized(source):
                    return
                result = await self.adapter.send_document(source["chat_id"], str(owner.home() / payload["relative"]),
                                                         file_name=payload["name"], reply_to=reply_to)
                if not getattr(result, "success", False):
                    raise RelayError(str(getattr(result, "error", "QQ拒绝发送文件")))
            except Exception as exc:
                await self.send(source, "下载失败：" + str(exc)[:300], reply_to=reply_to)
            finally:
                if payload and payload.get("token"):
                    try:
                        await self.request("/v2/files/release", {"token": payload["token"]})
                    except Exception:
                        logger.warning("Outgoing file snapshot cleanup deferred")
            return
        if value.lower() in {"/group bind", "/group status", "/group unbind"} and source["chat_type"] == "dm":
            if value.lower() == "/group status":
                message = "群聊已绑定，只接受你的 @消息。" if owner.group_binding() else "尚未绑定群聊。私聊 /group bind。"
            elif self.has_group():
                message = "先将群内所有连接 /tmux sel 编号 ext，再修改群绑定。"
            elif value.lower() == "/group unbind":
                owner.unbind_group()
                message = "已解除群聊绑定。"
            else:
                command = owner.prepare_group_pairing()
                message = "## 群聊绑定\n\n在目标群 @本 bot，发送：\n`" + command + "`\n\n10分钟有效；终端输出全群可见。"
            await self.send(source, message, reply_to=reply_to)
            return
        if value.lower() in {"/tmux help", "/tmux"}:
            await self.send(source, TMUX_HELP, reply_to=reply_to)
            return
        try:
            verb, number, argument = parse(value)
        except RelayError as exc:
            await self.send(source, str(exc), reply_to=reply_to)
            return
        # Recovery finishes before any route mutates persisted connections.
        if self.start_task and not self.start_task.done():
            await self.send(source, "正在恢复终端连接，请稍后重试。", reply_to=reply_to)
            return
        payload = {"text": value, "message_id": reply_to, "source": source}
        try:
            result = await self.request("/v2/route", payload)
        except Exception:
            try:
                receipt = await self.request("/v2/receipt", {"message_id": reply_to, "source": source})
            except Exception:
                receipt = {"status": "unknown"}
            if receipt.get("status") == "complete":
                result = receipt["result"]
            else:
                await self.send(source, f"{number or '终端'}提交状态不明，未重复发送。请 /tmux sel 编号 tail 100 核对。", reply_to=reply_to)
                return
        if result.get("duplicate"):
            return
        if result.get("error") or not number or result.get("uncertain"):
            if number and result.get("active") and number in self.channels:
                await self.channels[number].update(result, "error")
            await self.send(source, result.get("message", "桥接返回不完整。"), reply_to=reply_to)
            return
        channel = self.channels.get(number)
        if verb == "ext":
            if channel:
                await self.stop_channel(channel)
                self.channels.pop(number, None)
            await self.send(source, result["message"], reply_to=reply_to)
            return
        if not result.get("active"):
            await self.send(source, result.get("message", "连接不可用。"), reply_to=reply_to)
            return
        if channel is None or channel.closed:
            channel = Channel(self, result)
            self.channels[number] = channel
        await channel.update(result, verb, argument)
        # User activity is already persisted by the bridge route; sends renew only this ID.
        if result.get("message") and not result.get("quiet") and "screen" not in result:
            await self.send(source, result["message"], channel=number,
                            token=result.get("selection_token", ""), reply_to=reply_to)
