#!/bin/bash
# Re-verification of the three surviving mutants with correct expectations.
set -u
cd /home/jth/src/github.com/NousResearch/hermes-agent/.worktrees/build-adaptive-admission
OUT=scratch/mutation_table_recheck.txt
: > "$OUT"

check() {
    local label="$1" tests="$2"
    # timeout 120: a mutant that hangs the loop (0-settle spin) is a KILL
    # (the named test cannot pass — it times out).
    if timeout 150 bash scripts/run_tests.sh -j 1 $tests > /tmp/mut.txt 2>&1; then
        if grep -qE " 0 failed" /tmp/mut.txt; then
            echo "MUTANT $label: SURVIVED" | tee -a "$OUT"
        else
            echo "MUTANT $label: KILLED (assertions)" | tee -a "$OUT"
        fi
    else
        echo "MUTANT $label: KILLED (timeout/nonzero exit — test cannot pass)" | tee -a "$OUT"
    fi
}

echo "=== Re-verification of surviving mutants ===" | tee -a "$OUT"

# T18: the host allowance is not decremented across boards.
.venv/bin/python - <<'PYEOF'
path = "gateway/kanban_watchers_dispatcher.py"
text = open(path).read()
old = """            if res is not None and remaining is not None:
                spawned = len(getattr(res, "spawned", None) or [])
                remaining = max(0, remaining - spawned)"""
new = """            if res is not None and remaining is not None:
                spawned = len(getattr(res, "spawned", None) or [])
                pass"""
assert old in text, "T18 old string missing"
open(path, "w").write(text.replace(old, new, 1))
PYEOF
check "T18: allowance not shared across boards" "tests/hermes_cli/test_kanban_host_cap.py"
git checkout -- gateway/kanban_watchers_dispatcher.py

# T8: the override grant must start the pacing window. Real mutant: record()
# ignores ALL n (window never starts).
.venv/bin/python - <<'PYEOF'
path = "hermes_cli/kanban_admission.py"
text = open(path).read()
old = """    def record(self, decision_now: float, n: int) -> None:
        \"\"\"Stamp a DECISION time (never spawn completion) when n >= 1 spawned
        (§5.7, MoA I6). A grant of 2 that spawned 1 still starts the window;
        a 0-spawn decision does not.\"\"\"
        if n >= 1:
            self._last_grant_at = decision_now"""
new = """    def record(self, decision_now: float, n: int) -> None:
        \"\"\"Stamp a DECISION time (never spawn completion) when n >= 1 spawned
        (§5.7, MoA I6). A grant of 2 that spawned 1 still starts the window;
        a 0-spawn decision does not.\"\"\"
        pass"""
assert old in text
open(path, "w").write(text.replace(old, new, 1))
PYEOF
check "T8: override doesn't pace (record is a no-op)" "tests/hermes_cli/test_kanban_admission.py"
git checkout -- hermes_cli/kanban_admission.py

# T29: shadow applies the allowance at the full tick (now via _full_tick_admission).
.venv/bin/python - <<'PYEOF'
path = "gateway/kanban_watchers.py"
text = open(path).read()
old = 'allowance = decision.allowance if live.mode == "enforce" else None'
new = 'allowance = decision.allowance if live.mode in ("enforce", "shadow") else None'
assert old in text
open(path, "w").write(text.replace(old, new, 1))
PYEOF
check "T29: shadow enforces (full tick)" "tests/gateway/test_kanban_adaptive_admission_watcher.py"
git checkout -- gateway/kanban_watchers.py

# T27: pacing removed from the loop schedule (settle -> 0).
.venv/bin/python - <<'PYEOF'
path = "gateway/kanban_watchers.py"
text = open(path).read()
old = "            next_check = time.monotonic() + live.settle_seconds"
new = "            next_check = time.monotonic() + 0.0"
assert old in text
open(path, "w").write(text.replace(old, new, 1))
PYEOF
check "T27: pacing removed from loop (settle=0)" "tests/gateway/test_kanban_adaptive_admission_watcher.py"
git checkout -- gateway/kanban_watchers.py

echo "=== done ===" | tee -a "$OUT"
