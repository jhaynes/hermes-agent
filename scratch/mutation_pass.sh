#!/bin/bash
# Mutation pass for plan rev2 §10: apply each mutant, run the named tests,
# confirm failure, revert. Records the mutation table.
set -u
cd /home/jth/src/github.com/NousResearch/hermes-agent/.worktrees/build-adaptive-admission
OUT=scratch/mutation_table.txt
: > "$OUT"

run_tests() {
    bash scripts/run_tests.sh -j 1 "$@" 2>&1 | grep -E "tests passed|tests failed" | tail -1
}

mutate() {
    local file="$1" old="$2" new="$3" label="$4" tests="$5"
    .venv/bin/python - "$file" "$old" "$new" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
if old not in text:
    print(f"OLD-NOT-FOUND: {old!r}")
    sys.exit(2)
open(path, "w").write(text.replace(old, new, 1))
PYEOF
    if [ $? -ne 0 ]; then
        echo "MUTANT $label: OLD STRING NOT FOUND (skip)" | tee -a "$OUT"
        return
    fi
    result=$(run_tests $tests)
    if echo "$result" | grep -q " 0 failed"; then
        echo "MUTANT $label: SURVIVED (tests still green!) -> $result" | tee -a "$OUT"
    else
        echo "MUTANT $label: KILLED -> $result" | tee -a "$OUT"
    fi
    git checkout -- "$file"
}

M=hermes_cli/kanban_admission.py
T1="tests/hermes_cli/test_kanban_admission.py"
TP="tests/hermes_cli/test_kanban_host_cap.py tests/hermes_cli/test_kanban_memory_guard.py"
TW="tests/gateway/test_kanban_adaptive_admission_watcher.py"

echo "=== Mutation table (plan rev2 §10) ===" | tee -a "$OUT"

# T1 mutants
mutate $M "return settings.step, \"green\"" "return settings.step + 1, \"green\"" "T1: grant ignores step" $T1
mutate $M "if n >= 1:
            self._last_grant_at = decision_now" "if n > 1:
            self._last_grant_at = decision_now" "T1/T2: window not started on n=1-partial" $T1
mutate $M "now - self._last_grant_at < settings.settle_seconds - PACING_SLACK" "now - self._last_grant_at < settings.settle_seconds" "T1: slack removed" $T1
mutate $M "if n >= 1:
            self._last_grant_at = decision_now" "if n >= 0:
            self._last_grant_at = decision_now" "T2: window starts on n=0" $T1
# T3 mutants
mutate $M "if signals.mem_avail_bytes is not None and signals.mem_avail_bytes < floor:
        return \"AMBER\", \"headroom\"" "pass" "T3: headroom dropped from AMBER" $T1
mutate $M "if signals.cpu_psi is not None and signals.cpu_psi >= settings.cpu_psi_hold:
        return \"AMBER\", \"cpu_psi\"" "pass" "T3: cpu hold dropped from AMBER" $T1
mutate $M "if level == \"RED\":
                self._last_red_at = now" "if False:
                self._last_red_at = now" "T4: cooldown never starts" $T1
# T4 mutants
mutate $M "if signals.mem_level in (\"elevated\", \"critical\"):
        return \"RED\", \"mem_level\"" "pass" "T4: mem_level dropped from RED" $T1
mutate $M "if signals.cpu_psi is not None and signals.cpu_psi >= settings.cpu_psi_backoff:
        return \"RED\", \"cpu_psi\"" "pass" "T4: cpu backoff dropped from RED" $T1
# T4b mutant
mutate $M "if signals.cpu_psi is not None and signals.cpu_psi >= settings.cpu_psi_backoff:
        return \"RED\", \"cpu_psi\"" "if signals.mem_psi is not None and signals.mem_psi >= 5:
        return \"RED\", \"mem_psi\"" "T4b: memory PSI wired into control" $T1
# T5 mutant
mutate $M "now - self._last_red_at < settings.backoff_cooldown_seconds" "now - self._last_red_at < settings.backoff_cooldown_seconds * 0" "T5: cooldown removed" $T1
# T8 mutants
mutate $M "if level == \"RED\" and trigger == \"mem_level\":
            return allowance" "return max(allowance or 0, settings.min_running - host_running) if level == \"RED\" else allowance" "T8: override beats the tiers" $T1
mutate $M "if host_running >= settings.min_running:
            return allowance" "if True:
            return allowance" "T8: override removed" $T1
mutate $M "if n >= 1:
            self._last_grant_at = decision_now" "self._last_grant_at = decision_now if n >= 1 else None" "T8: override doesn't pace" $T1
# T9 mutant
mutate $M "if host_running is not None and host_running > ceiling:" "if host_running is not None and False:" "T7/ceiling: over-cap ignored" $T1
# T10 mutants
mutate $M "elif signals.cpu_psi is None:
            reason = \"admission_inactive:cpu_psi\"" "elif False:
            reason = \"admission_inactive:cpu_psi\"" "T10: partial-signal mode" $T1
mutate $M "elif signals.mem_avail_bytes is None:
            reason = \"admission_inactive:mem_avail\"" "elif signals.mem_total_bytes is not None:
            reason = \"admission_inactive:mem_avail\"" "T10: headroom evaluated on missing data" $T1
# T12 mutants
mutate $M "if isinstance(mode_raw, str) and mode_raw in _MODES:" "if mode_raw in (*_MODES, True):" "T12: boolean accepted as enforce" $T1
mutate $M "if not hold < backoff:" "if False:" "T12: hold>=backoff validation bypassed" $T1
# T13 mutants
mutate $M "if chunk.startswith(\"avg10=\"):" "if chunk.startswith(\"avg60=\"):" "T13: wrong field" $T1
mutate $M "if not text.startswith(\"some \"):
        return None" "if not text.startswith(\"full \"):
        return None" "T13: full line parsed" $T1
# T14 mutant
mutate $M "return settings.step, \"green\"" "return (settings.step if self._last_grant_at is None else 99), \"green\"" "T14: init grants ceiling" $T1
# T24 mutants
mutate $M "multiple = max(0, settings.headroom_worker_multiple) * max(0, worker_bound)" "multiple = max(0, worker_bound)" "T24: multiple ignored" $T1
mutate $M "floor = max(settings.headroom_min_gib * _GIB, multiple)" "floor = multiple" "T24: min ignored" $T1
mutate $M "if mem_total > 0:
        floor = min(floor, mem_total // _MEM_TOTAL_HALF_DIVISOR)" "if False:
        floor = min(floor, mem_total // _MEM_TOTAL_HALF_DIVISOR)" "T24: clamp missing" $T1
mutate $M "if mem_total > 0:
        floor = min(floor, mem_total // _MEM_TOTAL_HALF_DIVISOR)" "if mem_total > 0:
        floor = max(floor, mem_total // _MEM_TOTAL_HALF_DIVISOR)" "T24: clamp inverted" $T1
# T29 mutants
mutate gateway/kanban_watchers.py "if live.mode == \"enforce\":" "if live.mode in (\"enforce\", \"shadow\"):" "T29: shadow enforces" $TW
# T27 mutants
mutate gateway/kanban_watchers.py "next_check = time.monotonic() + live.settle_seconds" "next_check = time.monotonic() + 0.0" "T27: pacing removed from loop" $TW
mutate gateway/kanban_watchers.py "if not decision.allowance:
                continue" "if decision.allowance is None and not decision.allowance:
                continue" "T27: zero allowance still ticks" $TW
# T28 mutant
mutate gateway/kanban_watchers.py "any_spawned = any_spawned or bool(spawned_since_full_tick)" "any_spawned = any_spawned or False" "T28: sub-pass spawns ignored" $TW
# T30 mutants
mutate gateway/kanban_watchers.py "if spawned_total == 0:" "if spawned_total < 0:" "T30: latch never arms" $TW
mutate gateway/kanban_watchers.py "if fingerprints == latches[\"fingerprints\"]:
                    continue" "if True:
                    continue" "T30: latch never releases" $TW
# dispatch-layer mutants (T15/T18/T25/T26)
mutate hermes_cli/kanban_db_dispatch.py "if spawn_budget is None or spawn_budget > spawn_allowance:
            spawn_budget = spawn_allowance
            result.admission_hold = spawn_allowance" "pass" "T15: allowance not applied" $TP
mutate hermes_cli/kanban_db_dispatch.py "remaining = max(0, remaining - spawned)" "pass" "T18: allowance not shared across boards" $TP
mutate hermes_cli/kanban_db_dispatch.py "if record_guard_events and not dry_run:" "if not dry_run:" "T26: guard event written in sub-pass" $TP
mutate hermes_cli/kanban_db_dispatch.py "if not admission_only:
        _run_reclaim_phase(" "if True:
        _run_reclaim_phase(" "T25: reclaim runs in sub-pass" $TP

echo "=== done ===" | tee -a "$OUT"
