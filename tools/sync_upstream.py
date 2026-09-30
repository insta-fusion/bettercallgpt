#!/usr/bin/env python3
"""Keep bettercallgpt's `voice/` identical to upstream — the single source of truth.

bettercallgpt ships the upstream voice daemon unchanged. This tool is the only way `voice/`
and `tests/fixtures/` change here:

  sync   copy every git-tracked file under voice/ and tests/fixtures/ from an upstream
         checkout AT A COMMIT (committed content, never the working tree), apply the fixture
         scrubs below, and write UPSTREAM.json (commit + sha256 of every file).
  check  verify the tree matches UPSTREAM.json exactly — no edited, missing or extra file.
         CI runs this: a change to voice/ belongs upstream first, then a sync.

Usage:
  python tools/sync_upstream.py sync --from ~/path/to/upstream [--commit <sha>]
  python tools/sync_upstream.py check

Fixture scrubs: the recorded fixtures are real captures; identifiers that must not ship in
an open-source repo (home paths, private branch names) are replaced by neutral stand-ins.
Only fixtures are scrubbed — code under voice/ is byte-identical to upstream. No test reads
a scrubbed value (the upstream suite passes on the scrubbed tree; CI proves it).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "UPSTREAM.json"
SYNCED_DIRS = ("voice", "tests/fixtures")
UPSTREAM_REPO = "insta-fusion/agent-drivers"

# (pattern, replacement) applied to files under tests/fixtures/ only.
FIXTURE_SCRUBS = (
    (r"/Users/[A-Za-z0-9._-]+", "/Users/dev"),
    (r"/Users/dev/Work/tasks/runs/[A-Za-z0-9._/-]+", "/tmp/voice-fixtures"),
    (r"storyarcade/", "example/"),
)


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True).stdout


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _scrub(path: str, data: bytes) -> bytes:
    if not path.startswith("tests/fixtures/"):
        return data
    text = data.decode("utf-8")
    for pat, rep in FIXTURE_SCRUBS:
        text = re.sub(pat, rep, text)
    return text.encode("utf-8")


def _tracked(repo: Path, commit: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", commit, "--", *SYNCED_DIRS)
    return sorted(p for p in out.decode().splitlines() if "__pycache__" not in p)


def _local_files() -> list[str]:
    files = []
    for d in SYNCED_DIRS:
        for p in (ROOT / d).rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                files.append(p.relative_to(ROOT).as_posix())
    return sorted(files)


def sync(src: Path, commit: str) -> int:
    commit = _git(src, "rev-parse", commit).decode().strip()
    paths = _tracked(src, commit)
    if not paths:
        print(f"sync: nothing under {SYNCED_DIRS} at {commit}", file=sys.stderr)
        return 1
    for rel in _local_files():            # upstream deletions must disappear here too
        if rel not in paths:
            (ROOT / rel).unlink()
    files = {}
    for rel in paths:
        data = _scrub(rel, _git(src, "show", f"{commit}:{rel}"))
        dest = ROOT / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        files[rel] = _sha(data)
    MANIFEST.write_text(json.dumps({
        "upstream": UPSTREAM_REPO,
        "commit": commit,
        "commit_date": _git(src, "show", "-s", "--format=%cs", commit).decode().strip(),
        "synced_dirs": list(SYNCED_DIRS),
        "fixture_scrubs": [list(s) for s in FIXTURE_SCRUBS],
        "files": files,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"sync: {len(files)} files from {UPSTREAM_REPO}@{commit[:7]}")
    return 0


def check() -> int:
    if not MANIFEST.exists():
        print("check: UPSTREAM.json missing — run `sync` first", file=sys.stderr)
        return 1
    want = json.loads(MANIFEST.read_text(encoding="utf-8"))["files"]
    have = _local_files()
    problems = [f"missing: {p}" for p in sorted(set(want) - set(have))]
    problems += [f"not upstream: {p}" for p in sorted(set(have) - set(want))]
    problems += [f"edited: {p}" for p in sorted(set(want) & set(have))
                 if _sha((ROOT / p).read_bytes()) != want[p]]
    if problems:
        print("check: voice/ drifted from upstream — change it upstream, then sync:",
              file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1
    print(f"check: {len(want)} files match {UPSTREAM_REPO}@"
          f"{json.loads(MANIFEST.read_text(encoding='utf-8'))['commit'][:7]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--from", dest="src", required=True, type=Path)
    s.add_argument("--commit", default="HEAD")
    sub.add_parser("check")
    a = ap.parse_args(argv)
    return sync(a.src.expanduser(), a.commit) if a.cmd == "sync" else check()


if __name__ == "__main__":
    raise SystemExit(main())
