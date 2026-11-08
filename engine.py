"""Witness engine: grade skills at write time, track real outcomes, quarantine what was never earned."""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .evidence import constraints_from, grade
from .ledger import Ledger
from .trail import Trail

MODES = ("observe", "gate")
WRITE_ACTIONS = {"create", "edit", "patch", "update", "write_file", "write"}
VIEW_TOOLS = {"skill_view", "skill_use", "skills_view", "view_skill"}
CORRECTION = re.compile(r"(that'?s (wrong|not right|incorrect)|doesn'?t work|didn'?t work|you broke|undo that|revert (that|it)|wrong again|that (is|was)n'?t (right|what))", re.I)
MEMORY_FILES = ["memories/MEMORY.md", "memories/USER.md", "MEMORY.md", "USER.md"]


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".w-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class Witness:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home) if home else hermes_home()
        self.dir = self.home / "witness"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.dir)
        self.sessions: dict[str, Trail] = {}
        self.outbox: dict[str, list[str]] = {}
        self.warned: dict[str, set[str]] = {}
        self.rec = self._load("records.json", {})
        self.cfg = {"mode": "gate", **self._load("config.json", {})}

    def _load(self, name: str, default):
        try:
            return json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def _save(self) -> None:
        _atomic(self.dir / "records.json", json.dumps(self.rec, indent=1))

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of: {', '.join(MODES)}")
        self.cfg["mode"] = mode
        _atomic(self.dir / "config.json", json.dumps(self.cfg))

    def trail(self, key: str) -> Trail:
        return self.sessions.setdefault(key, Trail())

    # ---- hooks ----
    def _trace(self, key: str, tool: str, args, result) -> None:
        """Debug trace: tool names and argument KEYS only, never values."""
        try:
            row = {"ts": int(time.time()), "session": key[:12], "tool": tool,
                   "arg_keys": sorted(args)[:12] if isinstance(args, dict) else str(type(args).__name__),
                   "action": str(args.get("action", ""))[:20] if isinstance(args, dict) else "",
                   "result_type": type(result).__name__}
            p = self.dir / "trace.jsonl"
            lines = p.read_text().splitlines()[-199:] if p.exists() else []
            lines.append(json.dumps(row))
            _atomic(p, "\n".join(lines) + "\n")
        except Exception:
            pass

    def trace_rows(self, n: int = 25) -> list[dict]:
        try:
            return [json.loads(x) for x in (self.dir / "trace.jsonl").read_text().splitlines()[-n:]]
        except (OSError, ValueError):
            return []

    def on_tool(self, key: str, tool: str, args, result) -> None:
        self._trace(key, tool, args, result)
        args = args if isinstance(args, dict) else {}
        tr = self.trail(key)
        ev = tr.add(tool, args, result)
        if tool == "skill_manage" and ev.ok:
            # Hermes may batch writes as args["operations"] = [{action, name, ...}, ...]
            ops = args.get("operations") if isinstance(args.get("operations"), list) else [args]
            for op in ops:
                if not isinstance(op, dict):
                    continue
                name = str(op.get("name") or op.get("skill") or op.get("skill_name") or "")
                if str(op.get("action", "")).lower() in WRITE_ACTIONS and name:
                    self.evaluate(key, name)
        elif tool in VIEW_TOOLS:
            name = str(args.get("name") or args.get("skill") or args.get("skill_name") or "")
            if name:
                tr.used_skills.add(name)
                self._warn_if_weak(key, name)

    def on_user_message(self, key: str, text: str) -> None:
        if text and CORRECTION.search(text):
            self.trail(key).corrections += 1

    def on_end(self, key: str) -> None:
        tr = self.sessions.pop(key, None)
        self.outbox.pop(key, None)
        self.warned.pop(key, None)
        if not tr or not tr.used_skills:
            return
        outcome = tr.outcome()
        if outcome == "neutral":
            return
        for name in tr.used_skills:
            r = self.rec.get(name)
            if not r:
                continue
            r["uses"] += 1
            r["ok" if outcome == "succeeded" else "fail"] += 1
            self.ledger.append({"type": "outcome", "ts": int(time.time()), "skill": name, "outcome": outcome})
        self._save()

    # ---- grading ----
    def find_skill(self, name: str) -> Path | None:
        root = self.home / "skills"
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith(".")]
            if Path(dp).name == name and "SKILL.md" in fns:
                return Path(dp)
        return None

    def _memory_texts(self) -> list[str]:
        out = []
        for rel in MEMORY_FILES:
            try:
                out.append((self.home / rel).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        return out

    def evaluate(self, key: str, name: str) -> dict | None:
        d = self.find_skill(name)
        if not d:
            return None
        old = self.rec.get(name, {})
        if old.get("status") == "trusted":
            return old
        try:
            text = (d / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        extra = [p.name for p in d.rglob("*") if p.is_file() and p.name != "SKILL.md"]
        res = grade(text, self.trail(key), constraints_from(self._memory_texts()), extra)
        self.trail(key).written.append(name)
        status = "active"
        rec = {**{"uses": 0, "ok": 0, "fail": 0}, **{k: old[k] for k in ("uses", "ok", "fail") if k in old},
               "grade": res["grade"], "score": res["score"], "findings": res["findings"], "positive": res["positive"],
               "created": old.get("created") or int(time.time()), "status": status, "rel": d.relative_to(self.home).as_posix()}
        if res["grade"] == "D" and self.cfg["mode"] == "gate":
            q = self.dir / "quarantine" / f"{name}-{int(time.time())}"
            q.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(d), str(q))
                rec["status"], rec["qpath"] = "quarantined", q.relative_to(self.home).as_posix()
            except OSError:
                pass
        self.rec[name] = rec
        self._save()
        self.ledger.append({"type": "grade", "ts": int(time.time()), "session": key, "skill": name, "grade": res["grade"],
                            "score": res["score"], "findings": res["findings"], "status": rec["status"]})
        if res["grade"] in ("C", "D"):
            why = "; ".join(f["detail"] for f in res["findings"][:3])
            verb = "was QUARANTINED" if rec["status"] == "quarantined" else "is flagged"
            self.outbox.setdefault(key, []).append(f"- Skill '{name}' graded {res['grade']} and {verb}: {why}.")
        return rec

    def _warn_if_weak(self, key: str, name: str) -> None:
        r = self.rec.get(name)
        if not r or r.get("status") == "trusted":
            return
        if (r["grade"] in ("C", "D") or self.degraded(r)) and name not in self.warned.setdefault(key, set()):
            self.warned[key].add(name)
            why = "; ".join(f["detail"] for f in r["findings"][:2]) or f"{r['fail']} of {r['uses']} uses ended badly"
            self.outbox.setdefault(key, []).append(f"- Skill '{name}' is unproven (grade {r['grade']}): {why}. Verify its steps before relying on them.")

    @staticmethod
    def degraded(r: dict) -> bool:
        return r["uses"] >= 3 and r["fail"] / r["uses"] >= 0.6

    def notice_for(self, key: str) -> str | None:
        msgs = self.outbox.pop(key, [])
        if not msgs:
            return None
        return "[Witness evidence notice]\n" + "\n".join(msgs) + "\nDo not claim these skills are verified. Report this to the user."

    # ---- human actions (CLI only) ----
    def trust(self, name: str) -> str:
        r = self.rec.get(name)
        if not r:
            raise KeyError(f"unknown skill '{name}'")
        r["status"] = "trusted"
        self._save()
        self.ledger.append({"type": "human", "ts": int(time.time()), "skill": name, "action": "trusted"})
        return f"'{name}' marked trusted; Witness will stop flagging it."

    def release(self, name: str) -> str:
        r = self.rec.get(name)
        if not r or r.get("status") != "quarantined":
            raise ValueError(f"'{name}' is not quarantined")
        src, dst = self.home / r["qpath"], self.home / r["rel"]
        if dst.exists():
            raise ValueError(f"{dst} already exists; resolve manually")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        r["status"] = "trusted"
        self._save()
        self.ledger.append({"type": "human", "ts": int(time.time()), "skill": name, "action": "released"})
        return f"Restored '{name}' to {r['rel']} and marked trusted."

    def report(self) -> list[tuple[str, dict]]:
        order = {"D": 0, "C": 1, "B": 2, "A": 3}
        return sorted(self.rec.items(), key=lambda kv: (order.get(kv[1]["grade"], 9), kv[0]))


_W: Witness | None = None


def get_witness() -> Witness:
    global _W
    if _W is None or _W.home != hermes_home():
        _W = Witness()
    return _W
