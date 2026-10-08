"""Persist genuine, bounded QQ group reply windows; never invent message IDs."""

import json
import time

from . import owner


class GroupReplies:
    def __init__(self):
        self.path = owner.home() / "group-replies.json"
        try:
            self.state = json.loads(self.path.read_text())
        except FileNotFoundError:
            self.state = {}

    def note(self, group, message_id, timestamp):
        if not group or not message_id:
            return
        previous = self.state.get(group, {})
        if previous.get("message_id") == message_id:
            return
        self.state = {group: {"message_id": message_id, "at": min(timestamp, time.time()), "used": 0,
                              "blocked_at": previous.get("blocked_at", 0), "notified": previous.get("notified", False)}}
        owner.write_private(self.path, self.state)

    def reserve(self, group):
        entry = self.state.get(group, {})
        if entry.get("message_id") and 0 <= time.time() - entry["at"] < 290 and entry["used"] < 5:
            # Reserve before POST: a crash/timeout cannot reuse an uncertain sequence.
            entry["used"] += 1
            owner.write_private(self.path, self.state)
            return entry["message_id"], entry["used"]
        return None

    def blocked(self, group):
        blocked_at = self.state.get(group, {}).get("blocked_at", 0)
        return bool(blocked_at and time.time() - blocked_at < 60)

    def denied(self, group):
        entry = self.state.setdefault(group, {})
        entry["blocked_at"] = time.time()
        notify = not entry.get("notified", False)
        entry["notified"] = True
        owner.write_private(self.path, self.state)
        return notify

    def accepted_active(self, group):
        entry = self.state.get(group)
        if entry:
            entry["blocked_at"], entry["notified"] = 0, False
            owner.write_private(self.path, self.state)
