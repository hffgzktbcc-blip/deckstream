#!/usr/bin/env bash
# ==============================================================================
# DeckStream rollback: put back the scripts from before the last update.
#   bash ~/.config/deckstream/rollback.sh            restore the newest backup
#   bash ~/.config/deckstream/rollback.sh --list     show the backups
#   bash ~/.config/deckstream/rollback.sh NAME       restore a specific backup
# The installer keeps the three most recent backups. Your settings, storage key and
# queue password are never touched.
# ==============================================================================
set -e
INSTALL_DIR="${DECKSTREAM_DIR:-$HOME/.config/deckstream}"
STATE_DIR="${DECKSTREAM_STATE_DIR:-$HOME/.local/share/deckstream}"
BACKUP_ROOT="$INSTALL_DIR/backup"

if [ ! -d "$BACKUP_ROOT" ] || [ -z "$(ls -1 "$BACKUP_ROOT" 2>/dev/null)" ]; then
    echo "No backups found in $BACKUP_ROOT (the installer makes one before every update)."
    exit 1
fi
if [ "${1:-}" = "--list" ]; then
    echo "Backups (newest last):"; ls -1 "$BACKUP_ROOT" | sort | sed 's/^/  /'; exit 0
fi

PICK="${1:-$(ls -1 "$BACKUP_ROOT" | sort | tail -n 1)}"
SRC="$BACKUP_ROOT/$PICK"
[ -d "$SRC" ] || { echo "No backup named '$PICK'. Try: $0 --list"; exit 1; }

# Keep a copy of what is installed now, so the rollback itself can be undone.
STAMP="${DECKSTREAM_STAMP:-$(date +%Y%m%d-%H%M%S)}-before-rollback"
mkdir -p "$BACKUP_ROOT/$STAMP"
for f in "$SRC"/*; do
    n="$(basename "$f")"
    [ -f "$INSTALL_DIR/$n" ] && cp -p "$INSTALL_DIR/$n" "$BACKUP_ROOT/$STAMP/$n"
done

for f in "$SRC"/*; do
    n="$(basename "$f")"
    cp -p "$f" "$INSTALL_DIR/.$n.restore" && mv -f "$INSTALL_DIR/.$n.restore" "$INSTALL_DIR/$n"
done
echo "Restored the scripts from backup '$PICK'."

job_running() {
    [ -f "$STATE_DIR/current_job.json" ] || return 1
    pid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("pid",""))' "$STATE_DIR/current_job.json" 2>/dev/null || true)"
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
game_running() {
    # The runner's own lock (one folder per game, pid inside) in the runtime dir; a dead pid is a stale lock and does NOT count.
    local ld="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/deckstream_locks" f pid
    for f in "$ld"/runner_*.lock/pid; do
        [ -f "$f" ] || continue
        pid="$(cat "$f" 2>/dev/null || true)"
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then return 0; fi
    done
    pgrep -f "stream-runner.sh" >/dev/null 2>&1 && return 0
    pgrep -f "SteamLaunch.*AppId" >/dev/null 2>&1 && return 0
    pgrep -f "rclone.*mount.*deckstream" >/dev/null 2>&1 && return 0
    return 1
}
if job_running || game_running; then
    mkdir -p "$STATE_DIR"; touch "$STATE_DIR/restart-pending"
    echo "A game or job is running, so the daemon will restart itself when it finishes."
else
    rm -f "$STATE_DIR/restart-pending"
    systemctl --user restart deckstream.service && echo "Daemon restarted."
fi
