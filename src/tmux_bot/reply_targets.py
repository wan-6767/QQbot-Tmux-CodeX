"""Resolve QQ quotes against successful terminal deliveries in the same chat."""

import hashlib
import json
import logging
import re

from . import owner

MAX_RECORDS = 2048
logger = logging.getLogger("qq_tmuxbot.quotes")


def valid_record(record):
    if not isinstance(record, dict):
        return False
    source = record.get("source")
    channel, keys, digest = record.get("channel"), record.get("keys"), record.get("digest")
    return (isinstance(source, dict)
            and set(source) == {"chat_id", "user_id", "chat_type"}
            and all(isinstance(value, str) and value for value in source.values())
            and source["chat_type"] in {"dm", "group"}
            and isinstance(channel, str) and re.fullmatch(r"[0-9]{3}", channel) is not None
            and channel != "000" and isinstance(keys, list)
            and all(isinstance(key, str) and key for key in keys)
            and isinstance(digest, str) and (not digest or re.fullmatch(r"[0-9a-f]{64}", digest) is not None))


def content_digest(value):
    # QQ may return the displayed code block without its Markdown fences.
    lines = [line for line in value.splitlines()
             if not re.fullmatch(r"\s*`{3,}(?:text)?\s*", line)]
    normalized = " ".join("\n".join(lines).split())
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ""


def quote_data(raw):
    """Use quote fields only; never infer a destination from the user's text."""
    keys, contents, explicit_ids = [], [], []
    reference = raw.get("message_reference")
    if isinstance(reference, dict):
        for field in ("message_id", "id", "msg_idx"):
            if isinstance(reference.get(field), str) and reference[field]:
                keys.append(reference[field])
                if field != "msg_idx":
                    explicit_ids.append(reference[field])
    scene = raw.get("message_scene")
    if isinstance(scene, dict) and isinstance(scene.get("ext"), list):
        for item in scene["ext"]:
            if isinstance(item, str) and item.startswith("ref_msg_idx="):
                keys.append(item.split("=", 1)[1])
    is_quote = str(raw.get("message_type", "")) == "103"
    elements = raw.get("msg_elements")
    if is_quote and isinstance(elements, list):
        for item in elements:
            if not isinstance(item, dict):
                continue
            for field in ("message_id", "msg_id", "id", "msg_idx"):
                if isinstance(item.get(field), str) and item[field]:
                    keys.append(item[field])
                    if field != "msg_idx":
                        explicit_ids.append(item[field])
            if isinstance(item.get("content"), str) and item["content"].strip():
                contents.append(item["content"])
    return bool(reference or keys or is_quote), set(keys), contents, bool(explicit_ids)


class ReplyTargets:
    def __init__(self):
        self.path = owner.home() / "tmux-relay/reply-targets.json"
        try:
            data = json.loads(self.path.read_text())
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("records"), list):
                raise ValueError("invalid reply target index")
            self.records = [record for record in data["records"][-MAX_RECORDS:] if valid_record(record)]
        except FileNotFoundError:
            self.records = []
        except (OSError, ValueError):
            # This optional shortcut must not prevent normal numbered routing
            # or output recovery if its index is damaged or unreadable.
            logger.warning("Terminal quote index unavailable; use a newly delivered terminal reply")
            self.records = []

    def note(self, result, source, channel, message):
        if (not getattr(result, "success", False)
                or not re.fullmatch(r"[0-9]{3}", channel) or channel == "000"):
            return
        response = getattr(result, "raw_response", None)
        response = response if isinstance(response, dict) else {}
        keys = [getattr(result, "message_id", ""), response.get("id"), response.get("msg_idx")]
        keys = list(dict.fromkeys(k for k in keys if isinstance(k, str) and k))
        record = {"keys": keys, "source": source.copy(), "channel": channel,
                  "digest": content_digest(message)}
        self.records = (self.records + [record])[-MAX_RECORDS:]
        owner.write_private(self.path, {"version": 1, "records": self.records})

    def resolve(self, raw, source):
        quoted, keys, contents, explicit_ids = quote_data(raw)
        candidates = [r for r in self.records if r.get("source") == source]
        matches = {r["channel"] for r in candidates if keys.intersection(r.get("keys", []))}
        # An explicit message ID is authoritative. Unknown IDs must not fall
        # back to text that could have been copied from another bot/chat.
        # Some QQ quote events expose an opaque ref_msg_idx that differs from
        # the send API's ID. Their full quoted text is the fallback receipt.
        if not matches and not explicit_ids:
            digests = {content_digest(value) for value in contents}
            if len(contents) > 1:
                digests.add(content_digest("\n".join(contents)))
            matches = {r["channel"] for r in candidates if r.get("digest") in digests}
        return quoted, next(iter(matches)) if len(matches) == 1 else None
