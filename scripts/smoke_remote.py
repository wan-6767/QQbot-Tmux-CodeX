"""Exercise real SSH + isolated local/remote tmux servers, never existing panes."""

import argparse
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tmux_bot.bridge import Tmux
from tmux_bot.multiplex import MultiRelay
from tmux_bot.remote import RemoteTmux, TmuxFleet, load_hosts

ECHO = "import sys,json; print('READY',flush=True);\nfor line in sys.stdin: print('• received: '+json.dumps(line.rstrip('\\n'),ensure_ascii=False),flush=True)"
SETUP = """import json,subprocess,sys
p=json.load(sys.stdin)
assert p['socket'].startswith('/tmp/qq-tmux-accept-')
if p['action']=='create':
 subprocess.run(['tmux','-S',p['socket'],'new-session','-d','-s','acceptance','-x','120','-y','35',p['command']],check=True)
elif p['action']=='remove':
 subprocess.run(['tmux','-S',p['socket'],'kill-server'],check=False)
else: raise ValueError('invalid operation')
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-alias", help="read an existing verified SSH alias; never change SSH config")
    parser.add_argument("--hosts-file", type=Path)
    parser.add_argument("--server", default="s2")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="qt-accept-") as folder:
        root = Path(folder)
        if args.ssh_alias:
            result = subprocess.run(["ssh", "-G", args.ssh_alias], capture_output=True, text=True, check=True)
            values = {}
            for line in result.stdout.splitlines():
                key, _, value = line.partition(" ")
                values.setdefault(key, value)
            item = {"name": args.server, "host": values["hostname"], "port": int(values["port"]),
                    "user": values["user"], "identity_file": values["identityfile"],
                    "known_hosts_file": values["userknownhostsfile"].split()[0]}
            path = root / "hosts.json"
            path.touch(mode=0o600)
            path.write_text(json.dumps({"version": 1, "servers": [item]}))
            settings = load_hosts(path)[0]
        else:
            settings = next(s for s in load_hosts(args.hosts_file) if s["name"] == args.server)
        settings["socket"] = "/tmp/qq-tmux-accept-" + secrets.token_hex(8) + ".sock"
        remote = RemoteTmux(settings, root / "ssh")
        admin = remote.command[:-1] + [shlex.join(["python3", "-c", SETUP])]
        echo = shlex.join(["python3", "-u", "-c", ECHO])
        local_socket = str(root / "local.sock")
        local = Tmux(local_socket)
        source = {"chat_type": "dm", "chat_id": "acceptance", "user_id": "acceptance"}
        relay = None
        try:
            subprocess.run(admin, input=json.dumps({"action": "create", "socket": settings["socket"], "command": echo}).encode(), capture_output=True, check=True, timeout=20)
            subprocess.run(["tmux", "-S", local_socket, "new-session", "-d", "-s", "acceptance", "-x", "120", "-y", "35", echo], check=True)
            fleet = TmuxFleet(local, [settings], root / "ssh")
            relay = MultiRelay(fleet, root / "state")
            listing = relay.route("/tmux ls", "list", source)
            assert "local" in listing["message"] and args.server in listing["message"] and not fleet.errors, fleet.errors
            records = relay.db.execute("SELECT id,pane FROM terminals ORDER BY id").fetchall()
            assert len(records) == 2
            numbers = {json.loads(p)["server"]: n for n, p in records}
            for name, number in numbers.items():
                response = relay.route(f"/tmux sel {number} ent", "enter-" + number, source)
                assert response["active"] and name in response["target"]
                marker = "测试-" + secrets.token_hex(5) + "-$(touch NEVER_RUN);'"
                response = relay.route(f"/tmux sel {number} send {marker}", "input-" + number, source)
                assert response["submitted"]
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    capture = relay.screen(number, response["epoch"])
                    if "received: " + json.dumps(marker, ensure_ascii=False) in capture["screen"]:
                        break
                    time.sleep(.1)
                else:
                    raise AssertionError("real terminal did not receive the literal payload")
                duplicate = relay.route(f"/tmux sel {number} send {marker}", "input-" + number, source)
                assert duplicate["duplicate"]
                assert relay.screen(number, response["epoch"])["screen"].count("received: " + json.dumps(marker, ensure_ascii=False)) == 1
            relay.route(f"/tmux sel {numbers['local']} ext", "leave-local", source)
            assert relay._child(numbers[args.server]).selected
            print("PASS: real local + SSH tmux listing, independent literal input, deduplication and selective disconnect")
        finally:
            if relay:
                relay.close()
            subprocess.run(["tmux", "-S", local_socket, "kill-server"], capture_output=True)
            subprocess.run(admin, input=json.dumps({"action": "remove", "socket": settings["socket"]}).encode(), capture_output=True, timeout=20)


if __name__ == "__main__":
    main()
