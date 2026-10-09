"""Strict, key-authenticated SSH transport for the same tmux implementation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

from .bridge import KEYS, KEY_NAMES, RelayError, Tmux, normalize_key, resolve_tmux_binary


def worker_source():
    # The remote uses the exact tested Tmux class; prompts travel only as JSON stdin.
    return ("import json,os,re,shutil,subprocess,sys,time\nfrom pathlib import Path\n"
            "class RelayError(Exception): pass\nKEYS=" + repr(KEYS) + "\nKEY_NAMES=" + repr(KEY_NAMES) + "\n" + inspect.getsource(resolve_tmux_binary) + "\n" + inspect.getsource(normalize_key) + "\n" + inspect.getsource(Tmux) +
            "\np=json.load(sys.stdin)\n"
            "try:\n"
            " if p['method'] not in ('panes','capture','viewport','send','key'): raise ValueError('invalid operation')\n"
            " t=Tmux(p.get('socket'), p.get('binary'))\n"
            " result=getattr(t,p['method'])(*p.get('args',[]))\n"
            " print(json.dumps({'ok':True,'result':result}))\n"
            "except (RelayError,ValueError,OSError) as e:\n"
            " print(json.dumps({'ok':False,'error':str(e)}))\n")


class RemoteTmux:
    def __init__(self, settings, control_dir):
        self.settings = settings
        signature = json.dumps(settings, sort_keys=True)
        control_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if control_dir.is_symlink() or control_dir.stat().st_uid != os.getuid() or stat.S_IMODE(control_dir.stat().st_mode) != 0o700:
            raise ValueError("unsafe SSH control directory")
        socket = str(control_dir / ("cm-" + hashlib.sha256(signature.encode()).hexdigest()[:12]))
        # OpenSSH appends a random suffix when atomically creating the socket.
        # Long installation paths need a private, instance-isolated short path.
        if len(socket.encode()) > 85:
            short = Path("/tmp") / ("qq-tmux-ssh-" + str(os.getuid()))
            for directory in (short, short / hashlib.sha256(str(control_dir.resolve()).encode()).hexdigest()[:12]):
                directory.mkdir(exist_ok=True, mode=0o700)
                if directory.is_symlink() or directory.stat().st_uid != os.getuid() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
                    raise ValueError("unsafe SSH control directory")
            socket = str(directory / ("cm-" + hashlib.sha256(signature.encode()).hexdigest()[:12]))
        self.command = ["ssh", "-F", "/dev/null", "-T", "-p", str(settings["port"]),
            "-i", settings["identity_file"], "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + settings["known_hosts_file"],
            "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ConnectTimeout=4", "-o", "ConnectionAttempts=1",
            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
            "-o", "ControlMaster=auto", "-o", "ControlPersist=60", "-o", "ControlPath=" + socket,
            settings["user"] + "@" + settings["host"], shlex.join(["python3", "-c", worker_source()])]

    def rpc(self, method, *args):
        payload = {"method": method, "args": args, "socket": self.settings.get("socket"),
                   "binary": self.settings.get("tmux_binary")}
        try:
            result = subprocess.run(self.command, input=json.dumps(payload).encode(), capture_output=True, timeout=12)
            if result.returncode:
                raise RelayError("SSH连接失败：请检查网络、密钥权限和已核验的 known_hosts；未放宽主机校验。")
            data = json.loads(result.stdout)
            if not data["ok"]:
                raise RelayError(data["error"])
            return data["result"]
        except (subprocess.TimeoutExpired, OSError, ValueError, KeyError) as exc:
            raise RelayError("SSH响应超时或格式无效；输入状态请先用 /tmux sel 编号 tail 100 核对，不要重复提交。") from exc

    def panes(self):
        return self.rpc("panes")

    def capture(self, pane, lines=40):
        return self.rpc("capture", pane, lines)

    def viewport(self, pane):
        return self.rpc("viewport", pane)

    def send(self, pane, value, enter=True):
        return self.rpc("send", pane, value, enter)

    def key(self, pane, key):
        return self.rpc("key", pane, key)


def load_hosts(path):
    if not path.exists():
        return []
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("hosts.json must be private (chmod 600)")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("servers"), list):
        raise ValueError("invalid hosts config schema")
    hosts, names, endpoints = [], {"local"}, set()
    for raw in data["servers"]:
        if not isinstance(raw, dict):
            raise ValueError("invalid SSH server")
        if raw.get("enabled", True) is False:
            continue
        item = {**raw, "port": raw.get("port", 22)}
        if (not isinstance(item.get("name"), str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", item["name"])
                or item["name"] in names or not isinstance(item.get("host"), str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", item["host"])
                or not isinstance(item.get("user"), str) or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_.-]{0,63}", item["user"])
                or isinstance(item["port"], bool) or not isinstance(item["port"], int) or not 1 <= item["port"] <= 65535):
            raise ValueError("invalid SSH name, host, user or port")
        socket = item.get("socket")
        if socket is not None and (not isinstance(socket, str) or not socket.startswith("/") or any(ord(c) < 32 for c in socket)):
            raise ValueError("remote socket must be an absolute path")
        binary = item.get("tmux_binary")
        if binary is not None and (not isinstance(binary, str) or not binary.startswith("/")
                                   or any(ord(c) < 32 for c in binary)):
            raise ValueError("remote tmux_binary must be an absolute path")
        for key in ("identity_file", "known_hosts_file"):
            if not isinstance(item.get(key), str):
                raise ValueError("SSH identity_file and known_hosts_file are required")
            file = Path(item[key]).expanduser().resolve()
            if not file.is_file():
                raise ValueError("SSH file does not exist")
            if key == "identity_file" and (stat.S_IMODE(file.stat().st_mode) & 0o077 or file.stat().st_uid != os.getuid()):
                raise ValueError("SSH private key must belong to the bridge user with mode 600 or 400")
            item[key] = str(file)
        endpoint = (item["host"].lower(), item["port"], item["user"], socket)
        if endpoint in endpoints:
            raise ValueError("duplicate SSH endpoint/socket")
        endpoints.add(endpoint)
        names.add(item["name"])
        hosts.append(item)
    return hosts


class TmuxFleet:
    def __init__(self, local, settings, control_dir):
        self.backends = {"local": local}
        self.identities = {"local": ""}
        self.errors = {}
        for item in settings:
            self.backends[item["name"]] = RemoteTmux(item, control_dir)
            endpoint = {k: item.get(k) for k in ("host", "port", "user", "socket")}
            self.identities[item["name"]] = hashlib.sha256(json.dumps(endpoint, sort_keys=True).encode()).hexdigest() + ":"

    def _panes(self, name):
        try:
            panes = self.backends[name].panes()
            self.errors.pop(name, None)
            result = []
            for pane in panes:
                result.append({**pane, "native_pane_id": pane["pane_id"], "server": name,
                    "pane_id": name + "/" + pane["pane_id"],
                    "identity": self.identities[name] + pane["identity"],
                    "target": name + " · " + pane["target"]})
            return result
        except RelayError as exc:
            self.errors[name] = str(exc)
            return []

    def panes(self):
        with ThreadPoolExecutor(max_workers=min(8, len(self.backends))) as pool:
            groups = list(pool.map(self._panes, self.backends))
        return [pane for group in groups for pane in group]

    def resolve(self, candidate):
        name, _ = candidate["pane_id"].split("/", 1)
        if name not in self.backends:
            name = next((label for label, prefix in self.identities.items()
                         if prefix and candidate["identity"].startswith(prefix)), name)
        if name not in self.backends:
            raise RelayError("远程服务器已移除，请重新 /tmux ls。")
        native = candidate.get("native_pane_id", candidate["pane_id"].split("/", 1)[1])
        result = next((p for p in self._panes(name) if p["identity"] == candidate["identity"] and p["native_pane_id"] == native), None)
        if result is None and name in self.errors:
            raise RelayError(name + " 暂不可达：" + self.errors[name])
        return result

    def _backend(self, pane):
        name, native = pane.split("/", 1)
        if name not in self.backends or not re.fullmatch(r"%[0-9]+", native):
            raise RelayError("无效的服务器窗格。")
        return self.backends[name], native

    def capture(self, pane, lines=40):
        backend, native = self._backend(pane)
        return backend.capture(native, lines)

    def viewport(self, pane):
        backend, native = self._backend(pane)
        return backend.viewport(native)

    def send(self, pane, value, enter=True):
        backend, native = self._backend(pane)
        return backend.send(native, value, enter)

    def key(self, pane, key):
        backend, native = self._backend(pane)
        return backend.key(native, key)
