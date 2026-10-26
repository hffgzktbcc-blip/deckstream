#!/usr/bin/env bash
# ==============================================================================
# DeckStream Steam Deck Installer
# Sets up the DeckStream background daemon as a systemd user service on SteamOS.
# Usage:
#   curl -sSL https://YOUR_SITE/daemon/install.sh | bash -s -- [SERVER_URL] [DEVICE_ID] [STORAGE_KEY]
#   (STORAGE_WEBDAV_URL / STORAGE_WEBDAV_USER may be set in the environment instead of answering the questions)
# Or run locally:
#   bash install.sh
# ==============================================================================

set -e

GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}"
echo "=========================================================="
echo "      🎮 DeckStream - Steam Deck Companion Installer       "
echo "=========================================================="
echo -e "${NC}"

SERVER_URL="${1:-}"
DEVICE_ID="${2:-}"

# Questions are asked on the terminal (/dev/tty): with `curl ... | bash`, stdin IS this script, and a plain `read`
# would swallow the script's own next lines as the answer.
if [ -z "$SERVER_URL" ] && [ -r /dev/tty ]; then
    read -r -p "Enter your DeckStream site address (e.g. https://your-site.vercel.app): " SERVER_URL < /dev/tty || true
fi
[ -n "$SERVER_URL" ] || { echo -e "${RED}[!] No DeckStream web address given. Run: curl -sSL <site>/daemon/install.sh | bash -s -- <site> deck-main${NC}"; exit 1; }

# Strip trailing slash
SERVER_URL="${SERVER_URL%/}"

if [ -z "$DEVICE_ID" ] && [ -r /dev/tty ]; then
    read -r -p "Enter a Device ID for this Steam Deck [default: deck-main]: " DEVICE_ID < /dev/tty || true
fi
DEVICE_ID="${DEVICE_ID:-deck-main}"

echo ""
echo -e "${GREEN}[*] Configuring DeckStream with:${NC}"
echo -e "    Server:    ${CYAN}${SERVER_URL}${NC}"
echo -e "    Device ID: ${CYAN}${DEVICE_ID}${NC}"
echo ""

# Paths
INSTALL_DIR="$HOME/.config/deckstream"
SERVICE_DIR="$HOME/.config/systemd/user"
GAMES_DIR="$HOME/Games"

mkdir -p "$INSTALL_DIR"
mkdir -p "$SERVICE_DIR"
mkdir -p "$GAMES_DIR"

# Queue password (same value as DECKSTREAM_SECRET in Vercel). Prompted, not passed
# as an argument, so it stays out of shell history.
CONFIG_FILE="$INSTALL_DIR/daemon_config.json"
QUEUE_SECRET="${DECKSTREAM_SECRET:-}"
if [ -z "$QUEUE_SECRET" ] && [ -f "$CONFIG_FILE" ]; then
    QUEUE_SECRET=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("queue_secret",""))' "$CONFIG_FILE" 2>/dev/null || true)
fi
if [ -z "$QUEUE_SECRET" ] && [ -r /dev/tty ]; then
    read -r -s -p "Queue password (DECKSTREAM_SECRET from Vercel): " QUEUE_SECRET < /dev/tty || true
    echo
fi
if [ -z "$QUEUE_SECRET" ]; then
    echo -e "${YELLOW}[!] No queue password set - the server will reject this Deck until you add queue_secret to ${CONFIG_FILE}${NC}"
fi

# Write daemon config (via python so quotes in values can't break the JSON)
umask 077
SERVER_URL="$SERVER_URL" DEVICE_ID="$DEVICE_ID" QUEUE_SECRET="$QUEUE_SECRET" python3 - "$CONFIG_FILE" << 'PYEOF'
import json, os, sys
# Keep every other setting already there (a self-update runs this on every release); set only these three.
cfg = {"poll_interval_sec": 10, "default_storage": "internal", "proton_compatibility": True}
try:
    old = json.load(open(sys.argv[1]))
    if isinstance(old, dict):
        cfg.update(old)
except (OSError, ValueError):
    pass
cfg.update(server_url=os.environ["SERVER_URL"], device_id=os.environ["DEVICE_ID"], queue_secret=os.environ["QUEUE_SECRET"])
json.dump(cfg, open(sys.argv[1], "w"), indent=2)
PYEOF
chmod 600 "$CONFIG_FILE"
umask 022
echo -e "${GREEN}[✓] Saved daemon configuration to ${CONFIG_FILE} (private)${NC}"

# Storage server credentials for streaming (kept only on this Deck, never sent to the queue). The Deck reads your games
# over WebDAV (for example `rclone serve webdav` on your NAS, see docs/STORAGE_API.md); the key is the WebDAV password.
CRED_FILE="$INSTALL_DIR/storage.conf"
STORAGE_KEY_ARG="${3:-${STORAGE_KEY:-}}"
WEBDAV_URL_ARG="${STORAGE_WEBDAV_URL:-}"
WEBDAV_USER_ARG="${STORAGE_WEBDAV_USER:-deckstream}"
if [ ! -f "$CRED_FILE" ] && [ -r /dev/tty ]; then
    [ -n "$WEBDAV_URL_ARG" ] || read -r -p "WebDAV address of your games (e.g. http://nas.lan:8781, Enter to skip): " WEBDAV_URL_ARG < /dev/tty || true
    if [ -n "$WEBDAV_URL_ARG" ] && [ -z "$STORAGE_KEY_ARG" ]; then
        read -r -s -p "Storage key (the WebDAV password): " STORAGE_KEY_ARG < /dev/tty || true
        echo ""
    fi
fi
if [ -n "$STORAGE_KEY_ARG" ] && [ -n "$WEBDAV_URL_ARG" ]; then
    umask 077
    {
        printf 'STORAGE_KEY=%q\n' "$STORAGE_KEY_ARG"
        printf 'STORAGE_WEBDAV_URL=%q\n' "${WEBDAV_URL_ARG%/}"
        printf 'STORAGE_WEBDAV_USER=%q\n' "$WEBDAV_USER_ARG"
    } > "$CRED_FILE"
    chmod 600 "$CRED_FILE"
    umask 022
    echo -e "${GREEN}[✓] Saved storage credentials to ${CRED_FILE} (private)${NC}"
elif [ -f "$CRED_FILE" ]; then
    echo -e "${GREEN}[✓] Keeping existing storage credentials${NC}"
else
    echo -e "${YELLOW}[!] No storage server set - streaming will not work until ${CRED_FILE} has STORAGE_KEY, STORAGE_WEBDAV_URL and STORAGE_WEBDAV_USER.${NC}"
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

# ---- Files: stage, verify, syntax-check, back up, then swap ------------------------------------
# Nothing in the current install is touched until EVERY file has downloaded, matched its checksum
# (from manifest.json) and passed a syntax check. A failed update leaves the old version working.
DAEMON_SCRIPT="$INSTALL_DIR/deckstream-daemon.py"
STATE_DIR="${DECKSTREAM_STATE_DIR:-$HOME/.local/share/deckstream}"
mkdir -p "$STATE_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Running from a clone of the repo? Use those files; otherwise fetch from the server.
LOCAL_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    _d="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    [ -f "$_d/deckstream-daemon.py" ] && LOCAL_DIR="$_d"
fi

fetch_to() {   # fetch_to NAME DEST
    if [ -n "$LOCAL_DIR" ]; then cp "$LOCAL_DIR/$1" "$2" 2>/dev/null
    else curl -fsSL --retry 2 --retry-delay 2 -m 180 -o "$2" "${SERVER_URL}/daemon/$1" 2>/dev/null
    fi
}

job_running() {
    [ -f "$STATE_DIR/current_job.json" ] || return 1
    local pid
    pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid",""))' "$STATE_DIR/current_job.json" 2>/dev/null || true)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

game_running() {
    # The runner's own lock (one folder per game, with its pid inside) in the runtime dir; a dead pid is a stale lock and does NOT count.
    local ld="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/deckstream_locks" f pid
    for f in "$ld"/runner_*.lock/pid; do
        [ -f "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 0; fi
    done
    if pgrep -f "stream-runner.sh" >/dev/null 2>&1; then
        return 0
    fi
    if pgrep -f "SteamLaunch.*AppId" >/dev/null 2>&1; then
        return 0
    fi
    if pgrep -f "rclone.*mount.*deckstream" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}
# Self-update: record the outcome; if the new daemon does not stay up, put the previous version back and never retry
# this version (only a newer one).
record_update() {   # record_update RESULT
    python3 - "$STATE_DIR/self_update.json" "$OLD_VERSION" "$NEW_VERSION" "$1" <<'PYEOF' || true
import json, sys, time
path, old, new, result = sys.argv[1:5]
try:
    state = json.load(open(path))
except Exception:
    state = {}
state["last_update"] = {"from": old, "to": new, "result": result, "time": int(time.time())}
if result == "rolled-back" and new not in state.setdefault("bad_versions", []):
    state["bad_versions"].append(new)
json.dump(state, open(path, "w"))
PYEOF
}
MANIFEST="$STAGE/manifest.json"
AUTO="${DECKSTREAM_AUTO_UPDATE:-0}"      # 1 = started by the daemon's self-update (no questions, health check, rollback)
SWAPPED=0
# A self-update that stops before the swap (download, checksum or syntax failure, a job started) leaves a record, so the
# phone shows it and the daemon backs off instead of retrying every 30 minutes.
trap 'rc=$?; rm -rf "$STAGE"; if [ "$AUTO" = 1 ] && [ $rc -ne 0 ] && [ "$SWAPPED" != 1 ]; then record_update "failed before install (exit $rc)"; fi' EXIT
if [ -n "${DECKSTREAM_MANIFEST_FILE:-}" ]; then
    # A manifest the daemon already downloaded and verified against the pinned update key: use exactly that file,
    # so nothing fetched after the signature check can change what gets installed.
    cp "$DECKSTREAM_MANIFEST_FILE" "$MANIFEST" && python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$MANIFEST" \
        || { echo -e "${RED}[!] The verified manifest could not be read. Nothing was changed.${NC}"; exit 1; }
elif ! fetch_to manifest.json "$MANIFEST" || ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$MANIFEST" 2>/dev/null; then
    rm -f "$MANIFEST"
    [ "$AUTO" = 1 ] && { echo -e "${RED}[!] Self-update needs a manifest. Nothing was changed.${NC}"; exit 1; }
    echo -e "${YELLOW}[!] No checksum manifest available; installing without checksum verification.${NC}"
fi
OLD_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version",""))' "$INSTALL_DIR/manifest.json" 2>/dev/null || true)"
manifest_get() {   # manifest_get NAME FIELD
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["files"].get(sys.argv[2],{}).get(sys.argv[3],""))' "$MANIFEST" "$1" "$2" 2>/dev/null
}

if [ -f "$MANIFEST" ]; then
    FILES="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1]))["files"]))' "$MANIFEST")"
    ALL_REQUIRED=1
    NEW_VERSION="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version",""))' "$MANIFEST")"
else
    FILES="deckstream-daemon.py parallel_download.py stream-runner.sh cloud-install.sh game-launcher.sh umu-test.sh saves.py parallel_unzip.py doctor.py rollback.sh"
    ALL_REQUIRED=0
    NEW_VERSION="unverified"
fi

stage_file() {   # stage_file NAME -> $STAGE/NAME, checksum-verified when the manifest knows it
    local name="$1" want got try
    for try in 1 2 3; do
        fetch_to "$name" "$STAGE/$name" && [ -s "$STAGE/$name" ] || { sleep 1; continue; }
        if [ -f "$MANIFEST" ]; then
            want="$(manifest_get "$name" sha256)"; got="$(sha256_of "$STAGE/$name")"
            if [ -n "$want" ] && [ "$got" != "$want" ]; then echo -e "${YELLOW}    $name did not match its checksum (try $try)${NC}"; sleep 1; continue; fi
        fi
        return 0
    done
    rm -f "$STAGE/$name"
    return 1
}

echo -e "${YELLOW}[*] Fetching and verifying DeckStream files (version ${NEW_VERSION}) ...${NC}"
STAGED=""
for name in $FILES; do
    if stage_file "$name"; then
        STAGED="$STAGED $name"
    elif [ "$ALL_REQUIRED" = 1 ] || [ "$name" = "deckstream-daemon.py" ]; then
        echo -e "${RED}[!] Could not get a verified copy of ${name}. Nothing was changed; the current install still works.${NC}"
        exit 1
    else
        echo -e "${YELLOW}[!] Skipping ${name} (could not download it).${NC}"
    fi
done

for name in $STAGED; do
    case "$name" in
        *.py) python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$STAGE/$name" 2>/dev/null ;;
        *.sh) bash -n "$STAGE/$name" 2>/dev/null ;;
    esac || { echo -e "${RED}[!] ${name} failed its syntax check. Nothing was changed; the current install still works.${NC}"; exit 1; }
done

if [ "$AUTO" = 1 ] && { job_running || game_running; }; then
    echo "Self-update: a job started meanwhile; nothing was changed, trying again later"
    trap 'rm -rf "$STAGE"' EXIT
    exit 0
fi

# Back up what is installed now (never the settings or keys), keep the newest three.
BACKUP_ROOT="$INSTALL_DIR/backup"
BACKUP_DIR="$BACKUP_ROOT/${DECKSTREAM_STAMP:-$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$BACKUP_DIR"
HAVE_BACKUP=0
for name in $FILES manifest.json; do
    if [ -f "$INSTALL_DIR/$name" ]; then cp -p "$INSTALL_DIR/$name" "$BACKUP_DIR/$name" && HAVE_BACKUP=1; fi
done
[ "$HAVE_BACKUP" = 1 ] || rmdir "$BACKUP_DIR" 2>/dev/null || true
_n="$(ls -1 "$BACKUP_ROOT" 2>/dev/null | wc -l | tr -d ' ')"
if [ "${_n:-0}" -gt 3 ]; then
    ls -1 "$BACKUP_ROOT" | sort | head -n $((_n - 3)) | while read -r old; do rm -rf "${BACKUP_ROOT:?}/$old"; done
fi

# Swap in, one file at a time (each replacement is atomic).
SWAPPED=1
for name in $STAGED; do
    mode="$(manifest_get "$name" mode || true)"; mode="${mode:-755}"
    install -m "$mode" "$STAGE/$name" "$INSTALL_DIR/.$name.new" && mv -f "$INSTALL_DIR/.$name.new" "$INSTALL_DIR/$name"
done
[ -f "$MANIFEST" ] && install -m 644 "$MANIFEST" "$INSTALL_DIR/manifest.json"

# Self-update trust: the public key that must sign every future manifest. Pinned ONCE, at the first install that has
# it, and never replaced by an update (otherwise whoever could change the site could also change the key).
SIGNERS="$INSTALL_DIR/update_signers"
if [ ! -f "$SIGNERS" ] && fetch_to update_signers "$STAGE/update_signers" \
   && grep -q '^deckstream namespaces="deckstream-manifest" ssh-ed25519 ' "$STAGE/update_signers"; then
    install -m 644 "$STAGE/update_signers" "$SIGNERS"
    echo -e "${GREEN}[✓] Self-update enabled: future versions install themselves when the Deck is idle (signed updates only)${NC}"
fi
echo -e "${GREEN}[✓] Installed $(echo $STAGED | wc -w | tr -d ' ') files (version ${NEW_VERSION}); previous version kept in ${BACKUP_ROOT}${NC}"

# Ludusavi: finds and backs up game saves (see saves.py). Pinned version + checksum.
LUDUSAVI_VERSION="v0.31.0"
LUDUSAVI_SHA256="7322ff45d41eae7ae064a80d8c9ecccc5b8fb6fc090a603a66369cd4b054068d"
if [ "${DECKSTREAM_SKIP_LUDUSAVI:-0}" != 1 ] && [ ! -x "$HOME/.local/bin/ludusavi" ]; then
    mkdir -p "$HOME/.local/bin"
    LUD_TMP="$(mktemp -d)"
    if fetch_verified "https://github.com/mtkennerly/ludusavi/releases/download/${LUDUSAVI_VERSION}/ludusavi-${LUDUSAVI_VERSION}-linux.tar.gz" \
                      "$(DECKSTREAM_MIRROR="$SERVER_URL" tools_mirror)/ludusavi-${LUDUSAVI_VERSION}-linux.tar.gz" \
                      "$LUDUSAVI_SHA256" "$LUD_TMP/ludusavi.tar.gz" \
       && tar -xzf "$LUD_TMP/ludusavi.tar.gz" -C "$LUD_TMP" ludusavi \
       && mv "$LUD_TMP/ludusavi" "$HOME/.local/bin/ludusavi"; then
        chmod +x "$HOME/.local/bin/ludusavi"
        echo -e "${GREEN}[✓] Installed Ludusavi ${LUDUSAVI_VERSION} (game save backups)${NC}"
    else
        echo -e "${YELLOW}[!] Could not install Ludusavi now; save backups stay off until the installer is re-run.${NC}"
    fi
    rm -rf "$LUD_TMP"
fi

# Setup systemd user service
SERVICE_FILE="$SERVICE_DIR/deckstream.service"
cat > "$SERVICE_FILE" << EOF
[Unit]
Description=DeckStream SteamOS Companion Daemon
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $DAEMON_SCRIPT
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

echo -e "${GREEN}[✓] Created systemd user service: ${SERVICE_FILE}${NC}"

# Reload and enable systemd user service
systemctl --user daemon-reload
systemctl --user enable deckstream.service

if job_running || game_running; then
    touch "$STATE_DIR/restart-pending"
    echo -e "${YELLOW}[*] A game or job is running, so the new version will start when it finishes (the daemon restarts itself).${NC}"
    echo -e "${YELLOW}    If that never happens (the running daemon is an older version), restart it later with: systemctl --user restart deckstream${NC}"
else
    rm -f "$STATE_DIR/restart-pending"
    systemctl --user restart deckstream.service
fi

daemon_healthy() {   # active, and not restarted by systemd during the watch window (a crashing daemon flaps)
    local r0 r1
    sleep "${DECKSTREAM_HEALTH_WAIT:-15}"
    systemctl --user is-active --quiet deckstream.service || return 1
    r0="$(systemctl --user show -p NRestarts --value deckstream.service 2>/dev/null)"
    sleep "${DECKSTREAM_HEALTH_WINDOW:-60}"
    systemctl --user is-active --quiet deckstream.service || return 1
    r1="$(systemctl --user show -p NRestarts --value deckstream.service 2>/dev/null)"
    [ "$r0" = "$r1" ]
}
if [ "$AUTO" = 1 ]; then
    if job_running || game_running; then
        record_update "installed, restart pending"
    elif daemon_healthy; then
        record_update "ok"
        echo "Self-update: ${OLD_VERSION:-?} -> ${NEW_VERSION}: ok"
    else
        echo "Self-update: ${OLD_VERSION:-?} -> ${NEW_VERSION}: the new daemon did not stay up; rolling back"
        bash "$INSTALL_DIR/rollback.sh" || true
        record_update "rolled-back"
        exit 1
    fi
fi

# Daily backup of every registered game's saves (backups stay on the internal drive).
cat > "$SERVICE_DIR/deckstream-saves.service" << EOF
[Unit]
Description=DeckStream game save backup

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 $INSTALL_DIR/saves.py backup --all
EOF
cat > "$SERVICE_DIR/deckstream-saves.timer" << EOF
[Unit]
Description=Daily DeckStream game save backup

[Timer]
OnCalendar=daily
Persistent=true
RandomizedDelaySec=1800

[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload
systemctl --user enable --now deckstream-saves.timer >/dev/null 2>&1 || echo -e "${YELLOW}[!] Could not enable the daily save-backup timer.${NC}"

# Synchronize Decky Loader plugin if installed
DECKY_DIR="$HOME/homebrew/plugins/DeckStream"
if [ -d "$DECKY_DIR" ] && [ -w "$DECKY_DIR/main.py" ]; then
    DECKY_TMP="$(mktemp -d)"
    if curl -sSL -m 20 "$SERVER_URL/decky/DeckStream.zip" -o "$DECKY_TMP/DeckStream.zip" 2>/dev/null; then
        if unzip -qo "$DECKY_TMP/DeckStream.zip" -d "$DECKY_TMP/unpacked" 2>/dev/null; then
            for f in main.py package.json plugin.json; do
                [ -f "$DECKY_TMP/unpacked/DeckStream/$f" ] && [ -w "$DECKY_DIR/$f" ] && cp -f "$DECKY_TMP/unpacked/DeckStream/$f" "$DECKY_DIR/$f" 2>/dev/null || true
            done
            if [ -d "$DECKY_DIR/dist" ] && [ -f "$DECKY_TMP/unpacked/DeckStream/dist/index.js" ] && [ -w "$DECKY_DIR/dist/index.js" ]; then
                cp -f "$DECKY_TMP/unpacked/DeckStream/dist/index.js" "$DECKY_DIR/dist/index.js" 2>/dev/null || true
            fi
            echo -e "${GREEN}[✓] Synchronized Decky plugin from ${SERVER_URL}/decky/DeckStream.zip${NC}"
        fi
    fi
    rm -rf "$DECKY_TMP"
fi

echo ""
echo -e "${GREEN}==========================================================${NC}"
echo -e "${GREEN}  🎉 DeckStream Daemon Successfully Installed and Started! ${NC}"
echo -e "${GREEN}==========================================================${NC}"
echo ""
echo "Useful Commands:"
echo -e "  View Status:  ${CYAN}systemctl --user status deckstream${NC}"
echo -e "  View Logs:    ${CYAN}journalctl --user -u deckstream -f${NC}"
echo -e "  Restart:      ${CYAN}systemctl --user restart deckstream${NC}"
echo -e "  Stop:         ${CYAN}systemctl --user stop deckstream${NC}"
echo ""
if [ "${DECKSTREAM_SKIP_DOCTOR:-0}" != 1 ] && [ -f "$INSTALL_DIR/doctor.py" ]; then
    echo ""
    echo -e "${CYAN}Checking this Deck (only problems are shown) ...${NC}"
    python3 "$INSTALL_DIR/doctor.py" --brief || true
    echo -e "Full report any time: ${CYAN}python3 ~/.config/deckstream/doctor.py${NC}   Undo this update: ${CYAN}bash ~/.config/deckstream/rollback.sh${NC}"
fi
echo ""
echo -e "${YELLOW}Now open DeckStream on your phone and start managing your library!${NC}"
