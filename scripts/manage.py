"""Initialize an independent, owner-only bot instance. Host side uses stdlib only."""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tmux_bot import owner


def unit_arg(value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("paths must not contain line breaks")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def private_text(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def initialize(name, port, socket):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", name):
        raise ValueError("instance name must use lowercase letters, digits and hyphens")
    if not 1024 <= port <= 65535:
        raise ValueError("bridge port must be between 1024 and 65535")
    socket = Path(socket).expanduser().resolve()
    if not socket.is_socket():
        raise ValueError("tmux socket does not exist; create a tmux session first")
    instance = ROOT / "instances" / name
    if instance.exists():
        raise ValueError("instance already exists; refusing to overwrite credentials or state")
    instance.mkdir(parents=True, mode=0o700)
    data = instance / "data"
    data.mkdir(mode=0o700)
    os.environ["HERMES_HOME"] = str(data)
    relay = data / "tmux-relay"
    relay.mkdir(mode=0o700)
    private_text(relay / "token", secrets.token_hex(32) + "\n")
    owner.write_private(relay / "client.json", {
        "url": "http://127.0.0.1:" + str(port), "host_data_root": str(data)})
    owner.prepare_pairing()
    private_text(instance / "bot.env", "QQ_APP_ID=\nQQ_CLIENT_SECRET=\n")
    private_text(instance / "compose.env", "\n".join((
        "BOT_NAME=" + name, "BRIDGE_PORT=" + str(port),
        "BOT_UID=" + str(os.getuid()), "BOT_GID=" + str(os.getgid()),
        "BOT_DATA_DIR=" + json.dumps(str(data)), "BOT_ENV_FILE=" + json.dumps(str(instance / "bot.env")),
    )) + "\n")
    locks = ROOT / "instances" / "pane-locks"
    locks.mkdir(exist_ok=True, mode=0o700)
    command = [sys.executable, "-B", str(ROOT / "src/tmux_bot/bridge.py"),
               "--token-file", str(relay / "token"), "--socket", str(socket),
               "--port", str(port), "--lock-dir", str(locks), "--bot-name", name]
    directory = str(ROOT)
    if "\n" in directory or "\r" in directory:
        raise ValueError("paths must not contain line breaks")
    unit = ("[Unit]\nDescription=QQ tmux bridge (" + name + ")\nAfter=network.target\n\n"
            "[Service]\nType=simple\nWorkingDirectory=" + directory.replace("%", "%%") + "\n"
            "ExecStart=" + " ".join(map(unit_arg, command)) + "\n"
            "Restart=on-failure\nRestartSec=3\nNoNewPrivileges=true\nUMask=0077\n\n"
            "[Install]\nWantedBy=default.target\n")
    private_text(instance / ("qq-tmux-bridge-" + name + ".service"), unit)
    return instance


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create private config and a user-systemd unit")
    init.add_argument("name", nargs="?", default="default")
    init.add_argument("--port", type=int, default=18010)
    init.add_argument("--socket", required=True)
    pairing = commands.add_parser("pairing", help="show the one-time owner binding command locally")
    pairing.add_argument("name", nargs="?", default="default")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", args.name):
            raise ValueError("invalid instance name")
        if args.command == "init":
            if os.geteuid() == 0:
                raise ValueError("run initialization as the ordinary tmux owner, not root")
            instance = initialize(args.name, args.port, args.socket)
            print("Instance initialized: " + str(instance))
            print("Set QQ_APP_ID and QQ_CLIENT_SECRET in its private bot.env.")
            print("Start its user-systemd bridge and Compose project; see README.md.")
        else:
            instance = ROOT / "instances" / args.name
            if not (instance / "data/tmux-relay/client.json").is_file():
                raise ValueError("instance is not initialized")
            os.environ["HERMES_HOME"] = str(instance / "data")
            if owner.owner_id():
                print("Owner already bound; no pairing command remains.")
            else:
                owner.prepare_pairing()
                print(json.loads((owner.home() / "pairing-instruction.json").read_text())["command"])
    except (OSError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
