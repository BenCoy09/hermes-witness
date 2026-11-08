"""Objective tool-trail: what actually happened, parsed from tool results (never from the agent's own summary)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

META_TOOLS = re.compile(r"^(skill_manage|skill_view|skills_list|memory|todo|clarify|session_search|witness)", re.I)
ERR_TEXT = re.compile(r"(Traceback \(most recent call last\)|command not found|No such file or directory|Permission denied|"
                      r"\bHTTP[ /]*[45]\d\d\b|\b[45]\d\d (Client|Server) Error|ModuleNotFoundError|ECONNREFUSED|timed out|"
                      r"\bFAILED\b|\b\d+ failed\b|AssertionError|fatal:|\berror:)", re.I)
EXITED_RE = re.compile(r"(?:exited with code|\[exit)\s*(\d+)", re.I)
EXIT_RE = re.compile(r"(?:exit[ _]?code|returncode|status)\"?\s*[:=]\s*(-?\d+)", re.I)
VERIFY_CMD = re.compile(r"(pytest|npm (run )?test|go test|cargo test|make test|unittest|verify|--check|lint|tsc\b)", re.I)
SIG_CLEAN = re.compile(r"[0-9a-f]{6,}|\d+|/[\w./-]+")


def status(result) -> tuple[bool, str]:
    """-> (ok, error_signature). Conservative: unknown shapes count as ok."""
    text = result if isinstance(result, str) else json.dumps(result, default=str)
    try:
        data = json.loads(text) if isinstance(result, str) else result
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        if data.get("success") is False or data.get("ok") is False:
            return False, _sig(str(data.get("error") or data.get("message") or "failure"))
        if data.get("error"):
            return False, _sig(str(data["error"]))
        for k in ("exit_code", "returncode", "exitCode"):
            if isinstance(data.get(k), int) and data[k] != 0:
                return False, _sig(f"exit {data[k]} " + str(data.get("stderr") or data.get("output") or "")[:120])
    m = EXITED_RE.search(text[:4000])
    if m and m.group(1) != "0":
        return False, _sig(f"exit {m.group(1)} " + text[:120])
    m = EXIT_RE.search(text[:4000])
    if m and m.group(1) not in ("0", "200"):
        if not (m.group(1).isdigit() and int(m.group(1)) in range(200, 300)):
            return False, _sig(f"exit {m.group(1)} " + text[:120])
    m = ERR_TEXT.search(text[:6000])
    if m:
        return False, _sig(m.group(0))
    return True, ""


def _sig(s: str) -> str:
    return SIG_CLEAN.sub("#", s.strip().lower())[:80]


@dataclass
class Event:
    tool: str
    args: str
    ok: bool
    sig: str
    ts: int


@dataclass
class Trail:
    events: list[Event] = field(default_factory=list)
    used_skills: set[str] = field(default_factory=set)
    corrections: int = 0
    written: list[str] = field(default_factory=list)

    def add(self, tool: str, args, result) -> Event:
        ok, sig = status(result)
        try:
            a = json.dumps(args, default=str)[:400]
        except (TypeError, ValueError):
            a = str(args)[:400]
        ev = Event(tool or "", a, ok, sig, int(time.time()))
        self.events.append(ev)
        del self.events[:-200]
        return ev

    def work(self) -> list[Event]:
        return [e for e in self.events if not META_TOOLS.match(e.tool)]

    def outcome(self) -> str:
        w = self.work()
        if self.corrections:
            return "failed"
        if not w:
            return "neutral"
        if not w[-1].ok:
            return "failed"
        if len(w) >= 4 and sum(not e.ok for e in w) / len(w) >= 0.5:
            return "failed"
        return "succeeded"
