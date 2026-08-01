#!/usr/bin/env python3
"""Secret scanner for this repo (Week 2, W2-220).

`.gitignore` is the first line of defence for `backend/config.json` and
`backend/data/`, but it is only a default: `git add -f` overrides it without
a word, and a file that was tracked *before* the ignore rule existed stays
tracked forever. This script is the check that doesn't care about intent —
it looks at what is actually in the index/tree.

Two kinds of rule, deliberately:

  1. **File rules** — a small list of paths that must never be tracked at
     all (`backend/config.json`, anything under `backend/data/`, `.env`).
     These are exact and have no false positives, so they're errors.
  2. **Content rules** — heuristics for a real-looking `local_key`, a
     64-hex signing key or PIN hash, pasted into a file that *is* meant to
     be tracked. Heuristics can be wrong, so every content rule can be
     waived on a specific line with a `nosecret` marker in a comment. A
     waiver is visible in review, which is the point; a scanner nobody can
     silence gets deleted instead.

Usage:
    python .github/scripts/scan_secrets.py               # scan tracked files
    python .github/scripts/scan_secrets.py --staged      # pre-commit hook use
    python .github/scripts/scan_secrets.py --root DIR    # scan a directory

Exit code 0 = clean, 1 = findings (printed to stdout).
"""

import argparse
import os
import re
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Paths that must never be tracked, whatever anyone's reason. Matched as
# forward-slash repo-relative paths.
FORBIDDEN_PATHS = [
    ("backend/config.json", "real device credentials (device_id + local_key)"),
    (".env", "real secrets sourced from the environment"),
    ("backend/snapshot.json", "tinytuya scan output, contains device ids"),
]
FORBIDDEN_PREFIXES = [
    ("backend/data/", "runtime state: PIN hash, session signing key, audit chain key"),
    ("backend/backups/", "backup archives contain every device local_key"),
]

# Files whose whole point is to show the shape of a secret without being one.
PLACEHOLDER_FILES = {"backend/config.example.json", ".env.example"}

SKIP_DIRS = {".git", "venv", "__pycache__", "node_modules", ".claude", "graphify-out"}
TEXT_SUFFIXES = {".py", ".js", ".json", ".md", ".yml", ".yaml", ".html", ".css",
                 ".txt", ".sh", ".bat", ".cfg", ".ini", ".toml", ".env", ""}

WAIVER = re.compile(r"nosecret", re.IGNORECASE)

# A real Tuya local_key is 16 characters of mixed-case alphanumerics. The
# length floor plus the "has a digit AND a lowercase letter" test is what
# separates one from the placeholders this repo legitimately contains
# (REPLACE_WITH_LOCAL_KEY, PASTE_LOCAL_KEY_HERE, "...", "<obtained
# separately>") without needing to enumerate them.
LOCAL_KEY = re.compile(r'local_key["\']?\s*[:=]\s*["\']([^"\']+)["\']')
HEX64 = re.compile(r'(?:secret_key|pin_hash|security_audit_key)["\']?\s*[:=]\s*["\']([0-9a-fA-F]{64})["\']')
# 32-hex is the salt/jti width used in remote_auth; worth flagging in a
# tracked file for the same reason.
HEX32_SALT = re.compile(r'salt["\']?\s*[:=]\s*["\']([0-9a-fA-F]{32})["\']')


def _looks_like_real_key(value):
    if len(value) < 12:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", value):
        return False  # placeholders with <>, spaces, dots, dashes
    if value.upper() == value:
        return False  # REPLACE_WITH_LOCAL_KEY, PASTE_LOCAL_KEY_HERE
    has_digit = any(c.isdigit() for c in value)
    has_lower = any(c.islower() for c in value)
    return has_digit and has_lower


def _git(args, root):
    try:
        out = subprocess.run(["git"] + args, cwd=root, capture_output=True, text=True, check=True)
        return [line.strip() for line in out.stdout.splitlines() if line.strip()]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def collect_files(root, staged):
    """Tracked (or staged) files when inside a git repo, otherwise a plain
    walk -- so the same script works in CI, in a pre-commit hook, and
    against a scratch directory in a test."""
    if staged:
        files = _git(["diff", "--cached", "--name-only", "--diff-filter=ACM"], root)
        if files is None:
            print("not a git repository -- --staged needs one")
            sys.exit(2)
        return files
    files = _git(["ls-files"], root)
    if files is not None:
        return files
    walked = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root).replace(os.sep, "/")
            walked.append(rel)
    return walked


def scan(root=REPO_ROOT, staged=False):
    findings = []
    files = collect_files(root, staged)

    for rel in files:
        for path, why in FORBIDDEN_PATHS:
            if rel == path:
                findings.append((rel, 0, f"file must never be committed -- {why}"))
        for prefix, why in FORBIDDEN_PREFIXES:
            if rel.startswith(prefix):
                findings.append((rel, 0, f"file must never be committed -- {why}"))

    for rel in files:
        if rel in PLACEHOLDER_FILES:
            continue
        if os.path.splitext(rel)[1].lower() not in TEXT_SUFFIXES:
            continue
        abs_path = os.path.join(root, rel)
        if not os.path.isfile(abs_path):
            continue
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if WAIVER.search(line):
                continue
            for match in LOCAL_KEY.finditer(line):
                if _looks_like_real_key(match.group(1)):
                    findings.append((rel, lineno, "looks like a real Tuya local_key"))
            if HEX64.search(line):
                findings.append((rel, lineno, "64-hex signing key / PIN hash literal"))
            if HEX32_SALT.search(line):
                findings.append((rel, lineno, "32-hex salt literal"))

    return findings


def main():
    parser = argparse.ArgumentParser(description="Fail if a real secret is committed.")
    parser.add_argument("--root", default=REPO_ROOT)
    parser.add_argument("--staged", action="store_true",
                        help="scan the git index instead of the whole tree")
    args = parser.parse_args()

    findings = scan(root=args.root, staged=args.staged)
    if not findings:
        print("secret scan: clean")
        return 0
    print(f"secret scan: {len(findings)} finding(s)\n")
    for rel, lineno, why in findings:
        where = f"{rel}:{lineno}" if lineno else rel
        print(f"  {where}: {why}")
    print("\nIf a finding is a false positive, put the word 'nosecret' in a comment on "
          "that line. If it is real: remove it, rotate the credential, and rewrite the "
          "history that contains it -- deleting it in a later commit is not enough.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
