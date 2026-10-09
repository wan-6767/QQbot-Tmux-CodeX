"""Initialize and migrate an independent, owner-only bot instance.

The host-side bridge uses only the Python standard library.  Bot/QQ runtime
dependencies remain in the pinned container image; this script only writes
private instance configuration and a user-systemd unit.
"""

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import socket as socket_module
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from tmux_bot import owner
from tmux_bot.bridge import RelayError, resolve_tmux_binary

INSTANCE_RE = re.compile(r"[a-z][a-z0-9-]{0,39}")


def unit_arg(value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("paths must not contain line breaks")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def private_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    try:
        path.chmod(0o600)
    except OSError:
        pass


def instance_dir(name):
    if not INSTANCE_RE.fullmatch(name):
        raise ValueError("instance name must use lowercase letters, digits and hyphens")
    return ROOT / "instances" / name


def read_json(path, default=None):
    path = Path(path)
    if not path.is_file():
        return {} if default is None else default
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError("expected a JSON object")
        return result
    except (OSError, ValueError) as exc:
        raise ValueError("invalid JSON: " + str(path) + ": " + str(exc)) from exc


def validate_socket(value):
    path = Path(value).expanduser().resolve()
    if "\n" in str(path) or "\r" in str(path):
        raise ValueError("tmux socket path must not contain line breaks")
    if not path.is_socket():
        raise ValueError("tmux socket does not exist; create a tmux session first: " + str(path))
    return str(path)


def validate_lock_dir(value):
    path = Path(value).expanduser().resolve()
    if "\n" in str(path) or "\r" in str(path):
        raise ValueError("lock directory path must not contain line breaks")
    return str(path)


def validate_port(port, current=None, check_listener=True):
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("bridge port must be between 1024 and 65535")
    instances = ROOT / "instances"
    if instances.is_dir():
        for candidate in instances.iterdir():
            config = candidate / "data/tmux-relay/client.json"
            if not config.is_file() or (current is not None and candidate == Path(current)):
                continue
            other = read_json(config)
            if other.get("url") == "http://127.0.0.1:" + str(port):
                raise ValueError("bridge port " + str(port) + " is already assigned to instance " + candidate.name)
    if check_listener:
        probe = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
        try:
            probe.settimeout(0.25)
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise ValueError("bridge port " + str(port) + " is unavailable: " + str(exc)) from exc
        finally:
            probe.close()
    return port


def configured_tmux_binary(value):
    try:
        return resolve_tmux_binary(value)
    except (OSError, ValueError, RelayError) as exc:
        raise ValueError("tmux binary is not usable: " + str(exc)) from exc


def settings(name, port, socket_path, tmux_binary=None, idle_seconds=1800, lock_dir=None,
             usage_port=18014, bot_name=None, current=None, check_listener=True):
    instance_dir(name)
    port = validate_port(port, current=current, check_listener=check_listener)
    socket_path = validate_socket(socket_path)
    tmux_binary = configured_tmux_binary(tmux_binary)
    if not isinstance(idle_seconds, int) or not 60 <= idle_seconds <= 86400:
        raise ValueError("idle timeout must be between 60 and 86400 seconds")
    if not isinstance(usage_port, int) or not 1024 <= usage_port <= 65535:
        raise ValueError("Sub2API usage port must be between 1024 and 65535")
    if usage_port == port:
        raise ValueError("Sub2API usage port must differ from the bridge port")
    bot_name = name if bot_name is None else bot_name
    if (not isinstance(bot_name, str) or not bot_name.strip() or len(bot_name) > 80
            or any(ord(char) < 32 for char in bot_name)):
        raise ValueError("bot display name must contain 1-80 printable characters")
    lock_dir = validate_lock_dir(lock_dir or (ROOT / "instances" / "pane-locks"))
    return {
        "version": 1,
        "instance": name,
        "bot_name": bot_name,
        "port": port,
        "socket": socket_path,
        "tmux_binary": tmux_binary,
        "idle_seconds": idle_seconds,
        "usage_port": usage_port,
        "lock_dir": lock_dir,
        "bind": "127.0.0.1",
    }


def compose_environment(name, data, instance, config):
    return "\n".join((
        "BOT_NAME=" + json.dumps(config["bot_name"], ensure_ascii=False),
        "BRIDGE_PORT=" + str(config["port"]),
        "BOT_UID=" + str(os.getuid()),
        "BOT_GID=" + str(os.getgid()),
        "BOT_DATA_DIR=" + json.dumps(str(data)),
        "BOT_ENV_FILE=" + json.dumps(str(instance / "bot.env")),
        "BRIDGE_SOCKET=" + json.dumps(config["socket"]),
        "TMUX_BINARY=" + json.dumps(config["tmux_binary"]),
        "BRIDGE_IDLE_SECONDS=" + str(config["idle_seconds"]),
        "BRIDGE_LOCK_DIR=" + json.dumps(config["lock_dir"]),
        "SUB2API_USAGE_URL=" + json.dumps(
            "http://127.0.0.1:" + str(config["usage_port"]) + "/v1/sub2api/usage"),
    )) + "\n"


def service_text(name, relay, config):
    command = [sys.executable, "-B", str(ROOT / "src/tmux_bot/bridge.py"),
               "--token-file", str(relay / "token"), "--socket", config["socket"],
               "--tmux-binary", config["tmux_binary"], "--port", str(config["port"]),
               "--idle-seconds", str(config["idle_seconds"]), "--lock-dir", config["lock_dir"],
               "--bot-name", config["bot_name"]]
    directory = str(ROOT)
    if "\n" in directory or "\r" in directory:
        raise ValueError("paths must not contain line breaks")
    return ("[Unit]\nDescription=QQ tmux bridge (" + name + ")\n"
            "After=network.target\nStartLimitIntervalSec=60\nStartLimitBurst=3\n\n"
            "[Service]\nType=simple\nWorkingDirectory=" + directory.replace("%", "%%") + "\n"
            "ExecStart=" + " ".join(map(unit_arg, command)) + "\n"
            "Restart=on-failure\nRestartSec=3\nRestartPreventExitStatus=2\n"
            "NoNewPrivileges=true\nUMask=0077\n\n"
            "[Install]\nWantedBy=default.target\n")


def write_runtime(name, config, create_private=False):
    instance = instance_dir(name)
    data = instance / "data"
    relay = data / "tmux-relay"
    instance.mkdir(parents=True, exist_ok=True, mode=0o700)
    data.mkdir(mode=0o700, exist_ok=True)
    relay.mkdir(mode=0o700, exist_ok=True)
    os.environ["HERMES_HOME"] = str(data)
    if create_private:
        private_text(relay / "token", secrets.token_hex(32) + "\n")
        private_text(instance / "bot.env", "QQ_APP_ID=\nQQ_CLIENT_SECRET=\n")
        owner.prepare_pairing()
    owner.write_private(relay / "bridge.json", config)
    owner.write_private(relay / "client.json", {
        "url": "http://127.0.0.1:" + str(config["port"]),
        "host_data_root": str(data),
    })
    locks = Path(config["lock_dir"])
    locks.mkdir(parents=True, exist_ok=True, mode=0o700)
    private_text(instance / "compose.env", compose_environment(name, data, instance, config))
    private_text(instance / ("qq-tmux-bridge-" + name + ".service"),
                 service_text(name, relay, config))
    return instance


def initialize(name, port, socket_path, tmux_binary=None, idle_seconds=1800, lock_dir=None,
               usage_port=18014, bot_name=None):
    instance = instance_dir(name)
    if instance.exists():
        raise ValueError("instance already exists; refusing to overwrite credentials or state")
    config = settings(name, port, socket_path, tmux_binary, idle_seconds, lock_dir, usage_port, bot_name)
    return write_runtime(name, config, create_private=True)


def existing_settings(name, overrides):
    instance = instance_dir(name)
    data = instance / "data/tmux-relay"
    client = read_json(data / "client.json")
    bridge = read_json(data / "bridge.json")
    if not client:
        raise ValueError("instance is not initialized")
    try:
        old_port = int(str(client["url"]).rsplit(":", 1)[1])
    except (KeyError, ValueError, IndexError) as exc:
        raise ValueError("instance client.json has no valid local bridge port") from exc
    socket_path = overrides["socket"] or bridge.get("socket")
    if not socket_path:
        raise ValueError("migration requires --socket; the old instance has no saved tmux socket")
    binary = overrides["tmux_binary"] if overrides["tmux_binary"] is not None else bridge.get("tmux_binary")
    port = overrides["port"] if overrides["port"] is not None else old_port
    idle = overrides["idle_seconds"] if overrides["idle_seconds"] is not None else int(bridge.get("idle_seconds", 1800))
    lock_dir = overrides["lock_dir"] if overrides["lock_dir"] is not None else bridge.get("lock_dir")
    # A generated default lock directory belongs to the repository, not the
    # old machine. Preserve explicit shared paths, but relocate the default.
    old_data = client.get("host_data_root")
    if overrides["lock_dir"] is None and lock_dir and isinstance(old_data, str):
        old_path = Path(old_data)
        if len(old_path.parents) >= 3:
            old_default = old_path.parents[2] / "instances/pane-locks"
            if Path(lock_dir) == old_default:
                lock_dir = None
    usage_port = (overrides["usage_port"] if overrides["usage_port"] is not None
                  else int(bridge.get("usage_port", 18014)))
    bot_name = overrides["bot_name"] or bridge.get("bot_name")
    if bot_name is None and (instance / "compose.env").is_file():
        for line in (instance / "compose.env").read_text().splitlines():
            if line.startswith("BOT_NAME="):
                value = line.split("=", 1)[1]
                bot_name = json.loads(value) if value.startswith('"') else value
                break
    # Reconfiguring the current listener is safe; a changed port gets a real bind check.
    check_listener = port != old_port
    return settings(name, port, socket_path, binary, idle, lock_dir, usage_port, bot_name,
                    current=instance, check_listener=check_listener)


def configure(name, port=None, socket_path=None, tmux_binary=None, idle_seconds=None, lock_dir=None,
              usage_port=None, bot_name=None):
    overrides = {"port": port, "socket": socket_path, "tmux_binary": tmux_binary,
                 "idle_seconds": idle_seconds, "lock_dir": lock_dir, "usage_port": usage_port,
                 "bot_name": bot_name}
    config = existing_settings(name, overrides)
    return write_runtime(name, config, create_private=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create private config and a user-systemd unit")
    init.add_argument("name", nargs="?", default="default")
    init.add_argument("--port", type=int, default=18010)
    init.add_argument("--socket", required=True)
    init.add_argument("--tmux-binary", default=None, help="absolute path or PATH name; defaults to TMUX_BINARY/tmux")
    init.add_argument("--idle-seconds", type=int, default=1800)
    init.add_argument("--lock-dir", default=None)
    init.add_argument("--usage-port", type=int, default=18014)
    init.add_argument("--bot-name", default=None)
    config = commands.add_parser("configure", help="migrate or regenerate an existing instance")
    config.add_argument("name", nargs="?", default="default")
    config.add_argument("--port", type=int, default=None)
    config.add_argument("--socket", default=None)
    config.add_argument("--tmux-binary", default=None)
    config.add_argument("--idle-seconds", type=int, default=None)
    config.add_argument("--lock-dir", default=None)
    config.add_argument("--usage-port", type=int, default=None)
    config.add_argument("--bot-name", default=None)
    show = commands.add_parser("show", help="show non-secret instance runtime configuration")
    show.add_argument("name", nargs="?", default="default")
    pairing = commands.add_parser("pairing", help="show the one-time owner binding command locally")
    pairing.add_argument("name", nargs="?", default="default")
    hosts = commands.add_parser("hosts", help="validate local SSH server config without displaying keys")
    hosts.add_argument("name", nargs="?", default="default")
    args = parser.parse_args()
    try:
        if os.geteuid() == 0 and args.command in {"init", "configure"}:
            raise ValueError("run initialization as the ordinary tmux owner, not root")
        if args.command == "init":
            instance = initialize(args.name, args.port, args.socket, args.tmux_binary,
                                  args.idle_seconds, args.lock_dir, args.usage_port, args.bot_name)
            print("Instance initialized: " + str(instance))
            print("Set QQ_APP_ID and QQ_CLIENT_SECRET in its private bot.env.")
            print("Enable the generated user unit with: systemctl --user enable --now "
                  + str(instance / ("qq-tmux-bridge-" + args.name + ".service")))
        elif args.command == "configure":
            instance = configure(args.name, args.port, args.socket, args.tmux_binary,
                                  args.idle_seconds, args.lock_dir, args.usage_port, args.bot_name)
            print("Instance configuration regenerated: " + str(instance))
            print("Existing token, bot.env, pairing and state were preserved.")
            print("Reload/restart the bridge and recreate its QQ container to apply changes.")
        else:
            instance = instance_dir(args.name)
            data = instance / "data/tmux-relay"
            if not (data / "client.json").is_file():
                raise ValueError("instance is not initialized")
            os.environ["HERMES_HOME"] = str(instance / "data")
            if args.command == "show":
                print(json.dumps(read_json(data / "bridge.json"), ensure_ascii=False, indent=2))
            elif args.command == "hosts":
                from tmux_bot.remote import load_hosts
                servers = load_hosts(data / "hosts.json")
                print("SSH config validated: " + ", ".join(s["name"] for s in servers))
            elif owner.owner_id():
                print("Owner already bound; no pairing command remains.")
            else:
                owner.prepare_pairing()
                print(json.loads((owner.home() / "pairing-instruction.json").read_text())["command"])
    except (OSError, ValueError, RelayError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
