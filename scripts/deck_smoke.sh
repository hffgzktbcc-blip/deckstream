#!/usr/bin/env bash
# deck_smoke.sh: launch ONE streamed game on the Steam Deck from this Mac, watch it, stop it, and report. Nobody needs to
# be at the Deck for this, except for gameplay input (menus, license screens, controller checks), which this cannot do.
#
#   scripts/deck_smoke.sh --slug SLUG --appid SHORTCUT_APPID --exe GAME.exe [--minutes 10] [--interval 30]
#                         [--launch-timeout 180] [--out DIR] [--dry-run] [--preflight-only]
#
#   --slug     the game's id in ~/.config/deckstream/games.json (e.g. example_game)
#   --appid    the Steam shortcut AppID the daemon printed ("Added '...' to Steam (live, AppID 3155796970)")
#   --exe      the game's Windows process name as Linux sees it (e.g. bg3.exe); used only to stop the game cleanly
#
# Safety rules (all enforced below):
#   * one Deck operator at a time: a lock directory on the Deck (/tmp/deckstream_smoke.lock) and a refusal if a session marker,
#     a fill, a running game or an rclone mount already exists
#   * refuses on a discharging battery below 30% and when the card has under 40 GB free
#   * it stops ONLY the process it launched (TERM, never KILL) and never deletes anything on the Deck except its own lock
#   * no secrets: it uses your existing SSH key; nothing is read from or written to credentials
# UNVERIFIED ON A REAL DECK from this revision: the launch line
#   steam -ifrunning steam://rungameid/<(appid<<32)|0x02000000>
# is the standard way to start a non-Steam shortcut, but nobody has run it over SSH in Gaming Mode yet. The first real run must be
# done by whoever holds the Deck slot, with --minutes 2; if it fails ("no launch telemetry"), fix only the launch line.
# Env: DECK_HOST (deck@<deck-host>), DECK_KEY (~/.ssh/id_ed25519), DECK_SSH (ssh; tests point it at a fake)
# Exit codes: 0 ok, 1 the run failed (game never launched / did not wind down), 2 refused by a safety check, 64 usage.
set -u

HOST="${DECK_HOST:-}"
KEY="${DECK_KEY:-$HOME/.ssh/id_ed25519}"
SSH="${DECK_SSH:-ssh}"
SCP="${DECK_SCP:-scp}"
LOCK="/tmp/deckstream_smoke.lock"

SLUG="" APPID="" EXE="" MINUTES=10 INTERVAL=30 LAUNCH_TIMEOUT=180 OUT="" DRY=0 PREFLIGHT_ONLY=0

usage() { sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 64; }

while [ $# -gt 0 ]; do
    case "$1" in
        --slug|--appid|--exe|--minutes|--interval|--launch-timeout|--out)
            [ $# -ge 2 ] && [ -n "$2" ] || { echo "missing value for $1" >&2; exit 64; } ;;
    esac
    case "$1" in
        --slug) SLUG="${2:-}"; shift 2 ;;
        --appid) APPID="${2:-}"; shift 2 ;;
        --exe) EXE="${2:-}"; shift 2 ;;
        --minutes) MINUTES="${2:-}"; shift 2 ;;
        --interval) INTERVAL="${2:-}"; shift 2 ;;
        --launch-timeout) LAUNCH_TIMEOUT="${2:-}"; shift 2 ;;
        --out) OUT="${2:-}"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        --preflight-only) PREFLIGHT_ONLY=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

[ -n "$SLUG" ] && [ -n "$APPID" ] && [ -n "$EXE" ] || { echo "need --slug, --appid and --exe" >&2; usage; }
case "$APPID" in auto|[0-9]*) ;; *) echo "--appid must be a number (the 32-bit shortcut AppID) or 'auto'" >&2; exit 64 ;; esac
case "$APPID" in *[!0-9]*) [ "$APPID" = auto ] || { echo "--appid must be a number (the 32-bit shortcut AppID) or 'auto'" >&2; exit 64; } ;; esac
case "$MINUTES$INTERVAL$LAUNCH_TIMEOUT" in ''|*[!0-9]*) echo "--minutes/--interval/--launch-timeout must be whole numbers" >&2; exit 64 ;; esac
case "$SLUG$EXE" in *[!A-Za-z0-9._-]*) echo "--slug/--exe may only contain letters, digits, . _ -" >&2; exit 64 ;; esac

[ "$INTERVAL" -gt 0 ] && [ "$LAUNCH_TIMEOUT" -gt 0 ] || { echo "interval and launch-timeout must be positive" >&2; exit 64; }
[ "$APPID" = auto ] || [ "$APPID" -le 4294967295 ] || { echo "--appid must fit in 32 bits" >&2; exit 64; }

# Steam's "rungameid" wants the 64-bit game id of a non-Steam shortcut: (appid << 32) | 0x02000000
# (--appid auto looks it up on the Deck after the preflight, from ~/.config/deckstream/live_shortcuts_added.json)
gameid_of() { python3 -c "print((int('$1') << 32) | 0x02000000)"; }
GAMEID=""; [ "$APPID" = auto ] || GAMEID="$(gameid_of "$APPID")"
# Linux truncates a process name (comm) to 15 characters: 'Cyberpunk2077.exe' is seen as 'Cyberpunk2077.e', so pgrep -x must use that
PNAME="${EXE:0:15}"
PREGEX="${PNAME//./\\.}"
[ -n "$OUT" ] || OUT="${TMPDIR:-/tmp}/deck_smoke_${SLUG}_$(date +%Y%m%d_%H%M%S)"

dk() { "$SSH" -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"; }

if [ "$DRY" = "1" ]; then
    echo "[dry-run] nothing is sent to the Deck"
    echo "slug=$SLUG appid=$APPID gameid=${GAMEID:-(resolved on the Deck)} exe=$EXE process-name=$PNAME"
    echo "plan: preflight -> lock -> launch (steam://rungameid/${GAMEID:-<resolved>}) -> wait up to ${LAUNCH_TIMEOUT}s for launch telemetry ->"
    echo "      sample every ${INTERVAL}s for ${MINUTES} min (net, game/rclone reads, cache, iowait, screenshot) -> TERM $EXE -> report"
    echo "output: $OUT"
    exit 0
fi

[ -n "$HOST" ] || { echo "Set DECK_HOST to the SSH destination" >&2; exit 2; }
STORAGE_PATH="${DECKSTREAM_SMOKE_STORAGE:-/home/deck}"
case "$STORAGE_PATH" in /*) ;; *) echo "storage path must be absolute" >&2; exit 64 ;; esac
case "$STORAGE_PATH" in *[!A-Za-z0-9/._-]*) echo "unsupported storage path characters" >&2; exit 64 ;; esac

# ---- 1. preflight: one SSH call, key=value lines back ---------------------------------------------------------------
PRE="$(dk 'echo sessions=$(ls /home/deck/.local/share/deckstream/sessions 2>/dev/null | wc -l)
echo busy=$(ps -eo args | grep -E "[s]tartup_profile.py fill-cache|[u]mu-run|[r]clone mount" | wc -l)
if [ -d /tmp/deckstream_smoke.lock ]; then echo lock=held; else echo lock=free; fi
echo bat=$(cat /sys/class/power_supply/BAT1/status 2>/dev/null):$(cat /sys/class/power_supply/BAT1/capacity 2>/dev/null)
echo free_gb=$(df -BG --output=avail '"$STORAGE_PATH"'  2>/dev/null | tail -1 | tr -dc 0-9)' 2>&1)" || { echo "cannot reach the Deck over SSH (asleep or off the LAN?)" >&2; exit 2; }

val() { printf '%s\n' "$PRE" | sed -n "s/^$1=//p" | head -1; }
SESS="$(val sessions)"; BUSY="$(val busy)"; LOCKS="$(val lock)"; BAT="$(val bat)"; FREE="$(val free_gb)"
BSTAT="${BAT%%:*}"; BCAP="${BAT##*:}"

refuse() { echo "REFUSED: $1" >&2; exit 2; }
[ "${SESS:-x}" = "0" ] || refuse "a session marker exists on the Deck (a game or an unfinished session): sessions=${SESS:-?}"
[ "${BUSY:-x}" = "0" ] || refuse "a fill, a game or an rclone mount is already running: busy=${BUSY:-?}"
[ "${LOCKS:-x}" = "free" ] || refuse "another operator holds $LOCK on the Deck"
if [ "$BSTAT" != "Charging" ] && [ "$BSTAT" != "Full" ]; then
    case "$BCAP" in ''|*[!0-9]*) refuse "battery state unreadable (${BAT:-?})" ;; esac
    [ "$BCAP" -ge 30 ] || refuse "battery is $BSTAT at ${BCAP}% (need Charging, or at least 30%)"
fi
case "${FREE:-x}" in ''|*[!0-9]*) refuse "free space on the card unreadable" ;; esac
[ "$FREE" -ge 40 ] || refuse "only ${FREE} GB free on the card (need 40 GB)"
echo "preflight ok: sessions=0 busy=0 lock=free battery=$BAT free=${FREE}GB"
[ "$PREFLIGHT_ONLY" = "1" ] && exit 0

# ---- 2. lock (released on exit) ---------------------------------------------------------------------------------------
dk "mkdir /tmp/deckstream_smoke.lock 2>/dev/null && echo \"$(id -un)@$(hostname -s) $$ $(date +%s)\" > /tmp/deckstream_smoke.lock/owner" \
    || refuse "could not take $LOCK (someone else just did)"
release() { dk 'rm -f /tmp/deckstream_smoke.lock/owner; rmdir /tmp/deckstream_smoke.lock' >/dev/null 2>&1; }
trap release EXIT

if [ "$APPID" = auto ]; then
    APPID="$(dk "python3 - <<'EOF'
import json, os, re
try: d = json.load(open(os.path.expanduser('~/.config/deckstream/live_shortcuts_added.json')))
except Exception: d = {}
for name, v in d.items():
    if not isinstance(v, dict) or not v.get('appid'): continue
    if '${SLUG}' in str(v.get('exe', '')) or re.sub(r'[^a-z0-9]', '_', str(name).lower()) == '${SLUG}':
        print(int(v['appid']) & 0xFFFFFFFF); break
EOF" 2>/dev/null)"
    case "$APPID" in ''|*[!0-9]*) refuse "could not find the shortcut AppID for $SLUG on the Deck; pass --appid N (the daemon prints it when it adds the shortcut)" ;; esac
    GAMEID="$(gameid_of "$APPID")"
    echo "resolved shortcut AppID $APPID -> game id $GAMEID"
fi

mkdir -p "$OUT/shots"
echo "output: $OUT"
CSV="$OUT/samples.csv"
echo "ts,cache_mb,net_rx,game_rchar,rclone_rchar,rclone_wbytes,iowait,load1,battery,exe_alive" > "$CSV"

# ---- 3. launch -------------------------------------------------------------------------------------------------------
T0="$(dk 'date +%s')"
echo "launching steam://rungameid/$GAMEID (appid $APPID) at $T0"
dk "XDG_RUNTIME_DIR=/run/user/\$(id -u) DISPLAY=:0 steam -ifrunning steam://rungameid/$GAMEID >/dev/null 2>&1; echo launch-command-returned"

LF=""
waited=0
while [ "$waited" -lt "$LAUNCH_TIMEOUT" ]; do
    LF="$(dk "ls -t /home/deck/.local/share/deckstream/launches/${SLUG}.*.txt 2>/dev/null | head -1" 2>/dev/null)"
    if [ -n "$LF" ]; then
        EP="$(basename "$LF" | sed -E "s/^${SLUG}\.([0-9]+)\..*/\1/")"
        case "$EP" in ''|*[!0-9]*) EP=0 ;; esac
        [ "$EP" -ge $((T0 - 5)) ] && break
        LF=""
    fi
    sleep 5; waited=$((waited + 5))
done

shot() {   # shot NAME: gamescope pipewire screenshot copied to $OUT/shots/NAME.png (best effort)
    dk 'rm -f /tmp/smoke_shot.png; id=$(pw-dump 2>/dev/null | python3 -c "
import json,sys
for o in json.load(sys.stdin):
    p=o.get(\"info\",{}).get(\"props\",{})
    if p.get(\"node.name\")==\"gamescope\": print(o[\"id\"]); break"); [ -n "$id" ] && timeout 15 gst-launch-1.0 -q pipewiresrc path=$id num-buffers=2 ! videoconvert ! pngenc snapshot=true ! filesink location=/tmp/smoke_shot.png >/dev/null 2>&1' >/dev/null 2>&1
    "$SCP" -q -i "$KEY" -o IdentitiesOnly=yes "$HOST:/tmp/smoke_shot.png" "$OUT/shots/$1.png" >/dev/null 2>&1 || true
}

if [ -z "$LF" ]; then
    echo "FAILED: no launch telemetry for $SLUG within ${LAUNCH_TIMEOUT}s: the game never started (is Steam in Gaming Mode? is the AppID right?)" >&2
    shot "no_launch"
    exit 1
fi
echo "game launched; telemetry file: $(basename "$LF")"
# 'launched' only means the runner CALLED the game. Wait for the REAL process (exact comm name).
EXE_SEEN=-1; GAME_PID=""; pw=0
while [ "$pw" -lt "${DECKSTREAM_GAME_PROC_TIMEOUT:-150}" ]; do
    GAME_PID="$(dk "pgrep -x '$PREGEX' | head -1" 2>/dev/null)"
    case "$GAME_PID" in ''|*[!0-9]*) GAME_PID="" ;; *) EXE_SEEN=$(( $(dk 'date +%s') - T0 )); break ;; esac
    sleep 5; pw=$((pw + 5))
done
echo "$EXE_SEEN" > "$OUT/exe_seen.txt"
if [ "$EXE_SEEN" -ge 0 ]; then echo "real game process $PNAME seen ${EXE_SEEN}s after Play"; else echo "WARNING: telemetry says launched but NO $PNAME process appeared within ${DECKSTREAM_GAME_PROC_TIMEOUT:-150}s: the game did not start (check the screenshots and the Proton log)"; fi
shot "t000_launched"

# ---- 4. sample ---------------------------------------------------------------------------------------------------------
SAMPLE_CMD="gp=\$(pgrep -x '$PREGEX' 2>/dev/null | head -1); rp=\$(pgrep -x rclone 2>/dev/null | head -1)
echo \$(date +%s),\$(grep cache_mb_last $LF | tail -1 | sed 's/.*=//'),\$(cat /sys/class/net/wlan0/statistics/rx_bytes),\$(awk '/^rchar/{print \$2}' /proc/\$gp/io 2>/dev/null),\$(awk '/^rchar/{print \$2}' /proc/\$rp/io 2>/dev/null),\$(awk '/^write_bytes/{print \$2}' /proc/\$rp/io 2>/dev/null),\$(vmstat 1 2 | tail -1 | awk '{print \$16}'),\$(cut -d' ' -f1 /proc/loadavg),\$(cat /sys/class/power_supply/BAT1/capacity 2>/dev/null),\$([ -n \"\$gp\" ] && echo 1 || echo 0)"

END=$(( $(date +%s) + MINUTES * 60 ))
n=0
while [ "$(date +%s)" -lt "$END" ]; do
    dk "$SAMPLE_CMD" >> "$CSV" 2>/dev/null
    n=$((n + 1))
    shot "$(printf 't%03d' "$n")"
    sleep "$INTERVAL"
done

# ---- 5. stop only what we launched, never KILL ------------------------------------------------------------------------
echo "stopping $EXE (TERM)"
if [ -n "$GAME_PID" ]; then
    dk "[ \"\$(ps -p $GAME_PID -o comm=)\" = '$PNAME' ] && kill -TERM $GAME_PID && echo TERM-sent-to-$GAME_PID"
fi
wound=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 5
    left="$(dk 'echo $(ls /home/deck/.local/share/deckstream/sessions 2>/dev/null | wc -l)/$(ps -eo comm | grep -c "^umu.exe$")' 2>/dev/null)"
    [ "$left" = "0/0" ] && { wound=1; break; }
done

# ---- 6. report ---------------------------------------------------------------------------------------------------------
dk "cat $LF; python3 - <<'EOF'
import json,glob
for f in glob.glob('/home/deck/.local/share/deckstream/profiles/${SLUG}.json'):
    p=json.load(open(f)); print('PROFILE', {k:p.get(k) for k in ('runs','total_bytes','later_bytes','stable')}, 'startup_ranges', len(p.get('ranges',[])), 'later_ranges', len(p.get('later',[])))
EOF" > "$OUT/launch_and_profile.txt" 2>&1

python3 - "$CSV" "$OUT/launch_and_profile.txt" "$OUT/exe_seen.txt" > "$OUT/summary.txt" <<'PY'
import csv, re, sys
rows = list(csv.DictReader(open(sys.argv[1])))
def num(v):
    try: return float(v)
    except Exception: return None
print("samples:", len(rows))
alive = [r.get("exe_alive") for r in rows if r.get("exe_alive") in ("0", "1")]
if alive: print("REAL game process alive in %d of %d samples" % (sum(1 for a in alive if a == "1"), len(alive)))
if len(rows) >= 2:
    a, b = rows[0], rows[-1]
    dt = (num(b["ts"]) or 0) - (num(a["ts"]) or 0)
    if dt > 0:
        for key, label in (("net_rx", "network in"), ("game_rchar", "game read()"), ("rclone_wbytes", "cache writes")):
            x, y = num(a[key]), num(b[key])
            if x is not None and y is not None:
                print("%-14s %.1f MB/s average over %d s" % (label, (y - x) / dt / 1e6, dt))
    cm = [num(r["cache_mb"]) for r in rows if num(r["cache_mb"]) is not None]
    if cm: print("cache: %.0f -> %.0f MB (+%.0f MB)" % (cm[0], cm[-1], cm[-1] - cm[0]))
    io = [num(r["iowait"]) for r in rows if num(r["iowait"]) is not None]
    if io: print("iowait%%: min %.0f max %.0f avg %.1f" % (min(io), max(io), sum(io) / len(io)))
try:
    seen = int(open(sys.argv[3]).read().strip())
except Exception:
    seen = -1
print("GAME PROCESS: %s" % ("seen %d s after Play" % seen if seen >= 0 else "NEVER SEEN: the game did not start (telemetry 'launched' only means the runner called it)"))
t = open(sys.argv[2]).read()
kv = dict(re.findall(r"^(play|fill_start|fill_end|mounted|launched|cache_mb_at_launch|controller_wait_ms)=(\d+)", t, re.M))
if "play" in kv:
    p = int(kv["play"])
    print("timeline (s after play):", {k: int(v) - p for k, v in kv.items() if k in ("fill_start", "fill_end", "mounted", "launched")})
for k in ("cache_mb_at_launch", "controller_wait_ms"):
    if k in kv: print(k + ":", kv[k])
for line in t.splitlines():
    if line.startswith("PROFILE"): print(line)
PY
cat "$OUT/summary.txt"

if [ "${EXE_SEEN:--1}" -lt 0 ]; then
    echo "FAILED: the game process never appeared, so this run did NOT start the game. Report: $OUT/summary.txt (screenshots in $OUT/shots)" >&2
    exit 1
fi
if [ "$wound" = "1" ]; then
    echo "game stopped and the session wound down cleanly. Report: $OUT/summary.txt (screenshots in $OUT/shots)"
    exit 0
fi
echo "FAILED: the session did not wind down within 60 s of TERM. Nothing was forced; look at the Deck before starting another run." >&2
exit 1
