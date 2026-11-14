#!/usr/bin/env bash
# Line coverage for the Python daemon tests and the Node site API tests. Reports only; no threshold fails the build.
# Needs: pip install coverage==7.6.10 (Python) and network for npx c8 (Node).
# Writes coverage/summary.md, coverage/python/ (HTML) and coverage/node/ (HTML + lcov).
# Code a test launches in a subprocess (the daemon CLI, shell scripts) is not counted, so the numbers are a floor.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
rm -rf coverage .coverage .coverage.*
mkdir -p coverage

PY_TESTS=(demand_gate saves proton_matcher deck_mode language_fix umu_gameid doctor hedge_proxy
          parallel_download parallel_unzip decky_plugin)
for t in "${PY_TESTS[@]}"; do
    python3 -m coverage run --parallel-mode --source=daemon,decky --omit='*/node_modules/*' "scripts/test_$t.py" >/dev/null 2>&1
done
python3 -m coverage combine -q
python3 -m coverage html -q -d coverage/python
py_report="$(python3 -m coverage report --format=markdown)"

npx --yes c8@10.1.3 --reporter=text-summary --reporter=html --reporter=lcov --report-dir=coverage/node \
    --include='api/**' --include='scripts/storage_server.js' \
    bash -c 'node scripts/test_api.js && node scripts/test_push.js' > coverage/node.txt
node_report="$(grep -E '^(Statements|Branches|Functions|Lines)' coverage/node.txt || true)"

{
    echo "## Test coverage"
    echo
    echo "Line coverage from in-process tests. Subprocess runs are not counted, so treat these as a floor."
    echo
    echo "### Python (daemon/, decky/main.py)"
    echo
    echo "$py_report"
    echo
    echo "### Node (api/, storage server)"
    echo
    echo '```'
    echo "$node_report"
    echo '```'
} > coverage/summary.md
cat coverage/summary.md
