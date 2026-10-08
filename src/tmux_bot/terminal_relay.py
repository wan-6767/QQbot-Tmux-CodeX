"""Direct owner QQ terminal mode, consumed before journal/model dispatch."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import json
import logging
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import urllib.request
import urllib.parse

from . import terminal_files
from .qq_commands import terminal_command

logger = logging.getLogger("hermes.terminal_relay")
_watcher = None
_delivery = None
_watch_delivery = None
_generation = 0
_screens = {}
_content_screens = {}
_dialogs = {}
_pending_bodies = {}
_ui_states = {}
_turn_states = {}
_session = {}
_closing_notice = None
_checkpoint_root = None
_checkpoint_payload = None
_CHECKPOINT_VERSION = 7
_keyboard_available = True


class TerminalKeyboard:
    """C2C controls; the owner gateway hook, not a client user-ID ACL, authorizes input."""

    def __init__(self, scope: str, owner: str, reconnect: bool = False, *, compact: bool = False):
        self.scope, self.owner, self.reconnect, self.compact = scope, owner, reconnect, compact

    def to_dict(self):
        rows = [
            [("↑", "up"), ("↓", "down"), ("←", "left"), ("→", "right")],
            [("Enter", "enter"), ("Esc", "esc"), ("Tab", "tab"), ("Space", "space")],
            [("Backspace", "backspace"), ("Ctrl+C", "ctrl-c"), ("List100", "list100"), ("Exit", "exit")],
        ]
        if self.compact:
            rows = [[("List100", "list100"), ("Exit", "exit")]]
        if self.reconnect:
            rows = [[("Reconnect", "reconnect")]]
        return {"content": {"rows": [{"buttons": [{
            "id": action,
            "render_data": {"label": label, "visited_label": label, "style": 1},
            "action": {"type": 2, "data": (f"#tmux reconnect {self.scope}" if self.reconnect
                                             else f"#tmux button {self.scope} {action}"),
                       "enter": True, "reply": True,
                       "permission": {"type": 2},
                       "unsupport_tips": "请使用 /tmux help 中的文字命令"},
        } for label, action in row]} for row in rows]}}


class PaneListKeyboard:
    def to_dict(self):
        return {"content": {"rows": [{"buttons": [{
            "id": "tmux-list",
            "render_data": {"label": "/tmux ls", "visited_label": "/tmux ls", "style": 1},
            "action": {"type": 2, "data": "/tmux ls", "enter": True, "reply": True,
                       "permission": {"type": 2}, "unsupport_tips": "请发送 /tmux ls"},
        }]}]}}


class UploadKeyboard:
    def to_dict(self):
        return {"content": {"rows": [{"buttons": [{
            "id": "clear-upload-files",
            "render_data": {"label": "Clear files", "visited_label": "Clear files", "style": 1},
            "action": {"type": 2, "data": "#tmux files clear", "enter": True, "reply": True,
                       "permission": {"type": 2}, "unsupport_tips": "请发送 #tmux files clear"},
        }]}]}}


class PaneKeyboard:
    """Two compact rows of owner-gated choices, bound to the original pane identity."""

    def __init__(self, choices: list[dict], owner: str):
        self.choices, self.owner = choices[:8], owner

    def to_dict(self):
        buttons = [{
            "id": f"pane-{index}",
            "render_data": {"label": item["label"], "visited_label": item["label"], "style": 1},
            "action": {"type": 2, "data": "#tmux pick " + item["token"], "enter": True, "reply": True,
                       "permission": {"type": 2},
                       "unsupport_tips": "请用 #tmux select 编号 进入"},
        } for index, item in enumerate(self.choices)]
        return {"content": {"rows": [{"buttons": buttons[index:index + 4]}
                                     for index in range(0, len(buttons), 4)]}}


def paragraph_bodies(text: str) -> list[str]:
    bodies, lines, fence = [], [], None
    for line in text.splitlines():
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
        if not line.strip() and fence is None:
            if lines:
                bodies.append("\n".join(lines))
                lines = []
        else:
            lines.append(line.rstrip())
    if lines:
        bodies.append("\n".join(lines))
    return bodies


def message_bodies(text: str) -> list[str]:
    return paragraph_bodies(text)


_DIALOG_TITLE = re.compile(
    r"^(?:select (?:model|reasoning|effort|permission|approval)|"
    r"(?:configure |change )?(?:permissions?|approvals?|sandbox)|"
    r"(?:would you like|do you want|allow |approve |confirm )|"
    r"(?:enter|type) (?:a |the |new )?(?:name|path|message|value|url|text|title)\b|"
    r"(?:选择|切换)(?:模型|推理|权限)|权限设置|确认操作)", re.IGNORECASE,
)
_DIALOG_OPTION = re.compile(r"^\s*[›>❯→]?\s*(?:\d+[.)]|\[[ xX]\])\s+\S")
_SLASH_OPTION = re.compile(r"^\s*[›>❯→]?\s*/[a-z][a-z0-9_-]*\s+\S", re.IGNORECASE)


def dialog_block(viewport: str) -> str | None:
    """Recognize visible selection dialogs by their navigation footer and body."""
    lines = viewport.splitlines()
    nonempty = [index for index, line in enumerate(lines) if line.strip()]
    if not nonempty:
        return None
    end = nonempty[-1]
    footer = lines[end].strip().lower()
    if not (re.search(r"\b(?:enter|return)\b|回车", footer)
            and re.search(r"\besc(?:ape)?\b", footer)
            and re.search(r"\b(?:select|confirm|submit|continue|accept|choose|back|cancel|close)\b|确认|选择|返回|取消", footer)):
        return None
    titles = [i for i in range(end) if _DIALOG_TITLE.search(lines[i].strip())]
    if titles:
        start = titles[-1]
    else:
        options = [i for i in range(end) if _DIALOG_OPTION.match(lines[i]) or _SLASH_OPTION.match(lines[i])]
        if len(options) < 2:
            return None
        start = options[0]
        # Include an unfamiliar menu's adjacent title, but not preceding chat history.
        if start and lines[start - 1].strip():
            start -= 1
        elif start >= 2 and re.match(r"^\s*[›>❯]\s*/[a-z0-9_-]*\s*$", lines[start - 2], re.IGNORECASE):
            start -= 2
    return "\n".join(lines[start:end + 1])


_BUSY = re.compile(
    r"esc(?:ape)? to (?:interrupt|cancel)|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]|"
    r"(?m:^\s*[•◦] Browsing(?: the)? web\b)", re.IGNORECASE,
)
_COMPLETION = re.compile(
    r"^(?:(?:Worked|Thought) for \d[\d.hms ]*(?:[•·] .*)?|"
    r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d+ at \d+:\d+|"
    r"• (?:Interrupted|Turn aborted|Request cancelled|Error\b).*)$", re.IGNORECASE,
)
_WORKED = re.compile(r"^Worked for \d[\d.hms ]*(?:[•·] .*)?$", re.IGNORECASE)
_CODEX_CHROME = re.compile(
    r"^(?:GPT-[\w.-]+.*[·]|\??\s*(?:for shortcuts|\d+% context left)|"
    r"(?:Worked|Thought) for \d[\d.hms ]*(?:[•·] .*)?$|"
    r".*\bf2 to view\b|(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AP]M)?)$|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d+ at \d+:\d+)", re.IGNORECASE,
)
_QUEUED = re.compile(r"(?m)^• Queued follow-up inputs\n(?:[ \t]+\S[^\n]*(?:\n|$))+")
_TOOL_RECORD = re.compile(
    r"^[•◦] (?:(?:Ran|Explored|Edited|Added|Deleted|Called|Calling|Updated Plan|"
    r"Opened|Opening|Clicked|Clicking|Viewed|Viewing|Fetched|Fetching|"
    r"Searched(?: for|(?: the)? web)|Searching(?: for|(?: the)? web))(?:[ \t]|$)|"
    r"Browsing(?: the)? web(?=[ \t•◦]|$)|"
    r"(?:Failed|Succeeded|Completed|Exited|Finished) \(exit(?: code)? [+-]?\d+\)(?:[ \t]|$))"
)
_TOOL_DETAILS = re.compile(r"^[ \t]*\+ Show (?:details|full output|more)[ \t]*$")
_TOOL_FOLD = re.compile(r"^[ \t]*\+ \d+ (?:more )?lines? \(ctrl\+[a-z] to expand\)[ \t]*$", re.IGNORECASE)
_TOOL_DIFF = re.compile(r"^[ \t]*[└├] .+\(\+\d+ -\d+\)[ \t]*$")
_TOOL_DIFF_ROW = re.compile(r"^[ \t]{2,}\d+[ \t]+[+-]")
_TOOL_DIFF_TREE = re.compile(r"^[ \t]*[└├] .+")
_TOOL_DIFF_COUNT = re.compile(r"\(\+\d+ -\d+\)[ \t]*$")
_HTTP_ERROR = re.compile(
    r"\b(?:HTTP(?:/\d(?:\.\d)?)?|status(?:\s+code)?|error(?:\s+code)?)\s*[:=]?\s*(?:429|5\d{2})\b|"
    r"\b(?:429\s+Too Many Requests|503\s+Service Unavailable)\b|"
    r"(?:error|failed|failure|错误|失败|报错).*?\b(?:429|503)\b|"
    r"\b(?:429|503)\b.*?(?:error|failed|failure|错误|失败|报错)", re.IGNORECASE,
)


def operational_error(line: str) -> bool:
    if _TOOL_DIFF_ROW.match(line):
        return False
    if _TOOL_RECORD.match(line) and not re.match(r"^[•◦] Failed\b", line):
        return False
    return bool(re.match(r"^\s*■(?:\s|$)", line) or _HTTP_ERROR.search(line) or re.match(
        r"^\s*(?:■|⚠|• Error\b).*?(?:error|failed|failure|错误|失败|报错)", line, re.IGNORECASE))


def safe_error(line: str) -> str:
    try:
        from agent.redact import redact_sensitive_text
        line = redact_sensitive_text(line)
    except ImportError:
        pass
    line = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", line)
    line = re.sub(r"(\bBearer\s+)\S+", r"\1[REDACTED]", line, flags=re.IGNORECASE)
    line = re.sub(r"([?&](?:key|token|api_key|access_token|secret)=)[^&\s]+",
                  r"\1[REDACTED]", line, flags=re.IGNORECASE)
    return re.sub(r'''(\b(?:api_key|apikey|access_token|client_secret|password|cookie)\b["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''',
                  r"\1[REDACTED]", line, flags=re.IGNORECASE)


def terminal_busy(viewport: str) -> bool:
    lines = viewport.splitlines()
    last_busy = max((i for i, line in enumerate(lines) if _BUSY.search(line)), default=-1)
    last_completion = max((i for i, line in enumerate(lines) if _COMPLETION.match(line.strip())), default=-1)
    return last_busy > last_completion


def tool_diff_start(lines: list[str], index: int) -> bool:
    line = lines[index]
    if _TOOL_DIFF.match(line):
        return True
    if not _TOOL_DIFF_TREE.match(line):
        return False
    # Rename paths and their counters may wrap into several physical TUI rows.
    if re.search(r"\S+(?:/|\.[a-zA-Z0-9]+).* (?:→|->) ", line):
        return True
    for following in lines[index + 1:index + 17]:
        if not following.strip() or following.lstrip().startswith(("• ", "›", "```", "~~~", "└", "├")):
            break
        if _TOOL_DIFF_COUNT.search(following):
            return True
        if _TOOL_DIFF_ROW.match(following):
            break
    return False


def prose_content(lines: list[str], *, codex: bool, complete_only: bool) -> str:
    """Codex tool items own all their child lines, up to the next top-level item."""
    output, tool, anchored, fence, tail_closed = [], False, not codex, None, False
    block_start, paragraph_start = 0, 0
    for index, line in enumerate(lines):
        if fence is None and operational_error(line):
            if output and output[-1].strip():
                output.append("")
            output.extend([safe_error(line), ""])
            # A visible failure is a complete event, even in a hidden tool block.
            tool, anchored, tail_closed = True, True, True
            block_start = paragraph_start = len(output)
            continue
        if fence is None and _TOOL_DETAILS.match(line):
            continue
        if fence is None and _TOOL_RECORD.match(line):
            tool = True
            tail_closed = bool(output)
            continue
        if fence is None and (tool_diff_start(lines, index) or _TOOL_DIFF_ROW.match(line)):
            tool = True
            tail_closed = bool(output)
            continue
        if fence is None and _TOOL_FOLD.match(line):
            # A folded tool result may be cropped after its title. Its preceding
            # partial output is not prose, even if it happens to contain "OK".
            if not tool:
                del output[max(block_start, paragraph_start):]
            tool = True
            tail_closed = bool(output)
            continue
        if fence is None and line.startswith("• "):
            if output and output[-1].strip():
                output.append("")
            tool, anchored = False, True
            block_start = len(output)
        if fence is None and line.strip() and (not output or not output[-1].strip()):
            paragraph_start = len(output)
        if tool or not anchored:
            continue
        output.append(line)
        if line.strip():
            tail_closed = False
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
    text = "\n".join(output).strip("\n")
    if complete_only and not tail_closed:
        text = "\n\n".join(paragraph_bodies(text)[:-1])
    return text


def terminal_ui(viewport: str) -> dict:
    if dialog_block(viewport) is not None:
        return {"queue": "", "editor": ""}
    lines = [line.rstrip() for line in viewport.splitlines()]
    prompts = [i for i, line in enumerate(lines) if line.startswith("›")]
    if not prompts:
        return {"queue": "", "editor": ""}
    queue = _QUEUED.search("\n".join(lines))
    editor = []
    for line in lines[prompts[-1]:]:
        if not line.strip():
            break
        editor.append(line)
    text = "\n".join(editor)
    if text in {"›", "› Ask Codex to do anything"}:
        text = "› （空）"
    return {"queue": queue.group(0).strip() if queue else "", "editor": text}


def seed_screen(result: dict) -> None:
    """Restore the comparison baseline without replaying history on a client restart."""
    key = result["screen_key"]
    baseline = result.get("baseline", result["screen"])
    _screens[key] = baseline
    _content_screens[key] = clean_terminal_content(baseline)
    viewport = result.get("viewport", result["screen"])
    _ui_states[key] = terminal_ui(viewport)
    menu = dialog_block(viewport)
    if menu is not None:
        _dialogs[key] = menu
    busy = terminal_busy(viewport)
    _turn_states[key] = {"running": busy, "awaiting": busy,
                         "serial": result.get("input_serial", 0), "pending": busy,
                         "completion_before": completion_marker(baseline),
                         "completion_cleared": busy and not completion_marker(baseline),
                         "completion_armed": busy, "completion_generation": 0,
                         "prompt_signature": prompt_signature(baseline)}
    checkpoint()


async def deliver_ui(event, gateway, result: dict, show_editor: bool = False) -> bool:
    key = result["screen_key"]
    current = terminal_ui(result.get("viewport", result["screen"]))
    previous = _ui_states.get(key, {"queue": "", "editor": ""})
    text = ""
    editing = False
    if current["queue"] and current["queue"] != previous["queue"]:
        text = "Codex · 消息已排队，尚未开始。"
    elif show_editor and current["editor"] and current["editor"] != previous["editor"]:
        text = current["editor"]
        editing = True
    if text and not await send_terminal(event, gateway, result["target"], text,
                                        scope=result.get("selection_token", ""), full_controls=editing):
        return False
    _ui_states[key] = current
    checkpoint()
    return True


def clean_terminal_content(screen: str, *, complete_only: bool = False) -> str:
    """Extract prose for automatic delivery; entry and explicit snapshots stay raw."""
    screen = "\n".join(line.rstrip() for line in screen.splitlines())
    original_lines, menu_lines = screen.splitlines(), set()
    for end, line in enumerate(original_lines):
        if "esc" not in line.lower():
            continue
        menu = dialog_block("\n".join(original_lines[:end + 1]))
        if menu is not None:
            menu_lines.update(range(end + 1 - len(menu.splitlines()), end + 1))
    screen = "\n".join(line for index, line in enumerate(original_lines) if index not in menu_lines)
    codex = bool(re.search(r"(?m)^›(?: |$)", screen))
    if codex:
        screen = _QUEUED.sub("", screen)
    raw_lines = current_frame_lines(screen)
    lines, input_line = [], False
    for line in raw_lines:
        text = line.strip()
        if codex and line.startswith("›"):
            input_line = True
            continue
        if input_line:
            if not text:
                input_line = False
            else:
                continue
        if codex and not operational_error(line) and (
                _CODEX_CHROME.search(text) or (_BUSY.search(text) and not _TOOL_RECORD.match(line))):
            continue
        lines.append(line)
    return prose_content(lines, codex=codex, complete_only=complete_only)


def root() -> Path:
    return Path(os.environ.get("HERMES_HOME", "/opt/data")) / "tmux-relay"


def checkpoint(event=None, result: dict | None = None) -> None:
    global _checkpoint_payload
    if event is not None and result is not None and "epoch" in result:
        _session.update(epoch=result["epoch"], chat_id=event.source.chat_id,
                        user_id=event.source.user_id,
                        chat_type=getattr(event.source, "chat_type", "dm"),
                        selection_token=result.get("selection_token", ""))
        if "screen" in result and "handled" in result:
            _session["entry"] = result
    data = {"version": _CHECKPOINT_VERSION, "session": _session, "closing_notice": _closing_notice,
            "screens": _screens, "content": _content_screens, "dialogs": _dialogs,
            "pending": _pending_bodies, "ui": _ui_states, "turns": _turn_states}
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    if _checkpoint_payload == (root(), payload):
        return
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=root(),
                                     prefix=".delivery-", delete=False) as stream:
        path = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
            path.replace(root() / "delivery.json")
            _checkpoint_payload = (root(), payload)
        finally:
            path.unlink(missing_ok=True)


def load_checkpoint() -> None:
    global _checkpoint_root, _closing_notice
    if _checkpoint_root == root():
        return
    _checkpoint_root = root()
    try:
        data = json.loads((root() / "delivery.json").read_text())
    except FileNotFoundError:
        return
    for target, field in ((_screens, "screens"), (_content_screens, "content"),
                          (_dialogs, "dialogs"), (_pending_bodies, "pending"),
                          (_ui_states, "ui"), (_turn_states, "turns"), (_session, "session")):
        target.clear()
        target.update(data[field])
    _closing_notice = data.get("closing_notice")
    if data.get("version", 1) < _CHECKPOINT_VERSION:
        def with_errors(raw, previous):
            known = paragraph_bodies(clean_terminal_content(previous))
            bodies = paragraph_bodies(clean_terminal_content(raw))
            return "\n\n".join(body for body in bodies if body in known or operational_error(body.splitlines()[0]))

        if data.get("version", 1) == 5:
            # New error visibility must not replay acknowledged history or
            # absorb a still-buffered prose tail from the last sampled screen.
            for key, content in _content_screens.items():
                _content_screens[key] = with_errors(_screens.get(key, content), content)
        # Filter legacy queued batches as a whole, so a tool's children cannot
        # leak when their header was in an earlier, already acknowledged batch.
        for key, batch in _pending_bodies.items():
            bodies, sent = batch["bodies"], batch["next"]
            previous = _content_screens.get(key, clean_terminal_content(_screens.get(key, "")))
            acknowledged = clean_terminal_content(previous + "\n\n" + "\n\n".join(bodies[:sent]))
            filtered = clean_terminal_content(previous + "\n\n" + "\n\n".join(bodies))
            content = clean_terminal_content(batch["content"])
            if data.get("version", 1) == 5:
                content = with_errors(batch["baseline"], content)
                filtered = content
            batch.update(content=content,
                         bodies=message_bodies(screen_delta(acknowledged, filtered)), next=0)
        for key, content in _content_screens.items():
            _content_screens[key] = clean_terminal_content(content)
        checkpoint()


def clear_delivery() -> None:
    global _closing_notice
    for state in (_screens, _content_screens, _dialogs, _pending_bodies, _ui_states,
                  _turn_states, _session):
        state.clear()
    _closing_notice = None
    checkpoint()


def observe_turn(result: dict) -> None:
    key = result["screen_key"]
    state = _turn_states.setdefault(key, {"running": False, "awaiting": False, "serial": 0})
    if dialog_block(result.get("viewport", result["screen"])) is not None:
        return
    before = state.copy()
    baseline = result.get("baseline", result["screen"])
    previous = _screens.get(key, "")
    state.setdefault("completion_armed", bool(state.get("pending") or state.get("awaiting") or state["running"]))
    signature = prompt_signature(baseline)
    old_signature = state.get("prompt_signature", prompt_signature(previous))
    new_prompt = bool(signature[0] and (signature[0] > old_signature[0] or signature[1] != old_signature[1]))
    busy = terminal_busy(result.get("viewport", result["screen"]))
    if (busy and not state["running"]) or new_prompt:
        state["awaiting"] = True
        state["body_delivered"] = False
        state["pending"] = True
        state["completion_before"] = completion_marker(previous)
        state["completion_cleared"] = False
        state["completion_armed"] = True
        state["completion_generation"] = state.get("completion_generation", 0) + 1
    if (state.get("pending") or state["completion_armed"]) and not completion_marker(baseline):
        state["completion_cleared"] = True
    state["prompt_signature"] = signature
    state["running"] = busy
    if state != before:
        checkpoint()


def current_frame_lines(screen: str) -> list[str]:
    lines = screen.splitlines()
    if re.search(r"(?m)^›(?: |$)", screen):
        # Ignore erased full frames, not repeated questions within a live frame.
        footers = [i for i, line in enumerate(lines)
                   if re.match(r"^GPT-[\w.-]+.*[·]", line.strip())]
        boundaries = [i + 1 for i in footers if any(
            line.strip() and not _CODEX_CHROME.search(line.strip()) for line in lines[i + 1:])]
        if boundaries:
            lines = lines[boundaries[-1]:]
    return lines


def submitted_prompts(lines: list[str]) -> list[int]:
    menu = dialog_block("\n".join(lines))
    menu_end = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    menu_start = menu_end + 1 - len(menu.splitlines()) if menu else len(lines)
    prompts = [i for i, line in enumerate(lines) if line.startswith("›")
               and not (menu_start <= i <= menu_end)
               and line.strip() not in {"›", "› Ask Codex to do anything"}]
    # The last composer may contain a draft, not a submitted question.
    if prompts and any(re.match(r"^GPT-[\w.-]+.*[·]", line.strip(), re.IGNORECASE)
                       for line in lines[prompts[-1] + 1:]):
        tail = lines[prompts[-1] + 1:]
        if not any(_COMPLETION.match(line.strip()) or line.lstrip().startswith("•")
                   or _BUSY.search(line) for line in tail):
            prompts.pop()
    return prompts


def prompt_signature(screen: str) -> list:
    lines = current_frame_lines(screen)
    prompts = submitted_prompts(lines)
    return [len(prompts), lines[prompts[-1]].rstrip() if prompts else ""]


def completion_marker(screen: str) -> str:
    lines = current_frame_lines(screen)
    prompts = submitted_prompts(lines)
    start = prompts[-1] if prompts else 0
    markers = [line.strip() for line in lines[start:] if _COMPLETION.match(line.strip())]
    return f"{len(prompts)}:{markers[-1]}" if markers else ""


def worked_footer(screen: str) -> str:
    """Only accept the terminal's trailing duration, not code or tool output."""
    lines = current_frame_lines(screen)
    prompts = submitted_prompts(lines)
    start = prompts[-1] + 1 if prompts else 0
    fence, candidate = None, ""
    for line in lines[start:]:
        text = line.strip()
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
            candidate = ""
            continue
        if fence is not None:
            continue
        if _WORKED.fullmatch(text) and len(line) - len(line.lstrip()) <= 2:
            candidate = text
        elif line.startswith("›"):
            break
        elif text and not _CODEX_CHROME.search(text):
            candidate = ""
    return candidate


def completion_notice(result: dict, settled: bool) -> str:
    state = _turn_states.get(result["screen_key"], {})
    if not settled or state.get("running") or not state.get("completion_armed"):
        return ""
    baseline = result.get("baseline", result["screen"])
    if not state.get("completion_cleared") and completion_marker(baseline) == state.get("completion_before"):
        return ""
    return worked_footer(baseline)


def terminal_settled(result: dict) -> bool:
    viewport = result.get("viewport", result["screen"])
    if terminal_busy(viewport):
        return False
    state = _turn_states.get(result["screen_key"], {})
    if not state.get("pending") or not re.search(r"(?im)^\s*GPT-[\w.-]+.*[·]", viewport):
        return True
    marker = completion_marker(result.get("baseline", result["screen"]))
    return bool(marker and (state.get("completion_cleared") or marker != state.get("completion_before")))


def latest_turn(screen: str) -> tuple[str, str]:
    lines = current_frame_lines(screen)
    prompts = submitted_prompts(lines)
    if not prompts:
        return "", ""
    start = prompts[-1]
    return lines[start].rstrip(), clean_terminal_content("\n".join(lines[start:]))


def bridge_url() -> str:
    config = json.loads((root() / "client.json").read_text())
    url = config["url"]
    expected = os.environ.get("TMUX_BRIDGE_URL", "http://127.0.0.1:18010")
    parsed = urllib.parse.urlsplit(expected)
    if (url != expected or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or not parsed.port or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment):
        raise RuntimeError("unexpected tmux bridge endpoint")
    return url


def request(path: str, payload: dict) -> dict:
    token = (root() / "token").read_text().strip()
    url = bridge_url()
    req = urllib.request.Request(
        url + path, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=4) as response:
        return json.load(response)


def activity_scope() -> str:
    return _session.get("selection_token", "") if active() else ""


async def record_activity(scope: str, event_id: str = "") -> bool:
    if not scope:
        return False
    try:
        result = await asyncio.to_thread(request, "/v1/activity", {
            "selection_token": scope, "event_id": event_id,
        })
        return bool(result.get("recorded"))
    except Exception:
        logger.warning("QQ activity could not be recorded by terminal bridge")
        return False


def save_active(active: bool, pending_exit: bool = False) -> None:
    path = root() / "active.json"
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        os.chmod(temp, 0o600)
        json.dump({"active": active, "pending_exit": pending_exit}, stream)
    temp.replace(path)


def active() -> bool:
    try:
        return bool(json.loads((root() / "active.json").read_text())["active"])
    except (OSError, ValueError, KeyError):
        return False


def pending_exit() -> bool:
    try:
        return bool(json.loads((root() / "active.json").read_text()).get("pending_exit"))
    except (OSError, ValueError):
        return False


def control(text: str) -> bool:
    value = terminal_command(text).strip()
    return value == "#help" or value == "#tmux" or value.startswith("#tmux ")


def format_screen(target: str, text: str) -> str:
    # Remove terminal right-padding without changing indentation or blank lines.
    text = "\n".join(line.rstrip() for line in text.splitlines())
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", text)), default=0))
    return f"[{target}]\n{fence}text\n{text or '(终端暂无输出)'}\n{fence}"


async def send_terminal(event, gateway, target: str, text: str, prefix: str = "", scope: str = "", *,
                        full_controls: bool = False) -> bool:
    adapter = gateway._adapter_for_source(event.source)
    if adapter is None:
        return False
    message = format_screen(target, text)
    if prefix:
        message = prefix + "\n\n" + message
    keyboard = (TerminalKeyboard(scope, event.source.user_id, compact=not full_controls)
                if scope and getattr(adapter, "terminal_keyboards_enabled", True) else None)
    if len(message) <= getattr(adapter, "MAX_MESSAGE_LENGTH", 4000):
        success = await send(event, gateway, message, scope=scope, proactive=True, keyboard=keyboard)
        if success:
            has_keyboard = keyboard is not None and _keyboard_available and hasattr(adapter, "send_with_keyboard")
            rows = len(keyboard.to_dict()["content"]["rows"]) if has_keyboard else 0
            logger.info("Terminal frame accepted (controls=%s, rows=%d)",
                        ("full" if full_controls else "compact") if has_keyboard else "none", rows)
        return success
    # One complete attachment avoids both QQ's text splitter and silent truncation.
    directory = root().parent / "workspace" / "tmux-relay"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt",
                                         dir=directory, delete=False) as stream:
            path = Path(stream.name)
            stream.write("\n".join(line.rstrip() for line in text.splitlines()))
        caption = f"[{target}] 完整终端文本（超出单条文字限制，未拆分或截断）"
        if prefix:
            caption = prefix + "\n\n" + caption
        result = await adapter.send_document(event.source.chat_id, str(path),
                                            caption=caption, file_name="terminal-context.txt",
                                            reply_to=None)
        success = bool(getattr(result, "success", False))
        if success:
            if keyboard:
                await send(event, gateway, f"[{target}] 终端快捷操作", scope=scope, proactive=True, keyboard=keyboard)
        return success
    except Exception:
        logger.warning("Complete terminal attachment delivery failed")
        return False
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def unwrap_prose(text: str) -> str:
    """Join TUI prose continuation rows, not Markdown structure or raw snapshots."""
    lines = text.splitlines()
    output, fence, previous, list_indent = [], None, "", None
    structural = re.compile(r"^\s*(?:#{1,6}\s|>\s?|\|)|^\s*(?:[-*_]\s*){3,}$")
    list_start = re.compile(r"^(\s*)(?:[-+*•]\s|\d+[.)]\s)")
    for line in lines:
        stripped = line.strip()
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        item = list_start.match(line)
        indentation = len(line) - len(line.lstrip(" "))
        list_continuation = list_indent is not None and list_indent < indentation < list_indent + 4
        if marker:
            output.append(line)
            if fence is None:
                fence = marker.group(1)
            elif marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence):
                fence = None
        elif (fence is not None or not stripped or not output or not output[-1].strip() or item or
              structural.match(line) or structural.match(previous) or
              (line.startswith(("    ", "\t")) and not list_continuation) or
              (previous.startswith(("    ", "\t")) and not list_continuation) or
              (list_start.match(previous) and not list_continuation) or previous.endswith("\\")):
            output.append(line)
        else:
            left = output[-1].rstrip()
            # Chinese wraps need no extra space; Latin word boundaries do.
            cjk = lambda char: "\u2e80" <= char <= "\ua4cf" or "\uac00" <= char <= "\ud7af" or "\uf900" <= char <= "\ufaff"
            url_tail = re.search(r"https?://\S+$", left)
            url_continuation = url_tail and not re.search(r"\s", stripped) and not stripped.startswith(("http://", "https://"))
            separator = "" if url_continuation or cjk(left[-1]) or cjk(stripped[0]) else " "
            output[-1] = left + separator + stripped
        if item:
            list_indent = len(item.group(1))
        elif not stripped or (not list_continuation and fence is None):
            list_indent = None
        previous = line
    return "\n".join(output)


async def flush_bodies(event, gateway, key: str) -> bool:
    batch = _pending_bodies[key]
    while batch["next"] < len(batch["bodies"]):
        text = unwrap_prose(batch["bodies"][batch["next"]])
        if not await send_terminal(event, gateway, batch["target"], text, scope=batch["scope"]):
            return False
        batch["next"] += 1
        checkpoint()
    if batch.get("completion"):
        if not await send_terminal(event, gateway, batch["target"], batch["completion"], scope=batch["scope"]):
            return False
        state = _turn_states.get(key, {})
        if state.get("completion_generation", 0) == batch.get("completion_generation", 0):
            state["completion_armed"] = False
        batch.pop("completion")
        checkpoint()
    _screens[key] = batch["baseline"]
    _content_screens[key] = batch["content"]
    _dialogs.pop(key, None)
    del _pending_bodies[key]
    checkpoint()
    return True


def screen_delta(previous: str, current: str, *, paragraphs: bool = False) -> str:
    """Keep inserted/repainted lines, not the common scrollback or unchanged TUI."""
    old = paragraph_bodies(previous) if paragraphs else [line.rstrip() for line in previous.splitlines()]
    new = paragraph_bodies(current) if paragraphs else [line.rstrip() for line in current.splitlines()]
    changes = []
    for operation, _, _, start, end in SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if operation in {"insert", "replace"}:
            changes.extend(new[start:end])
    return ("\n\n" if paragraphs else "\n").join(changes).strip("\n")


async def deliver_screen(event, gateway, result: dict, snapshot: bool = False, settled: bool = True) -> bool:
    key = result["screen_key"]
    observe_turn(result)
    delivered_pending = False
    if not snapshot and key in _pending_bodies:
        if not await flush_bodies(event, gateway, key):
            return False
        delivered_pending = True
    baseline = result.get("baseline", result["screen"])
    previous = _screens.get(key)
    content = clean_terminal_content(baseline, complete_only=not settled and not snapshot)
    dialog = dialog_block(result.get("viewport", result["screen"]))
    full_context = snapshot and result.get("initial_context", False)
    if full_context:
        text = result["screen"]
    elif dialog is not None:
        if not snapshot and _dialogs.get(key) == dialog:
            _screens[key] = baseline
            checkpoint()
            return False
        text = dialog
    elif snapshot:
        text = result["screen"]
    elif previous is None:
        # A failed entry snapshot must not cause the watcher to resend old history.
        return False
    else:
        previous_content = _content_screens.get(key, clean_terminal_content(previous))
        text = screen_delta(previous_content, content,
                            paragraphs=bool(re.search(r"(?m)^›(?: |$)", baseline)))
        turn = _turn_states[key]
        if not text and settled and dialog is None:
            old_prompt, old_answer = latest_turn(previous)
            new_prompt, new_answer = latest_turn(baseline)
            if new_answer and (
                (turn["awaiting"] and not turn.get("body_delivered"))
                or (old_prompt and new_prompt != old_prompt and old_answer == new_answer)
            ):
                # A cropped TUI can replace the entire previous turn with an identical answer.
                text = new_answer
        completion = completion_notice(result, settled)
        if not text and not completion:
            _screens[key] = baseline
            if settled:
                turn["awaiting"] = False
                turn["pending"] = False
                _content_screens[key] = content
            checkpoint()
            return delivered_pending
        batches = message_bodies(text)
        if text:
            turn["body_delivered"] = True
        if settled:
            turn["awaiting"] = False
            turn["pending"] = False
        _pending_bodies[key] = {"target": result["target"], "baseline": baseline,
                                "content": content,
                                "bodies": batches, "next": 0,
                                "completion": completion,
                                "completion_generation": turn.get("completion_generation", 0),
                                "scope": result.get("selection_token", "")}
        checkpoint()
        return await flush_bodies(event, gateway, key)
    if not await send_terminal(event, gateway, result["target"], text, result.get("message", ""),
                               scope=result.get("selection_token", ""), full_controls=snapshot or dialog is not None):
        return False
    # Advance only after delivery; unsuccessful messages remain available for retry.
    if snapshot:
        _screens.clear()
        _content_screens.clear()
        _dialogs.clear()
        _pending_bodies.clear()
        _ui_states.clear()
        _turn_states.clear()
        _session.pop("entry", None)
    _screens[key] = baseline
    if snapshot:
        _ui_states[key] = terminal_ui(result.get("viewport", result["screen"]))
    if snapshot or dialog is None:
        _content_screens[key] = content
    if dialog is not None:
        _dialogs[key] = dialog
    else:
        _dialogs.pop(key, None)
    busy = terminal_busy(result.get("viewport", result["screen"]))
    _turn_states.setdefault(key, {"running": busy, "awaiting": busy, "pending": busy,
                                 "serial": result.get("input_serial", 0),
                                 "completion_before": completion_marker(baseline),
                                 "completion_cleared": busy and not completion_marker(baseline),
                                 "completion_armed": busy, "completion_generation": 0,
                                 "prompt_signature": prompt_signature(baseline)})
    checkpoint()
    return True


async def send(event, gateway, message: str, scope: str = "", *, proactive: bool = False,
               reconnect: bool = False, keyboard=None) -> bool:
    global _keyboard_available
    adapter = gateway._adapter_for_source(event.source)
    if adapter is None:
        return False
    try:
        # Screen updates are independent C2C pushes, not more replies to one inbound id.
        reply_to = None if proactive else getattr(event, "message_id", None)
        if ((scope or keyboard) and getattr(adapter, "terminal_keyboards_enabled", True)
                and _keyboard_available and hasattr(adapter, "send_with_keyboard")):
            result = await adapter.send_with_keyboard(event.source.chat_id, message,
                         keyboard or TerminalKeyboard(scope, event.source.user_id, reconnect, compact=True), reply_to=reply_to)
            error = str(getattr(result, "error", "") or "").lower()
            if not getattr(result, "success", False) and ("304057" in error or "not allowd custom keyborad" in error):
                _keyboard_available = False
                logger.warning("QQ custom keyboards are not enabled; keeping complete text replies")
                result = await adapter.send(event.source.chat_id, message, reply_to=reply_to)
        else:
            result = await adapter.send(event.source.chat_id, message, reply_to=reply_to)
        if not getattr(result, "success", False):
            logger.warning("Terminal reply delivery failed")
        success = bool(getattr(result, "success", False))
        return success
    except Exception:
        logger.warning("Terminal reply delivery failed")
        return False


async def watch(event, gateway, epoch: int, entry: dict | None = None, show_editor: bool = False) -> None:
    global _watch_delivery, _closing_notice
    candidate = None
    candidate_since = 0.0
    retry_at = 0.0
    failures = 0
    loop = asyncio.get_running_loop()
    try:
        await asyncio.sleep(0.25)
        while True:
            if _closing_notice is not None:
                notice = _closing_notice
                _watch_delivery = asyncio.create_task(send(event, gateway, notice["message"], proactive=True,
                    scope=notice.get("reconnect_token", ""), reconnect=True))
                if not await asyncio.shield(_watch_delivery):
                    await asyncio.sleep(5)
                    continue
                clear_delivery()
                return
            try:
                result = await asyncio.to_thread(request, "/v1/screen", {"epoch": epoch})
            except Exception:
                failures += 1
                logger.warning("Terminal screen refresh unavailable; retrying")
                await asyncio.sleep(min(60, 2 ** min(failures, 6)))
                continue
            if not result.get("active"):
                if result.get("reason") in {"idle", "closed", "claimed"}:
                    save_active(False)
                    _closing_notice = result
                    checkpoint()
                    continue
                return
            if result.get("redrawing"):
                # capture-pane history and viewport are two reads. Never combine
                # an unfinished body from one frame with an idle footer from the next.
                candidate = None
                await asyncio.sleep(0.5)
                continue
            menu = dialog_block(result.get("viewport", ""))
            observe_turn(result)
            ui = terminal_ui(result.get("viewport", result.get("screen", "")))
            settled = terminal_settled(result)
            screen = (menu if menu is not None else clean_terminal_content(
                          result.get("screen", ""), complete_only=not settled),
                      ui["queue"], ui["editor"] if show_editor else "", settled,
                      completion_notice(result, settled) if menu is None else "")
            now = loop.time()
            if screen != candidate:
                candidate, candidate_since = screen, now
            stable_after = 0.3 if menu is not None else 2.0
            if now >= retry_at and now - candidate_since >= stable_after:
                first = entry is not None or result["screen_key"] not in _screens
                if first:
                    if entry is None:
                        seed_screen(result)
                        await asyncio.sleep(0.5)
                        continue
                    result = entry
                async def deliver_frame():
                    sent = await deliver_screen(event, gateway, result, snapshot=first,
                        settled=terminal_settled(result))
                    # Keep queue/editor feedback separate from assistant paragraphs.
                    ui_sent = await deliver_ui(event, gateway, result, show_editor=show_editor)
                    return sent, ui_sent
                _watch_delivery = asyncio.create_task(deliver_frame())
                # A new keypress cancels polling, never a POST already in flight.
                sent, ui_sent = await asyncio.shield(_watch_delivery)
                if first and sent:
                    entry = None
                if not ui_sent or (not sent and (result["screen_key"] in _pending_bodies or first)):
                    failures += 1
                    retry_at = now + min(60, 2 ** min(failures + 1, 6))
                else:
                    failures = 0
                    retry_at = now + 0.5
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        return
    except Exception:
        logger.warning("Terminal screen refresh failed")


async def resume(gateway) -> None:
    global _watcher, _closing_notice
    load_checkpoint()
    if not _session or (not active() and _closing_notice is None):
        return
    from gateway.config import Platform
    from gateway.session import SessionSource
    if hasattr(gateway, "_terminal_recovery_source"):
        source = gateway._terminal_recovery_source(_session)
        if source is None:
            logger.warning("Terminal recovery refused: binding changed")
            return
    else:
        from .owner import owner_id
        owner = owner_id()
        if (_session["user_id"] != owner or _session["chat_id"] != owner
                or _session.get("chat_type", "dm") != "dm"):
            logger.warning("Terminal recovery refused: private owner changed")
            return
        source = SessionSource(platform=Platform.QQBOT, chat_id=owner, user_id=owner)
    event = SimpleNamespace(source=source, message_id=None)
    if not gateway._is_user_authorized_for_source(event.source):
        return
    while _watcher is None or _watcher.done():
        if not active() and _closing_notice is None:
            return
        try:
            result = await asyncio.to_thread(request, "/v1/state", {})
        except Exception:
            await asyncio.sleep(5)
            continue
        if _watcher is not None and not _watcher.done():
            return
        if result.get("active"):
            if _session.get("selection_token") != result.get("selection_token"):
                clear_delivery()
            entry = _session.get("entry")
            if result["screen_key"] not in _screens and entry is None:
                seed_screen(result)
            checkpoint(event, result)
            save_active(True)
            _watcher = asyncio.create_task(watch(event, gateway, result["epoch"], entry=entry))
            logger.info("Terminal observer restored with persisted delivery progress")
        elif _closing_notice or result.get("reason") in {"idle", "closed", "claimed"}:
            if _closing_notice is None:
                _closing_notice = result
                checkpoint()
            save_active(False)
            _watcher = asyncio.create_task(watch(event, gateway, _session["epoch"]))
        else:
            save_active(False)
            clear_delivery()
        return


def handle_gateway_start(gateway, **_kwargs) -> None:
    if (root() / "client.json").is_file():
        asyncio.get_running_loop().create_task(resume(gateway))


def handle(event, gateway) -> bool:
    global _watcher, _delivery, _generation
    if not (root() / "client.json").is_file():
        return False
    raw = getattr(event, "raw_message", None)
    text = raw.get("content") if isinstance(raw, dict) else None
    if not isinstance(text, str):
        text = str(getattr(event, "text", "") or "")
    command = text.strip().lower()
    if command == "/help":
        return False
    text = terminal_command(text)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return control(text) or active()
    try:
        load_checkpoint()
        was_active = active()
        if pending_exit():
            request("/v1/route", {"text": "#tmux exit"})
            save_active(False)
        media = bool(getattr(event, "media_urls", None)) or (
            isinstance(raw, dict) and bool(raw.get("attachments"))
        )
        uploads = (getattr(event, "metadata", None) or {}).get("qqbot_cached_attachments")
        if active() and media and uploads:
            try:
                message = terminal_files.receipt(event, root())
            except (OSError, ValueError):
                logger.warning("Terminal upload receipt could not be saved")
                message = "无法确认缓存路径，请重新发送文件。"
            result = {"handled": True, "active": True, "message": message, "upload_receipt": True}
        elif text.strip() == "#tmux files clear":
            message = "仅限 tmux 模式。"
            if active():
                try:
                    message = terminal_files.clear(root())
                except (OSError, ValueError):
                    logger.warning("Terminal upload cleanup failed")
                    message = "清理失败，未清空文件记录。"
            result = {"handled": True, "active": active(), "message": message}
        elif active() and (media or not text.strip()) and not control(text):
            result = {"handled": True, "active": True, "message": "终端模式只转发文字；/tmux exit 退出。"}
        else:
            if active() and not _screens:
                # Read before forwarding input, so an immediate reply cannot be
                # mistaken for old history when the plugin has just restarted.
                before = request("/v1/state", {})
                if before.get("active") and "screen" in before:
                    seed_screen(before)
            result = request("/v1/route", {
                "text": text, "message_id": str(getattr(event, "message_id", "") or ""),
            })
        if was_active and not result.get("handled"):
            result = {"handled": True, "active": False,
                      "message": "终端连接已重置，消息未输入终端。请 /tmux ls 重新进入。"}
        save_active(bool(result.get("active")))
    except Exception:
        receipt = None
        try:
            receipt = request("/v1/receipt", {"message_id": str(getattr(event, "message_id", "") or "")})
        except Exception:
            pass
        if receipt and receipt.get("status") == "complete":
            result = receipt["result"]
            save_active(bool(result.get("active")))
        elif text.strip() == "#tmux exit":
            save_active(False, pending_exit=True)
            result = {"handled": True, "exited": True,
                      "message": "已在本地退出终端模式，后续消息恢复 Hermes；桥接服务恢复后会清除旧连接。"}
        elif not control(text) and not active():
            return False
        else:
            result = {"handled": True, "message": "提交状态暂无法确认；请先用 /tmux list100 核对，勿重复发送。/tmux exit 退出。"}
    if not result.get("handled"):
        return False
    if result.get("duplicate"):
        return True
    if result.get("active") and "epoch" in result:
        checkpoint(event, result)
        if result.get("submitted") and _screens:
            for key, turn in _turn_states.items():
                if turn.get("serial") != result.get("input_serial"):
                    # Menus and queued messages are not completed assistant turns.
                    turn["serial"] = result.get("input_serial", 0)
                    turn["awaiting"] = bool(turn.get("running"))
                    if key not in _dialogs:
                        turn["pending"] = True
                        turn["completion_armed"] = True
                        turn["completion_before"] = completion_marker(_screens.get(key, ""))
                        turn["completion_cleared"] = False
            checkpoint()
    restart_watch = "epoch" in result or result.get("active") is False
    if restart_watch:
        _generation += 1
    generation = _generation
    if restart_watch and _watcher and not _watcher.done():
        _watcher.cancel()
    previous = _delivery
    previous_watch = _watch_delivery

    async def deliver():
        global _watcher, _closing_notice
        if previous and not previous.done():
            await asyncio.shield(previous)
        if previous_watch and not previous_watch.done():
            await asyncio.shield(previous_watch)
        if generation != _generation and not result.get("upload_receipt"):
            return
        message = result.get("message", "")
        if "screen" in result:
            await deliver_screen(event, gateway, result, snapshot=True)
        elif not result.get("quiet"):
            if len(message) > 3600 and not result.get("upload_receipt"):
                message = message[:300] + "\n[只显示屏幕末尾]\n" + message[-3100:]
            keyboard = UploadKeyboard() if result.get("upload_receipt") else PaneListKeyboard() if result.get("exited") else (
                PaneKeyboard(result["pane_shortcuts"], event.source.user_id)
                if result.get("pane_shortcuts") else None)
            sent = await send(event, gateway, message,
                       scope=result.get("reconnect_token") or result.get("selection_token", ""),
                       reconnect=bool(result.get("reconnect_token")),
                       keyboard=keyboard)
            if result.get("active") is False:
                if not sent and result.get("reason") in {"idle", "closed", "claimed"}:
                    _closing_notice = result
                    checkpoint(event, result)
                    _watcher = loop.create_task(watch(event, gateway, result["epoch"]))
                else:
                    clear_delivery()
        if result.get("active") and "epoch" in result and generation == _generation:
            parts = text.strip().split()
            editing = (len(parts) >= 3 and parts[:2] == ["#tmux", "type"]) or (
                len(parts) >= 3 and parts[:2] in (["#tmux", "key"], ["#tmux", "button"])
                and parts[-1].lower() in {"backspace", "delete", "space", "left", "right", "home", "end"})
            _watcher = loop.create_task(watch(event, gateway, result["epoch"],
                entry=_session.get("entry"), show_editor=editing))

    _delivery = loop.create_task(deliver())
    return True
