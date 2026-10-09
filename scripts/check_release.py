"""Check a source tree or git index for accidental runtime files and obvious secrets."""

import argparse
import ast
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SECRET = re.compile(r"sk-[a-zA-Z0-9_-]{20,}|-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----")
PRIVATE_NAME = {"auth.json", "owner.json", "pairing.json", "pairing-instruction.json", "token",
                "hosts.json",
                "group.json", "group-pairing.json", "delivery.json", "client.json", "bot.env", "compose.env"}
SECRET_FIELDS = {"api_key", "access_token", "refresh_token", "client_secret", "qq_client_secret", "appsecret"}


def credential_literal(text, filename):
    if not filename.endswith(".py"):
        return False
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and str(key.value).lower() in SECRET_FIELDS
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and re.fullmatch(r"[a-zA-Z0-9_-]{24,}", value.value)):
                    return True
    return False


def check(root, indexed=False):
    if indexed:
        result = subprocess.run(["git", "ls-files", "--stage", "-z"], cwd=root, check=True, capture_output=True)
        entries = []
        for record in result.stdout.decode().split("\0"):
            if record:
                metadata, name = record.split("\t", 1)
                mode, object_id, stage = metadata.split()
                entries.append((Path(name), mode, object_id, stage))
    else:
        entries = [(path.relative_to(root), None, None, "0") for path in root.rglob("*") if path.is_file()
                   and not set(path.relative_to(root).parts) & {".git", "instances", "backups", "__pycache__", ".venv", "dist", "build", "node_modules", "test-results", "playwright-report"}]
    failures = []
    for name, mode, object_id, stage in entries:
        if stage != "0":
            failures.append(str(name) + ": unresolved Git index entry")
            continue
        if (name.name in PRIVATE_NAME or (name.name.startswith(".env") and name.name != ".env.example")
                or name.suffix == ".env" or set(name.parts) & {"instances", "backups", "node_modules", "test-results", "playwright-report"}
                or name.suffix in {".sqlite3", ".db", ".log", ".pem", ".key", ".jsonl", ".zip", ".bak"}):
            failures.append(str(name) + ": private runtime file")
            continue
        path = root / name
        if mode == "120000" or (not indexed and path.is_symlink()):
            failures.append(str(name) + ": symlink is not permitted in the release")
            continue
        if mode == "160000":
            failures.append(str(name) + ": submodule is not permitted in the release")
            continue
        content = (subprocess.run(["git", "cat-file", "blob", object_id], cwd=root,
                                  check=True, capture_output=True).stdout if indexed else path.read_bytes())
        text = content.decode("utf-8", errors="replace")
        if SECRET.search(text) or credential_literal(text, str(name)):
            failures.append(str(name) + ": possible credential; value redacted")
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--indexed", action="store_true", help="check only Git-tracked paths")
    args = parser.parse_args()
    failures = check(ROOT, args.indexed)
    for failure in failures:
        print(failure)
    if failures:
        raise SystemExit(1)
    print("Release hygiene passed: no private runtime files or obvious key literals.")


if __name__ == "__main__":
    main()
