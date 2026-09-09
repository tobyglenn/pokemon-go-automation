#!/usr/bin/env python3
"""Reject private artifacts before publication, without printing their contents.

Run from the repository root. By default scan the exact Git index and every
reachable commit, so an ignored file or a later deletion cannot hide a leak.
Before the first staging operation, use --working-tree. Optional --deny-file
accepts a JSON list of private literal identifiers stored OUTSIDE the repository.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys


PRIVATE_PARTS = frozenset({
    "logs", "log", "backups", "screenshots", "recordings", "runtime", "private",
    "local", "__pycache__", ".agents", ".claude", ".codex", "venv", "venv.broken",
    ".venv", "node_modules", ".ssh", ".local", "output", "outputs", "captures",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".tox", "dist", "build",
    "state", "cache", "artifacts",
})
PRIVATE_SUFFIXES = frozenset({
    ".log", ".sqlite", ".sqlite3", ".db", ".pem", ".key", ".p12", ".pfx",
    ".mobileprovision", ".pyc", ".pyo", ".apk", ".mp4", ".mov", ".heic",
    ".png", ".jpg", ".jpeg", ".webp", ".csv", ".tsv", ".har", ".zip",
})
PATTERNS = (
    ("private home or mounted-volume path", re.compile(r"/(?:Users|home|Volumes)/[A-Za-z0-9][^\s\"'<>]*")),
    ("private network address", re.compile(r"(?<![\d.])(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?![\d.])")),
    ("iOS hardware identifier", re.compile(r"\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}\b")),
    ("hardware serial candidate", re.compile(r"\b(?=[A-Z0-9]{14,24}\b)(?=[A-Z0-9]*[A-Z])(?=[A-Z0-9]*\d)[A-Z0-9]+\b")),
    ("private key material", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("access token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16}|sk-[A-Za-z0-9]{32,}|xox[baprs]-[A-Za-z0-9-]{20,})\b")),
    ("credential in URL", re.compile(r"https?://[^\s/:@]+:[^\s/@]+@")),
)
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
EXAMPLE_EMAIL_DOMAINS = frozenset({"example.com", "example.org", "example.net"})
TRAINER_CODE = re.compile(r"(?<![\w.])(?:\d{12}|\d{4}[ -]\d{4}[ -]\d{4})(?![\w.])")


def synthetic_trainer_code(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return digits in {"123456789012", "987654321098"} or all(
        len(set(digits[start:start + 4])) == 1 for start in (0, 4, 8)
    )


def git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise RuntimeError("Git operation failed: " + " ".join(args[:2]))
    return result.stdout


def path_problems(name: str) -> list[str]:
    path = Path(name)
    problems = []
    if any(part.lower() in PRIVATE_PARTS or part.endswith(".egg-info") for part in path.parts):
        problems.append("private/runtime directory")
    if path.suffix.lower() in PRIVATE_SUFFIXES:
        problems.append("private/runtime or unreviewed binary file type")
    if path.name in {".DS_Store", "id_rsa", "id_ed25519", "known_hosts", "authorized_keys"}:
        problems.append("machine-specific file")
    if path.name.startswith(".env") and path.name not in {".env.example", ".env.template"}:
        problems.append("local environment file")
    if path.parts and path.parts[0] == "config" and path.suffix in {".yaml", ".yml", ".json", ".toml"}:
        if ".example." not in path.name and ".template." not in path.name and path.name != "ConfigTemplate.yaml":
            problems.append("live configuration rather than an example")
    return problems


def content_problems(data: bytes, private_literals: list[str]) -> list[tuple[int, str]]:
    if b"\x00" in data:
        return [(0, "binary file requires explicit review")]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [(0, "non-UTF-8 file requires explicit review")]
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        for label, pattern in PATTERNS:
            if pattern.search(line):
                problems.append((number, label))
        if any(m.group(1).lower() not in EXAMPLE_EMAIL_DOMAINS for m in EMAIL.finditer(line)):
            problems.append((number, "non-example email address"))
        if any(not synthetic_trainer_code(m.group()) for m in TRAINER_CODE.finditer(line)):
            problems.append((number, "trainer code candidate"))
        for literal in private_literals:
            if re.search(r"(?<![\w])" + re.escape(literal) + r"(?![\w])", line, re.I):
                problems.append((number, "private identifier from external deny list"))
                break
    return problems


def index_files(root: Path):
    for entry in git(root, "ls-files", "--stage", "-z").split(b"\x00"):
        if not entry:
            continue
        meta, raw_name = entry.split(b"\t", 1)
        mode, oid, stage = meta.decode().split()
        name = raw_name.decode("utf-8")
        if stage != "0":
            raise RuntimeError("Unmerged index entry")
        yield name, mode, git(root, "cat-file", "blob", oid)


def history_files(root: Path):
    seen = set()
    for commit in git(root, "rev-list", "--all").decode().splitlines():
        commit_data = git(root, "cat-file", "commit", commit)
        message = commit_data.partition(b"\n\n")[2]
        yield f"history/{commit[:12]}/commit-message", "commit-message", "100644", message
        for entry in git(root, "ls-tree", "-r", "-z", commit).split(b"\x00"):
            if not entry:
                continue
            meta, raw_name = entry.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            name = raw_name.decode("utf-8")
            marker = (name, oid)
            if marker in seen:
                continue
            seen.add(marker)
            data = git(root, "cat-file", "blob", oid) if kind == "blob" else b""
            yield f"history/{commit[:12]}/{name}", name, mode, data


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--working-tree", action="store_true", help="scan all working files except .git before staging")
    parser.add_argument("--deny-file", type=Path, help="external JSON list of private identifiers; never commit it")
    args = parser.parse_args()
    root = args.root.resolve()
    private_literals = []
    if args.deny_file:
        deny = args.deny_file.resolve()
        if deny == root or root in deny.parents:
            parser.error("--deny-file must be outside the repository")
        private_literals = json.loads(deny.read_text())
        if not isinstance(private_literals, list) or any(not isinstance(x, str) or not x for x in private_literals):
            parser.error("--deny-file must contain a JSON list of nonempty strings")
    count = 0
    findings = []

    def check(display: str, name: str, mode: str, data: bytes):
        nonlocal count
        count += 1
        for problem in path_problems(name):
            findings.append(f"{display}: {problem}")
        for _, problem in content_problems(name.encode("utf-8"), private_literals):
            findings.append(f"{display}: filename contains {problem}")
        if mode not in {"100644", "100755"}:
            findings.append(f"{display}: symlink or submodule requires explicit review")
            return
        for number, problem in content_problems(data, private_literals):
            findings.append(f"{display}:{number}: {problem}")

    try:
        if args.working_tree:
            for directory, subdirs, filenames in os.walk(root, followlinks=False):
                current = Path(directory)
                for name in list(subdirs):
                    path = current / name
                    relative = str(path.relative_to(root))
                    if name == ".git":
                        subdirs.remove(name)
                    elif path.is_symlink():
                        check(relative, relative, "120000", b"")
                        subdirs.remove(name)
                    elif path_problems(relative):
                        # Reject the directory once instead of reading thousands
                        # of ignored dependencies or private runtime artifacts.
                        check(relative, relative, "100644", b"")
                        subdirs.remove(name)
                for name in sorted(filenames):
                    path = current / name
                    relative = str(path.relative_to(root))
                    if path.is_symlink():
                        check(relative, relative, "120000", b"")
                    else:
                        check(relative, relative, "100644", path.read_bytes())
        else:
            for name, mode, data in index_files(root):
                check(name, name, mode, data)
            for display, name, mode, data in history_files(root):
                check(display, name, mode, data)
        if not count:
            raise RuntimeError("No files scanned; stage the public files or use --working-tree")
    except (RuntimeError, OSError, ValueError) as error:
        print(f"Privacy audit failed: {error}", file=sys.stderr)
        return 2
    if findings:
        print("Privacy audit blocked publication:")
        print("\n".join(findings))
        return 1
    print(f"Privacy audit passed: {count} file versions scanned, including reachable history in index mode.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
