#!/usr/bin/env bash
# ==============================================================================
# DeckStream - Cloud Installer
# Installs a game from your storage server's WebDAV straight to a local folder (e.g. MicroSD)
# WITHOUT downloading the installer/archive first:
#   - Inno Setup installers (for example GOG offline installers, setup_*.exe + .bin): run silently under
#     Proton, reading the .bin files directly from the WebDAV mount, writing the game to DEST_DIR
#   - .zip releases: extracted directly from the mount into DEST_DIR
#   - .rar/.7z releases: extracted with 7z if available
#   - GOG Linux installers (.sh): the zip inside is unpacked (data/noarch/), the .sh is NEVER run
#   - Pre-installed folders: copied to DEST_DIR
# Resume: zip, rar/7z and GOG .sh keep files already extracted at their full size; a tar stream and an
# installer restart from the beginning.
#
# Usage: cloud-install.sh GAME_SLUG "WEBDAV_FOLDER" "DEST_DIR"
#   INTERACTIVE=1 cloud-install.sh ...   -> shows the installer window
#                                           (run from Desktop Mode if silent fails)
# Credentials: ~/.config/deckstream/storage.conf (STORAGE_KEY, STORAGE_WEBDAV_URL, STORAGE_WEBDAV_USER)
# ==============================================================================

GAME_SLUG="${1:-}"
WEBDAV_FOLDER="${2:-}"
DEST_DIR="${3:-}"

CONF_DIR="$HOME/.config/deckstream"
CRED_FILE="$CONF_DIR/storage.conf"
export PATH="$HOME/.local/bin:$PATH"

log()  { echo "[$(date +%H:%M:%S)] $*"; }
fail() { echo "DECKSTREAM_ERROR: $*"; exit 1; }

[ -z "$GAME_SLUG" ] || [ -z "$WEBDAV_FOLDER" ] || [ -z "$DEST_DIR" ] && fail "Usage: $0 GAME_SLUG WEBDAV_FOLDER DEST_DIR"

# shellcheck disable=SC1090
[ -f "$CRED_FILE" ] && source "$CRED_FILE"
[ -z "${STORAGE_KEY:-}" ] && fail "No storage key on the Deck ($CRED_FILE)"
[ -z "${STORAGE_WEBDAV_URL:-}" ] && fail "No STORAGE_WEBDAV_URL on the Deck ($CRED_FILE)"

DEST_ROOT="$(dirname "$DEST_DIR")"
WORK="$DEST_ROOT/.deckstream"
SRC_MNT="$HOME/.local/share/deckstream/install-src/$GAME_SLUG"
PREFIX="$WORK/prefixes/_installer"
mkdir -p "$DEST_DIR" "$SRC_MNT" "$PREFIX" || fail "Cannot create folders on $DEST_ROOT"

RCLONE_PID=""
cleanup() {
    [ -n "${WINESERVER_BIN:-}" ] && WINEPREFIX="$PREFIX/pfx" "$WINESERVER_BIN" -k 2>/dev/null || true
    fusermount -u "$SRC_MNT" 2>/dev/null || true
    [ -n "$RCLONE_PID" ] && kill "$RCLONE_PID" 2>/dev/null || true
}
trap cleanup EXIT
trap 'fail "Cancelled"' INT TERM

# --- rclone ---------------------------------------------------------------
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
RCLONE_VERSION="1.75.1"
RCLONE_SHA256="982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab"   # from downloads.rclone.org/v1.75.1/SHA256SUMS

if ! command -v rclone &>/dev/null; then
    log "Installing rclone..."
    mkdir -p "$HOME/.local/bin"
    RC_TMP="$(mktemp -d)" || fail "Could not create temp dir"
    fetch_verified "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-amd64.zip" "$(tools_mirror)/rclone-v${RCLONE_VERSION}-linux-amd64.zip" "$RCLONE_SHA256" "$RC_TMP/rclone.zip" || { rm -rf "$RC_TMP"; fail "rclone download failed (tried rclone.org and the DeckStream mirror)"; }
    unzip -q -o "$RC_TMP/rclone.zip" -d "$RC_TMP/x" || { rm -rf "$RC_TMP"; fail "rclone unzip failed"; }
    mv "$RC_TMP"/x/rclone-*-linux-amd64/rclone "$HOME/.local/bin/rclone"
    chmod +x "$HOME/.local/bin/rclone"
    rm -rf "$RC_TMP"
fi

export RCLONE_CONFIG_DSSRC_TYPE="webdav"
export RCLONE_CONFIG_DSSRC_URL="$STORAGE_WEBDAV_URL"
export RCLONE_CONFIG_DSSRC_VENDOR="other"
export RCLONE_CONFIG_DSSRC_USER="${STORAGE_WEBDAV_USER:-deckstream}"
RCLONE_CONFIG_DSSRC_PASS="$(rclone obscure "$STORAGE_KEY")" || fail "Could not obscure the storage key"
export RCLONE_CONFIG_DSSRC_PASS
REMOTE="dssrc:$WEBDAV_FOLDER"

# --- Inspect the game folder -----------------------------------------------
log "Reading storage folder: $WEBDAV_FOLDER"
LISTING="$(rclone lsf -R --max-depth 3 "$REMOTE" 2>&1)" || fail "Cannot read the storage folder: $(echo "$LISTING" | tail -n 1)"
[ -z "$LISTING" ] && fail "The storage folder is empty"

KIND="preinstalled"
if echo "$LISTING" | grep -qiE '^setup[^/]*\.exe$' && ! echo "$LISTING" | grep -iE '\.exe$' | grep -viE '^setup[^/]*\.exe$' | grep -q .; then
    KIND="installer"
elif echo "$LISTING" | grep -qiE '\.zip$' && ! echo "$LISTING" | grep -qiE '\.z[0-9]{2}$'; then
    KIND="zip"
elif echo "$LISTING" | grep -qiE '\.(tar\.gz|tgz|tar\.xz|tar\.bz2|tar\.7z|tar)$'; then
    KIND="tar"
elif echo "$LISTING" | grep -qiE '\.(rar|7z)$'; then
    KIND="rar7z"
elif echo "$LISTING" | grep -qiE '\.z[0-9]{2}$'; then
    fail "Split .zip (.z01) releases aren't supported yet"
elif echo "$LISTING" | grep -qiE '^[^/]+\.sh$' && ! echo "$LISTING" | grep -qiE '\.exe$'; then
    KIND="linux-sh"
fi
log "Release type: $KIND"
echo "DECKSTREAM_KIND=$KIND"

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Pre-installed: just copy ----------------------------------------------
# Other-language files are left out when the English twin is there (startup_profile.py lang-skips: Cyberpunk
# lang_ru_voice.archive, FF7 Remake FR/DE movies, BG3 Language Pack folders); the list goes to .deckstream-skipped.json in
# the game folder, so the app can offer "Get skipped files" (DECKSTREAM_INSTALL_ALL=1: the copy fetches only what is
# missing). Unknown layouts, or no startup_profile.py, copy everything.
SKIP_MARK="$DEST_DIR/.deckstream-skipped.json"
if [ "$KIND" = "preinstalled" ]; then
    COPY_SKIP=()
    if [ "${DECKSTREAM_INSTALL_ALL:-0}" != "1" ] && [ -f "$SELF_DIR/startup_profile.py" ]; then
        FULL="$(rclone lsf -R --files-only --format sp --separator $'\t' "$REMOTE" 2>/dev/null)" || FULL=""
        SKIPS="$(printf '%s\n' "$FULL" | python3 "$SELF_DIR/startup_profile.py" lang-skips --folder "$WEBDAV_FOLDER" --json-out "$SKIP_MARK" 2>/dev/null)" || SKIPS=""
        if [ -n "$SKIPS" ]; then
            SKIP_LIST="$WORK/skip-$GAME_SLUG.txt"
            mkdir -p "$WORK" && printf '%s\n' "$SKIPS" | sed -e 's/[][\\*?{}]/\\&/g' -e 's#^#/#' > "$SKIP_LIST" && COPY_SKIP=(--exclude-from "$SKIP_LIST")
            log "Skipping $(printf '%s\n' "$SKIPS" | wc -l | tr -d ' ') other-language files (English is kept)"
        fi
    fi
    log "Copying pre-installed game to $DEST_DIR ..."
    rclone copy "$REMOTE" "$DEST_DIR" "${COPY_SKIP[@]}" --transfers 8 --multi-thread-streams 8 --stats 30s --stats-one-line -v 2>&1 \
        | grep --line-buffered -E "Transferred|ERROR"
    [ "${PIPESTATUS[0]}" -ne 0 ] && fail "Copy failed"
    [ "${#COPY_SKIP[@]}" -eq 0 ] && rm -f "$SKIP_MARK"
    echo "DECKSTREAM_DONE"
    exit 0
fi

# --- Mount the release read-only (nothing is downloaded up front) -----------
# Per-file parallel chunk reads (rclone >= 1.67). 7z and an installer read the volumes one at a
# time, so one file is all that is in flight: its chunk streams ARE the connection count. Measured against
# a remote server (parallel_download.py): 1 stream ~10 MB/s, 8 streams ~22 MB/s (the line limit). Override with
# DECKSTREAM_INSTALL_STREAMS (1-16); 8 is the default.
CHUNK_STREAMS=""
INSTALL_STREAMS="${DECKSTREAM_INSTALL_STREAMS:-8}"
case "$INSTALL_STREAMS" in ''|*[!0-9]*) INSTALL_STREAMS=8;; esac
[ "$INSTALL_STREAMS" -lt 1 ] && INSTALL_STREAMS=1
[ "$INSTALL_STREAMS" -gt 16 ] && INSTALL_STREAMS=16
if rclone mount --help 2>/dev/null | grep -q -- "--vfs-read-chunk-streams"; then
    CHUNK_STREAMS="--vfs-read-chunk-streams $INSTALL_STREAMS"
fi
log "Mount: $INSTALL_STREAMS chunk streams per file"

SKIP_ARGS=()
fusermount -u "$SRC_MNT" 2>/dev/null || true
log "Mounting the game folder from your storage server..."
rclone mount "$REMOTE" "$SRC_MNT" \
    --read-only \
    --vfs-cache-mode off \
    --buffer-size 64M \
    --vfs-read-chunk-size 32M \
    $CHUNK_STREAMS "${SKIP_ARGS[@]}" \
    --dir-cache-time 24h --attr-timeout 24h \
    --no-modtime --log-level NOTICE &
RCLONE_PID=$!
for _ in $(seq 1 40); do
    mountpoint -q "$SRC_MNT" && ls "$SRC_MNT" &>/dev/null && break
    kill -0 "$RCLONE_PID" 2>/dev/null || fail "rclone mount exited"
    sleep 0.5
done
mountpoint -q "$SRC_MNT" || fail "Mount did not come up"

# --- Zip: extract straight from the mount ------------------------------------
if [ "$KIND" = "zip" ]; then
    UNZIP="$SELF_DIR/parallel_unzip.py"
    [ -f "$UNZIP" ] || fail "parallel_unzip.py missing next to cloud-install.sh. Re-run the DeckStream installer."
    find "$SRC_MNT" -maxdepth 3 -iname "*.zip" | sort | while read -r Z; do
        log "Extracting $(basename "$Z") directly from your storage server (4 files at a time) ..."
        python3 "$UNZIP" "$Z" "$DEST_DIR" 4 || exit 1
    done || fail "Zip extraction failed"
    echo "DECKSTREAM_DONE"
    exit 0
fi

# --- GOG Linux installer: a shell script with a zip appended. Unzip the game out of it; never run the script ---
if [ "$KIND" = "linux-sh" ]; then
    UNZIP="$SELF_DIR/parallel_unzip.py"
    [ -f "$UNZIP" ] || fail "parallel_unzip.py missing next to cloud-install.sh. Re-run the DeckStream installer."
    SH="$(find "$SRC_MNT" -maxdepth 1 -iname "*.sh" | sort | head -n 1)"
    [ -z "$SH" ] && fail "No .sh installer found in mount"
    if ! python3 -c 'import sys, zipfile; sys.exit(0 if zipfile.is_zipfile(sys.argv[1]) else 1)' "$SH"; then
        # A plain script in an already-installed native folder, not an installer: copy the folder as it is.
        log "$(basename "$SH") is a plain script, not an installer: copying the folder to $DEST_DIR ..."
        rclone copy "$REMOTE" "$DEST_DIR" --transfers 8 --multi-thread-streams 8 --stats 30s --stats-one-line -v 2>&1 \
            | grep --line-buffered -E "Transferred|ERROR"
        [ "${PIPESTATUS[0]}" -ne 0 ] && fail "Copy failed"
        echo "DECKSTREAM_DONE"
        exit 0
    fi
    log "Unpacking the Linux game from $(basename "$SH") directly from your storage server (no Proton needed) ..."
    python3 "$UNZIP" "$SH" "$DEST_DIR" 4 --strip "data/noarch/" || fail "Unpacking the Linux installer failed"
    find "$DEST_DIR" -type l -delete
    echo "DECKSTREAM_DONE"
    exit 0
fi

# --- Tar / Streaming Archives: stream straight from mount via pipe ------------
if [ "$KIND" = "tar" ]; then
    SEVENZ="$(command -v 7z || command -v 7za || command -v 7zz || true)"
    [ -z "$SEVENZ" ] && fail "This release is a tar archive and 7-Zip isn't installed on the Deck"
    FIRST="$(find "$SRC_MNT" -maxdepth 3 \( -iname "*.tar.gz" -o -iname "*.tgz" -o -iname "*.tar.xz" -o -iname "*.tar.bz2" -o -iname "*.tar.7z" -o -iname "*.tar" \) | sort | head -n 1)"
    [ -z "$FIRST" ] && fail "No tar archive found in mount"
    SEVENZ_THREADS="${DECKSTREAM_7Z_THREADS:--mmt=4}"
    [[ "$SEVENZ_THREADS" != -mmt* ]] && SEVENZ_THREADS="-mmt=$SEVENZ_THREADS"
    log "Streaming extraction of $(basename "$FIRST") directly from your storage server ($SEVENZ_THREADS); a tar stream restarts from the beginning if it is cut off ..."
    "$SEVENZ" x "$FIRST" -so -y -bsp0 -bso0 "$SEVENZ_THREADS" | tar -xf - -C "$DEST_DIR"
    RC_7Z="${PIPESTATUS[0]}"
    RC_TAR="${PIPESTATUS[1]}"
    [ "$RC_7Z" -ne 0 ] && fail "7z streaming decompression failed (code $RC_7Z)"
    [ "$RC_TAR" -ne 0 ] && fail "tar extraction failed (code $RC_TAR)"
    find "$DEST_DIR" -type l -delete
    echo "DECKSTREAM_DONE"
    exit 0
fi

# --- RAR / 7z ------------------------------------------------------------------
if [ "$KIND" = "rar7z" ]; then
    SEVENZ="$(command -v 7z || command -v 7za || command -v 7zz || true)"
    [ -z "$SEVENZ" ] && fail "This release is .rar/.7z and 7-Zip isn't installed on the Deck"
    FIRST="$(find "$SRC_MNT" -maxdepth 3 \( -iname "*.part1.rar" -o -iname "*.part01.rar" -o -iname "*.7z.001" -o -iname "*.7z" -o -iname "*.rar" \) | sort | head -n 1)"
    [ -z "$FIRST" ] && fail "No archive found in mount"
    SEVENZ_THREADS="${DECKSTREAM_7Z_THREADS:--mmt=4}"
    [[ "$SEVENZ_THREADS" != -mmt* ]] && SEVENZ_THREADS="-mmt=$SEVENZ_THREADS"
    # Resume: a file left half-written by an earlier run (its size differs from the listing) is deleted, then -aos
    # skips every file that is already there, so only the missing ones are extracted again.
    REDO=0
    while IFS=$'\t' read -r SIZE P; do
        case "$P" in ''|/*|../*|*/../*|*/..) continue ;; esac
        F="$DEST_DIR/$P"
        if [ -f "$F" ] && [ ! -L "$F" ] && [ "$(wc -c < "$F" 2>/dev/null | tr -d ' ')" != "$SIZE" ]; then
            rm -f -- "$F" && REDO=$((REDO + 1))
        fi
    done < <("$SEVENZ" l -slt "$FIRST" 2>/dev/null | awk -F' = ' '/^----------/{body=1; next} body && /^Path = /{p=$2} body && /^Size = /{print $2 "\t" p}')
    [ "$REDO" -gt 0 ] && log "Resuming: $REDO partly written files will be extracted again"
    log "Extracting $(basename "$FIRST") directly from your storage server ($SEVENZ_THREADS) ..."
    "$SEVENZ" x "$FIRST" -o"$DEST_DIR" -aos -bsp0 -bso0 "$SEVENZ_THREADS" || fail "7z extraction failed"
    # An archive could contain symlinks pointing outside the game folder: drop them.
    find "$DEST_DIR" -type l -delete
    echo "DECKSTREAM_DONE"
    exit 0
fi

# --- Installer: run setup*.exe silently under Proton (Inno Setup switches) --
PROTON_BIN=""
for p in "$HOME"/.steam/root/compatibilitytools.d/*/proton \
         "$HOME/.local/share/Steam/steamapps/common/Proton - Experimental/proton" \
         "$HOME"/.local/share/Steam/steamapps/common/Proton*/proton; do
    [ -f "$p" ] && { PROTON_BIN="$p"; break; }
done
[ -z "$PROTON_BIN" ] && fail "No Proton found. Install Proton Experimental from Steam (Library > Tools)."
WINESERVER_BIN="$(dirname "$PROTON_BIN")/files/bin/wineserver"
[ -f "$WINESERVER_BIN" ] || WINESERVER_BIN="$(dirname "$PROTON_BIN")/dist/bin/wineserver"

export STEAM_COMPAT_DATA_PATH="$PREFIX"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
export DISPLAY="${DISPLAY:-:0}"
export PROTON_LOG=0

WIN_DEST="Z:$(echo "$DEST_DIR" | sed 's#/#\\#g')"
SETUP="$(find "$SRC_MNT" -maxdepth 1 -iname 'setup*.exe' | sort | head -n 1)"
[ -n "$SETUP" ] || fail "No setup*.exe at the top of the game folder"

if [ "${INTERACTIVE:-0}" = "1" ]; then
    log "Launching installer window. Set the install folder to: $WIN_DEST"
    "$PROTON_BIN" waitforexitandrun "$SETUP" "/DIR=$WIN_DEST"
else
    log "Running the installer silently with $(basename "$(dirname "$PROTON_BIN")") -> $DEST_DIR"
    log "This is CPU-heavy and can take 1-3+ hours for big games. If it is interrupted it restarts from the beginning."
    "$PROTON_BIN" waitforexitandrun "$SETUP" \
        /SP- /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /NOCANCEL /LANG=english "/DIR=$WIN_DEST"
fi
RC=$?

# Installer must have produced a game .exe
if ! find "$DEST_DIR" -maxdepth 6 -iname "*.exe" ! -iname "unins*" | grep -q .; then
    fail "Installer finished (code $RC) but no game files were written. Try INTERACTIVE=1 from Desktop Mode."
fi
echo "DECKSTREAM_DONE"
exit 0
