#!/usr/bin/env bash
# ==============================================================================
# DeckStream Test Runner
# Daemon, site API, storage server, Decky panel and a browser smoke test of the app
# ==============================================================================
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FAILED=0
run_test() {
    local name="$1"; shift
    printf "%-25s " "$name"
    if out=$("$@" 2>&1); then
        echo "PASS"
    else
        echo "FAIL"
        echo "$out" | tail -n 15
        FAILED=1
    fi
}

echo "Running DeckStream test suite..."
run_test "manifest"         python3 scripts/make_manifest.py --check
run_test "version"          python3 scripts/check_version.py
run_test "early_launch_calc" python3 scripts/test_early_launch_calc.py
run_test "replay_rig"        python3 scripts/test_replay_rig.py
run_test "deck_smoke"        bash scripts/test_deck_smoke.sh
run_test "rollback_safety"   python3 scripts/test_rollback_safety.py
run_test "suspend_resume"    python3 scripts/test_suspend_resume.py
run_test "runner_lock"       bash scripts/test_runner_lock.sh
run_test "daemon_safety"     python3 scripts/test_daemon_safety.py
run_test "webdav_e2e"        python3 scripts/test_webdav_e2e.py
run_test "demand_gate"      python3 scripts/test_demand_gate.py
run_test "saves"            python3 scripts/test_saves.py
run_test "proton_matcher"   python3 scripts/test_proton_matcher.py
run_test "deck_mode"        python3 scripts/test_deck_mode.py
run_test "daemon_parts"     python3 scripts/test_daemon_parts.py
run_test "language_fix"     python3 scripts/test_language_fix.py
run_test "umu_gameid"       python3 scripts/test_umu_gameid.py
run_test "doctor"           python3 scripts/test_doctor.py
run_test "hedge_proxy"      python3 scripts/test_hedge_proxy.py
run_test "parallel_dl"      python3 scripts/test_parallel_download.py
run_test "parallel_unzip"   python3 scripts/test_parallel_unzip.py
run_test "shell_syntax"     bash -c 'for f in daemon/*.sh scripts/*.sh; do bash -n "$f" || exit 1; done'
run_test "python_compile"   python3 -m py_compile daemon/*.py decky/main.py
run_test "clean_tree"       python3 scripts/test_clean_tree.py
run_test "api"              node scripts/test_api.js
run_test "push"             node scripts/test_push.js
run_test "decky_plugin"     python3 scripts/test_decky_plugin.py
run_test "app_e2e"          node scripts/test_e2e_app.js

if [ $FAILED -eq 0 ]; then
    echo "=========================================="
    echo "🎉 ALL DECKSTREAM TESTS PASSED CLEANLY"
    echo "=========================================="
    exit 0
else
    echo "=========================================="
    echo "❌ SOME DECKSTREAM TESTS FAILED"
    echo "=========================================="
    exit 1
fi
