#!/usr/bin/env bash
# ==============================================================================
# DeckStream Test Runner
# Verifies all daemon, runtime, profiler, and companion components
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
run_test "demand_gate"      python3 scripts/test_demand_gate.py
run_test "saves"            python3 scripts/test_saves.py
run_test "proton_matcher"   python3 scripts/test_proton_matcher.py
run_test "deck_mode"        python3 scripts/test_deck_mode.py
run_test "language_fix"     python3 scripts/test_language_fix.py
run_test "umu_gameid"       python3 scripts/test_umu_gameid.py
run_test "doctor"           python3 scripts/test_doctor.py
run_test "hedge_proxy"      python3 scripts/test_hedge_proxy.py
run_test "parallel_dl"      python3 scripts/test_parallel_download.py
run_test "parallel_unzip"   python3 scripts/test_parallel_unzip.py

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
