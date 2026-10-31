"""Mutation check: break each security guarantee on purpose and confirm the test suite goes red.

    python scripts/mutation_check.py      (needs a clean git tree: every mutated file is restored with git)

"A test that cannot fail is not a test" (Control Room test-engineer brief). Last run 2026-10-30:
all 6 mutants killed.
"""
import subprocess, sys
PY = r".venv\Scripts\python.exe"
MUTANTS = [
    ("step-up skipped for risky actions", "app/csm/policy.py",
     'return Verdict(Decision.STEP_UP, "risky action needs a one-time code", NOT_AUTHORIZED)',
     'return Verdict(Decision.ALLOW, "MUTANT")'),
    ("identity gate removed", "app/csm/policy.py",
     'return Verdict(Decision.NEED_VERIFICATION, "account data needs identity verification", NOT_AUTHORIZED)',
     'return Verdict(Decision.ALLOW, "MUTANT")'),
    ("unbacked action claims allowed", "app/csm/guardrails.py",
     'if rx.search(text) and not _NEG.search(text) and action not in done:',
     'if False:'),
    ("inbound security refusals off", "app/csm/guardrails.py",
     'return bool({"injection", "secret_request", "third_party"} & set(self.hits))',
     'return False'),
    ("redaction off", "app/csm/guardrails.py",
     '    if not text:\n        return text\n    kept: list[str] = []',
     '    return text\n    kept: list[str] = []'),
    ("lockout off", "app/csm/policy.py",
     'state.identity = LOCKED if state.id_failures >= MAX_ID_FAILURES else',
     'state.identity = LOCKED if False else'),
]
ok = True
for name, path, a, b in MUTANTS:
    src = open(path, encoding="utf-8").read()
    assert src.count(a) == 1, (name, "anchor not found")
    open(path, "w", encoding="utf-8", newline="").write(src.replace(a, b))
    try:
        r = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:warnings", "tests"], capture_output=True, text=True)
        tail = r.stdout.strip().splitlines()[-1]
        killed = r.returncode != 0
        ok &= killed
        print(f"{'KILLED ' if killed else 'SURVIVED'} {name:<38} {tail}")
    finally:
        subprocess.run(["git", "checkout", "--", path], check=True)
print("all mutants killed" if ok else "SOME MUTANTS SURVIVED")
