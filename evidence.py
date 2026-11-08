"""Deterministic grading of a freshly written skill against the session trail and the user's own recorded constraints."""
from __future__ import annotations

import re
from collections import Counter

from .trail import VERIFY_CMD, Trail

VERIFY_CLAIM = re.compile(r"\b(verified|validated|proven|tested|battle-tested|reliable|guaranteed)\b", re.I)
OVERCONFIDENT = re.compile(r"(always works|never fails|100%|guarantee[sd]?|maximi[sz]e success|bulletproof)", re.I)
PLACEHOLDER = re.compile(r"\b(TODO|FIXME|XXX|REPLACE_ME|CHANGEME)\b|<your[-_ ]|\byour[-_]api[-_]key\b", re.I)
SCRIPT_REF = re.compile(r"(?:scripts?/)?([\w.-]+\.(?:sh|py|js|ts|rb))", re.I)
HIGH_IMPACT = [
    ("push", re.compile(r"\bgit\s+push\b", re.I)), ("merge", re.compile(r"\bgit\s+merge\b|\bgh\s+pr\s+merge\b", re.I)),
    ("deploy", re.compile(r"\b(deploy|kubectl\s+apply|terraform\s+apply|helm\s+upgrade)\b", re.I)),
    ("delete", re.compile(r"\brm\s+-rf?\b|\bDROP\s+(TABLE|DATABASE)\b|\bkubectl\s+delete\b|--force\b", re.I)),
    ("publish", re.compile(r"\b(npm|cargo|twine)\s+publish\b|\bpip\s+upload\b", re.I)),
]
CONSTRAINT = re.compile(r"\b(only|never|always|must|do not|don't|except|unless|before)\b", re.I)
KEY_TOKENS = re.compile(r"\b(main|master|develop|dev|staging|prod|production|release|hotfix|branch\w*|review|approval|confirm\w*|ask)\b", re.I)
ENVS = r"(main|master|develop|dev|staging|prod|production|release|hotfix)"
ONLY = re.compile(r"\bonly\b[^.\n]{0,40}?\b" + ENVS + r"\b", re.I)
NEVER = re.compile(r"\b(never|do not|don't)\b[^.\n]{0,40}?\b(main|master|production|prod|staging)\b", re.I)
WEIGHTS = {"unresolved-failure": 4, "omits-user-constraint": 4, "verification-never-executed": 3, "claims-verified-without-evidence": 2,
           "repeated-error": 2, "placeholders": 2, "overconfident-language": 1}


def constraints_from(memory_texts: list[str]) -> list[str]:
    out = []
    for t in memory_texts:
        for line in re.split(r"[\n.;]", t):
            line = line.strip(" -*•\t")
            if 12 <= len(line) <= 220 and CONSTRAINT.search(line) and KEY_TOKENS.search(line):
                out.append(line)
    return out[:40]


def grade(skill_text: str, trail: Trail, constraints: list[str], extra_files: list[str] | None = None) -> dict:
    f: dict[str, str] = {}
    window = trail.work()[-12:]
    # 1. unresolved failure at the moment of writing
    if window and not window[-1].ok:
        f["unresolved-failure"] = f"last tool call before saving failed: {window[-1].tool} → {window[-1].sig or 'error'}"
    # 2. same error repeated
    sigs = Counter(e.sig for e in window if not e.ok and e.sig)
    rep = [s for s, n in sigs.items() if n >= 2]
    if rep:
        f["repeated-error"] = f"same error {sigs[rep[0]]}x: {rep[0]}"
    # 3. verification claims vs. execution
    scripts = set(m.group(1).lower() for m in SCRIPT_REF.finditer(skill_text)) | {s.lower() for s in (extra_files or [])}
    ran = " ".join(e.args.lower() for e in trail.work() if e.ok)
    unrun = sorted(s for s in scripts if s not in ran and ("verif" in s or "test" in s or "check" in s))
    if unrun:
        f["verification-never-executed"] = f"{', '.join(unrun)} was written but never run successfully in this session"
    positive = any(e.ok and VERIFY_CMD.search(e.args) for e in window)
    if VERIFY_CLAIM.search(skill_text) and not positive and "verification-never-executed" not in f:
        f["claims-verified-without-evidence"] = "skill claims it is verified/reliable; no passing test or verify command in the trail"
    # 4. dropped user constraints (the 'pushed to main' class)
    for name, rx in HIGH_IMPACT:
        m = rx.search(skill_text)
        if not m:
            continue
        action_lines = [ln.lower() for ln in skill_text.splitlines() if rx.search(ln)]
        low = skill_text.lower()
        for c in constraints:
            only = ONLY.search(c)
            never = NEVER.search(c)
            if only and only.group(1).lower() not in low:
                f["omits-user-constraint"] = f"skill performs '{name}' but never mentions '{only.group(1).lower()}' from your rule: “{c[:110]}”"
            elif never and any(never.group(2).lower() in ln for ln in action_lines):
                f["omits-user-constraint"] = f"skill performs '{name}' on '{never.group(2).lower()}', which your rule forbids: “{c[:110]}”"
            if "omits-user-constraint" in f:
                break
        if "omits-user-constraint" in f:
            break
    if PLACEHOLDER.search(skill_text):
        f["placeholders"] = "contains TODO/placeholder text"
    if OVERCONFIDENT.search(skill_text) and not positive:
        f["overconfident-language"] = "confident claims with no supporting evidence"
    score = sum(WEIGHTS[k] for k in f)
    recovered = any(not e.ok for e in window) and window and window[-1].ok
    if positive:
        score -= 2
    elif recovered:
        score -= 1
    score = max(score, 0)
    g = "D" if score >= 4 else "C" if score >= 2 else "A" if positive else "B"
    return {"grade": g, "score": score, "positive": bool(positive), "findings": [{"rule": k, "detail": v, "weight": WEIGHTS[k]} for k, v in f.items()]}
