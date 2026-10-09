"""Authenticated loopback service exposing only a fixed quota refresh operation."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from .reader import UsageReader


def make_server(reader, token, port=18014):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.connection.settimeout(10)
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(403)
                return
            if self.path not in {"/v1/sub2api/usage", "/healthz"}:
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid request size")
                if json.loads(self.rfile.read(size)) != {}:
                    raise ValueError("quota command takes no arguments")
            except (ValueError, OSError):
                self.send_error(400)
                return
            try:
                result = reader.refresh() if self.path == "/v1/sub2api/usage" else {"ok": True}
            except Exception:
                result = {"ok": False, "error": "额度服务异常，请检查服务日志"}
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18014)
    parser.add_argument("--init-token", action="store_true")
    args = parser.parse_args()
    try:
        if not 1024 <= args.port <= 65535:
            raise ValueError("port must be between 1024 and 65535")
        if args.init_token:
            args.token_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(args.token_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(secrets.token_urlsafe(32))
            return
        if not args.config:
            raise ValueError("--config is required")
        token = args.token_file.read_text().strip()
        if len(token) < 32:
            raise ValueError("quota service token is missing or invalid")
        with make_server(UsageReader(args.config), token, args.port) as server:
            server.serve_forever()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        parser.error("quota service startup failed: " + str(exc))


if __name__ == "__main__":
    main()
