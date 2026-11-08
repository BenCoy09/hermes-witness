import importlib.util, io, json, os, sys, tempfile, unittest, contextlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("hermes_witness", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
mod = importlib.util.module_from_spec(spec); sys.modules["hermes_witness"] = mod; spec.loader.exec_module(mod)
from hermes_witness import trail, evidence, engine, cli  # noqa: E402
from hermes_witness.ledger import Ledger  # noqa: E402

ERR = json.dumps({"success": False, "error": "HTTP 403"}); OK = json.dumps({"success": True})


class TestStatus(unittest.TestCase):
    def test_parsing(self):
        for r in (json.dumps({"exit_code": 2}), "Traceback (most recent call last):\n...", "bash: foo: command not found",
                  '{"error": "boom"}', "3 failed, 2 passed", "fatal: not a git repository"):
            self.assertFalse(trail.status(r)[0], r)
        for r in (json.dumps({"exit_code": 0, "output": "ok"}), "done", "", '{"status": 200}', "12 passed"):
            self.assertTrue(trail.status(r)[0], r)
    def test_signature_normalizes_numbers(self):
        self.assertEqual(trail.status("HTTP 403 at /a/b/1234")[1], trail.status("HTTP 404 at /x/y/99")[1].replace("#", "#"))


def tr(*evs):
    t = trail.Trail()
    for tool, args, res in evs:
        t.add(tool, args, res)
    return t


class TestGrade(unittest.TestCase):
    def test_unresolved_and_repeated(self):
        g = evidence.grade("# x\nok\n", tr(("web_fetch", {}, ERR), ("web_fetch", {}, ERR), ("web_fetch", {}, ERR)), [])
        self.assertEqual(g["grade"], "D")
        self.assertTrue({"unresolved-failure", "repeated-error"} <= {f["rule"] for f in g["findings"]})
    def test_unrun_verify_script(self):
        g = evidence.grade("run scripts/verify-api.sh\nValidated approach", tr(("terminal", {"command": "ls"}, OK)), [], ["verify-api.sh"])
        self.assertIn("verification-never-executed", {f["rule"] for f in g["findings"]})
    def test_verified_by_passing_test_is_A(self):
        g = evidence.grade("# Fix\nPin seed.", tr(("terminal", {"command": "pytest -q"}, json.dumps({"exit_code": 0}))), [])
        self.assertEqual(g["grade"], "A")
    def test_recovery_after_failure_ok(self):
        g = evidence.grade("# x\n", tr(("terminal", {"command": "make"}, ERR), ("terminal", {"command": "make -j1"}, OK)), [])
        self.assertIn(g["grade"], ("A", "B"))
    def test_omitted_user_constraint(self):
        cons = evidence.constraints_from(["User rule: only modify the develop branch. Never push to main."])
        self.assertTrue(cons)
        g = evidence.grade("# Ship\n1. git push origin main\n", tr(("terminal", {"command": "git status"}, OK)), cons)
        self.assertEqual(g["grade"], "D")
        g2 = evidence.grade("# Ship\nOnly the develop branch.\n git push origin develop\n", tr(("terminal", {"command": "git status"}, OK)), cons)
        self.assertNotIn("omits-user-constraint", {f["rule"] for f in g2["findings"]})
    def test_benign_plain_skill_is_B(self):
        self.assertEqual(evidence.grade("# Notes\nUse rg.\n", tr(("terminal", {"command": "ls"}, OK)), [])["grade"], "B")
    def test_placeholders_and_overconfidence(self):
        g = evidence.grade("TODO fill. Works 100% of the time.", tr(), [])
        self.assertEqual(g["grade"], "C")


class Base(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory(); self.home = Path(self.t.name)
        (self.home / "memories").mkdir(); self.w = engine.Witness(self.home)
    def tearDown(self): self.t.cleanup()
    def skill(self, name, body, **extra): cli._skill(self.home, name, body, extra)
    def create(self, key, name):
        self.w.on_tool(key, "skill_manage", {"action": "create", "name": name}, OK)


class TestEngine(Base):
    def test_issue_72269_quarantined_and_agent_told(self):
        for _ in range(3): self.w.on_tool("s", "web_fetch", {}, ERR)
        self.skill("weather", "# R\nValidated.\nRun scripts/verify-w.sh\n", **{"scripts/verify-w.sh": "x"})
        self.create("s", "weather")
        r = self.w.rec["weather"]
        self.assertEqual((r["grade"], r["status"]), ("D", "quarantined"))
        self.assertFalse((self.home / "skills/weather").exists())
        self.assertIn("QUARANTINED", self.w.notice_for("s")); self.assertIsNone(self.w.notice_for("s"))
    def test_observe_mode_keeps_skill(self):
        self.w.set_mode("observe")
        for _ in range(3): self.w.on_tool("s", "web_fetch", {}, ERR)
        self.skill("w", "# x\n"); self.create("s", "w")
        self.assertEqual(self.w.rec["w"]["status"], "active"); self.assertTrue((self.home / "skills/w").exists())
    def test_release_and_trust(self):
        for _ in range(3): self.w.on_tool("s", "web_fetch", {}, ERR)
        self.skill("w", "# x\n"); self.create("s", "w")
        self.w.release("w"); self.assertTrue((self.home / "skills/w/SKILL.md").exists())
        self.assertEqual(self.w.rec["w"]["status"], "trusted")
        self.create("s", "w"); self.assertEqual(self.w.rec["w"]["status"], "trusted")  # not re-flagged
        with self.assertRaises(ValueError): self.w.release("w")
    def test_failed_skill_manage_not_graded(self):
        self.skill("z", "# x\n"); self.w.on_tool("s", "skill_manage", {"action": "create", "name": "z"}, ERR)
        self.assertNotIn("z", self.w.rec)
    def test_outcome_tracking_and_degraded(self):
        self.w.on_tool("s", "terminal", {"command": "pytest"}, json.dumps({"exit_code": 0}))
        self.skill("good", "# g\n"); self.create("s", "good")
        for i in range(3):
            self.w.on_tool(f"u{i}", "skill_view", {"name": "good"}, OK)
            self.w.on_tool(f"u{i}", "terminal", {"command": "x"}, ERR); self.w.on_end(f"u{i}")
        r = self.w.rec["good"]; self.assertEqual((r["uses"], r["fail"]), (3, 3)); self.assertTrue(engine.Witness.degraded(r))
        self.w.on_tool("v", "skill_view", {"name": "good"}, OK)
        self.assertIn("unproven", self.w.notice_for("v"))
    def test_user_correction_counts_as_failure(self):
        self.skill("g", "# g\n"); self.create("s", "g")
        self.w.on_tool("u", "skill_view", {"name": "g"}, OK); self.w.on_tool("u", "terminal", {}, OK)
        self.w.on_user_message("u", "No, that's wrong, undo that"); self.w.on_end("u")
        self.assertEqual(self.w.rec["g"]["fail"], 1)
    def test_constraint_from_real_memory_file(self):
        (self.home / "memories/MEMORY.md").write_text("Only modify the develop branch. Never push to main.\n")
        self.w.on_tool("s", "terminal", {"command": "git status"}, OK)
        self.skill("ship", "git push origin main\n"); self.create("s", "ship")
        self.assertEqual(self.w.rec["ship"]["grade"], "D")
    def test_state_persists_and_ledger_verifies(self):
        for _ in range(3): self.w.on_tool("s", "web_fetch", {}, ERR)
        self.skill("w", "# x\n"); self.create("s", "w")
        w2 = engine.Witness(self.home); self.assertIn("w", w2.rec)
        self.assertTrue(w2.ledger.verify()[0])
        p = w2.ledger.path; p.write_text(p.read_text().replace('"D"', '"A"'))
        self.assertFalse(Ledger(w2.dir).verify()[0])


class TestBatchOps(Base):
    def test_operations_list_is_graded(self):
        for _ in range(2): self.w.on_tool("s", "terminal", {"command": "cat /nope"}, "Command executed: cat /nope (exited with code 1).")
        self.skill("test-broken-skill", "# x\nThis is a reliable, verified method.\n")
        self.w.on_tool("s", "skill_manage", {"operations": [{"action": "create", "name": "test-broken-skill"}]}, "Skill updated/created")
        r = self.w.rec["test-broken-skill"]
        self.assertEqual(r["grade"], "D"); self.assertEqual(r["status"], "quarantined")
    def test_exited_with_code_text_is_failure(self):
        self.assertFalse(trail.status("Command executed: x (exited with code 1).")[0])
        self.assertFalse(trail.status("$ cat /nonexistent [exit 1]")[0])
        self.assertTrue(trail.status("done (exited with code 0)")[0])


class TestTrace(Base):
    def test_trace_records_keys_not_values(self):
        self.w.on_tool("s", "terminal", {"command": "SECRET-VALUE"}, OK)
        rows = self.w.trace_rows()
        self.assertEqual(rows[-1]["tool"], "terminal"); self.assertEqual(rows[-1]["arg_keys"], ["command"])
        self.assertNotIn("SECRET-VALUE", (self.w.dir / "trace.jsonl").read_text())
    def test_trace_is_capped(self):
        for i in range(260): self.w.on_tool("s", "t", {}, OK)
        self.assertLessEqual(len((self.w.dir / "trace.jsonl").read_text().splitlines()), 200)


class TestGlue(unittest.TestCase):
    def test_register_and_hooks_never_raise(self):
        reg = {}
        class C:
            def register_hook(s, n, f): reg[n] = f
            def register_command(s, *a, **k): reg["cmd"] = 1
            def register_cli_command(s, *a, **k): reg["cli"] = 1
        mod.register(C())
        self.assertEqual(set(reg), {"post_tool_call", "pre_llm_call", "on_session_end", "cmd", "cli"})
        reg["post_tool_call"](tool_name=None, args=None, result=None)
        reg["pre_llm_call"](); reg["on_session_end"]()
    def test_demo_reproduces_published_cases(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): recs = cli.demo()
        self.assertEqual(recs["weather-queries"]["status"], "quarantined")
        self.assertEqual(recs["ship-it"]["grade"], "D")
        self.assertEqual(recs["fix-flaky-tests"]["grade"], "A")
        self.assertTrue(engine.Witness.degraded(recs["fix-flaky-tests"]))
        self.assertIn("intact", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
