#!/bin/bash
# R2 reconcile mutation re-check: the mutants the R1 verdicts named, re-run
# against the post-fix code (loop restructure + WAL fingerprint could have
# altered earlier kills).
set -u
cd /home/jth/src/github.com/NousResearch/hermes-agent/.worktrees/build-adaptive-admission
OUT=scratch/r2_mutation_recheck.txt
: > "$OUT"

run_tests() {
    scripts/run_tests.sh -j 1 "$@" 2>&1 | grep -E "Summary: " | tail -1
}

mutate() {
    local file="$1" old="$2" new="$3" label="$4" tests="$5"
    cp "$file" /tmp/r2_mut_backup.py
    python3 - "$file" "$old" "$new" <<'PYEOF'
import sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
if old not in text:
    print(f"OLD-NOT-FOUND: {old!r}")
    sys.exit(2)
open(path, "w").write(text.replace(old, new, 1))
PYEOF
    if [ $? -ne 0 ]; then
        echo "MUTANT $label: OLD-NOT-FOUND (skip)" | tee -a "$OUT"
        return
    fi
    result=$(run_tests $tests)
    if echo "$result" | grep -q " 0 failed"; then
        echo "MUTANT $label: SURVIVED (tests still green!) -> $result" | tee -a "$OUT"
    else
        echo "MUTANT $label: KILLED -> $result" | tee -a "$OUT"
    fi
    cp /tmp/r2_mut_backup.py "$file"
}

W="tests/gateway/test_kanban_adaptive_admission_watcher.py"

# W2: full tick passes allowance=None in enforce
mutate gateway/kanban_watchers.py \
"spawn_allowance=allowance,
                            admission_reason=reason,
                        ))" \
"spawn_allowance=None,
                            admission_reason=reason,
                        ))" \
"W2_fulltick_allowance_none" "$W"

# W1: full-tick record dropped
mutate gateway/kanban_watchers.py \
"if allowance is not None and decision is not None:
                        # Pacing stamps the DECISION time, never spawn
                        # completion (§5.2 MoA B2, §5.7; R1 arch A3).
                        controller.record(decision.now, spawned_total)" \
"if False:
                        pass" \
"W1_fulltick_record_dropped" "$W"

# A3: record stamped at completion time
mutate gateway/kanban_watchers.py \
"controller.record(decision.now, spawned_total)
                    ready_pending = await _to_thread_process_service(dispatcher.ready_nonempty)" \
"controller.record(time.monotonic(), spawned_total)
                    ready_pending = await _to_thread_process_service(dispatcher.ready_nonempty)" \
"A3_completion_stamp" "$W"

# Q2: WAL sidecar dropped from the fingerprint
mutate gateway/kanban_watchers_dispatcher.py \
"        main_stat = _stat(path)
        wal_stat = _stat(path.parent / (path.name + \"-wal\"))
        return (resolved, main_stat, wal_stat)" \
"        main_stat = _stat(path)
        return (resolved, main_stat, (None, None))" \
"Q2_wal_blind_fingerprint" "$W"

# Sub-pass spawns never feed bad_ticks (tests F2 / arch A2)
mutate gateway/kanban_watchers.py \
"spawned_since_full_tick = spawned_total + sub_passes_last_interval" \
"spawned_since_full_tick = spawned_total" \
"F2_subpass_spawns_not_fed" "$W"

# controller=None crash reintroduced (gate on boot mode only)
mutate gateway/kanban_watchers.py \
"controller = _ka.AdmissionController(_ka.parse_admission_settings(kanban_cfg))" \
"controller = None
        if _ka.parse_admission_settings(kanban_cfg).mode != \"off\":
            controller = _ka.AdmissionController(_ka.parse_admission_settings(kanban_cfg))" \
"F1_bootoff_controller_none" "$W"

# parse_admission_settings raises on mixed-type keys (sys F1)
mutate hermes_cli/kanban_admission.py \
"    for key in set(block) - _KNOWN_KEYS:
        _warn_bad(key, block[key], warn)" \
"    for key in sorted(set(block) - _KNOWN_KEYS):
        _warn_bad(key, block[key], warn)" \
"sysF1_mixed_type_keys_sort" "tests/hermes_cli/test_kanban_admission.py"

# Huge int OverflowError (sys F1 / quality Q4)
mutate hermes_cli/kanban_admission.py \
"    try:
        value = float(raw)
    except (OverflowError, ValueError):  # e.g. int too large for float (§6)
        _warn_bad(key, raw, warn)
        return default" \
"    value = float(raw)" \
"sysF1_huge_int_overflow" "tests/hermes_cli/test_kanban_admission.py"

# Shadow never decides on the sub-pass schedule (quality Q3 / arch A5)
mutate gateway/kanban_watchers.py \
"                if live.mode == \"shadow\":
                    # Shadow computes every decision on the sub-pass schedule
                    # but NEVER spawns (§5.1, T29) — its counters must
                    # predict enforce's cadence (R1 quality Q3 / arch A5).
                    continue" \
"                if live.mode == \"shadow\" or True:
                    continue" \
"Q3_shadow_never_decides" "$W"

echo "=== done ===" | tee -a "$OUT"
