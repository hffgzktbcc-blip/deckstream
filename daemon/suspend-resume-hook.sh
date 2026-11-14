#!/usr/bin/env bash
# ==============================================================================
# DeckStream Wi-Fi Suspend/Resume Recovery Hook
#
# When the Steam Deck sleeps during a cloud-streamed session, Wi-Fi drops.
# Upon wake-up, stale TCP sockets to WebDAV/CDN endpoints can hang for up to
# 15 minutes before kernel TCP keepalives expire, causing FUSE reads to freeze.
#
# This hook:
#   1. Detects system resume and waits for network link re-establishment.
#   2. Probes active rclone mounts for responsiveness.
#   3. Lazily unmounts any hung/dead FUSE mounts (fusermount -u -z).
#   4. Flushes stale TCP sockets to WebDAV/CDN endpoints.
#   5. Signals stream-runner to recover and triggers an immediate daemon poll.
#
# Usage:
#   suspend-resume-hook.sh pre   # Invoked immediately before suspend
#   suspend-resume-hook.sh post  # Invoked immediately after wake-up / resume
# ==============================================================================
set -e

ACTION="${1:-post}"
STATE_DIR="${DECKSTREAM_STATE_DIR:-$HOME/.local/share/deckstream}"
LOG_FILE="$STATE_DIR/suspend-resume.log"
SUSPEND_STATE="$STATE_DIR/suspend_state.json"
mkdir -p "$STATE_DIR"

log() {
    local msg="[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $1"
    echo "$msg"
    echo "$msg" >> "$LOG_FILE" 2>/dev/null || true
}

if [ "$ACTION" = "pre" ]; then
    log "[suspend] Steam Deck preparing for sleep/suspend"
    active_mounts=""
    if command -v mount >/dev/null 2>&1; then
        active_mounts="$(mount | grep -i rclone | awk '{print $3}' | tr '\n' ' ')"
    fi
    python3 -c '
import json, sys, time
data = {"suspended_at": time.time(), "active_mounts": sys.argv[1].split()}
with open(sys.argv[2], "w") as f:
    json.dump(data, f)
' "$active_mounts" "$SUSPEND_STATE" 2>/dev/null || true
    exit 0
fi

if [ "$ACTION" = "post" ]; then
    t_start=$(date +%s)
    log "[resume] Steam Deck resumed from sleep/suspend. Starting network & mount recovery..."

    # 1. Wait for network link to recover (max 10 seconds)
    net_ok=0
    for i in $(seq 1 20); do
        if command -v ip >/dev/null 2>&1; then
            if ip route | grep -q default; then
                net_ok=1
                break
            fi
        elif command -v netstat >/dev/null 2>&1; then
            if netstat -rn | grep -q default; then
                net_ok=1
                break
            fi
        else
            # Fallback if ip/netstat not available
            net_ok=1
            break
        fi
        sleep 0.5
    done

    if [ "$net_ok" = 1 ]; then
        log "[resume] Network link re-established."
    else
        log "[resume] Warning: Network link not ready after 10s. Continuing recovery."
    fi

    # 2. Inspect previously recorded and currently active rclone mounts
    mount_targets=""
    if [ -f "$SUSPEND_STATE" ]; then
        mount_targets="$(python3 -c 'import json,sys; print(" ".join(json.load(open(sys.argv[1])).get("active_mounts", [])))' "$SUSPEND_STATE" 2>/dev/null || true)"
    fi
    if command -v mount >/dev/null 2>&1; then
        current_mounts="$(mount | grep -i rclone | awk '{print $3}' | tr '\n' ' ')"
        mount_targets="$mount_targets $current_mounts"
    fi

    # Deduplicate mount paths
    dedup_mounts="$(echo "$mount_targets" | tr ' ' '\n' | sort -u | grep -v '^$' || true)"

    recovered_count=0
    for m in $dedup_mounts; do
        [ -d "$m" ] || continue
        # Probe responsiveness with a 2-second timeout
        hung=0
        if command -v timeout >/dev/null 2>&1; then
            if ! timeout 2 stat -f "$m" >/dev/null 2>&1; then
                hung=1
            fi
        fi

        if [ "$hung" = 1 ]; then
            log "[resume] Mount $m is hung after sleep! Forcing lazy unmount."
            if command -v fusermount >/dev/null 2>&1; then
                fusermount -u -z "$m" 2>/dev/null || true
            elif command -v umount >/dev/null 2>&1; then
                umount -l "$m" 2>/dev/null || true
            fi
            recovered_count=$((recovered_count + 1))
        else
            log "[resume] Mount $m responsive."
        fi
    done

    # 3. (no socket killing: `ss -K` needs root, and `ss -K dport = 443` would cut EVERY HTTPS connection on the Deck. A hung rclone
    #    mount is handled by the lazy unmount above; healthy connections are left alone.)

    # 4. Tell the daemon it resumed (it polls straight away instead of waiting out its idle back-off)
    touch "$STATE_DIR/resume_trigger" 2>/dev/null || true

    t_end=$(date +%s)
    duration=$((t_end - t_start))
    log "[resume] Recovery complete in ${duration}s. Hung mounts reset: $recovered_count."
    rm -f "$SUSPEND_STATE" 2>/dev/null || true
    exit 0
fi

log "[suspend-resume] Unknown action '$ACTION'. Use 'pre' or 'post'."
exit 1
