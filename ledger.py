"""Append-only, hash-chained JSONL ledger. Edits/deletions in the middle are detectable;
`seal` output lets you anchor the head hash somewhere the agent cannot write."""
from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

GENESIS = "0" * 64


def _digest(prev: str, body: dict) -> str:
    return hashlib.sha256((prev + json.dumps(body, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()


@contextmanager
def _lock(path: Path):
    fh = open(path.with_suffix(".lock"), "a+")
    try:
        try:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield
    finally:
        fh.close()


class Ledger:
    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / "ledger.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self) -> list[dict]:
        out = []
        try:
            with open(self.path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            out.append({"seq": -1, "corrupt": True})
        except FileNotFoundError:
            pass
        return out

    def append(self, body: dict) -> dict:
        with _lock(self.path):
            prev_entries = self.entries()
            last = prev_entries[-1] if prev_entries else None
            seq = len(prev_entries) + 1
            prev = last["hash"] if last and "hash" in last else GENESIS
            body = {**body, "seq": seq}
            entry = {**body, "prev": prev, "hash": _digest(prev, body)}
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            return entry

    def verify(self, expect_head: str | None = None) -> tuple[bool, str]:
        prev = GENESIS
        entries = self.entries()
        for e in entries:
            if e.get("corrupt"):
                return False, "unreadable line in ledger"
            body = {k: v for k, v in e.items() if k not in ("prev", "hash")}
            if e.get("prev") != prev or _digest(prev, body) != e.get("hash"):
                return False, f"chain broken at entry #{e.get('seq')}"
            prev = e["hash"]
        if expect_head and expect_head not in (prev, prev[:len(expect_head)]):
            return False, "head hash does not match your anchor (entries removed or rewritten?)"
        return True, f"{len(entries)} entries, head {prev[:16]}"

    def head(self) -> str:
        es = self.entries()
        return es[-1]["hash"] if es and "hash" in es[-1] else GENESIS
