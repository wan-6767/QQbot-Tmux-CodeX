"""Reuse the existing QQ adapter and terminal relay; never create an AIAgent."""

import asyncio
import logging
import os
import signal
import time

from gateway.config import Platform, PlatformConfig
from gateway.platforms.qqbot.adapter import QQAdapter
from gateway.session import SessionSource

from . import owner, qq_commands, terminal_relay, group_delivery
from .bridge import HELP

logger = logging.getLogger("qq_tmuxbot")
HELP_TEXT = HELP.replace("返回 Hermes", "返回终端选择模式").replace(
    "/help` · Hermes 全局帮助", "/help` · 本机器人终端帮助").replace(
    "不调用 Hermes 模型", "不调用任何模型")
HELP_TEXT += "\n\n## 群聊\n群内每条指令或文字都需 @本 bot，只有绑定的本人可操作。\n私聊 `/group bind` 获取一次性绑定指令，`/group status` 查看，`/group unbind` 解除。\n每个 bot 同时连接一个窗格；不同 bot 不能占用同一窗格。跨私聊/群聊切换前先 `/tmux exit`。"
HELP_TEXT = "\n".join(line for line in HELP_TEXT.splitlines()
                      if not line.startswith(("- 上传文件或图片", "- 回执下方 `Clear files`")))
HELP_TEXT += "\n\n## 功能边界\n本项目只转发文字终端，不提供附件中转、额度查询或 AI 对话。"


def bot_name():
    return os.environ.get("TMUX_BOT_NAME", "QQ Tmux")


def service_message(text):
    return {
        "已退出终端，返回 Hermes。": "已退出终端。",
        "已在本地退出终端模式，后续消息恢复 Hermes；桥接服务恢复后会清除旧连接。": "已退出终端；桥接恢复后清除旧连接。",
        "终端连接已重置，消息未输入终端。请 #tmux ls 重新进入；后续消息恢复 Hermes 对话。": "连接已重置，消息未输入终端。请 /tmux ls 重新进入。",
        "提交状态暂无法确认；请先用 List100 核对，勿重复发送。消息未交给 Hermes。#tmux exit 返回聊天。": "提交状态不明，请用 List100 核对，勿重复发送。",
    }.get(text, text)


class TerminalGateway:
    def __init__(self, adapter):
        self.adapter = adapter

    def _adapter_for_source(self, source):
        return self.adapter if self._is_user_authorized_for_source(source) else None

    def _is_user_authorized_for_source(self, source):
        if source.platform != Platform.QQBOT:
            return False
        if source.chat_type == "dm":
            return bool(owner.owner_id()) and source.user_id == source.chat_id == owner.owner_id()
        return source.chat_type == "group" and owner.group_allowed(source.chat_id, source.user_id)

    def _terminal_recovery_source(self, session):
        source = SessionSource(platform=Platform.QQBOT, chat_id=session["chat_id"],
                               user_id=session["user_id"], chat_type=session.get("chat_type", "dm"))
        if not self._is_user_authorized_for_source(source):
            return None
        self.adapter._chat_type_map[source.chat_id] = "group" if source.chat_type == "group" else "c2c"
        return source

    def _same_terminal_chat(self, source):
        session = terminal_relay._session
        return (not session or (session.get("chat_id") == source.chat_id
                               and session.get("chat_type", "dm") == source.chat_type))

    async def dispatch(self, event):
        if not self._is_user_authorized_for_source(event.source):
            return
        message_id = str(getattr(event, "message_id", "") or "")
        terminal_relay.load_checkpoint()
        scope = terminal_relay.activity_scope() if self._same_terminal_chat(event.source) else ""
        await terminal_relay.record_activity(scope,
                                             "user:" + message_id if message_id else "")
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        text = str(raw.get("content", event.text) or "").strip()
        if (text.split(maxsplit=1) or [""])[0].lower() in {"#tmux", "#help"}:
            await self.adapter.send(event.source.chat_id, "指令已统一为 / 开头，请发送 /tmux help。", reply_to=event.message_id)
            return
        if (text.split(maxsplit=1) or [""])[0].lower() == "/download" or text.lower() in {"/files clear", "/tmux files clear"}:
            await self.adapter.send(event.source.chat_id, "本项目不提供文件收发，请只发送文字终端指令。", reply_to=event.message_id)
            return
        if event.source.chat_type == "dm" and text.lower() in {"/group bind", "/group unbind", "/group status"}:
            if text.lower() == "/group status":
                message = "群聊已绑定，只接受你的 @消息。" if owner.group_binding() else "尚未绑定群聊。私聊发送 /group bind。"
            elif terminal_relay.active() and terminal_relay._session.get("chat_type") == "group":
                message = "先在群里 /tmux exit，再修改群聊绑定。"
            elif text.lower() == "/group unbind":
                owner.unbind_group()
                message = "已解除群聊绑定。"
            else:
                command = owner.prepare_group_pairing()
                message = "## 群聊绑定\n\n在目标群 @" + bot_name() + "，发送：\n`" + command + "`\n\n10 分钟有效，仅使用一次。群内终端输出所有成员可见。"
            await self.adapter.send(event.source.chat_id, message, reply_to=event.message_id)
            return
        if text.lower() == "/sub2api usage":
            await self.adapter.send(event.source.chat_id, "本项目不提供额度查询。", reply_to=event.message_id)
            return
        if text.lower() in {"/help", "/tmux help", "/tmux"}:
            await self.adapter.send(event.source.chat_id, HELP_TEXT, reply_to=event.message_id)
            return
        if terminal_relay.active() and not self._same_terminal_chat(event.source):
            if qq_commands.terminal_command(text).strip() == "#tmux ls":
                result = await asyncio.to_thread(terminal_relay.request, "/v1/route", {
                    "text": "#tmux ls", "message_id": message_id})
                await self.adapter.send(event.source.chat_id, result["message"], reply_to=event.message_id)
            else:
                await self.adapter.send(event.source.chat_id, "此 bot 正连接另一个聊天。先在原聊天 /tmux exit，再在这里选择终端。", reply_to=event.message_id)
            return
        if not terminal_relay.handle(event, self):
            await self.adapter.send(event.source.chat_id, "先 /tmux ls，再 /tmux select 编号。", reply_to=event.message_id)


class TerminalAdapter(QQAdapter):
    terminal_keyboards_enabled = False

    def __init__(self, config):
        super().__init__(config)
        self.gateway = TerminalGateway(self)
        self.dispatch_lock = asyncio.Lock()
        self.panel_task = None
        self.group_replies = group_delivery.GroupReplies()
        self.group_send_lock = asyncio.Lock()
        self.last_dispatch_at = 0
        self.last_group_message_at = 0
        self.set_interaction_callback(None)

    def _dispatch_payload(self, payload):
        if payload.get("op") == 0:
            self.last_dispatch_at = time.time()
            logger.info("QQ event received: %s", payload.get("t", "unknown"))
        super()._dispatch_payload(payload)

    def _wire_plugin_handlers(self, native=None):
        pass

    @staticmethod
    def _has_file_attachments(data):
        return bool(data.get("attachments")) or any(
            TerminalAdapter._has_file_attachments(item)
            for item in data.get("msg_elements") or [] if isinstance(item, dict))

    async def _handle_c2c_message(self, data, message_id, content, author, timestamp):
        user = str(author.get("user_openid") or "")
        if not owner.owner_id():
            # Pairing is authorized before any attachment download or terminal access.
            if not owner.bind(user, content.strip()):
                return
            self._dm_policy, self._allow_from = "allowlist", [user]
            self._chat_type_map[user] = "c2c"
            await self.send(user, bot_name() + " · 绑定完成。\n/tmux help 查看指令；私聊 /group bind 绑定群。", reply_to=message_id)
            self.panel_task = asyncio.create_task(self.sync_panel())
            logger.info("Private owner bound; terminal access enabled")
            return
        if user != owner.owner_id():
            return
        if self._has_file_attachments(data):
            await self.send(user, "终端机器人仅接收文字，不下载图片、语音或文件。", reply_to=message_id)
            return
        await super()._handle_c2c_message(data, message_id, content, author, timestamp)

    def _is_group_allowed(self, group, member=""):
        return owner.group_allowed(group, member)

    async def _handle_group_message(self, data, message_id, content, author, timestamp):
        group = str(data.get("group_openid") or "")
        member = str(author.get("member_openid") or "")
        # QQ delivers only GROUP_AT_MESSAGE_CREATE here, not ordinary group chatter.
        # Current QQ events already strip the bot mention. Only remove known
        # legacy prefixes, never an actual user prompt beginning with @someone.
        text = content.strip()
        for prefix in ("@" + bot_name(), "@" + self._app_id,
                       "<@" + self._app_id + ">", "<@!" + self._app_id + ">"):
            if text.startswith(prefix) and (len(text) == len(prefix) or text[len(prefix)].isspace()):
                text = text[len(prefix):].strip()
                break
        if text.startswith("/group bind "):
            async with self.dispatch_lock:
                if terminal_relay.active() and terminal_relay._session.get("chat_type") == "group":
                    return
                if not owner.bind_group(group, member, text):
                    return
                self.group_replies.note(group, message_id, self._parse_qq_timestamp(timestamp).timestamp())
                self._chat_type_map[group] = "group"
                await self.send(group, bot_name() + " · 绑定完成。@我 /tmux help 查看指令。", reply_to=message_id)
            return
        if not owner.group_allowed(group, member):
            binding = owner.group_binding()
            logger.warning("Group input rejected (group_match=%s, member_match=%s, author_present=%s)",
                           group == binding.get("group_openid"), member == binding.get("member_openid"), bool(member))
            return
        self.last_group_message_at = time.time()
        logger.info("Group input accepted (chars=%d, control=%s)", len(text),
                    qq_commands.terminal_command(text).strip().startswith("#tmux"))
        self.group_replies.note(group, message_id, self._parse_qq_timestamp(timestamp).timestamp())
        self._chat_type_map[group] = "group"
        if self._has_file_attachments(data):
            await self.send(group, "终端机器人仅接收文字，不下载图片、语音或文件。", reply_to=message_id)
            return
        # Keep raw content normalized too: relay intentionally reads it for literal input.
        normalized = {**data, "content": text}
        await self._ingest(normalized, message_id, text, data.get("attachments"), timestamp,
                           chat_id=group, qq_chat_type="group", user_id=member, chat_type="group")

    async def _handle_guild_message(self, *args):
        return

    async def _send_group_text(self, group, content, reply_to=None, keyboard=None):
        return await self._send_text_to("group", group, content, reply_to, keyboard)

    async def _post_message(self, path, body):
        parts = path.split("/")
        if len(parts) == 5 and parts[1:3] == ["v2", "groups"] and parts[4] == "messages":
            return await self._send_group_body(parts[3], body)
        return await super()._post_message(path, body)

    async def _send_group_body(self, group, body):
        async with self.group_send_lock:
            passive = self.group_replies.reserve(group)
            body = dict(body)
            body.pop("keyboard", None)
            if passive:
                message_id, sequence = passive
                body.update(msg_id=message_id, msg_seq=sequence)
                result = await super()._post_message(self._messages_path("group", group), body)
                logger.info("Group reply accepted (passive, slot %d/5)", sequence)
                return result
            if self.group_replies.blocked(group):
                raise RuntimeError("QQ 群主动消息未授权；待发内容保留，请允许机器人主动发言或重新 @bot。")
            body.pop("msg_id", None)
            try:
                result = await super()._post_message(self._messages_path("group", group), body)
                self.group_replies.accepted_active(group)
                logger.info("Group reply accepted (active)")
                return result
            except RuntimeError as exc:
                if "主动消息失败" in str(exc) and "权限" in str(exc):
                    if self.group_replies.denied(group) and owner.owner_id():
                        try:
                            await self._send_c2c_text(owner.owner_id(), "## 群消息受限\n\n" + bot_name()
                                + "的主动群消息被 QQ 拒绝。\n请在 QQ 群机器人权限中允许主动发言；有权限后待发内容会自动重试。\n"
                                  "普通 @回复已兼容；也可发送 /tmux list100 查看。")
                        except Exception:
                            logger.warning("Group delivery warning could not reach private owner")
                raise

    async def _handle_dm_message(self, *args):
        return

    async def handle_message(self, event):
        # Bypass the agent/debounce/busy-session pipeline entirely.
        async with self.dispatch_lock:
            await self.gateway.dispatch(event)

    async def _stt_voice_attachment(self, *args, **kwargs):
        return None

    async def send(self, chat_id, content, **kwargs):
        scope = self._activity_scope(chat_id)
        result = await super().send(chat_id, service_message(content), **kwargs)
        await self._record_sent(result, scope)
        return result

    async def send_with_keyboard(self, chat_id, text, keyboard, **kwargs):
        # Old queued replies may still supply a keyboard; never emit it again.
        return await self.send(chat_id, text, **kwargs)

    async def send_document(self, chat_id, file_path, **kwargs):
        scope = self._activity_scope(chat_id)
        result = await super().send_document(chat_id, file_path, **kwargs)
        await self._record_sent(result, scope)
        return result

    def _activity_scope(self, chat_id):
        session = terminal_relay._session
        expected = session.get("chat_id", owner.owner_id())
        return terminal_relay.activity_scope() if chat_id == expected else ""

    async def _record_sent(self, result, scope):
        if getattr(result, "success", False):
            message_id = str(getattr(result, "message_id", "") or "")
            await terminal_relay.record_activity(scope, "bot:" + message_id if message_id else "")

    async def sync_panel(self):
        # Reuse audited owner-scoped panel API; this app gets no planning/agent commands.
        qq_commands.PANEL_REMARK = "qq-tmuxbot-shortcuts-v1"
        qq_commands.PANEL_COMMANDS = ("/help", "/tmux ls")
        qq_commands.COMMANDS = tuple((name, "查看终端帮助" if name == "/help" else description, target)
                                    for name, description, target in qq_commands.COMMANDS)
        async def api(method, path, *, body=None, params=None):
            from gateway.platforms.qqbot.constants import API_BASE
            response = await self._require_http_client().request(
                method, API_BASE + path, headers=await self._auth_headers(), json=body, params=params)
            data = response.json()
            if response.is_error or not isinstance(data, dict) or data.get("code") not in {None, 0}:
                raise qq_commands.PanelAPIError(response.status_code)
            return data
        try:
            result = await qq_commands.sync_panel(api, owner.owner_id(), owner.home() / "qq-command-panel", apply=True)
            logger.info("Command panel verified: %s", result["action"])
        except Exception as exc:
            logger.warning("Command panel sync failed (%s); manual commands remain usable", type(exc).__name__)


async def run():
    owner.prepare_pairing()
    principal = owner.owner_id()
    config = PlatformConfig(enabled=True, typing_indicator=False, gateway_restart_notification=False,
                            extra={"app_id": os.environ["QQ_APP_ID"], "client_secret": os.environ["QQ_CLIENT_SECRET"],
                                   "markdown_support": True, "dm_policy": "allowlist",
                                   "allow_from": [principal] if principal else [], "group_policy": "allowlist",
                                   "group_allow_from": [owner.group_binding().get("group_openid", "")]})
    adapter = TerminalAdapter(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        if not await adapter.connect():
            raise RuntimeError("QQ terminal bot could not connect")
        logger.info("Terminal bot connected; owner_bound=%s; group_bound=%s; llm_enabled=false",
                    bool(principal), bool(owner.group_binding()))
        if principal:
            adapter.panel_task = asyncio.create_task(adapter.sync_panel())
        if principal or owner.group_binding():
            terminal_relay.handle_gateway_start(adapter.gateway)
        while not stop.is_set():
            if not adapter._running:
                raise RuntimeError("CodeX QQ connection stopped; container restart required")
            owner.write_private(owner.home() / "health.json", {
                "connected": adapter.is_connected, "owner_bound": bool(owner.owner_id()),
                "group_bound": bool(owner.group_binding()),
                "gateway_ready": bool(adapter._session_id),
                "last_dispatch_at": adapter.last_dispatch_at,
                "last_group_message_at": adapter.last_group_message_at,
                "llm_enabled": False, "heartbeat_at": time.time()})
            try:
                await asyncio.wait_for(stop.wait(), timeout=10)
            except TimeoutError:
                pass
    finally:
        for task in (terminal_relay._delivery, terminal_relay._watch_delivery):
            if task and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except (TimeoutError, Exception):
                    pass
        tasks = [t for t in (terminal_relay._watcher, terminal_relay._delivery,
                            terminal_relay._watch_delivery, adapter.panel_task,
                            ) if t and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await adapter.disconnect()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("gateway.platforms.qqbot.adapter").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
