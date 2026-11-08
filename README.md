# ☤ Hermes Witness

**Evidence-gated learning for Hermes Agent.** Your agent grades its own homework. Witness checks the receipts.

Hermes writes skills from tasks it judges successful. Maintainers and users have documented where that breaks: an unresolved failure saved as a "validated" skill (NousResearch/hermes-agent #72269), a learned skill that silently dropped a "develop branch only" rule, and an open request for a write-time gate (#25833). Witness is that gate. It is deterministic, local, and uses no LLM calls.

```
$ hermes witness demo
D  weather-queries   QUARANTINED
   ! unresolved-failure: last tool call before saving failed: web_fetch → http # forbidden
   ! repeated-error: same error 3x
   ! verification-never-executed: verify-weather-api.sh was written but never run
D  ship-it           QUARANTINED
   ! omits-user-constraint: skill performs 'push' but never mentions 'develop' from your rule
A  fix-flaky-tests   → after 3 real uses, 0/3 ok  DEGRADED
```

## What it checks (at write time)
| Signal | Source |
|---|---|
| Unresolved failure when the skill was saved | tool results, parsed (exit codes, errors, tracebacks, HTTP 4xx/5xx) |
| Same error repeated | normalized error signatures |
| Verify script written but never run | skill files vs. successful tool calls |
| "Verified/reliable" claims with no passing test | skill text vs. trail |
| Skill's high-impact action (push, deploy, delete, publish) vs. **your own rules** in MEMORY.md / USER.md | "only X" / "never Y" constraint extraction |
| Placeholders, unsupported confidence | skill text |

Grades: **A** verified by a passing command · **B** no negative evidence · **C** weak · **D** contradicted by the trail.

## What it tracks (after)
Each time a graded skill is loaded, the session's objective outcome is recorded (final tool result, error ratio, user corrections such as "that's wrong"). A skill used 3+ times that fails 60%+ is marked DEGRADED. Real use beats self-reports.

## Response
- `gate` (default): quarantine grade D (moved to `~/.hermes/witness/quarantine`, fully reversible).
- `observe`: label only.
- The agent is told next turn (`pre_llm_call`), and weak skills show a caution when loaded.
- `trust` / `release` are **CLI-only**: the agent cannot vouch for itself.
- All decisions go to a hash-chained ledger (`hermes witness verify`).

## Commands
`report` · `show <skill>` · `trust <skill>` · `release <skill>` · `mode [observe|gate]` · `verify` · `demo`. `/witness` in-session is read-only.

## Install
```bash
cp -r hermes-witness ~/.hermes/plugins/ && hermes plugins enable hermes-witness
hermes plugins validate ~/.hermes/plugins/hermes-witness && hermes plugins doctor hermes-witness
hermes witness demo
```

## Honest limits
- Heuristics parse tool results; unknown result shapes count as success, so it errs toward missing, not crying wolf.
- Skill writes are detected via `skill_manage` calls (create/edit/patch). Writes by other routes aren't graded; pair with a file-watcher such as Tripwire.
- Constraint checking understands "only <env/branch>" and "never <main/prod/…>" phrasing, not arbitrary prose.
- It cannot prove a skill correct. It can show when the evidence contradicts it.
- Hook argument shapes (`skill_manage` args, `skill_view` name key, `pre_llm_call` user_message) are based on public docs and source; verify with `hermes plugins doctor`.

## Develop
`python3 -m unittest discover -s tests -v` (stdlib only, 19 tests)
