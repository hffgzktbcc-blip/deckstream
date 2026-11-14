#!/usr/bin/env bash
# Verifies the per-game launch lock in daemon/stream-runner.sh: one launch per game, even when Play is pressed several times at once,
# and even after a runner was killed at the worst moment (kill -9 of the holder, leftover folders from the old mkdir lock).
set -euo pipefail

R="$(cd "$(dirname "$0")/.." && pwd)/daemon/stream-runner.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT

grep -q 'runner_${GAME_ID}.lock' "$R" || { echo "[x] lock filename missing"; exit 1; }
grep -q '# >>> launch_lock' "$R" && grep -q '# <<< launch_lock' "$R" || { echo "[x] launch_lock markers missing"; exit 1; }
echo "[ok] stream-runner.sh contains the per-game launch lock"

TEST_HOME="$T/home"
mkdir -p "$TEST_HOME/.config/deckstream" "$TEST_HOME/.local/share/deckstream/logs"
echo "STORAGE_KEY=mock-key" > "$TEST_HOME/.config/deckstream/storage.conf"

# the extracted lock block, run by a stub "runner" that HOLDS the lock for a moment (a winner that exits at once would leave a
# stale lock for a late racer to win legitimately, which is not a race)
sed -n '/# >>> launch_lock/,/# <<< launch_lock/p' "$R" > "$T/lockblock.sh"
cat > "$T/stub.sh" <<STUB
#!/usr/bin/env bash
set -uo pipefail
export XDG_RUNTIME_DIR="\$1"; OUT="\$2"; N="\$3"
GAME_ID=testgame GAME_NAME="Test Game" NC="" YELLOW=""
. "$T/lockblock.sh"
echo "WON \$\$" > "\$OUT/won_\$N"
sleep "\${HOLD:-1.5}"
rm -rf "\$RUN_LOCK" "\$RUN_LOCK.reclaim"
STUB

scenario() {   # scenario NAME SETUP... : 8 simultaneous launches, prints the number of winners
    local name="$1" setup="$2" S; S="$(mktemp -d "$T/scenario.XXXXXX")"
    local LD="$S/rt/deckstream_locks"; mkdir -p "$LD" "$S/out"
    case "$setup" in
        fresh) ;;
        stale) mkdir "$LD/runner_testgame.lock"; echo 999999 > "$LD/runner_testgame.lock/pid" ;;
        leftover_dead) mkdir "$LD/runner_testgame.lock" "$LD/runner_testgame.lock.reclaim"; echo 999999 > "$LD/runner_testgame.lock/pid"; echo 999998 > "$LD/runner_testgame.lock.reclaim/pid" ;;
        leftover_nopid_old) mkdir "$LD/runner_testgame.lock" "$LD/runner_testgame.lock.reclaim"; echo 999999 > "$LD/runner_testgame.lock/pid"; touch -t 202001010000 "$LD/runner_testgame.lock.reclaim" ;;
        nopid_old) mkdir "$LD/runner_testgame.lock"; touch -t 202001010000 "$LD/runner_testgame.lock" ;;
        live_holder) mkdir "$LD/runner_testgame.lock"; echo "$$" > "$LD/runner_testgame.lock/pid" ;;
        nopid_fresh) mkdir "$LD/runner_testgame.lock" ;;
        killed_holder)   # a real runner holds the lock and is then killed with SIGKILL: its lock must be free at once
            HOLD=30 bash "$T/stub.sh" "$S/rt" "$S/held" 0 >/dev/null 2>&1 & local hp=$!
            mkdir -p "$S/held"; local w; for w in $(seq 1 100); do [ -f "$S/held/won_0" ] && break; sleep 0.05; done
            kill -9 "$hp"; wait "$hp" 2>/dev/null || true; sleep 0.5 ;;
    esac
    local i; for i in 1 2 3 4 5 6 7 8; do bash "$T/stub.sh" "$S/rt" "$S/out" "$i" > "$S/out/log_$i" 2>&1 & done
    wait
    ls "$S/out" | grep -c '^won_' || true
}
expect() {   # expect NAME SETUP WINNERS
    local got; got="$(scenario "$1" "$2")"
    if [ "$got" = "$3" ]; then echo "[ok] $1: $got winner(s) of 8 simultaneous launches"; else echo "[x] $1: expected $3 winner(s), got $got"; exit 1; fi
}
expect "fresh lock" fresh 1
expect "stale lock (dead holder)" stale 1
expect "dead lock + reclaim mutex left by a KILLED reclaimer" leftover_dead 1
expect "dead lock + reclaim mutex with no pid, old" leftover_nopid_old 1
expect "lock folder made but pid never written, old (killed in between)" nopid_old 1
expect "a live holder keeps its lock" live_holder 0
expect "leftover lock folder with no pid and no flock holder: free" nopid_fresh 1
expect "holder killed with kill -9: lock free at once, one new winner" killed_holder 1
for r in 1 2 3; do expect "repeat $r: stale lock race" stale 1; done
# CI run 94 caught 2 winners in this scenario (about 1 run in 30 on the old mkdir lock); hammer it.
for r in $(seq 1 15); do HOLD=0.4 expect "repeat $r: leftover reclaim mutex race" leftover_nopid_old 1 >/dev/null || { echo "[x] leftover reclaim mutex race: 2 winners on repeat $r"; exit 1; }; done
echo "[ok] leftover reclaim mutex race: 1 winner in 15 of 15 repeats"

# the real runner script: a duplicate press prints the message and exits 0 without doing anything
export XDG_RUNTIME_DIR="$T/runtime"; mkdir -p "$XDG_RUNTIME_DIR/deckstream_locks/runner_testgame.lock"
echo "$$" > "$XDG_RUNTIME_DIR/deckstream_locks/runner_testgame.lock/pid"
out="$(HOME="$TEST_HOME" bash "$R" testgame "Test Game" "Test/Folder" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && echo "$out" | grep -q "already in progress" && echo "[ok] the real runner ignores a duplicate press cleanly (exit 0)" || { echo "[x] duplicate press: rc=$rc $out"; exit 1; }

echo "RUNNER LOCK TESTS PASSED"
