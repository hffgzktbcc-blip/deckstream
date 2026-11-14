#!/usr/bin/env bash
# Tests scripts/deck_smoke.sh with a FAKE ssh/scp (nothing ever reaches a real Deck): argument checks, the shortcut game-id
# arithmetic, the dry run, and every preflight refusal.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="$ROOT/scripts/deck_smoke.sh"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
fail=0
ok()  { echo "[ok] $1"; }
bad() { echo "[x] $1"; fail=1; }

# fake ssh: logs every call, prints $FAKE_OUT (the preflight answer) and exits $FAKE_RC
cat > "$T/fakessh" <<'EOF'
#!/usr/bin/env bash
echo "ssh $*" >> "$FAKE_LOG"
printf '%b' "${FAKE_OUT:-}"
exit "${FAKE_RC:-0}"
EOF
chmod +x "$T/fakessh"
export DECK_HOST="deck@deck.invalid" DECK_SSH="$T/fakessh" DECK_SCP="$T/fakessh" FAKE_LOG="$T/log" DECK_KEY="$T/nokey"
: > "$FAKE_LOG"
run() { bash "$S" --slug game_x --appid 3155796970 --exe game.exe "$@" 2>&1; }

bash -n "$S" && ok "deck_smoke.sh parses" || bad "syntax error"

# 1) usage errors
out="$(bash "$S" 2>&1)"; rc=$?
[ $rc -eq 64 ] && ok "missing arguments exit 64" || bad "missing args rc=$rc"
out="$(bash "$S" --slug a --appid abc --exe g.exe 2>&1)"; rc=$?
[ $rc -eq 64 ] && echo "$out" | grep -q "must be a number" && ok "a non-numeric appid is rejected" || bad "appid check rc=$rc: $out"
out="$(bash "$S" --slug 'a;b' --appid 1 --exe g.exe 2>&1)"; rc=$?
[ $rc -eq 64 ] && ok "shell characters in --slug are rejected" || bad "slug injection rc=$rc"
out="$(bash "$S" --slug a --appid 1 --exe 'g.exe;reboot' 2>&1)"; rc=$?
[ $rc -eq 64 ] && ok "shell characters in --exe are rejected" || bad "exe injection rc=$rc"

# 2) game-id arithmetic: (appid << 32) | 0x02000000
want="$(python3 -c "print((3155796970 << 32) | 0x02000000)")"
out="$(run --dry-run)"; rc=$?
[ $rc -eq 0 ] && echo "$out" | grep -q "gameid=$want" && ok "dry run prints the 64-bit game id ($want)" || bad "dry run rc=$rc: $out"
[ ! -s "$FAKE_LOG" ] && ok "a dry run sends nothing to the Deck" || bad "dry run called ssh: $(cat "$FAKE_LOG")"

# 3) preflight refusals (each must exit 2 and say why) and the happy path
good='sessions=0\nbusy=0\nlock=free\nbat=Charging:55\nfree_gb=100\n'
refuses() {   # refuses "label" "preflight text" "expected phrase"
    : > "$FAKE_LOG"
    out="$(FAKE_OUT="$2" run --preflight-only)"; rc=$?
    [ $rc -eq 2 ] && echo "$out" | grep -q "$3" && ok "refuses: $1" || bad "$1 (rc=$rc): $out"
}
refuses "a session marker exists" 'sessions=1\nbusy=0\nlock=free\nbat=Charging:55\nfree_gb=100\n' "session marker"
refuses "a fill or game is running" 'sessions=0\nbusy=2\nlock=free\nbat=Charging:55\nfree_gb=100\n' "already running"
refuses "another operator holds the lock" 'sessions=0\nbusy=0\nlock=held\nbat=Charging:55\nfree_gb=100\n' "another operator"
refuses "battery low and discharging" 'sessions=0\nbusy=0\nlock=free\nbat=Discharging:14\nfree_gb=100\n' "battery"
refuses "too little free space" 'sessions=0\nbusy=0\nlock=free\nbat=Charging:55\nfree_gb=12\n' "GB free"
refuses "unreadable battery" 'sessions=0\nbusy=0\nlock=free\nbat=:\nfree_gb=100\n' "battery"

out="$(FAKE_OUT="$good" run --preflight-only)"; rc=$?
[ $rc -eq 0 ] && echo "$out" | grep -q "preflight ok" && ok "a healthy Deck passes the preflight" || bad "happy preflight rc=$rc: $out"
out="$(FAKE_OUT='sessions=0\nbusy=0\nlock=free\nbat=Discharging:80\nfree_gb=100\n' run --preflight-only)"; rc=$?
[ $rc -eq 0 ] && ok "discharging at 80% is allowed" || bad "80% discharging rc=$rc: $out"

# 4) an unreachable Deck is a refusal, not a crash
out="$(FAKE_RC=255 FAKE_OUT='' run --preflight-only)"; rc=$?
[ $rc -eq 2 ] && ok "an unreachable Deck exits 2" || bad "unreachable rc=$rc: $out"

# 5) the preflight really is read-only: no kill, no rm, no steam launch in what was sent
: > "$FAKE_LOG"; FAKE_OUT="$good" run --preflight-only >/dev/null
if grep -qE "kill|rm -|rmdir|rungameid|reboot|poweroff|systemctl" "$FAKE_LOG"; then bad "preflight sent a mutating command: $(cat "$FAKE_LOG")"; else ok "the preflight sends only read-only commands"; fi

# 6) the script itself never uses KILL, sudo or a variable-path rm
if grep -nE "kill -9|kill -KILL|sudo|rm -rf" "$S" >/dev/null; then bad "deck_smoke.sh contains kill -9 / sudo / rm -rf"; else ok "no kill -9, sudo or rm -rf in deck_smoke.sh"; fi

# 7) 13-14 Oct lessons: 'launched' in telemetry is NOT proof; Linux cuts a process name to 15 chars; the appid can be looked up
: > "$FAKE_LOG"
out="$(bash "$S" --slug example_game --appid auto --exe Cyberpunk2077.exe --dry-run 2>&1)"; rc=$?
[ $rc -eq 0 ] && echo "$out" | grep -q "resolved on the Deck" && echo "$out" | grep -q "process-name=Cyberpunk2077.e" && [ ! -s "$FAKE_LOG" ] \
  && ok "--appid auto is accepted (dry run sends nothing) and a 17-char exe is matched by its 15-char process name" || bad "appid auto / process name (rc=$rc): $out"
out="$(bash "$S" --slug a --appid 12abc --exe g.exe 2>&1)"; rc=$?
[ $rc -eq 64 ] && ok "a half-numeric appid is still rejected" || bad "12abc accepted rc=$rc"
if grep -qE 'pgrep -x \$EXE' "$S"; then bad "pgrep -x \$EXE is back: a 15+ char exe name never matches (comm is truncated)"; else ok "every pgrep -x uses the truncated process name"; fi
grep -q 'exe_alive' "$S" && ok "each sample records whether the REAL game process is alive" || bad "no exe_alive column"
# the summary says plainly when the game process never appeared
sed -n "/^python3 - \"\$CSV\"/,/^PY\$/p" "$S" | sed '1d;$d' > "$T/summary.py"
printf 'ts,cache_mb,net_rx,game_rchar,rclone_rchar,rclone_wbytes,iowait,load1,battery,exe_alive\n100,1,1000,1,1,1,0,0.1,80,0\n130,2,2000,2,2,2,0,0.1,80,0\n' > "$T/s.csv"
printf 'play=1\nlaunched=5\n' > "$T/l.txt"; echo -1 > "$T/seen_no.txt"; echo 42 > "$T/seen_yes.txt"
python3 "$T/summary.py" "$T/s.csv" "$T/l.txt" "$T/seen_no.txt" 2>&1 | grep -q "NEVER SEEN" && ok "summary: a game process that never appeared is reported as NEVER SEEN" || bad "summary does not flag a missing game process"
python3 "$T/summary.py" "$T/s.csv" "$T/l.txt" "$T/seen_yes.txt" 2>&1 | grep -q "seen 42 s after Play" && ok "summary: a game process that appeared is reported with its time" || bad "summary does not report the process time"

# the process check must never fall back to pgrep -f: over ssh it matches the checking shell itself (a false "seen 7s after Play")
if grep -q "pgrep -f" "$S"; then bad "pgrep -f fallback is back (self-matches over ssh)"; else ok "no pgrep -f self-match fallback"; fi
# Full mocked launch paths: no telemetry, telemetry without process, and success.
cat > "$T/lifessh" <<'MOCK'
#!/usr/bin/env bash
cmd="${!#}"
echo "$cmd" >> "$FAKE_LOG"
case "$cmd" in
  'echo sessions='*) printf 'sessions=0\nbusy=0\nlock=free\nbat=Charging:90\nfree_gb=100\n' ;;
  'date +%s') echo 1000 ;;
  'ls -t '*) [ "${MOCK_MODE:-}" = no_launch ] || echo /home/deck/.local/share/deckstream/launches/game_x.1000.1.txt ;;
  'pgrep -x '*) [ "${MOCK_MODE:-}" = no_process ] || echo 424242 ;;
  'echo $(ls '*) echo 0/0 ;;
  'cat '*) printf 'play=1000\nlaunched=1000\n' ;;
esac
exit 0
MOCK
cat > "$T/sleep" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
chmod +x "$T/lifessh" "$T/sleep"
for mode in no_launch no_process success; do
    : > "$FAKE_LOG"
    out="$(PATH="$T:$PATH" DECK_SSH="$T/lifessh" MOCK_MODE="$mode" DECKSTREAM_GAME_PROC_TIMEOUT=5 run --minutes 0 --launch-timeout 5 --out "$T/$mode")"; rc=$?
    if [ "$mode" = success ]; then
        [ "$rc" -eq 0 ] && grep -q 'kill -TERM 424242' "$FAKE_LOG" && ok "mock launch sees and stops its recorded PID" || bad "mock success rc=$rc: $out"
    else
        [ "$rc" -eq 1 ] && ! grep -q 'kill -TERM' "$FAKE_LOG" && ok "$mode fails without stopping an unobserved process" || bad "$mode rc=$rc: $out"
    fi
    grep -q 'rmdir /tmp/deckstream_smoke.lock' "$FAKE_LOG" && ok "$mode releases its operator lock" || bad "$mode leaked operator lock"
done
out="$(bash "$S" --slug 2>&1)"; rc=$?
[ "$rc" -eq 64 ] && ok "missing option value is rejected" || bad "missing value rc=$rc"
out="$(run --interval 0 --dry-run)"; rc=$?
[ "$rc" -eq 64 ] && ok "zero interval is rejected" || bad "zero interval rc=$rc"
: > "$FAKE_LOG"
out="$(DECK_HOST='' run --preflight-only)"; rc=$?
[ "$rc" -eq 2 ] && [ ! -s "$FAKE_LOG" ] && ok "missing destination never contacts SSH" || bad "missing host rc=$rc"

[ $fail -eq 0 ] && { echo; echo "DECK SMOKE TESTS PASSED"; exit 0; }
echo; echo "DECK SMOKE TESTS FAILED"; exit 1
