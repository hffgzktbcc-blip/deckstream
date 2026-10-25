#!/usr/bin/env bash
# ==============================================================================
# DeckStream - Stream Runner & Launcher
# Streams a PRE-INSTALLED (ready-to-play) game folder on demand from RemoteStorage WebDAV.
#   - rclone VFS mount of ONLY the game's folder, with a capped rolling cache
#   - fuse-overlayfs write layer so saves/configs land on local SSD (optional)
#   - isolated Proton prefix per game
#
# NOTE: Repacks (installer archives and binaries) and archives (.zip/.rar/.7z)
# CANNOT be streamed - they must be installed/extracted first. This script
# detects those and exits with a clear message.
#
# Usage: stream-runner.sh GAME_ID "GAME NAME" "WEBDAV_FOLDER" [EXE_REL_PATH]
# Credentials are read from ~/.config/deckstream/storage.conf (STORAGE_PASS=...)
# ==============================================================================

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

# Optional settings file (KEY=value lines, e.g. DECKSTREAM_WARMUP=0): lets a test switch features on or off without editing scripts.
[ -f "$HOME/.config/deckstream/stream.env" ] && . "$HOME/.config/deckstream/stream.env"
GAME_ID="${1:-}"
GAME_NAME="${2:-}"
WEBDAV_FOLDER="${3:-}"
EXE_REL_PATH="${4:-}"

CONF_DIR="$HOME/.config/deckstream"
CRED_FILE="$CONF_DIR/storage.conf"
LOG_DIR="$HOME/.local/share/deckstream/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/stream_${GAME_ID:-unknown}.log"
# Mirror all output to a log so Gaming Mode launches can be debugged later
# Steam's overlay is preloaded as both a 32- and a 64-bit .so, so ld.so prints "wrong ELF class: ELFCLASS32 ... ignored" for EVERY
# process the game spawns (573 of 1,383 lines in one Baldur's Gate 3 log). It is harmless and buries the real lines; drop only that one.
exec > >(grep --line-buffered -v -F 'wrong ELF class: ELFCLASS32' | tee -a "$LOG_FILE") 2>&1

export PATH="$HOME/.local/bin:$PATH"

fail() {
    echo -e "${RED}[!] $1${NC}"
    command -v notify-send &>/dev/null && notify-send "DeckStream: $GAME_NAME" "$1" || true
    exit 1
}

if [ -z "$GAME_ID" ] || [ -z "$GAME_NAME" ] || [ -z "$WEBDAV_FOLDER" ]; then
    fail "Usage: $0 GAME_ID \"GAME NAME\" \"WEBDAV_FOLDER\" [EXE_REL_PATH]"
fi

# >>> launch_lock
# Per-game launch lock: prevents duplicate runner instances if Play is pressed again.
# The lock is a kernel flock on runner_<id>.flock, held by a small helper that exits as soon as this runner is gone, so a runner
# killed at any moment never leaves a stale lock. (The old mkdir lock had to guess when a leftover folder was stale, and two racers
# could both "clear" it: 2 winners of 8 in CI run 94.) The helper is not a child of this script (double fork), so nothing here waits
# on it, and the game does not inherit the lock. runner_<id>.lock/pid stays as a marker for install.sh/rollback.sh (game running?).
RUN_LOCK_DIR="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/deckstream_locks"
mkdir -p "$RUN_LOCK_DIR" 2>/dev/null || true
RUN_LOCK="$RUN_LOCK_DIR/runner_${GAME_ID}.lock"
RUN_FLOCK="$RUN_LOCK_DIR/runner_${GAME_ID}.flock"
lock_duplicate() {   # lock_duplicate WHAT: tell the user, then leave without touching anything
    echo -e "${YELLOW}[!] Launch already in progress ($1) for $GAME_NAME ($GAME_ID). Ignoring duplicate launch press.${NC}"
    command -v notify-send &>/dev/null && notify-send "DeckStream" "$GAME_NAME is already launching." || true
    exit 0
}
LOCK_STATE="$RUN_LOCK_DIR/.state_${GAME_ID}_$$"
rm -f "$LOCK_STATE"
( LD_PRELOAD= python3 -c '
import fcntl, os, sys, time
f = open(sys.argv[1], "a")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    open(sys.argv[3], "w").write("BUSY\n"); sys.exit(0)
open(sys.argv[3], "w").write("OK\n")
pid = int(sys.argv[2])
while True:                      # hold the lock exactly as long as the runner lives (also after kill -9)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        break
    except PermissionError:
        pass
    time.sleep(0.3)
' "$RUN_FLOCK" "$$" "$LOCK_STATE" </dev/null >/dev/null 2>&1 & )
for _ in $(seq 1 100); do [ -s "$LOCK_STATE" ] && break; sleep 0.05; done
_lock_state="$(cat "$LOCK_STATE" 2>/dev/null || true)"; rm -f "$LOCK_STATE"
case "$_lock_state" in
    OK*) ;;
    BUSY*) lock_duplicate "pid $(cat "$RUN_LOCK/pid" 2>/dev/null || echo '?')" ;;
    *) echo "[!] launch lock helper did not answer (python3 missing?); continuing without a lock" ;;   # never block a launch on the lock
esac
# A runner from before this lock (no flock) that is still alive also counts as a launch in progress.
_marker="$(cat "$RUN_LOCK/pid" 2>/dev/null || true)"
[ -n "$_marker" ] && [ "$_marker" != "$$" ] && kill -0 "$_marker" 2>/dev/null && lock_duplicate "pid $_marker"
rm -rf "$RUN_LOCK" "${RUN_LOCK}.reclaim"; mkdir -p "$RUN_LOCK" && echo "$$" > "$RUN_LOCK/pid"
# <<< launch_lock

# Run a helper for at most N seconds (coreutils timeout, which SteamOS has); without it the helper still runs, unbounded.
bounded() { local secs="$1"; shift; if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; else "$@"; fi; }

# shellcheck disable=SC1090
[ -f "$CRED_FILE" ] && source "$CRED_FILE"
WEBDAV_URL="${STORAGE_WEBDAV_URL:-https://storage.local}"
WEBDAV_USER="${STORAGE_WEBDAV_USER:-storage}"
[ -z "${STORAGE_PASS:-}" ] && fail "No RemoteStorage key. Re-run the installer with your key, or put STORAGE_PASS=... in $CRED_FILE"

BASE_DIR="$HOME/.local/share/deckstream/stream"

# The read cache can be tens of GB. Put it on the MicroSD card (ext4) when there is one; the
# internal drive on a Deck is usually nearly full and a full SteamOS disk breaks Steam.
MEDIA_ROOT="${DECKSTREAM_MEDIA_ROOT:-/run/media}"
CACHE_BASE="$BASE_DIR/cache"
CACHE_DEFAULT="3G"
cache_cap_gb() {   # cache_cap_gb FREE_GB OWN_GB -> 60% of (free space + this game's own cache), clamped to 10-150 GB,
                   # but never below what this game already has cached (rclone deletes data above the cap)
    local cap=$(( ($1 + $2) * 60 / 100 ))
    [ "$cap" -lt "$2" ] && cap="$2"
    [ "$cap" -lt 10 ] && cap=10
    [ "$cap" -gt 150 ] && [ "$2" -le 150 ] && cap=150
    echo "$cap"
}
for card in "$MEDIA_ROOT"/*/*; do
    # Only a real mounted volume, not a folder that happens to sit under /run/media.
    [ -d "$card" ] && [ -w "$card" ] && mountpoint -q "$card" || continue
    [ "$(stat -f -c %T "$card" 2>/dev/null)" = "ext2/ext3" ] || [ "$(stat -f -c %T "$card" 2>/dev/null)" = "ext4" ] || continue
    CACHE_BASE="$card/.deckstream/stream-cache"
    # Big games read tens of GB at launch; a cap below that makes rclone evict and re-download what
    # it just fetched. Use 60% of the card's free space, clamped to 10-150 GB.
    FREE_GB="$(df -BG --output=avail "$card" 2>/dev/null | tail -n 1 | tr -dc '0-9')"
    FREE_GB="${FREE_GB:-50}"
    # The card's free space already EXCLUDES this game's own cache, so count that cache back in: otherwise the
    # cap shrinks as the cache grows and eventually drops below what is cached, and rclone deletes data at mount.
    OWN_MB="$(du -sm "$card/.deckstream/stream-cache/$GAME_ID" 2>/dev/null | cut -f1)"
    CACHE_DEFAULT="$(cache_cap_gb "$FREE_GB" $(( ${OWN_MB:-0} / 1024 )))G"
    break
done
GAME_DIR="$BASE_DIR/games/$GAME_ID"
CACHE_DIR="$CACHE_BASE/$GAME_ID"
WRITE_DIR="$BASE_DIR/writes/$GAME_ID"
WORK_DIR="$BASE_DIR/work/$GAME_ID"
MERGED_DIR="$BASE_DIR/merged/$GAME_ID"
# GE-Proton's protonfixes takes the last number in this path as the game id and crashes with an
# IndexError when there is none, so always end the folder name in a number.
COMPAT_ID="$(printf '%s' "$GAME_ID" | cksum | cut -d' ' -f1)"
COMPAT_DIR="$HOME/.local/share/Steam/steamapps/compatdata/deckstream_${GAME_ID}_${COMPAT_ID}"
CACHE_MAX="${DECKSTREAM_CACHE_MAX:-$CACHE_DEFAULT}"
SP_PY="$HOME/.config/deckstream/startup_profile.py"

echo -e "${CYAN}=========================================================="
echo " ⚡ DeckStream Cloud Stream Runner: $GAME_NAME"
echo -e "==========================================================${NC}"

# Lazy-unmount a FUSE directory. A plain unmount fails with "Device or resource busy" while
# Proton's wineserver still holds files open, which left stale mounts that broke the next launch.
umount_fuse() {
    for d in "$@"; do
        # A DEAD FUSE mount ("Transport endpoint is not connected") fails `mountpoint -q`, so it used to be skipped and
        # stayed under the next session's mount. A folder that cannot even be listed is unmounted too.
        mountpoint -q "$d" 2>/dev/null || ! ls "$d" >/dev/null 2>&1 || continue
        fusermount3 -uz "$d" 2>/dev/null || fusermount -uz "$d" 2>/dev/null || true
    done
}

# >>> mount_users
# PIDs whose working directory or an open file is inside one of the given folders. rclone and fuse-overlayfs (which
# serve the folders) and Wine's background services (which linger for a few seconds and never hold game files) are not
# counted, nor is this script. Only our own processes are readable, which is all we need.
mount_users() {   # one Python pass over /proc (a readlink per open file from bash would fork thousands of times)
    LD_PRELOAD= python3 - "${DECKSTREAM_PROC_ROOT:-/proc}" "$$" "$@" <<'PY'
import os, sys
root, me, dirs = sys.argv[1], sys.argv[2], [d.rstrip("/") for d in sys.argv[3:] if d]
skip = {"rclone", "fuse-overlayfs", "wineserver", "services.exe", "winedevice.exe", "umu.exe", "plugplay.exe", "explorer.exe",
        "rpcss.exe", "svchost.exe", "tabtip.exe", "conhost.exe", "start.exe"}
def inside(t):
    return any(t == d or t.startswith(d + "/") for d in dirs)
for pid in os.listdir(root):
    if not pid.isdigit() or pid == me:
        continue
    p = os.path.join(root, pid)
    try:
        with open(os.path.join(p, "comm")) as f:
            if f.read().strip() in skip:
                continue
    except OSError:
        continue
    links = [os.path.join(p, "cwd")]
    try:
        links += [os.path.join(p, "fd", n) for n in os.listdir(os.path.join(p, "fd"))]
    except OSError:
        pass
    for link in links:
        try:
            if inside(os.readlink(link)):
                print(pid)
                break
        except OSError:
            continue
PY
}
# <<< mount_users

# Clean up any stale mounts and orphaned rclone processes from a previous run
pkill -f "rclone mount .*$GAME_DIR" 2>/dev/null || true
umount_fuse "$MERGED_DIR" "$GAME_DIR"
mkdir -p "$GAME_DIR" "$CACHE_DIR" "$WRITE_DIR" "$WORK_DIR" "$MERGED_DIR" "$COMPAT_DIR"

RCLONE_PID=""
CACHE_NS=""
WARM_PID=""
PREFETCH_PID=""
RECORD_PID=""
WINESERVER_BIN=""
# Steam ends this script's process tree right after the game exits (measured: the runner's log stops at the
# "[stats]" line and cleanup never runs), so nothing important can be left to cleanup(). Instead the runner keeps a
# small session marker; the daemon, which Steam does not control, finishes the session once rclone has exited:
# it repairs rclone's range records, merges older cache folders, builds the start-up profile from a cold run and
# backs up the saves (startup_profile.py finish-session). cleanup() only does the quick safe part, and also asks for
# the same finish in case it does get to run (finish-session is idempotent and takes a lock).
# One marker PER SESSION, not per game: a game quit and started again within seconds would otherwise overwrite the
# previous session's marker before the daemon finished it, and that finisher would then delete the new session's marker.
SESSION_MARKER="$HOME/.local/share/deckstream/sessions/${GAME_ID}.$(date +%s).$$.json"
# A session that ended badly (battery died, crash, quit mid-load) may not be finished yet; its recording shares this
# game's log name and the recorder below truncates it. Merge it into the profile now, before the fill reads the profile.
[ -f "$SP_PY" ] && LD_PRELOAD= python3 "$SP_PY" merge-pending --profile "$HOME/.local/share/deckstream/profiles/${GAME_ID}.json" \
    --log "$HOME/.local/share/deckstream/profiles/${GAME_ID}.log" --window "${DECKSTREAM_PROFILE_SECONDS:-1200}" \
    --folder "$WEBDAV_FOLDER" 2>&1 | sed 's/^/[profile] /'
# Launch timings (key=value lines, last value wins): Play -> fill -> mount -> game start, and how much was still
# downloaded while playing. The daemon's launch_report.py turns finished ones into a summary for the user's site.
LAUNCH_FILE="$HOME/.local/share/deckstream/launches/${GAME_ID}.$(date +%s).$$.txt"
mkdir -p "$(dirname "$LAUNCH_FILE")" 2>/dev/null
mark() { printf '%s=%s\n' "$1" "${2:-$(date +%s)}" >> "$LAUNCH_FILE" 2>/dev/null; }
mark slug "$GAME_ID"; mark play; mark desktop "${XDG_CURRENT_DESKTOP:-unknown}"
finish_session_now() {
    [ -f "$SP_PY" ] && [ -f "$SESSION_MARKER" ] || return 0
    local cmd="LD_PRELOAD= python3 '$SP_PY' finish-session --marker '$SESSION_MARKER'"
    if command -v systemd-run >/dev/null 2>&1 && systemd-run --user --no-block --collect bash -c "$cmd" >/dev/null 2>&1; then
        return 0
    fi
    local sid=""; command -v setsid >/dev/null 2>&1 && sid="setsid"
    ($sid nohup bash -c "$cmd" >/dev/null 2>&1 &)
}
cleanup() {
    # The warm-up and the prefetch read through the mount, so they must stop before the mount can be released.
    [ -n "$WARM_PID" ] && kill "$WARM_PID" 2>/dev/null || true
    [ -n "$PREFETCH_PID" ] && kill "$PREFETCH_PID" 2>/dev/null || true
    [ -n "$RECORD_PID" ] && kill "$RECORD_PID" 2>/dev/null || true
    # Make Proton let go of the files first, then unmount.
    if [ -n "$WINESERVER_BIN" ] && [ -x "$WINESERVER_BIN" ]; then
        WINEPREFIX="$COMPAT_DIR/pfx" "$WINESERVER_BIN" -k 2>/dev/null || true
        sleep 1
    fi
    echo -e "${CYAN}[*] Unmounting cloud stream...${NC}"
    umount_fuse "$MERGED_DIR" "$GAME_DIR"
    [ -n "$RCLONE_PID" ] && kill "$RCLONE_PID" 2>/dev/null || true
    [ -n "${HEDGE_PID:-}" ] && kill "$HEDGE_PID" 2>/dev/null || true
    [ -n "${RUN_LOCK:-}" ] && rm -rf "$RUN_LOCK" "${RUN_LOCK}.reclaim" 2>/dev/null || true
    finish_session_now        # only if we get this far; the daemon finishes the session otherwise
}
trap cleanup EXIT

# 1. rclone
# >>> fetch_verified (identical in stream-runner.sh, cloud-install.sh, umu-test.sh and install.sh)
# Download a tool from upstream; if that fails (DNS trouble, blocked host), fall back to the DeckStream
# mirror. The SHA-256 is pinned by the calling script, so the mirror is only a transport: a file that
# does not match is discarded, whichever server it came from.
tools_mirror() {
    local base="${DECKSTREAM_MIRROR:-}"
    if [ -z "$base" ] && [ -f "$HOME/.config/deckstream/daemon_config.json" ]; then
        base="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("server_url",""))' "$HOME/.config/deckstream/daemon_config.json" 2>/dev/null)"
    fi
    echo "${base:-https://deckstream.vercel.app}/daemon/tools"
}
sha256_of() { (sha256sum "$1" 2>/dev/null || shasum -a 256 "$1") | awk '{print $1}'; }
fetch_verified() {   # fetch_verified UPSTREAM_URL MIRROR_URL SHA256 DEST
    local url
    for url in "$1" "$2"; do
        [ -n "$url" ] || continue
        rm -f "$4.part"
        curl -fsSL --proto "${DECKSTREAM_FETCH_PROTO:-=https}" --tlsv1.2 -m 600 -o "$4.part" "$url" 2>/dev/null || continue
        if [ "$(sha256_of "$4.part")" = "$3" ]; then mv "$4.part" "$4"; return 0; fi
        rm -f "$4.part"
        echo "(download from $url did not match the expected checksum; ignored)" >&2
    done
    return 1
}
# <<< fetch_verified
RCLONE_VERSION="1.75.1"
RCLONE_SHA256="982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab"   # from downloads.rclone.org/v1.75.1/SHA256SUMS

if ! command -v rclone &>/dev/null; then
    echo -e "${YELLOW}[*] Installing standalone rclone binary...${NC}"
    mkdir -p "$HOME/.local/bin"
    RC_TMP="$(mktemp -d)" || fail "Could not create temp dir"
    fetch_verified "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip" "$(tools_mirror)/rclone-v${RCLONE_VERSION}-linux-amd64.zip" "$RCLONE_SHA256" "$RC_TMP/rclone.zip" || { rm -rf "$RC_TMP"; fail "rclone download failed (tried rclone.org and the DeckStream mirror)"; }
    unzip -q -o "$RC_TMP/rclone.zip" -d "$RC_TMP/x" || { rm -rf "$RC_TMP"; fail "rclone unzip failed"; }
    mv "$RC_TMP"/x/rclone-*-linux-amd64/rclone "$HOME/.local/bin/rclone"
    chmod +x "$HOME/.local/bin/rclone"
    rm -rf "$RC_TMP"
fi

# 2. In-memory WebDAV remote (nothing written to rclone.conf)
export RCLONE_CONFIG_TBSTREAM_TYPE="webdav"
export RCLONE_CONFIG_TBSTREAM_URL="$WEBDAV_URL"
export RCLONE_CONFIG_TBSTREAM_VENDOR="other"
export RCLONE_CONFIG_TBSTREAM_USER="$WEBDAV_USER"
# `rclone obscure` returns a DIFFERENT string on every call, and rclone derives the name of its cache folder
# (vfs/tbstream{hash}) from the remote's settings. A fresh obscured password per launch therefore meant a fresh, empty
# cache folder per launch: the cache never survived (Cyberpunk's 50 GB was spread over three folders). Obscure once per
# key, keep the result in a private file next to storage.conf (which already holds the key itself), and reuse it.
PASS_FILE="$HOME/.config/deckstream/.stream_pass"
KEY_SHA="$(printf '%s' "$STORAGE_PASS" | sha256sum | cut -d' ' -f1)"
if [ -f "$PASS_FILE" ] && [ "$(sed -n 1p "$PASS_FILE")" = "$KEY_SHA" ] && [ -n "$(sed -n 2p "$PASS_FILE")" ]; then
    RCLONE_CONFIG_TBSTREAM_PASS="$(sed -n 2p "$PASS_FILE")"
else
    RCLONE_CONFIG_TBSTREAM_PASS="$(rclone obscure "$STORAGE_PASS")" || fail "Could not obscure RemoteStorage key"
    ( umask 077; printf '%s\n%s\n' "$KEY_SHA" "$RCLONE_CONFIG_TBSTREAM_PASS" > "$PASS_FILE" )
fi
export RCLONE_CONFIG_TBSTREAM_PASS
REMOTE="tbstream:$WEBDAV_FOLDER"

# >>> hedge_proxy (OFF unless DECKSTREAM_HEDGE=1: no Deck run yet, AHEADOFTHEPACK row 34)
# Every range request is sent twice through a local helper and the first answer is used: a first launch waits on one
# read at a time and RemoteStorage's first byte is random (median 1.4 s, p90 3.2 s on the Deck). Bounded: if the helper does
# not answer within 3 s the mount talks to RemoteStorage directly, exactly as without the flag. The mount URL then differs,
# so rclone files the cache under a new folder name; adopt-cache moves the earlier data over after mounting.
HEDGE_PID=""
start_hedge_proxy() {
    [ "${DECKSTREAM_HEDGE:-0}" = "1" ] || return 0
    local py="${HEDGE_PY:-$CONF_DIR/hedge_proxy.py}" port="${DECKSTREAM_HEDGE_PORT:-47811}" ok=0
    [ -f "$py" ] || { echo "[*] DECKSTREAM_HEDGE=1 but hedge_proxy.py is missing: direct to RemoteStorage"; mark hedge_failed missing; return 0; }
    LD_PRELOAD='' python3 "$py" --upstream "$WEBDAV_URL" --port "$port" --race "${DECKSTREAM_HEDGE_RACE:-2}" \
        --delay-ms "${DECKSTREAM_HEDGE_DELAY_MS:-0}" --parent-pid $$ --stats "$LOG_DIR/hedge_${GAME_ID}.json" \
        >> "$LOG_DIR/hedge_${GAME_ID}.log" 2>&1 &
    HEDGE_PID=$!
    for _ in $(seq 1 12); do
        if curl -fsS -m 1 "http://127.0.0.1:$port/__deckstream_hedge__" >/dev/null 2>&1; then ok=1; break; fi
        kill -0 "$HEDGE_PID" 2>/dev/null || break
        sleep 0.25
    done
    if [ "$ok" = 1 ]; then
        export RCLONE_CONFIG_TBSTREAM_URL="http://127.0.0.1:$port"
        echo -e "${CYAN}[*] Racing requests on: each range is asked for ${DECKSTREAM_HEDGE_RACE:-2} times, first answer wins${NC}"
        mark hedge "${DECKSTREAM_HEDGE_RACE:-2}"
    else
        kill "$HEDGE_PID" 2>/dev/null || true
        HEDGE_PID=""
        echo "[*] Racing helper did not start: direct to RemoteStorage"
        mark hedge_failed start
    fi
}
# <<< hedge_proxy
start_hedge_proxy

# 3. Pre-flight: can we log in, and is this folder actually playable?
echo -e "${GREEN}[*] Checking RemoteStorage folder: ${CYAN}$WEBDAV_FOLDER${NC}"
LISTING="$(rclone lsf -R --max-depth 2 "$REMOTE" 2>&1)" || fail "Cannot read RemoteStorage folder (login or folder name wrong): $(echo "$LISTING" | tail -n 1)"
[ -z "$LISTING" ] && fail "RemoteStorage folder is empty: $WEBDAV_FOLDER"

# >>> preflight_kind
# RemoteStorage's WebDAV names a folder after the first folder in a torrent's file paths, so two torrents with the same top
# folder are MERGED into one. When both hold the same file names (two versions of one game), the listing shows each name
# twice and the game would read a mix of both: 18 Oct, Spider-Man Remastered (InsaneRamZes + a rutor copy, 19 files of
# different sizes) died at start with c000007b. Refuse it and say what to do.
DUPES="$(echo "$LISTING" | grep -v '/$' | sort | uniq -d | head -n 3)"
if [ -n "$DUPES" ]; then
    fail "RemoteStorage put two releases in this folder (both have $(echo "$DUPES" | head -n 1 | sed 's|.*/||')): the game would mix their files. Remove one of them in RemoteStorage, then send the game again."
fi
if echo "$LISTING" | grep -qiE '(^|/)fg-[0-9]+\.bin$|(^|/)fg-optional.*\.bin$|(^|/)setup-?[0-9]*\.bin$'; then
    fail "This is a REPACK (installer payload). Repacks must be installed first and can't be streamed."
fi
EXE_COUNT=$(echo "$LISTING" | grep -iE '\.exe$' | grep -ivE '(^|/)(setup|unins|vcredist|dxsetup|dotnet|redist|crash)[^/]*$' | grep -c . || true)
if [ "$EXE_COUNT" -eq 0 ] && echo "$LISTING" | grep -qiE '(^|/)setup[^/]*\.exe$'; then
    fail "This looks like an installer (GOG/setup), not an extracted game. Use 'Install from RemoteStorage' instead."
fi
# The listing is 2 levels deep, but the plan's exe can sit deeper (Cyberpunk: Cyberpunk 2077/bin/x64/Cyberpunk2077.exe).
# If that exact file is there, the folder holds the game, even with another release's zip beside it (18 Oct: RemoteStorage put
# the 69 GB GOG-Rip folder and a 148 GB InsaneRamZes zip in one WebDAV folder, and this refused it as an archive).
if [ "$EXE_COUNT" -eq 0 ] && [ -n "$EXE_REL_PATH" ] && \
   rclone lsf "$REMOTE/$(dirname -- "$EXE_REL_PATH")" 2>/dev/null | grep -qixF -- "$(basename -- "$EXE_REL_PATH")"; then
    EXE_COUNT=1
fi
ARCHIVE_COUNT=$(echo "$LISTING" | grep -ciE '\.(zip|rar|7z|r[0-9]{2}|part[0-9]+\.rar)$' || true)
if [ "$ARCHIVE_COUNT" -gt 0 ] && [ "$EXE_COUNT" -eq 0 ]; then
    fail "This release is a compressed archive (.zip/.rar/.7z). It must be extracted first and can't be streamed."
fi
# <<< preflight_kind

# Parallel chunk reads (rclone >= 1.67) fetch several pieces of a file at once; one stream tops out
# around 100 Mbps against RemoteStorage WebDAV while several reach about 250 Mbps.
# Measured against RemoteStorage from South Africa: one stream ~10 MB/s, ~8 streams in flight reach the
# ~23 MB/s ceiling, and every range request takes ~1 s to start. So use big chunks; a game keeps
# several files open, and on the Deck 1 chunk stream per file beat 4 (see CHUNK_STREAMS below).
# Read-ahead: measured on the Deck (Sifu, 29 GB pak): with 128M read-ahead a cold random 4 MB read took
# ~6 s because earlier reads were still downloading; 32M read-ahead with 16M chunks cut that to ~3.5 s and
# kept sequential reads at ~17 MB/s (vs ~18). See RESEARCH.md section 9. Override without editing code:
#   DECKSTREAM_CHUNK_SIZE=32M DECKSTREAM_CHUNK_STREAMS=4 DECKSTREAM_READ_AHEAD=128M
#   (lower the chunk size or streams if the Deck ever feels memory-starved)
CHUNK_SIZE="${DECKSTREAM_CHUNK_SIZE:-16M}"
READ_AHEAD="${DECKSTREAM_READ_AHEAD:-32M}"
CHUNK_STREAMS=""
if rclone mount --help 2>/dev/null | grep -q -- "--vfs-read-chunk-streams"; then
    # 1 stream for every game (25 Oct): cold Cyberpunk Play -> title 447-519 s on 1 stream vs 642-673 s on 4,
    # no 429s; profiled BG3 cut over-fetch 2.6 -> 0.6 MB/s. Override with DECKSTREAM_CHUNK_STREAMS.
    STREAMS="${DECKSTREAM_CHUNK_STREAMS:-1}"
    CHUNK_STREAMS="--vfs-read-chunk-streams $STREAMS"
fi
# List the whole folder tree in the background right after mounting. Each uncached directory lookup
# is a slow WebDAV request, and a game opens thousands of files. (Deliberately NOT using
# --vfs-fast-fingerprint: changing it would invalidate the cache that has already been downloaded.)
VFS_REFRESH=""
if rclone mount --help 2>/dev/null | grep -q -- "--vfs-refresh"; then
    VFS_REFRESH="--vfs-refresh"
fi

# A prestage (the daemon fills a game's cache when it is queued, or overnight while charging) writes into rclone's
# cache files directly, which is only safe while no rclone uses them. Stop EVERY background fill, not only this
# game's: this one's must stop before mounting, and any other game's would compete with this game for the line.
# The daemon starts the next one when no game is running.
for PRESTAGE_PID_FILE in "$HOME/.local/share/deckstream/prestage/"*.pid; do
    [ -f "$PRESTAGE_PID_FILE" ] || continue
    PPID_PRESTAGE="$(cat "$PRESTAGE_PID_FILE" 2>/dev/null)"
    # only if that pid really is our prestage (a stale file after a reboot could name an unrelated process)
    if [ -n "$PPID_PRESTAGE" ] && kill -0 "$PPID_PRESTAGE" 2>/dev/null \
       && tr '\0' ' ' < "/proc/$PPID_PRESTAGE/cmdline" 2>/dev/null | grep -q "startup_profile.py"; then
        echo -e "${CYAN}[*] Stopping a background prestage ($(basename "$PRESTAGE_PID_FILE" .pid)) so the game can start...${NC}"
        # The daemon starts it in its own session, so its pid is also its process group: stop the whole group
        # (a step it is running must not keep writing into the cache after rclone mounts).
        kill -TERM -- "-$PPID_PRESTAGE" 2>/dev/null || kill "$PPID_PRESTAGE" 2>/dev/null
        for _ in $(seq 1 40); do kill -0 -- "-$PPID_PRESTAGE" 2>/dev/null || break; sleep 0.25; done
        kill -KILL -- "-$PPID_PRESTAGE" 2>/dev/null || true
    fi
    rm -f "$PRESTAGE_PID_FILE"
done

# Pre-mount exact fill: fetch the bytes this game's start-up profile needs with direct range requests, written straight into
# rclone's cache (no over-fetch), before rclone mounts. DEFAULT = the full fill (start-up AND later ranges, up to 12 GB): that
# is what Cyberpunk needed to actually run (a 4 GB guess made it die within seconds, 14 Oct). The EARLY launch (fill only the
# profile's early_fill_bytes) is OPT-IN with DECKSTREAM_EARLY_LAUNCH=1 and only ever used with a MEASURED demand curve under 8 GB;
# an estimated one never counts. Turn it on per game only after a Deck run shows the game on screen.
NS_FILE="$HOME/.config/deckstream/stream_ns"
STREAM_NS="$(cat "$NS_FILE" 2>/dev/null)"
STREAM_PROFILE="$HOME/.local/share/deckstream/profiles/${GAME_ID}.json"

# >>> choose_fill_args (sets FILL_MODE and FILL_BUDGET_ARGS; the test sources this function)
choose_fill_args() {
    FILL_MODE="full"
    FILL_BUDGET_ARGS=(--include-later --max-gb "${DECKSTREAM_FILL_MAX_GB:-12}")
    [ "${DECKSTREAM_EARLY_LAUNCH:-0}" = "1" ] || return 0
    local calc early gb
    calc="$(python3 "$SP_PY" early-fill --profile "$STREAM_PROFILE" 2>/dev/null || true)"
    early="$(printf '%s' "$calc" | python3 -c "import json,sys; d=json.load(sys.stdin); print(int(d.get('early_fill_bytes', 0)) if d.get('measured') else 0)" 2>/dev/null || echo 0)"
    # The demand gate (1.5x safety margin on the recorded demand curve) says how big the head start must be; never go below it.
    gate_req="$(python3 "$(dirname "$SP_PY")/demand_gate.py" --profile "$STREAM_PROFILE" --line-mbps 15 --fill-bytes 0 2>/dev/null | python3 -c "import json,sys; print(int(json.load(sys.stdin).get('required_head_start_bytes', 0)))" 2>/dev/null || echo 0)"
    if [ "$early" -gt 0 ] 2>/dev/null && [ "${gate_req:-0}" -gt "$early" ] 2>/dev/null; then early="$gate_req"; fi
    if [ "$early" -gt 0 ] 2>/dev/null && [ "$early" -lt 8589934592 ]; then
        gb="$(python3 -c "print(round($early / 1e9, 2))")"
        FILL_MODE="early"
        FILL_BUDGET_ARGS=(--max-gb "$gb")
    fi
}
# <<< choose_fill_args

if [ "${DECKSTREAM_PREMOUNT_FILL:-0}" = "1" ] && [ -f "$SP_PY" ] && [ -f "$STREAM_PROFILE" ] && [ -n "$STREAM_NS" ]; then
    choose_fill_args
    FILL_ARGS=(--ns "$STREAM_NS" --folder "$WEBDAV_FOLDER" --profile "$STREAM_PROFILE" --workers "${DECKSTREAM_FILL_WORKERS:-8}" --log "$LOG_DIR/stream_fill_${GAME_ID}.log" "${FILL_BUDGET_ARGS[@]}")
    if [ "$FILL_MODE" = "early" ]; then
        echo -e "${GREEN}[*] Early launch (opt-in, measured demand): pre-filling ${FILL_BUDGET_ARGS[1]} GB before mount...${NC}"
    else
        echo -e "${GREEN}[*] Fetching this game's start-up data before mounting (exact ranges, max ${DECKSTREAM_FILL_MAX_GB:-12} GB)...${NC}"
    fi
    command -v notify-send >/dev/null 2>&1 && notify-send "DeckStream: $GAME_NAME" "Preparing stream cache... Please wait." || true
    mark cache_mb_before_fill "$(du -sm "$CACHE_DIR" 2>/dev/null | cut -f1)"; mark fill_start
    # The Deck suspends after a few idle minutes and a cold fill takes ~10: a sleep froze the download with the person
    # waiting for their game (15 Oct, Control). Hold a sleep lock just for the fill (finite, ends with it); battery or
    # charger, because they pressed Play. Off with DECKSTREAM_FILL_INHIBIT=0; without systemd-inhibit nothing changes.
    FILL_INHIBIT=()
    if [ "${DECKSTREAM_FILL_INHIBIT:-1}" = "1" ] && command -v systemd-inhibit >/dev/null 2>&1 && systemd-inhibit --what=sleep true >/dev/null 2>&1; then
        FILL_INHIBIT=(systemd-inhibit --what=sleep --who=DeckStream "--why=Preparing a game to play" --mode=block)
        mark sleep_inhibited 1
    fi
    LD_PRELOAD= ${FILL_INHIBIT[@]+"${FILL_INHIBIT[@]}"} python3 "$SP_PY" fill-cache --cache-dir "$CACHE_DIR" "${FILL_ARGS[@]}" 2>&1 | tail -n 3 | sed 's/^/[fill] /'
    FILL_RC="${PIPESTATUS[0]}"
    mark fill_end; mark fill_rc "$FILL_RC"
    case "$FILL_RC" in
        0) echo "[fill] done" ;;
        3) echo "[fill] skipped: a running rclone uses this cache" ;;
        *) echo "[fill] some pieces failed; rclone will fetch the rest" ;;
    esac
fi

# rclone saves which byte ranges of a cached file it holds only when the file is closed, and Steam kills the whole
# process tree when a game exits, so that record is often empty while the data is on disk. At the next mount rclone
# then treats the file as "nothing cached" and DISCARDS the data (measured: 5.7 GB cached -> 0.5 GB). Rebuild the
# record from the data files before rclone sees them.
if [ -f "$SP_PY" ]; then
    LD_PRELOAD= python3 "$SP_PY" repair-meta --cache-dir "$CACHE_DIR" 2>&1 | sed 's/^/[cache] /'
fi

# 4. Mount just this game's folder (cached data is kept 90 days by default, not 3: a game left alone for a few days
#    must not go cold again. The size cap and --vfs-cache-min-free-space do the real eviction.)
echo -e "${GREEN}[*] Mounting RemoteStorage stream (cache: $CACHE_MAX at $CACHE_DIR)...${NC}"
# The game shares the Deck's 16 GB with rclone. --buffer-size is per OPEN file, so a game with
# hundreds of files open can use gigabytes; keep buffers small and cap rclone's heap.
# Demand log (measurement runs only, DECKSTREAM_DEMAND_LOG=1 in game.env/stream.env): rclone logs every read the game
# makes (DEBUG, microsecond times) to its own file, so "startup_profile.py demand" can build the game's TRUE demand
# curve (when each byte was asked for with the data already local), which sizes an early launch. Large and a little
# CPU, so off by default.
RCLONE_LOG_ARGS=(--log-file "$LOG_DIR/rclone_${GAME_ID}.log" --log-level NOTICE)
if [ "${DECKSTREAM_DEMAND_LOG:-0}" = "1" ]; then
    DEMAND_LOG="$LOG_DIR/demand_${GAME_ID}.$(date +%s).log"
    RCLONE_LOG_ARGS=(--log-file "$DEMAND_LOG" --log-level DEBUG --log-format date,time,microseconds)
    mark demand_log "$DEMAND_LOG"
    echo -e "${CYAN}[*] Demand log on: every read is recorded to $(basename "$DEMAND_LOG")${NC}"
fi
GOMEMLIMIT=1GiB rclone mount "$REMOTE" "$GAME_DIR" \
  --read-only \
  --vfs-cache-mode full \
  --vfs-cache-max-size "$CACHE_MAX" \
  --vfs-cache-max-age "${DECKSTREAM_CACHE_MAX_AGE:-2160h}" \
  --vfs-cache-min-free-space 3G \
  --vfs-read-chunk-size "$CHUNK_SIZE" \
  $CHUNK_STREAMS \
  $VFS_REFRESH \
  --vfs-read-ahead "$READ_AHEAD" \
  --buffer-size 8M \
  --dir-cache-time 24h \
  --attr-timeout 24h \
  --no-modtime \
  --cache-dir "$CACHE_DIR" \
  "${RCLONE_LOG_ARGS[@]}" &
RCLONE_PID=$!

# Wait up to 20s for the mount to become readable
for _ in $(seq 1 40); do
    if mountpoint -q "$GAME_DIR" && ls "$GAME_DIR" &>/dev/null; then break; fi
    kill -0 "$RCLONE_PID" 2>/dev/null || fail "rclone mount exited. See $LOG_DIR/rclone_${GAME_ID}.log"
    sleep 0.5
done
mountpoint -q "$GAME_DIR" || fail "Mount did not come up in time. See $LOG_DIR/rclone_${GAME_ID}.log"
echo -e "${GREEN}[✓] Cloud stream mounted at ${GAME_DIR}${NC}"
command -v notify-send >/dev/null 2>&1 && notify-send "DeckStream: $GAME_NAME" "🎮 Cloud stream mounted. Starting game..." || true
mark mounted

# The cache folder rclone is using now (its remote name is shown as the mount source, e.g. tbstream{AbCdE}).
CACHE_NS="$(awk -v m="$GAME_DIR" '$2 == m {print $1; exit}' /proc/mounts | sed 's/:.*//')"
# The namespace depends on the remote's settings, not the game, so one saved value serves every game's prestage/fill.
case "$CACHE_NS" in tbstream\{*\}) ( umask 077; printf '%s\n' "$CACHE_NS" > "$NS_FILE"; printf '%s\n' "$CACHE_BASE" > "$HOME/.config/deckstream/stream_cache_base" ) ;; esac
if [ -n "$CACHE_NS" ] && [ -f "$SP_PY" ]; then
    # Bring over what earlier launches downloaded (they each used a different cache folder): a rename, instant, and
    # it happens before the game opens any file.
    LD_PRELOAD= python3 "$SP_PY" adopt-cache --cache-dir "$CACHE_DIR" --current "$CACHE_NS" --folder "$WEBDAV_FOLDER" 2>&1 | sed 's/^/[cache] /'
    LD_PRELOAD= python3 "$SP_PY" repair-meta --cache-dir "$CACHE_DIR" 2>&1 | sed 's/^/[cache] /'
    # The session marker for the daemon: the profile is built only after a COLD run (almost empty cache at launch).
    COLD=0; [ "$(du -sm "$CACHE_DIR" 2>/dev/null | cut -f1)" -lt 500 ] 2>/dev/null && COLD=1
    LD_PRELOAD= python3 "$SP_PY" session-start --marker "$SESSION_MARKER" --game-id "$GAME_ID" --cache-dir "$CACHE_DIR" \
        --ns "$CACHE_NS" --folder "$WEBDAV_FOLDER" --profile "$HOME/.local/share/deckstream/profiles/${GAME_ID}.json" \
        --profile-log "$HOME/.local/share/deckstream/profiles/${GAME_ID}.log" --rclone-pid "$RCLONE_PID" --cold "$COLD" \
        --profile-seconds "${DECKSTREAM_PROFILE_SECONDS:-1200}"
fi

# 5. Optional local write layer (saves/configs/shader caches on SSD)
RUN_PATH="$GAME_DIR"
if command -v fuse-overlayfs &>/dev/null; then
    # English text: releases that pick their language from a file in the game folder (GOG goggame-*.info, Goldberg
    # steam_settings) get an English copy in this game's write folder, written before the layer is mounted. Per game
    # id, so a new id for the same release is covered too. Never replaces a file already there. Off: DECKSTREAM_ENGLISH=0.
    if [ "${DECKSTREAM_ENGLISH:-1}" != "0" ] && [ -f "$HOME/.config/deckstream/language_fix.py" ]; then
        LD_PRELOAD= bounded 30 python3 "$HOME/.config/deckstream/language_fix.py" --game "$GAME_DIR" --writes "$WRITE_DIR" 2>&1 | head -n 20 || true
    fi
    OVERLAY_ERR=$(fuse-overlayfs -o lowerdir="$GAME_DIR",upperdir="$WRITE_DIR",workdir="$WORK_DIR" "$MERGED_DIR" 2>&1)
    OVERLAY_RC=$?
    if [ "$OVERLAY_RC" -eq 0 ]; then
        RUN_PATH="$MERGED_DIR"
        echo -e "${GREEN}[✓] Local write layer active${NC}"
        # Auto-apply crack if _crack or Crack folder exists in the release
        for cdir in "$GAME_DIR/_crack" "$GAME_DIR/_Crack" "$GAME_DIR/Crack" "$GAME_DIR/crack"; do
            if [ -d "$cdir" ]; then
                echo -e "${GREEN}[✓] Auto-applying crack files from $(basename "$cdir") to local write layer...${NC}"
                cp -rn "$cdir"/* "$WRITE_DIR/" 2>/dev/null || true
                break
            fi
        done
    else
        echo -e "${YELLOW}[*] Write layer unavailable - running read-only (saves still go to the Proton prefix)${NC}"
        # Kept, not hidden: a game that runs from the cloud copy shows its default (Russian) text, so the reason matters.
        echo "[*] fuse-overlayfs failed (exit $OVERLAY_RC): $(printf '%s' "$OVERLAY_ERR" | head -c 400 | tr '\n' ' ')"
        mark overlay_failed "$OVERLAY_RC" 2>/dev/null
    fi
fi

# 6. Locate game executable
TARGET_EXE=""
case "/$EXE_REL_PATH/" in */../*|*/./*|*//*) EXE_REL_PATH="" ;; esac   # never leave the mounted folder
if [ -n "$EXE_REL_PATH" ] && [ -f "$RUN_PATH/$EXE_REL_PATH" ]; then
    TARGET_EXE="$RUN_PATH/$EXE_REL_PATH"
else
    # Prefer UE *-Shipping.exe, then largest remaining exe within depth 5
    TARGET_EXE=$(find "$RUN_PATH" -maxdepth 6 -iname "*-Win64-Shipping.exe" 2>/dev/null | head -n 1)
    if [ -z "$TARGET_EXE" ]; then
        TARGET_EXE=$(find "$RUN_PATH" -maxdepth 6 -iname "*.exe" \
            ! -iname "*unins*" ! -iname "*setup*" ! -iname "*crash*" ! -iname "*redist*" \
            ! -iname "*vc_*" ! -iname "*dxsetup*" ! -iname "*dotnet*" ! -iname "*CEFSubProcess*" \
            ! -iname "*QuickSFV*" ! -iname "*easyanticheat*" ! -iname "*handler*" -printf '%d\t%s\t%p\n' 2>/dev/null \
            | sort -t "$(printf '\t')" -k1,1n -k2,2nr | head -n 1 | cut -f3-)
    fi
fi
if [ -z "$TARGET_EXE" ]; then
    echo "[!] Top of the mounted folder (for diagnosis):"
    find "$RUN_PATH" -maxdepth 3 2>/dev/null | head -n 25
    fail "Mounted, but no game .exe found in $WEBDAV_FOLDER"
fi
echo -e "${GREEN}[✓] Executable: ${CYAN}${TARGET_EXE}${NC}"

# Refuse releases that require Windows kernel drivers or hypervisor cracks (.sys) which cannot run on Linux/Proton
if find "$GAME_DIR" -maxdepth 4 \( -name "*.sys" -o -iname "*simplesvm*" -o -iname "*hyperkd*" \) 2>/dev/null | grep -q .; then
    fail "This release requires Windows kernel hypervisor drivers (.sys) that cannot run on SteamOS/Proton"
fi

# Refuse games using Microsoft DirectStorage (dstorage.dll) which stall or freeze over network streaming mounts
if find "$GAME_DIR" -maxdepth 4 -iname "dstorage.dll" 2>/dev/null | grep -q .; then
    fail "This game uses Microsoft DirectStorage (dstorage.dll) which cannot run over a network stream. Install it to the SD card to play."
fi

# 7. Find Proton (GE / custom first, then Valve)
PROTON_BIN=""
for p in "$HOME"/.steam/root/compatibilitytools.d/*/proton \
         "$HOME/.local/share/Steam/steamapps/common/Proton - Experimental/proton" \
         "$HOME"/.local/share/Steam/steamapps/common/Proton*/proton; do
    [ -f "$p" ] && { PROTON_BIN="$p"; break; }
done
[ -z "$PROTON_BIN" ] && fail "No Proton found. Install Proton Experimental from the Steam library (Tools)."

WINESERVER_BIN="$(dirname "$PROTON_BIN")/files/bin/wineserver"
[ -x "$WINESERVER_BIN" ] || WINESERVER_BIN="$(dirname "$PROTON_BIN")/dist/bin/wineserver"

export STEAM_COMPAT_DATA_PATH="$COMPAT_DIR"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
export DXVK_STATE_CACHE_PATH="$WRITE_DIR"
CACHE_KEY="$GAME_ID"
# >>> game_env (same block in stream-runner.sh and game-launcher.sh)
# Per-game settings for every game, streamed or installed (KEY=value lines in ~/.config/deckstream/game.env).
[ -f "$HOME/.config/deckstream/game.env" ] && . "$HOME/.config/deckstream/game.env"
# Shader caches: Mesa's default is ONE folder shared by every game and capped at 1 GB, so games evict each other's
# shaders and stutter while recompiling. Unless Steam already gave this launch its own cache folder, keep one per
# game that nothing ever wipes (not the stream cache, not a re-created shortcut). Off with DECKSTREAM_SHADER_CACHE=0.
case "$CACHE_KEY" in */*|.|..) CACHE_KEY="" ;; esac   # a folder name, never a path
# Deck mode, the per-game "Deck mode: 30 fps, low graphics" switch on the phone and the Decky panel: a file only the
# daemon or the panel writes (deck_mode/<slug>.env). Exported, so game-launcher.sh, run by the stream runner, sees it.
for DM_KEY in $CACHE_KEY ${DECK_MODE_KEYS:-}; do
    case "$DM_KEY" in */*|.|..) continue ;; esac
    if [ -f "$HOME/.config/deckstream/deck_mode/$DM_KEY.env" ]; then
        set -a; . "$HOME/.config/deckstream/deck_mode/$DM_KEY.env"; set +a
        echo "[*] Deck mode: 30 fps, low graphics"; break
    fi
done
if [ "${DECKSTREAM_SHADER_CACHE:-1}" != "0" ] && [ -n "$CACHE_KEY" ]; then
    if [ -z "${MESA_SHADER_CACHE_DIR:-}" ] && [ -z "${STEAM_COMPAT_SHADER_PATH:-}" ]; then
        SHADER_DIR="$HOME/.local/share/deckstream/shadercache/$CACHE_KEY"
        if mkdir -p "$SHADER_DIR" 2>/dev/null; then
            export MESA_SHADER_CACHE_DIR="$SHADER_DIR"
            export MESA_SHADER_CACHE_MAX_SIZE="${MESA_SHADER_CACHE_MAX_SIZE:-4G}"
            export VKD3D_SHADER_CACHE_PATH="${VKD3D_SHADER_CACHE_PATH:-$SHADER_DIR}"
        fi
    fi
    echo "[*] Shader cache: ${MESA_SHADER_CACHE_DIR:-${STEAM_COMPAT_SHADER_PATH:-Mesa default}}"
fi
# Frame-rate cap (opt-in, e.g. DECKSTREAM_FPS_CAP=40): less GPU work, heat, fan and battery. DXVK covers DX9-11,
# VKD3D-Proton DX12. In Gaming Mode the Quick Access menu's per-game frame limit does the same.
case "${DECKSTREAM_FPS_CAP:-}" in
    ''|0|*[!0-9]*) ;;
    *) export DXVK_FRAME_RATE="$DECKSTREAM_FPS_CAP" VKD3D_FRAME_RATE="$DECKSTREAM_FPS_CAP"
       echo "[*] Frame rate capped at $DECKSTREAM_FPS_CAP fps" ;;
esac
# <<< game_env
# Proton's own log (wine errors, missing DLLs, crashes) for diagnosing a game that never opens.
# Proton's full trace log (wine errors, missing DLLs, crashes) is for diagnosing a game that crashes or never opens. It has a
# cost on every run, so it is OFF unless DECKSTREAM_PROTON_LOG=1.
if [ "${DECKSTREAM_PROTON_LOG:-0}" = "1" ]; then
    export PROTON_LOG=1
    export PROTON_LOG_DIR="$LOG_DIR"
fi

# Saves: remember this game's prefix so it can be backed up (Ludusavi), then back up after the session.
SAVES_PY="$HOME/.config/deckstream/saves.py"
[ -f "$SAVES_PY" ] && python3 "$SAVES_PY" register --slug "$GAME_ID" --title "$GAME_NAME" --prefix "$COMPAT_DIR" --kind stream >/dev/null 2>&1
LAUNCH_AT=$(date +%s)
mark launched "$LAUNCH_AT"
echo -e "${GREEN}[+] Launching with $(basename "$(dirname "$PROTON_BIN")")...${NC}"
# The runner and its helpers stay OUT of the game folder (only the game is started from there), so that after the game
# exits, "is anything still using the mount?" sees only the game's own processes.
cd "$HOME" || cd /
# Start-up profile: record which byte ranges the game reads (read from rclone's sparse cache files, with times);
# if an earlier cold run left a profile, prefetch its ranges in parallel right now. Off with DECKSTREAM_PROFILE=0.
# The prefetch reads whatever the pre-mount fill did not cover (start-up, then later, in first-need order), so a game
# whose profile is bigger than the fill's 12 GB cap keeps reading ahead of itself during play.
PROFILE_DIR="$HOME/.local/share/deckstream/profiles"
PROFILE="$PROFILE_DIR/${GAME_ID}.json"
PROFILE_LOG="$PROFILE_DIR/${GAME_ID}.log"
START_CACHE_MB="$(du -sm "$CACHE_DIR" 2>/dev/null | cut -f1)"
mark cache_mb_at_launch "$START_CACHE_MB"
if [ "${DECKSTREAM_PROFILE:-1}" != "0" ] && [ -f "$SP_PY" ]; then
    mkdir -p "$PROFILE_DIR"
    # With a profile already there, record only what is NEW this run (what is cached now, from earlier runs or the
    # pre-mount fill, is the baseline); finish-session merges it into the profile so it grows run by run.
    BASELINE=""; [ -f "$PROFILE" ] && BASELINE="--baseline"
    LD_PRELOAD= python3 "$SP_PY" record --cache-dir "$CACHE_DIR" --folder "$WEBDAV_FOLDER" --log "$PROFILE_LOG" --interval 10 $BASELINE >/dev/null 2>&1 &
    RECORD_PID=$!
    # Post-mount background prefetch: fetches later ranges (or edges) while the game runs.
    # DECKSTREAM_PREFETCH_DELAY (seconds, default 0: immediate) holds back the background prefetch so it
    # does not compete with the game's own initial reads for line bandwidth (measured on Cyberpunk 16 Oct: saves ~2 min to screen).
    PREFETCH_DELAY="${DECKSTREAM_PREFETCH_DELAY:-0}"
    if [ -f "$PROFILE" ] && [ "${DECKSTREAM_PROFILE_PREFETCH:-1}" != "0" ]; then
        if [ "$PREFETCH_DELAY" -gt 0 ] 2>/dev/null; then
            LD_PRELOAD= bash -c 'sleep "$1"; exec nice -n 10 python3 "$2" prefetch --mount "$3" --profile "$4" \
                --workers "$5" --cache-dir "$6" --folder "$7" --include-later --log "$8"' _ \
                "$PREFETCH_DELAY" "$SP_PY" "$GAME_DIR" "$PROFILE" "${DECKSTREAM_PREFETCH_WORKERS:-6}" \
                "$CACHE_DIR" "$WEBDAV_FOLDER" "$LOG_DIR/stream_prefetch_${GAME_ID}.log" >/dev/null 2>&1 &
        else
            LD_PRELOAD= nice -n 10 python3 "$SP_PY" prefetch --mount "$GAME_DIR" --profile "$PROFILE" \
                --workers "${DECKSTREAM_PREFETCH_WORKERS:-6}" --cache-dir "$CACHE_DIR" --folder "$WEBDAV_FOLDER" \
                --include-later --log "$LOG_DIR/stream_prefetch_${GAME_ID}.log" >/dev/null 2>&1 &
        fi
        PREFETCH_PID=$!
    elif [ ! -f "$PROFILE" ] && [ "${DECKSTREAM_EDGE_PREFETCH:-1}" != "0" ]; then
        # No profile yet (a game never played here): at least fetch the header and index (first and last MB) of every big
        # archive in parallel, since a game always reads those first. Skips what is cached. DECKSTREAM_EDGE_PREFETCH=0 turns it off.
        if [ "$PREFETCH_DELAY" -gt 0 ] 2>/dev/null; then
            LD_PRELOAD= bash -c 'sleep "$1"; exec nice -n 10 python3 "$2" prefetch-edges --mount "$3" \
                --cache-dir "$4" --folder "$5" --log "$6"' _ \
                "$PREFETCH_DELAY" "$SP_PY" "$GAME_DIR" "$CACHE_DIR" "$WEBDAV_FOLDER" \
                "$LOG_DIR/stream_edges_${GAME_ID}.log" >/dev/null 2>&1 &
        else
            LD_PRELOAD= nice -n 10 python3 "$SP_PY" prefetch-edges --mount "$GAME_DIR" --cache-dir "$CACHE_DIR" --folder "$WEBDAV_FOLDER" \
                --log "$LOG_DIR/stream_edges_${GAME_ID}.log" >/dev/null 2>&1 &
        fi
        PREFETCH_PID=$!
    fi
fi
# Warm-up: the game's own reads are latency-bound (~8 MB/s in the Sifu test) while the mount can do 18-22,
# so quietly read the rest of the game into the cache. It waits (default 2 min) so start-up reads come first,
# stays under DECKSTREAM_WARMUP_MBPS (default 8) and stops when the game ends.
# Off with DECKSTREAM_WARMUP=0. Default "auto": skipped once the game's start-up profile is stable, because then the
# daemon fetches what the game needs BEFORE play (queue-time prestage, overnight warm-up) and the Deck should not
# spend network, CPU and battery on reads while the game runs. DECKSTREAM_WARMUP=1 forces it on.
WARM_PY="$HOME/.config/deckstream/warmup.py"
WARMUP_MODE="${DECKSTREAM_WARMUP:-auto}"
if [ "$WARMUP_MODE" = "auto" ] && [ -f "$PROFILE" ] && grep -q '"stable": true' "$PROFILE" 2>/dev/null; then
    echo -e "${CYAN}[*] Start-up profile is stable: no in-game warm-up (the daemon fills the cache before play)${NC}"
    WARMUP_MODE=0
fi
if [ "$WARMUP_MODE" != "0" ] && [ -f "$WARM_PY" ]; then
    # During a recording run the warm-up would pollute the profile with files the game never asked for (it reads
    # everything, smallest first), so it waits until the recorded window is over.
    WARM_DELAY="${DECKSTREAM_WARMUP_DELAY:-120}"
    # While the start-up profile is still growing (missing, or the last run still added data), the warm-up would be
    # recorded as "the game read this" and bloat it, so it waits until the start-up window is over.
    if [ -n "$RECORD_PID" ] && { [ ! -f "$PROFILE" ] || ! grep -q '"stable": true' "$PROFILE" 2>/dev/null; }; then
        WARM_DELAY=$(( ${DECKSTREAM_PROFILE_SECONDS:-1200} + 30 ))
    fi
    # If a prefetch (profile replay or header/footer fetch) is running it uses the whole line, so the warm-up waits for it.
    LD_PRELOAD= bash -c 'pf="$1"; shift; while [ -n "$pf" ] && kill -0 "$pf" 2>/dev/null; do sleep 3; done; exec nice -n 19 "$@"' _ "$PREFETCH_PID" \
        python3 "$WARM_PY" "$GAME_DIR" --delay "$WARM_DELAY" --mbps "${DECKSTREAM_WARMUP_MBPS:-8}" \
        --log "$LOG_DIR/stream_warmup_${GAME_ID}.log" >/dev/null 2>&1 &
    WARM_PID=$!
fi
# Cache growth over time (MB read from RemoteStorage so far), so a session can be judged afterwards.
STATS_LOG="$LOG_DIR/stream_stats_${GAME_ID}.log"
( unset LD_PRELOAD   # Steam's 32-bit overlay preload makes every helper command print a loader error
  while kill -0 "$RCLONE_PID" 2>/dev/null; do
      MB="$(du -sm "$CACHE_DIR" 2>/dev/null | cut -f1)"
      echo "$(date +%T) cache_mb=$MB"
      printf 'cache_mb_last=%s\nlast_seen=%s\n' "$MB" "$(date +%s)" >> "$LAUNCH_FILE" 2>/dev/null
      sleep 30
  done ) >> "$STATS_LOG" 2>&1 &
STATS_PID=$!
# Same launcher as installed games (umu when ready, Proton otherwise). It only needs the exe and the
# prefix; saves are registered and backed up here, so no slug is passed.
LAUNCHER="$HOME/.config/deckstream/game-launcher.sh"
run_game() {
    if [ -f "$LAUNCHER" ]; then
        bash "$LAUNCHER" "$TARGET_EXE" "$COMPAT_DIR"
    else
        ( cd "$(dirname "$TARGET_EXE")" || true; exec "$PROTON_BIN" waitforexitandrun "$TARGET_EXE" )
    fi
}
# A Wine server left running by an earlier session of this game (Steam ends the runner before its cleanup can stop it)
# still holds that session's folders; the new game would attach to it. Stop it first; with none running this is a no-op.
if [ -n "$WINESERVER_BIN" ] && [ -x "$WINESERVER_BIN" ]; then
    WINEPREFIX="$COMPAT_DIR/pfx" "$WINESERVER_BIN" -k 2>/dev/null || true
fi
# should_retry_launch SECONDS_RAN: decide whether the launch failed for a reason a fresh write layer fixes.
# Seen on the Deck (Cyberpunk 2026-10-10 22:59 and 2026-10-11 17:48): the game died within 2-5 s with
# "OSError: [Errno 107] Transport endpoint is not connected" because the write layer ($MERGED_DIR) had died;
# pressing Play again worked. Return 0 to remount and launch once more, 1 to give up.
should_retry_launch() {
    local ran="$1"
    # Only a launch that died at once (a quit from the menu takes minutes), only when the game ran from the write
    # layer, and only when that layer is now dead: another kind of crash would just crash again.
    [ "$ran" -lt 30 ] && [ "$RUN_PATH" = "$MERGED_DIR" ] && ! ls "$MERGED_DIR" >/dev/null 2>&1
}
# >>> controller_guard
# Steam's virtual gamepad (USB 28de:11ff, "Microsoft X-Box 360 pad") must exist before Wine starts: FF7 cold launches
# failed with "plugplay Failed to create or open device HID\VID_28DE&PID_11FF ... error 0xe000020b" and the game stayed
# in keyboard mode. Wait for THAT device (any /dev/input node would match the Deck's power button at once), at most
# DECKSTREAM_CONTROLLER_TIMEOUT seconds (5), then a short settle; log how long it took. Never blocks a launch.
wait_for_controller() {
    local devices="${DECKSTREAM_INPUT_DEVICES:-/proc/bus/input/devices}"
    local timeout="${DECKSTREAM_CONTROLLER_TIMEOUT:-5}"
    local settle="${DECKSTREAM_CONTROLLER_SETTLE:-0.5}"
    local start now waited
    start=$(date +%s%N 2>/dev/null); case "$start" in *N|'') start=$(( $(date +%s) * 1000000000 )) ;; esac
    while :; do
        now=$(date +%s%N 2>/dev/null); case "$now" in *N|'') now=$(( $(date +%s) * 1000000000 )) ;; esac
        waited=$(( (now - start) / 1000000 ))
        if grep -qiE '^I:.*Vendor=28de Product=11ff' "$devices" 2>/dev/null; then
            sleep "$settle"
            echo -e "${GREEN}[✓] Controller: Steam virtual pad (28de:11ff) present after ${waited} ms${NC}"
            mark controller_wait_ms "$waited" 2>/dev/null
            return 0
        fi
        if [ "$waited" -ge $(( timeout * 1000 )) ]; then
            echo -e "${YELLOW}[*] Controller: Steam virtual pad not seen within ${timeout}s; launching anyway${NC}"
            # Evidence for the next failing launch: which input devices the kernel did list at that moment.
            echo "[*] Controller devices at timeout: $(grep -E '^N: Name=' "$devices" 2>/dev/null | head -c 600 | tr '\n' ';')"
            mark controller_wait_ms "-1" 2>/dev/null
            return 0
        fi
        sleep 0.1
    done
}
# <<< controller_guard
wait_for_controller
LAUNCH_TRY_AT=$(date +%s)
run_game
if should_retry_launch $(( $(date +%s) - LAUNCH_TRY_AT )); then
    echo -e "${YELLOW}[!] The game stopped at once and the write layer was gone; remounting it and starting again${NC}"
    mark retry
    umount_fuse "$MERGED_DIR"
    if [ "$RUN_PATH" = "$MERGED_DIR" ] && fuse-overlayfs -o lowerdir="$GAME_DIR",upperdir="$WRITE_DIR",workdir="$WORK_DIR" "$MERGED_DIR" 2>&1; then
        [ -n "$WINESERVER_BIN" ] && [ -x "$WINESERVER_BIN" ] && { WINEPREFIX="$COMPAT_DIR/pfx" "$WINESERVER_BIN" -k 2>/dev/null || true; }
        LAUNCH_AT=$(date +%s); mark launched "$LAUNCH_AT"     # timings describe the launch that ran (last value wins)
        run_game
    fi
fi
kill "$STATS_PID" 2>/dev/null
# Some releases start through a small launcher .exe that starts the real game and exits, so the launcher returning does
# not mean the game has ended. Unmounting now (cleanup) would pull the files out from under the running game. Wait while
# any process other than our own helpers and Wine's background services still uses the mounted folders. (Quitting from
# the Steam menu ends this whole process tree, including this wait, so it can never keep a quit game alive.)
WAITED=0
while users="$(mount_users "$GAME_DIR" "$MERGED_DIR" | grep -vxE "${PREFETCH_PID:-x}|${WARM_PID:-x}|${RECORD_PID:-x}")"; [ -n "$users" ]; do
    [ $(( WAITED % 60 )) -eq 0 ] && echo "[*] The game is still running (pids: $(echo $users)); waiting for it to exit"
    sleep 5; WAITED=$(( WAITED + 5 ))
done
[ -n "$WARM_PID" ] && kill "$WARM_PID" 2>/dev/null
SESSION_DONE=1
[ -f "$SP_PY" ] && LD_PRELOAD= python3 "$SP_PY" session-done --marker "$SESSION_MARKER" >/dev/null 2>&1   # the daemon takes it from here
GAME_DURATION=$(( $(date +%s) - LAUNCH_AT ))
echo -e "${CYAN}[*] Game exited after ${GAME_DURATION}s.${NC}"
echo "[stats] cached from RemoteStorage: $(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1) (cap $CACHE_MAX)"

# Cap the Proton logs on EVERY exit (they reached ~1 GB when only short runs were rotated; CHECKLIST 59)
if [ -f "$CONF_DIR/proton_matcher.py" ]; then
    for candidate in "$LOG_DIR/steam-"*.log "$HOME/steam-"*.log; do
        [ -f "$candidate" ] && timeout 60 python3 "$CONF_DIR/proton_matcher.py" "$candidate" --rotate-only >/dev/null 2>&1
    done
fi

mark exit_seconds "$GAME_DURATION"
if [ "$GAME_DURATION" -lt 30 ] && [ -f "$CONF_DIR/proton_matcher.py" ]; then
    EXIT_KEY="none"
    for candidate in "$LOG_DIR/steam-"*.log "$HOME/steam-"*.log; do
        [ -f "$candidate" ] && { EXIT_KEY="$(timeout 20 python3 "$CONF_DIR/proton_matcher.py" "$candidate" --key 2>/dev/null | head -1)"; break; }
    done
    case "$EXIT_KEY" in ""|none) EXIT_KEY="unknown_early" ;; esac
    mark exit_reason "$EXIT_KEY"
fi

# Diagnose abnormal exits (< 30s run) with Proton crash signature matcher
if [ "$GAME_DURATION" -lt 30 ] && [ -f "$CONF_DIR/proton_matcher.py" ]; then
    for candidate in "$LOG_DIR/steam-"*.log "$HOME/steam-"*.log "$LOG_FILE"; do
        if [ -f "$candidate" ]; then
            python3 "$CONF_DIR/proton_matcher.py" "$candidate" --rotate 2>&1 | while read -r line; do
                echo -e "${YELLOW}[proton-diag] $line${NC}"
            done
            break
        fi
    done
fi
