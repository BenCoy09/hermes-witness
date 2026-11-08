"""`hermes witness ...`. trust/release are CLI-only: the agent cannot vouch for itself."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

from .engine import MODES, Witness, get_witness

TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if TTY else s
GC = {"A": lambda s: _c("32", s), "B": lambda s: _c("36", s), "C": lambda s: _c("33", s), "D": lambda s: _c("31", s)}
DIM = lambda s: _c("2", s)  # noqa: E731


def setup(parser) -> None:
    sub = parser.add_subparsers(dest="w_cmd")
    sub.add_parser("report", help="Every learned skill, graded, with evidence")
    sub.add_parser("show", help="Evidence for one skill").add_argument("skill")
    sub.add_parser("trust", help="You vouch for a skill").add_argument("skill")
    sub.add_parser("release", help="Restore a quarantined skill").add_argument("skill")
    m = sub.add_parser("mode", help="observe (label only) | gate (quarantine grade D)")
    m.add_argument("mode", nargs="?")
    sub.add_parser("verify", help="Check ledger integrity")
    sub.add_parser("trace", help="What the hooks last received (tool names and arg keys only)")
    sub.add_parser("demo", help="Replay published failure cases in a sandbox")


def row(name: str, r: dict) -> str:
    uses = f"{r['ok']}/{r['uses']} ok" if r["uses"] else "unused"
    flag = {"quarantined": " QUARANTINED", "trusted": " trusted"}.get(r["status"], "")
    deg = " DEGRADED" if Witness.degraded(r) else ""
    return f"{GC[r['grade']](r['grade'])}  {name:<28} {uses:<9}{flag}{deg}"


def handle(args) -> None:
    w, cmd = get_witness(), getattr(args, "w_cmd", None)
    try:
        if cmd == "report":
            print("\n".join(row(n, r) for n, r in w.report()) or
                  "No agent-written skills graded yet. If you expected one, run: hermes witness trace")
        elif cmd == "show":
            r = w.rec.get(args.skill)
            if not r:
                raise KeyError(f"unknown skill '{args.skill}'")
            print(f"{args.skill}: grade {r['grade']} (score {r['score']}) · {r['status']} · {r['uses']} uses")
            for f in r["findings"]:
                print(f"  ! {f['rule']}: {f['detail']}")
            if not r["findings"]:
                print("  no negative evidence" + (" · verified by a passing command" if r["positive"] else ""))
        elif cmd == "trust":
            print(w.trust(args.skill))
        elif cmd == "release":
            print(w.release(args.skill))
        elif cmd == "mode":
            if args.mode:
                w.set_mode(args.mode)
            print(f"mode: {w.cfg['mode']}  ({' | '.join(MODES)})")
        elif cmd == "trace":
            rows = w.trace_rows()
            for r in rows:
                print(f"{r['tool']:<22} keys={r['arg_keys']} action={r['action'] or '-'} result={r['result_type']}")
            if not rows:
                print("No hook calls recorded. Witness hooks have not fired in any session yet.")
        elif cmd == "verify":
            ok, msg = w.ledger.verify()
            print(("✓ " if ok else "✗ ") + msg)
            if not ok:
                raise SystemExit(2)
        elif cmd == "demo":
            demo()
        else:
            print("Usage: hermes witness {report|show|trust|release|mode|verify|trace|demo}")
    except (KeyError, ValueError) as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        raise SystemExit(1)


def slash(raw: str = "") -> str:
    w = get_witness()
    return "\n".join(row(n, r) for n, r in w.report()) or "No agent-written skills graded yet."


def _skill(home: Path, name: str, body: str, extra: dict | None = None) -> None:
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body)
    for fn, txt in (extra or {}).items():
        (d / fn).parent.mkdir(exist_ok=True)
        (d / fn).write_text(txt)


def demo() -> dict:
    """Replays two published failure classes plus a healthy skill. Returns records for tests."""
    def say(s, p=0.5):
        print(s); sys.stdout.flush(); time.sleep(p if TTY else 0)
    ERR = json.dumps({"success": False, "error": "HTTP 403 Forbidden"})
    OK = json.dumps({"success": True})
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        (home / "memories").mkdir()
        (home / "memories" / "MEMORY.md").write_text("User rule: only modify the develop branch. Never push to main.\n")
        w = Witness(home)
        say("☤ Witness demo — sandbox only. The agent grades its own work; Witness checks the receipts.\n")
        say("CASE 1 · issue #72269: an unresolved failure written up as a 'validated' skill")
        for _ in range(3):
            w.on_tool("a", "web_fetch", {"url": "https://api.weather.example"}, ERR)
        _skill(home, "weather-queries", "# Reliable Retrieval Patterns\nValidated, reliable approach.\nRun scripts/verify-weather-api.sh\n",
               {"scripts/verify-weather-api.sh": "curl https://api.weather.example"})
        w.on_tool("a", "skill_manage", {"action": "create", "name": "weather-queries"}, OK)
        say("  " + row("weather-queries", w.rec["weather-queries"]))
        for f in w.rec["weather-queries"]["findings"]:
            say(DIM(f"     ! {f['rule']}: {f['detail']}"), 0.2)
        say("\nCASE 2 · a learned skill that quietly drops your own rule")
        w.on_tool("b", "terminal", {"command": "git status"}, OK)
        _skill(home, "ship-it", "# Ship it\n1. git add -A && git commit\n2. git push origin main\n")
        w.on_tool("b", "skill_manage", {"action": "create", "name": "ship-it"}, OK)
        say("  " + row("ship-it", w.rec["ship-it"]))
        for f in w.rec["ship-it"]["findings"]:
            say(DIM(f"     ! {f['rule']}: {f['detail']}"), 0.2)
        say("\nCASE 3 · a skill with a passing test in the trail")
        w.on_tool("c", "terminal", {"command": "pytest -q"}, json.dumps({"exit_code": 0, "output": "12 passed"}))
        _skill(home, "fix-flaky-tests", "# Fix flaky tests\nPin the seed, rerun with pytest -q.\n")
        w.on_tool("c", "skill_manage", {"action": "create", "name": "fix-flaky-tests"}, OK)
        say("  " + row("fix-flaky-tests", w.rec["fix-flaky-tests"]))
        say("\nOUTCOMES · real use beats self-reports")
        for i in range(3):
            w.on_tool(f"u{i}", "skill_view", {"name": "fix-flaky-tests"}, OK)
            w.on_tool(f"u{i}", "terminal", {"command": "pytest"}, json.dumps({"exit_code": 1, "output": "3 failed"}))
            w.on_end(f"u{i}")
        say("  " + row("fix-flaky-tests", w.rec["fix-flaky-tests"]))
        say("\nAgent is told in-session:\n" + DIM("  " + (w.notice_for("a") or "").replace("\n", "\n  ")))
        ok, msg = w.ledger.verify()
        say(f"\nLedger {'✓ intact' if ok else '✗ tampered'} — {msg}")
        return dict(w.rec)
