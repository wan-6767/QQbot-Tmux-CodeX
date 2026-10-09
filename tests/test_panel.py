import copy
import json
from pathlib import Path
import tempfile
import unittest

from tmux_bot import qq_commands as panel

COMMANDS = ("/tmux ls", "/tmux sel", "/tmux help")


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_exact_three_commands_keep_owner_private_scope(self):
        payload = panel.panel_payload("owner")
        self.assertEqual(panel.PANEL_COMMANDS, COMMANDS)
        self.assertEqual([i["name"] for i in payload["panel"]["items"]], ["tmux ls", "tmux sel", "tmux help"])
        self.assertEqual(payload["scope"], "c2c")
        self.assertEqual(payload["target_type"], "specific")
        self.assertEqual(payload["user_openids"], ["owner"])
        self.assertNotIn("group_openids", payload)

    async def test_existing_panel_is_updated_then_read_back_without_duplicate_creation(self):
        detail = {"panel_id": "original", **panel.panel_payload("owner", ("/help", "/tmux ls"))}
        calls = []
        async def api(method, path, **kwargs):
            calls.append((method, path))
            if path == "/v2/panels":
                return {"records": [copy.deepcopy(detail)], "is_end": True}
            if method == "PUT":
                detail["panel"] = copy.deepcopy(kwargs["body"]["panel"])
                return {}
            return copy.deepcopy(detail)
        result = await panel.sync_panel(api, "owner", self.root, apply=True, commands=COMMANDS)
        self.assertEqual(result["action"], "update")
        self.assertTrue(result["read_back_verified"])
        self.assertEqual(result["commands"], list(COMMANDS))
        self.assertNotIn(("POST", "/v2/panels"), calls)
        self.assertEqual(panel.selected_commands(self.root, "owner"), COMMANDS)
        self.assertEqual(len(list((self.root / "backups").glob("*.json"))), 1)
        again = await panel.sync_panel(api, "owner", self.root, apply=True, commands=COMMANDS)
        self.assertEqual(again["action"], "unchanged")

    async def test_ambiguous_managed_panels_are_not_modified(self):
        payload = panel.panel_payload("owner")
        mutations = []
        async def api(method, path, **kwargs):
            if method != "GET": mutations.append(method)
            return {"records": [{"panel_id": name, **payload} for name in ("a", "b")], "is_end": True}
        with self.assertRaises(ValueError):
            await panel.sync_panel(api, "owner", self.root, apply=True)
        self.assertEqual(mutations, [])

    def test_invalid_or_duplicate_shortcuts_are_rejected(self):
        for commands in ((), ("/unknown",), ("/tmux sel", "/tmux sel")):
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                panel.panel_payload("owner", commands)
