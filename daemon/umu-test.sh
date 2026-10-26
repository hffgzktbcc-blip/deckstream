#!/usr/bin/env bash
# ==============================================================================
# DeckStream - umu experiment
# Runs ONE already-installed Windows game through umu-run (Proton outside Steam), the way the Hydra
# launcher does, so we can compare start time and stability with the Steam-shortcut launcher.
#
# Safe by design: it uses its OWN throwaway prefix (your game's real prefix and saves are not
# touched), pins umu to a known version + checksum, and needs no Steam, no sudo.
#
# Usage: umu-test.sh "/path/to/Game.exe" [GAMEID]
#   GAMEID is optional (e.g. umu-1234 for a known Steam app id). Without it umu uses its defaults.
#   Env overrides: UMU_TEST_PREFIX=/dir  PROTONPATH=/path/to/GE-Proton...  UMU_TEST_PYTHON=python3.12
# ==============================================================================
set -u

UMU_VERSION="1.4.4"
UMU_URL="https://github.com/Open-Wine-Components/umu-launcher/releases/download/${UMU_VERSION}/umu-launcher-${UMU_VERSION}-zipapp.tar"
UMU_SHA256="eb590691841f7fad3fc3ad8fd5db4ccb87849fe7948e62b28ece7a4ee48cc851"

EXE="${1:-}"
GAMEID_ARG="${2:-}"
WORK="$HOME/.local/share/deckstream/umu-test"
LOG_DIR="$HOME/.local/share/deckstream/logs"
mkdir -p "$WORK" "$LOG_DIR"
LOG="$LOG_DIR/umu_test.log"

say()  { echo "[umu-test] $*"; }
fail() { echo "[umu-test] ERROR: $*" >&2; exit 1; }

[ -n "$EXE" ] && [ -f "$EXE" ] || fail "Usage: $0 \"/path/to/Game.exe\" [GAMEID]"
PY="${UMU_TEST_PYTHON:-python3}"
command -v "$PY" >/dev/null || fail "python3 not found"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || fail "umu needs Python 3.10 or newer"

# umu downloads Valve's Steam Linux Runtime into ~/.local/share/umu on first run. The Deck's internal
# drive is usually nearly full, so refuse to start without headroom.
FREE_KB="$(df -Pk "$HOME" | awk 'NR==2 {print $4}')"
if [ "${FREE_KB:-0}" -lt "${UMU_TEST_MIN_FREE_KB:-3145728}" ]; then
    fail "Only $(( ${FREE_KB:-0} / 1024 )) MB free on the internal drive. umu needs about 3 GB for its runtime. Free some space first."
fi

# >>> fetch_verified (identical in stream-runner.sh, cloud-install.sh, umu-test.sh and install.sh)
# Download a tool from upstream; if that fails (DNS trouble, blocked host), fall back to the DeckStream
# mirror. The SHA-256 is pinned by the calling script, so the mirror is only a transport: a file that
# does not match is discarded, whichever server it came from.
tools_mirror() {
    local base="${DECKSTREAM_MIRROR:-}"
    if [ -z "$base" ] && [ -f "$HOME/.config/deckstream/daemon_config.json" ]; then
        base="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("server_url",""))' "$HOME/.config/deckstream/daemon_config.json" 2>/dev/null)"
    fi
    if [ -n "$base" ]; then echo "$base/daemon/tools"; fi
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

# ---- 1. get umu (pinned, checksum-verified) ----------------------------------
UMU_RUN="$WORK/umu/umu-run"
if [ ! -f "$UMU_RUN" ]; then
    TAR="${UMU_TEST_LOCAL_TAR:-$WORK/umu-${UMU_VERSION}.tar}"
    if [ ! -f "$TAR" ]; then
        say "Downloading umu ${UMU_VERSION} (about 0.4 MB) from the official GitHub release ..."
        fetch_verified "$UMU_URL" "$(tools_mirror)/umu-launcher-${UMU_VERSION}-zipapp.tar" "$UMU_SHA256" "$TAR" || fail "Download failed (tried GitHub and the DeckStream mirror)"
    fi
    GOT="$( (sha256sum "$TAR" 2>/dev/null || shasum -a 256 "$TAR") | awk '{print $1}')"
    if [ "$GOT" != "$UMU_SHA256" ] && [ "${UMU_TEST_SKIP_SHA:-0}" != "1" ]; then
        fail "Checksum mismatch for the umu download (got $GOT). Not running it."
    fi
    tar -xf "$TAR" -C "$WORK" || fail "Could not unpack umu"
    chmod +x "$UMU_RUN" 2>/dev/null || true
fi
[ -f "$UMU_RUN" ] || fail "umu-run not found after unpacking"

# ---- 2. choose a Proton (latest GE-Proton that has both proton and toolmanifest.vdf) -----
if [ -z "${PROTONPATH:-}" ]; then
    BEST=""
    for root in "$HOME/.steam/root/compatibilitytools.d" "$HOME/.local/share/Steam/compatibilitytools.d"; do
        [ -d "$root" ] || continue
        for d in "$root"/GE-Proton*; do
            [ -f "$d/proton" ] && [ -f "$d/toolmanifest.vdf" ] && BEST="$(printf '%s\n%s\n' "$BEST" "$d" | sed '/^$/d' | sort -V | tail -n 1)"
        done
    done
    if [ -n "$BEST" ]; then
        PROTONPATH="$BEST"
    else
        PROTONPATH="GE-Proton"   # umu downloads the latest GE-Proton itself
        say "No installed GE-Proton found; umu will download the latest one."
    fi
fi
export PROTONPATH

# ---- 3. a throwaway prefix, on the SD card when the game is there --------------
SLUG="$(basename "$(dirname "$EXE")" | tr -c 'A-Za-z0-9\n' '_' | tr 'A-Z' 'a-z')"
if [ -z "${UMU_TEST_PREFIX:-}" ]; then
    case "$EXE" in
        /run/media/*/*)
            CARD="$(echo "$EXE" | cut -d/ -f1-5)"
            UMU_TEST_PREFIX="$CARD/.deckstream/umu-test-prefixes/$SLUG" ;;
        *) UMU_TEST_PREFIX="$WORK/prefixes/$SLUG" ;;
    esac
fi
mkdir -p "$UMU_TEST_PREFIX" || fail "Cannot create prefix at $UMU_TEST_PREFIX"
export WINEPREFIX="$UMU_TEST_PREFIX"
[ -n "$GAMEID_ARG" ] && export GAMEID="$GAMEID_ARG"
export PROTON_LOG=1
export PROTON_LOG_DIR="$LOG_DIR"

say "umu:      $UMU_RUN  (v$UMU_VERSION)"
say "proton:   $PROTONPATH"
say "prefix:   $WINEPREFIX   (throwaway; delete this folder to reset)"
say "game:     $EXE"
say "log:      $LOG"
say "Start your stopwatch now. Note when the game window appears and when you reach the main menu."
echo

cd "$(dirname "$EXE")" || true
START="$(date +%s)"
"$PY" "$UMU_RUN" "$EXE" 2>&1 | tee -a "$LOG"
RC="${PIPESTATUS[0]}"
say "umu-run exited with code $RC after $(( $(date +%s) - START ))s"
