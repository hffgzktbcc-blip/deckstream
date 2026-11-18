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
#
# RIG_SCENARIO selects what is emulated around the same real runner (default: basic):
#   basic       the chain above.
#   conditions  adds an ext4 loop-mounted "SD card" (the cache must land on it), a shaping proxy in front of the WebDAV server
#               (EMULATED latency and bandwidth: no Wi-Fi, NAS or SD-card measurement) and a Steam-virtual-pad entry in the
#               device list the runner reads (the controller-wait logic only: no uinput device, no real input).
#   gfx         launches a Direct3D 11 probe: clear, read-back and an exact pixel check on Wine's own renderer over software GL
#               (not DXVK/VKD3D, no GPU, no performance).
#   proton      the same Direct3D 11 probe, but through REAL GE-Proton (pinned release, SHA-512 checked) via the launcher's
#               plain-Proton path (DECKSTREAM_LAUNCHER=proton), so DXVK runs on software Vulkan (llvmpipe). Needs a non-root
#               user and downloads ~0.5 GB once into RIG_CACHE (default ~/.cache/deckstream-rig). No umu or Steam Linux Runtime
#               (container runtime), no GPU, no performance.
set -uo pipefail
SCENARIO="${RIG_SCENARIO:-basic}"
case "$SCENARIO" in basic|conditions|gfx|proton) ;; *) echo "unknown RIG_SCENARIO: $SCENARIO" >&2; exit 2 ;; esac

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
SUDO=""
if [ "$SCENARIO" = conditions ]; then
    command -v mkfs.ext4 >/dev/null 2>&1 || skip "conditions scenario needs mkfs.ext4 (e2fsprogs)"
    [ "$(id -u)" = 0 ] || { sudo -n true 2>/dev/null && SUDO="sudo -n"; }
    [ "$(id -u)" = 0 ] || [ -n "$SUDO" ] || skip "conditions scenario needs root or passwordless sudo for a loop mount"
fi

GE_TAG="GE-Proton10-34"
GE_SHA512="9fd0b2cfbd501c0b5c892239c392c7283a029b5e5d5a77d3f85b0ce190d555456241a18eebca16b53f094b403499201c13550a3f0b9b365e1a5eb5737cbb7303"
GE_URL="https://github.com/GloriousEggroll/proton-ge-custom/releases/download/$GE_TAG/$GE_TAG.tar.gz"
GE_DIR=""
if [ "$SCENARIO" = proton ]; then
    [ "$(id -u)" != 0 ] || skip "proton scenario must not run as root (Proton refuses to)"
    [ -r /dev/fuse ] && [ -w /dev/fuse ] || skip "proton scenario needs /dev/fuse usable by this user"
    for c in curl tar sha512sum; do command -v "$c" >/dev/null 2>&1 || skip "proton scenario needs '$c'"; done
    CACHE="${RIG_CACHE:-$HOME/.cache/deckstream-rig}"
    GE_DIR="$CACHE/$GE_TAG"
    if [ ! -f "$GE_DIR/proton" ]; then
        mkdir -p "$CACHE" || skip "cannot create $CACHE"
        TARBALL="$CACHE/$GE_TAG.tar.gz"
        if [ ! -f "$TARBALL" ] || ! echo "$GE_SHA512  $TARBALL" | sha512sum -c - >/dev/null 2>&1; then
            curl -fsSL -m 900 -o "$TARBALL.part" "$GE_URL" || { rm -f "$TARBALL.part"; skip "could not download $GE_TAG"; }
            echo "$GE_SHA512  $TARBALL.part" | sha512sum -c - >/dev/null 2>&1 || { rm -f "$TARBALL.part"; echo "[x] $GE_TAG download does not match the pinned SHA-512"; exit 1; }
            mv "$TARBALL.part" "$TARBALL"
        fi
        EXTRACT="$(mktemp -d "$CACHE/extract.XXXXXX")"
        tar -xzf "$TARBALL" -C "$EXTRACT" && mv "$EXTRACT/$GE_TAG" "$GE_DIR" && rmdir "$EXTRACT" || { echo "[x] could not unpack $GE_TAG"; exit 1; }
    fi
fi

T="$(mktemp -d)"
SERVE_PID=""
cleanup() {
    [ -n "$SERVE_PID" ] && kill "$SERVE_PID" 2>/dev/null
    for m in $(awk -v t="$T" 'index($2, t) == 1 {print $2}' /proc/mounts | sort -r); do
        fusermount3 -uz "$m" 2>/dev/null || $SUDO umount -l "$m" 2>/dev/null
    done
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
EXE_NAME=probe_game.exe
if [ "$SCENARIO" = gfx ] || [ "$SCENARIO" = proton ]; then
    EXE_NAME=probe_gfx.exe
    x86_64-w64-mingw32-gcc -mwindows -o "$LIB/$EXE_NAME" "$ROOT/scripts/fixtures/probe_gfx.c" -ld3d11 -ldxgi -luuid || { echo "[x] could not build the graphics probe"; exit 1; }
    EXPECT="fl=b000 pixel=ff8000ff"
else
    x86_64-w64-mingw32-gcc -mwindows -o "$LIB/$EXE_NAME" "$ROOT/scripts/fixtures/probe_game.c" || { echo "[x] could not build the probe exe"; exit 1; }
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
fi

# --- the shipped daemon files, laid out the way the installer does ---------------------------------------------------------
find "$ROOT/daemon" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' \) -exec cp {} "$CONF/" \;
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
cat > "$CONF/storage.conf" <<EOF
STORAGE_KEY=$KEY
STORAGE_WEBDAV_URL=http://127.0.0.1:$PORT
STORAGE_WEBDAV_USER=deckstream
EOF
chmod 600 "$CONF/storage.conf"
PAD_FILE=""
RATE_KB=3000; DELAY_MS=40
if [ "$SCENARIO" = conditions ]; then
    # (1) SD card: a 4 GiB sparse ext4 image, loop-mounted where the runner looks for removable cards
    CARD="$T/media/deck/card"; mkdir -p "$CARD"
    truncate -s 4G "$T/card.img" && mkfs.ext4 -q -F "$T/card.img" || { echo "[x] could not build the SD card image"; exit 1; }
    $SUDO mount -o loop "$T/card.img" "$CARD" || skip "conditions scenario could not loop-mount the card image"
    $SUDO chown "$(id -u):$(id -g)" "$CARD"
    # (2) slow link: rclone talks to the shaping proxy, which forwards to the real WebDAV server
    PROXY_PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')"
    sed -i "s|STORAGE_WEBDAV_URL=.*|STORAGE_WEBDAV_URL=http://127.0.0.1:$PROXY_PORT|" "$CONF/storage.conf"
    python3 "$ROOT/scripts/fixtures/shaping_proxy.py" "$PROXY_PORT" "$PORT" "$DELAY_MS" "$RATE_KB" "$T" >"$T/proxy.log" 2>&1 &
    # (3) controller: the one device line the runner waits for, in /proc/bus/input/devices format
    PAD_FILE="$T/input_devices"
    printf 'I: Bus=0003 Vendor=28de Product=11ff Version=0001\nN: Name="Microsoft X-Box 360 pad 0"\n\n' > "$PAD_FILE"
fi
# Rig-only switches (stream.env is the runner's documented test hook): no warm-up traffic, a short controller wait.
printf 'DECKSTREAM_WARMUP=0\nDECKSTREAM_CONTROLLER_TIMEOUT=1\nDECKSTREAM_FOCUS_HELPER=0\n' > "$CONF/stream.env"

if [ "$SCENARIO" = proton ]; then
    # real GE-Proton, used through the launcher's plain-Proton path
    ln -s "$GE_DIR" "$HOME_DIR/.steam/root/compatibilitytools.d/$GE_TAG"
    printf 'DECKSTREAM_LAUNCHER=proton\nDECKSTREAM_PROTON_LOG=1\n' >> "$CONF/stream.env"
else
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
fi

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
env -i PWD="$T" PATH=/usr/local/bin:/usr/bin:/bin HOME="$HOME_DIR" DISPLAY="\$DISPLAY" XAUTHORITY="\${XAUTHORITY:-}" \\
    XDG_RUNTIME_DIR="$T/run" DECKSTREAM_MEDIA_ROOT="$T/media" \\
    DS_PROBE_OUT="Z:$T/probe_out.txt" DS_PROBE_HOLD_MS=9000 \\
    ${PAD_FILE:+DECKSTREAM_INPUT_DEVICES="$PAD_FILE"} \\
    bash "$CONF/stream-runner.sh" $GAME_ID "Probe Game" "$FOLDER" $EXE_NAME >"$T/runner.out" 2>&1 &
RUNNER=\$!
GAME_MOUNT="$HOME_DIR/.local/share/deckstream/stream/games/$GAME_ID"
for _ in \$(seq 1 360); do
    [ -s "$T/mount_fstype" ] || awk -v m="\$GAME_MOUNT" '\$2 == m {print \$3}' /proc/mounts > "$T/mount_fstype"
    if [ ! -f "$T/window" ] && xwininfo -root -tree 2>/dev/null | grep -q DeckStreamProbe; then date +%s.%N > "$T/window"; fi
    { [ -f "$T/window" ] && [ -s "$T/mount_fstype" ]; } && [ "$SCENARIO" != gfx ] && break
    kill -0 \$RUNNER 2>/dev/null || break
    sleep 0.25
done
wait \$RUNNER; echo \$? > "$T/runner_rc"
INNER
timeout 240 xvfb-run -a -s "-screen 0 1280x800x24" bash "$T/inner.sh"

fail=0
check() { if [ "$1" = ok ]; then echo "[ok] $2"; else echo "[x] $2"; fail=1; fi; }
if [ "$SCENARIO" = gfx ] || [ "$SCENARIO" = proton ]; then
    :   # the graphics probe has no window to look for
else
    [ -f "$T/window" ] && check ok "a Windows window titled DeckStreamProbe appeared under Xvfb" || check no "no game window appeared"
fi
grep -q fuse "$T/mount_fstype" 2>/dev/null && check ok "the game folder was a FUSE (rclone) mount while the game ran" || check no "game folder was not a FUSE mount (fstype: $(cat "$T/mount_fstype" 2>/dev/null))"
GOT="$(cat "$T/probe_out.txt" 2>/dev/null | tr -d '\r')"
if [ "$SCENARIO" = proton ]; then
    [ "$GOT" = "$EXPECT" ] && check ok "Direct3D 11 clear + read-back through real $GE_TAG: $GOT" || check no "graphics probe: got '$GOT', expected '$EXPECT'"
    PLOG="$(cat "$T/runner.out" "$HOME_DIR"/.local/share/deckstream/logs/play_*.log 2>/dev/null)"   # DXVK logs to the game's stdout
    echo "$PLOG" | grep -q "DXVK: v" && echo "$PLOG" | grep -q "Found device: llvmpipe" && check ok "the game output shows DXVK running on the software Vulkan device (llvmpipe)" || check no "no DXVK-on-llvmpipe evidence in the game output"
    grep -q "launching .* with $GE_TAG" "$T/runner.out" && check ok "the launcher used $GE_TAG through its plain-Proton path" || check no "launcher did not report using $GE_TAG"
elif [ "$SCENARIO" = gfx ]; then
    [ "$GOT" = "$EXPECT" ] && check ok "Direct3D 11 clear + read-back on Wine's renderer (software GL): $GOT" || check no "graphics probe: got '$GOT', expected '$EXPECT'"
else
    [ "$GOT" = "$EXPECT" ] && check ok "exe read the full payload through the stream: $GOT" || check no "payload mismatch: got '$GOT', expected '$EXPECT'"
fi
if [ "$SCENARIO" = conditions ]; then
    CACHE_FILES="$(find "$CARD/.deckstream/stream-cache/$GAME_ID" -type f 2>/dev/null | wc -l)"
    HOME_CACHE="$(find "$HOME_DIR/.local/share/deckstream/stream/cache/$GAME_ID" -type f 2>/dev/null | wc -l)"
    [ "$CACHE_FILES" -gt 0 ] && [ "$HOME_CACHE" = 0 ] && check ok "read cache landed on the ext4 SD card ($CACHE_FILES files), none on the internal drive" || check no "cache placement wrong (card: $CACHE_FILES files, internal: $HOME_CACHE files)"
    grep -q "Steam virtual pad (28de:11ff) present" "$T/runner.out" && check ok "controller wait saw the Steam virtual pad entry and launched without the timeout" || check no "controller wait did not report the virtual pad"
    if [ -f "$T/window" ] && [ -f "$T/probe_out.txt" ]; then
        SECS="$(python3 -c "import os,sys;print(round(os.stat(sys.argv[1]).st_mtime-float(open(sys.argv[2]).read()),2))" "$T/probe_out.txt" "$T/window")"
        MIN="$(python3 -c "print(round(8*1024/$RATE_KB*0.5,2))")"
        python3 -c "import sys;sys.exit(0 if float('$SECS')>=float('$MIN') else 1)" && check ok "slow link was in effect: window to payload-read took ${SECS}s (>= ${MIN}s; EMULATED ${RATE_KB} KB/s, ${DELAY_MS} ms per request, not a Wi-Fi or NAS measurement)" || check no "reads were not slowed: ${SECS}s < ${MIN}s"
    else
        check no "no timing data for the slow-link check"
    fi
fi
RC="$(cat "$T/runner_rc" 2>/dev/null || echo missing)"
[ "$RC" = 0 ] && check ok "runner exited 0 after the game quit" || check no "runner exit code $RC"
LEFT="$(awk -v t="$T" -v card="${CARD:-}" 'index($2, t) == 1 && $2 != card {print $2}' /proc/mounts)"   # the rig's own SD card is not the runner's to unmount
[ -z "$LEFT" ] && check ok "no mounts left behind after exit" || check no "mounts left behind: $LEFT"
if pgrep -f "rclone mount .*$T" >/dev/null 2>&1; then check no "rclone mount process left running"; else check ok "no rclone mount process left running"; fi
if [ "$fail" -ne 0 ]; then
    echo "---- logs written: $(ls "$HOME_DIR/.local/share/deckstream/logs" 2>/dev/null | tr '\n' ' ') ----"
    echo "---- runner output (tail) ----"; tail -n 40 "$T/runner.out" 2>/dev/null
    for l in "$HOME_DIR"/.local/share/deckstream/logs/*; do echo "---- $(basename "$l") (tail) ----"; tail -n 15 "$l"; done 2>/dev/null
    exit 1
fi
if [ "$SCENARIO" = proton ]; then STAND="real $GE_TAG (plain-Proton path; no umu or Steam Linux Runtime), software Vulkan"; else STAND="Wine stood in for Proton"; fi
echo "[note] scenario=$SCENARIO. $STAND; automated clean-Linux evidence only, not a Steam Deck result."
