#!/usr/bin/env bash
# Clean-Linux launch rig: runs the REAL daemon/stream-runner.sh and daemon/game-launcher.sh end to end.
#   rclone WebDAV server (basic auth) -> rclone FUSE mount -> fuse-overlayfs -> launcher -> a Windows exe under Wine in Xvfb.
# The "game" is scripts/fixtures/probe_game.c, written for this repo: it opens a window and reads payload.bin through the
# mount, so the check is on bytes that really crossed the streamed filesystem. Nothing here is a third-party game.
#
# What a pass means (docs/LIMITS.md): AUTOMATED, clean-Linux integration. What it does NOT show:
#   - Wine stands in for Proton: no Proton, no umu, no Steam Linux Runtime, no DXVK/VKD3D (software Vulkan at best).
#   - No SteamOS, Gaming Mode/gamescope, controller, Decky, SD card, Wi-Fi or NAS latency, and no timings worth quoting.
#   - No real game. Process + window presence is not playability.
# Skips (exit 0, "[skip]") when a prerequisite is missing, so a bare laptop still passes the suite.
# DECKSTREAM_RIG_REQUIRED=1 (set in CI) turns a skip into a failure, so CI can never pass by silently skipping.
set -uo pipefail

skip() { echo "[skip] $1"; [ "${DECKSTREAM_RIG_REQUIRED:-0}" = 1 ] && exit 1; exit 0; }

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
need=(rclone fusermount3 xvfb-run xwininfo x86_64-w64-mingw32-gcc python3)
for c in "${need[@]}"; do
    command -v "$c" >/dev/null 2>&1 || skip "launch rig needs '$c' on PATH"
done
WINE64=""
for w in /usr/lib/wine/wine64 /usr/lib/wine-stable/wine64 "$(command -v wine64 2>/dev/null)"; do
    [ -n "$w" ] && [ -x "$w" ] && { WINE64="$w"; break; }
done
[ -n "$WINE64" ] || skip "launch rig needs wine64"
WINESERVER="$(dirname "$WINE64")/wineserver64"
[ -x "$WINESERVER" ] || WINESERVER="$(dirname "$WINE64")/wineserver"
[ -e /dev/fuse ] || skip "launch rig needs /dev/fuse"

T="$(mktemp -d)"
SERVE_PID=""
cleanup() {
    [ -n "$SERVE_PID" ] && kill "$SERVE_PID" 2>/dev/null
    for m in $(awk -v t="$T" 'index($2, t) == 1 {print $2}' /proc/mounts | sort -r); do fusermount3 -uz "$m" 2>/dev/null; done
    pkill -f "$T" 2>/dev/null
    rm -rf "$T"
}
trap cleanup EXIT

KEY="rig-fixture-key-not-a-secret"
GAME_ID=4242
FOLDER="Probe Game 1"
HOME_DIR="$T/home"
CONF="$HOME_DIR/.config/deckstream"
LIB="$T/library/$FOLDER"
mkdir -p "$CONF" "$HOME_DIR/.local/share/deckstream/logs" "$LIB" "$T/media" "$T/run" "$HOME_DIR/.steam/root/compatibilitytools.d"
chmod 700 "$T/run"

# --- fixture "game": own source + deterministic 8 MiB payload (expected hash computed independently in Python) ----------
x86_64-w64-mingw32-gcc -mwindows -o "$LIB/probe_game.exe" "$ROOT/scripts/fixtures/probe_game.c" || { echo "[x] could not build the probe exe"; exit 1; }
EXPECT="$(python3 - "$LIB/payload.bin" <<'PY'
import sys
data = bytes((i * 31 + (i >> 8)) & 0xFF for i in range(8 * 1024 * 1024))
open(sys.argv[1], "wb").write(data)
h = 1469598103934665603
for b in data:
    h = ((h ^ b) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
print(f"{len(data)} {h:016x}")
PY
)"

# --- the shipped daemon files, laid out the way the installer does ---------------------------------------------------------
find "$ROOT/daemon" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' \) -exec cp {} "$CONF/" \;
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
cat > "$CONF/storage.conf" <<EOF
STORAGE_KEY=$KEY
STORAGE_WEBDAV_URL=http://127.0.0.1:$PORT
STORAGE_WEBDAV_USER=deckstream
EOF
chmod 600 "$CONF/storage.conf"
# Rig-only switches (stream.env is the runner's documented test hook): no warm-up traffic, a short controller wait.
printf 'DECKSTREAM_WARMUP=0\nDECKSTREAM_CONTROLLER_TIMEOUT=1\nDECKSTREAM_FOCUS_HELPER=0\n' > "$CONF/stream.env"

# --- stand-in for Proton: same CLI the launcher uses (`proton waitforexitandrun <exe>`), Wine underneath -----------------
PDIR="$HOME_DIR/.steam/root/compatibilitytools.d/Rig-Wine-Not-Proton"
mkdir -p "$PDIR/dist/bin"
cat > "$PDIR/proton" <<EOF
#!/usr/bin/env bash
shift   # the Proton verb (run / waitforexitandrun)
export WINEPREFIX="\$STEAM_COMPAT_DATA_PATH/pfx" WINEDEBUG=-all WINEARCH=win64
mkdir -p "\$WINEPREFIX"
exec "$WINE64" "\$@"
EOF
chmod +x "$PDIR/proton"
ln -s "$WINESERVER" "$PDIR/dist/bin/wineserver"

# --- storage: local WebDAV with basic auth -----------------------------------------------------------------------------------
rclone serve webdav "$T/library" --addr "127.0.0.1:$PORT" --user deckstream --pass "$KEY" >"$T/serve.log" 2>&1 &
SERVE_PID=$!
for _ in $(seq 1 40); do
    curl -s -o /dev/null -u "deckstream:$KEY" "http://127.0.0.1:$PORT/" && break
    sleep 0.25
done
curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" | grep -q 401 || { echo "[x] WebDAV fixture accepted an unauthenticated request"; exit 1; }

# --- run: the real runner, under Xvfb, with a scrubbed environment (no host tokens reach the game) ---------------------------
cat > "$T/inner.sh" <<INNER
#!/usr/bin/env bash
cd "$T"
env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$HOME_DIR" DISPLAY="\$DISPLAY" XAUTHORITY="\${XAUTHORITY:-}" \\
    XDG_RUNTIME_DIR="$T/run" DECKSTREAM_MEDIA_ROOT="$T/media" \\
    DS_PROBE_OUT="Z:$T/probe_out.txt" DS_PROBE_HOLD_MS=9000 \\
    bash "$CONF/stream-runner.sh" $GAME_ID "Probe Game" "$FOLDER" probe_game.exe >"$T/runner.out" 2>&1 &
RUNNER=\$!
for _ in \$(seq 1 180); do
    xwininfo -root -tree 2>/dev/null | grep -q DeckStreamProbe && { echo found > "$T/window"; break; }
    kill -0 \$RUNNER 2>/dev/null || break
    sleep 0.5
done
GAME_MOUNT="$HOME_DIR/.local/share/deckstream/stream/games/$GAME_ID"
awk -v m="\$GAME_MOUNT" '\$2 == m {print \$3}' /proc/mounts > "$T/mount_fstype"
wait \$RUNNER; echo \$? > "$T/runner_rc"
INNER
timeout 240 xvfb-run -a -s "-screen 0 1280x800x24" bash "$T/inner.sh"

fail=0
check() { if [ "$1" = ok ]; then echo "[ok] $2"; else echo "[x] $2"; fail=1; fi; }
[ -f "$T/window" ] && check ok "a Windows window titled DeckStreamProbe appeared under Xvfb" || check no "no game window appeared"
grep -q fuse "$T/mount_fstype" 2>/dev/null && check ok "the game folder was a FUSE (rclone) mount while the game ran" || check no "game folder was not a FUSE mount (fstype: $(cat "$T/mount_fstype" 2>/dev/null))"
GOT="$(cat "$T/probe_out.txt" 2>/dev/null | tr -d '\r')"
[ "$GOT" = "$EXPECT" ] && check ok "exe read the full payload through the stream: $GOT" || check no "payload mismatch: got '$GOT', expected '$EXPECT'"
RC="$(cat "$T/runner_rc" 2>/dev/null || echo missing)"
[ "$RC" = 0 ] && check ok "runner exited 0 after the game quit" || check no "runner exit code $RC"
LEFT="$(awk -v t="$T" 'index($2, t) == 1 {print $2}' /proc/mounts)"
[ -z "$LEFT" ] && check ok "no mounts left behind after exit" || check no "mounts left behind: $LEFT"
if pgrep -f "rclone mount .*$T" >/dev/null 2>&1; then check no "rclone mount process left running"; else check ok "no rclone mount process left running"; fi
if [ "$fail" -ne 0 ]; then
    echo "---- runner output (tail) ----"; tail -n 40 "$T/runner.out" 2>/dev/null
    for l in "$HOME_DIR"/.local/share/deckstream/logs/*; do echo "---- $(basename "$l") (tail) ----"; tail -n 15 "$l"; done 2>/dev/null
    exit 1
fi
echo "[note] Wine stood in for Proton; automated clean-Linux evidence only, not a Steam Deck result."
