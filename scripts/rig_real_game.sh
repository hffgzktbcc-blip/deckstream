#!/usr/bin/env bash
# Real-game rig: launch a game YOU own through the real DeckStream stream-runner.sh, using only local files.
#   your folder -> local rclone WebDAV (read-only, localhost, throwaway password) -> rclone FUSE mount -> launcher -> game.
# Run it on the machine that has the game (any Linux box, or a Deck in Desktop Mode). It never copies, uploads or
# redistributes game files, and the sandbox HOME is thrown away afterwards. It does not say where to get games.
#
# Usage:
#   bash scripts/rig_real_game.sh --game-dir "/path/to/GameFolder" [--exe "Rel/Path/Game.exe"] [options]
#   (without --exe the largest plausible .exe in the folder is used)
# Options:
#   --real-proton   use the Proton already installed for your user (Steam compatibilitytools.d / Proton* / umu). Without it
#                   the rig uses Wine as a stand-in, which says little about Proton-specific behaviour.
#   --seconds N     how long to watch the game after launch (default 60)
#   --display real  use your current $DISPLAY instead of a virtual Xvfb screen (needed to actually see/play the game)
#   --report FILE   where to write the report (default ./rig_report_<time>.txt)
#
# The report is evidence of ONE launch of ONE game on ONE machine (docs/LIMITS.md: "measured on a device" only if that machine
# is a Steam Deck and you ran it there). Process presence is not playability: say what you saw on screen in your report.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GAME_DIR=""; EXE_REL=""; REAL_PROTON=0; WATCH=60; DISP=xvfb; REPORT="./rig_report_$(date +%Y%m%d_%H%M%S).txt"
while [ $# -gt 0 ]; do
    case "$1" in
        --game-dir) GAME_DIR="${2:-}"; shift 2 ;;
        --exe) EXE_REL="${2:-}"; shift 2 ;;
        --real-proton) REAL_PROTON=1; shift ;;
        --seconds) WATCH="${2:-60}"; shift 2 ;;
        --display) DISP="${2:-xvfb}"; shift 2 ;;
        --report) REPORT="${2:-}"; shift 2 ;;
        -h|--help) sed -n 2,19p "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done
[ -d "$GAME_DIR" ] || { echo "need --game-dir DIR (and optionally --exe REL, a file inside DIR)" >&2; exit 2; }
if [ -z "$EXE_REL" ]; then
    # no --exe: pick the largest .exe within 3 levels, skipping installers, uninstallers, crash reporters and redistributables
    EXE_REL="$(cd "$GAME_DIR" && find . -maxdepth 3 -type f -iname '*.exe' -printf '%s %P\n' 2>/dev/null \
        | grep -v -i -E 'unins|crash|redist|setup|vcredist|dxsetup|dotnet|installer|helper|report' | sort -rn | head -n 1 | cut -d' ' -f2-)"
    [ -n "$EXE_REL" ] || { echo "no .exe found in $GAME_DIR; pass --exe REL" >&2; exit 2; }
    echo "auto-picked exe: $EXE_REL  (pass --exe to override)"
fi
[ -f "$GAME_DIR/$EXE_REL" ] || { echo "--exe must be a file inside --game-dir" >&2; exit 2; }
case "/$EXE_REL/" in */../*) echo "--exe must stay inside --game-dir" >&2; exit 2 ;; esac
GAME_DIR="$(cd "$GAME_DIR" && pwd)"
for c in rclone fusermount3 python3 curl; do command -v "$c" >/dev/null 2>&1 || { echo "missing: $c" >&2; exit 2; }; done
[ -e /dev/fuse ] || { echo "missing: /dev/fuse" >&2; exit 2; }
[ "$DISP" = xvfb ] && { for c in xvfb-run xwininfo; do command -v "$c" >/dev/null 2>&1 || { echo "missing: $c (or use --display real)" >&2; exit 2; }; done; }
REAL_HOME="$HOME"

T="$(mktemp -d)"; SERVE_PID=""
cleanup() {
    [ -n "$SERVE_PID" ] && kill "$SERVE_PID" 2>/dev/null
    for m in $(awk -v t="$T" 'index($2, t) == 1 {print $2}' /proc/mounts | sort -r); do fusermount3 -uz "$m" 2>/dev/null; done
    pkill -f "$T" 2>/dev/null
    rm -rf "$T"
}
trap cleanup EXIT

KEY="rig-$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')"   # throwaway, valid for this run only
GAME_ID=4343
HOME_DIR="$T/home"; CONF="$HOME_DIR/.config/deckstream"; FOLDER="$(basename "$GAME_DIR")"
mkdir -p "$CONF" "$HOME_DIR/.local/share/deckstream/logs" "$T/media" "$T/run" "$HOME_DIR/.steam/root"
chmod 700 "$T/run"
# serve the PARENT of the game folder (so the runner's folder name matches), read-only, localhost only
PARENT="$(dirname "$GAME_DIR")"
find "$ROOT/daemon" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' \) -exec cp {} "$CONF/" \;
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
printf 'STORAGE_KEY=%s\nSTORAGE_WEBDAV_URL=http://127.0.0.1:%s\nSTORAGE_WEBDAV_USER=deckstream\n' "$KEY" "$PORT" > "$CONF/storage.conf"
chmod 600 "$CONF/storage.conf"
printf 'DECKSTREAM_WARMUP=0\nDECKSTREAM_FOCUS_HELPER=0\n' > "$CONF/stream.env"

if [ "$REAL_PROTON" = 1 ]; then
    # reuse what is installed for the real user, without touching it: link the Steam/Proton/umu folders into the sandbox HOME
    mkdir -p "$HOME_DIR/.local/share"
    for d in .steam/root/compatibilitytools.d .local/share/Steam .local/share/umu .local/share/deckstream/umu-test; do
        [ -e "$REAL_HOME/$d" ] && { mkdir -p "$HOME_DIR/$(dirname "$d")"; ln -s "$REAL_HOME/$d" "$HOME_DIR/$d"; }
    done
else
    WINE64=""; for w in /usr/lib/wine/wine64 /usr/lib/wine-stable/wine64 "$(command -v wine64 2>/dev/null)"; do [ -n "$w" ] && [ -x "$w" ] && { WINE64="$w"; break; }; done
    [ -n "$WINE64" ] || { echo "no wine64 for the stand-in; install Wine or pass --real-proton" >&2; exit 2; }
    PDIR="$HOME_DIR/.steam/root/compatibilitytools.d/Rig-Wine-Not-Proton"; mkdir -p "$PDIR/dist/bin"
    printf '#!/usr/bin/env bash\nshift\nexport WINEPREFIX="$STEAM_COMPAT_DATA_PATH/pfx" WINEDEBUG=-all WINEARCH=win64\nmkdir -p "$WINEPREFIX"\nexec "%s" "$@"\n' "$WINE64" > "$PDIR/proton"
    chmod +x "$PDIR/proton"
    ws="$(dirname "$WINE64")/wineserver64"; [ -x "$ws" ] || ws="$(dirname "$WINE64")/wineserver"; ln -s "$ws" "$PDIR/dist/bin/wineserver"
fi

rclone serve webdav "$PARENT" --read-only --addr "127.0.0.1:$PORT" --user deckstream --pass "$KEY" >"$T/serve.log" 2>&1 &
SERVE_PID=$!
for _ in $(seq 1 40); do curl -s -o /dev/null -u "deckstream:$KEY" "http://127.0.0.1:$PORT/" && break; sleep 0.25; done

EXE_BASE="$(basename "$EXE_REL")"
cat > "$T/inner.sh" <<INNER
#!/usr/bin/env bash
cd "$T"
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$HOME_DIR" DISPLAY="\$DISPLAY" XAUTHORITY="\${XAUTHORITY:-}" WAYLAND_DISPLAY="\${WAYLAND_DISPLAY:-}" \\
    XDG_RUNTIME_DIR="$T/run" DECKSTREAM_MEDIA_ROOT="$T/media" \\
    bash "$CONF/stream-runner.sh" $GAME_ID "Rig Game" "$FOLDER" "$EXE_REL" >"$T/runner.out" 2>&1 &
RUNNER=\$!
START=\$(date +%s); SEEN=""; LAST=""
GAME_MOUNT="$HOME_DIR/.local/share/deckstream/stream/games/$GAME_ID"
COMM="\$(printf '%s' "$EXE_BASE" | cut -c1-15)"   # the kernel shows a process name of at most 15 characters
while [ \$(( \$(date +%s) - START )) -lt $WATCH ]; do
    kill -0 \$RUNNER 2>/dev/null || break
    [ -s "$T/mount_fstype" ] || awk -v m="\$GAME_MOUNT" '\$2 == m {print \$3}' /proc/mounts > "$T/mount_fstype"
    # the game itself (process name = exe name), not the runner script whose arguments merely mention the exe
    if ps -eo comm= | grep -i -q -F -- "\$COMM"; then
        NOW=\$(( \$(date +%s) - START )); [ -z "\$SEEN" ] && SEEN=\$NOW; LAST=\$NOW
    elif [ -n "\$SEEN" ]; then break   # it ran and has now quit: let the runner finish its own cleanup
    fi
    sleep 1
done
echo "\$SEEN" > "$T/first_seen"; echo "\$LAST" > "$T/last_seen"
if kill -0 \$RUNNER 2>/dev/null && ps -eo comm= | grep -i -q -F -- "\$COMM"; then
    echo still_running_at_end_of_window > "$T/state"; kill -TERM \$RUNNER 2>/dev/null   # the runner's trap cleans up
fi
for _ in \$(seq 1 60); do kill -0 \$RUNNER 2>/dev/null || break; sleep 1; done          # up to 60 s for its cleanup
kill -0 \$RUNNER 2>/dev/null && { echo runner_did_not_exit > "$T/state"; kill -KILL \$RUNNER 2>/dev/null; }
wait \$RUNNER; echo \$? > "$T/runner_rc"
INNER
if [ "$DISP" = xvfb ]; then
    timeout $(( WATCH + 120 )) xvfb-run -a -s "-screen 0 1280x800x24" bash "$T/inner.sh"
else
    [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] || { echo "--display real needs a running desktop session" >&2; exit 2; }
    timeout $(( WATCH + 120 )) bash "$T/inner.sh"
fi
for _ in $(seq 1 20); do [ "$(awk -v t="$T" 'index($2, t) == 1' /proc/mounts | wc -l)" = 0 ] && break; sleep 1; done

FST="$(cat "$T/mount_fstype" 2>/dev/null)"; SEEN="$(cat "$T/first_seen" 2>/dev/null)"; LAST="$(cat "$T/last_seen" 2>/dev/null)"
STATE="$(cat "$T/state" 2>/dev/null || echo finished)"; RC="$(cat "$T/runner_rc" 2>/dev/null || echo missing)"
LEFT="$(awk -v t="$T" 'index($2, t) == 1 {print $2}' /proc/mounts | wc -l)"
{
    echo "DeckStream real-game rig report"
    echo "time (UTC):        $(date -u +%FT%TZ)"
    echo "DeckStream commit: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    echo "host:              $(uname -srm); $(. /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
    echo "runner:            $([ "$REAL_PROTON" = 1 ] && echo "real installed Proton/umu" || echo "Wine stand-in (NOT Proton)")"
    echo "display:           $DISP"
    echo "exe:               $EXE_BASE"
    echo "watch window:      ${WATCH}s"
    echo "mount type:        ${FST:-none seen}"
    if [ -n "$SEEN" ]; then echo "exe process seen:  yes, first at ${SEEN}s, last at ${LAST}s"; else echo "exe process seen:  no"; fi
    echo "end state:         $STATE (runner exit code $RC)"
    echo "mounts left over:  $LEFT"
    echo
    echo "---- runner output (last 60 lines; check for paths or names you want to redact) ----"
    tail -n 60 "$T/runner.out" 2>/dev/null
} > "$REPORT"
cat "$REPORT" | sed -n 1,12p
VERDICT=PASS
case "$FST" in *fuse*) ;; *) VERDICT=FAIL ;; esac
[ -n "$SEEN" ] || VERDICT=FAIL
[ "$LEFT" = 0 ] || VERDICT=FAIL
echo "verdict: $VERDICT  (full report: $REPORT)"
echo "[note] This shows the launch chain started the exe; it does not show the game is playable. Add what you saw on screen."
[ "$VERDICT" = PASS ]
