#!/usr/bin/env bash
# DeckStream - bring a game's window to the front once it appears (Desktop Mode).
#
# Seen on the Deck, Desktop Mode (Plasma on Wayland, the game on XWayland): a game started from a Steam shortcut
# often shows a BLANK screen although it is running and playing sound; the picture only appears after the Steam
# menu button is pressed, which changes the focus. This waits for the game's window and activates it once.
# Before it acts it logs the window state, so a failure can be diagnosed from ~/.local/share/deckstream/logs/focus.log.
#
# Usage: focus_game.sh [seconds_to_watch (default 900)]
# Off with DECKSTREAM_FOCUS_HELPER=0 (checked by the caller). Does nothing without xdotool or an X display.

WATCH="${1:-900}"
LOG_DIR="$HOME/.local/share/deckstream/logs"; mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/focus.log"
say() { echo "$(date +%T) $*" >> "$LOG"; }

command -v xdotool >/dev/null 2>&1 || exit 0
export DISPLAY="${DISPLAY:-:0}"
if [ -z "${XAUTHORITY:-}" ]; then
    XAUTHORITY="$(ls "/run/user/$(id -u)"/xauth_* 2>/dev/null | head -n 1)"
    [ -n "$XAUTHORITY" ] && export XAUTHORITY
fi
xdotool getdisplaygeometry >/dev/null 2>&1 || exit 0

# Windows that are never the game.
EXCLUDE='^(Steam|steam|steamwebhelper|Steam Big Picture Mode|Chromium.*|Plasma.*|plasmashell.*|KDE.*|kwin.*|Konsole.*|Dolphin.*|Default IME|Input|Untitled|Xalia.*|Qt Selection.*|xsettingsd|MSCTFIME.*|CiceroUI.*|Wine.*|explorer\.exe|Desktop|Spectacle.*|Discover.*|.*[Oo]verlay.*|Wine System Tray)$'
declare -A DONE
end=$(( $(date +%s) + WATCH ))
say "watching for the game window for ${WATCH}s"
while [ "$(date +%s)" -lt "$end" ]; do
    for w in $(xdotool search --name '.' 2>/dev/null); do
        [ -n "${DONE[$w]:-}" ] && continue
        name="$(xdotool getwindowname "$w" 2>/dev/null)"
        [[ "$name" =~ $EXCLUDE ]] && continue
        geo="$(xdotool getwindowgeometry --shell "$w" 2>/dev/null)"
        width="$(echo "$geo" | sed -n 's/^WIDTH=//p')"; height="$(echo "$geo" | sed -n 's/^HEIGHT=//p')"
        [ "${width:-0}" -ge 640 ] && [ "${height:-0}" -ge 400 ] || continue
        DONE[$w]=1
        active="$(xdotool getactivewindow getwindowname 2>/dev/null)"
        state="$(xwininfo -id "$w" 2>/dev/null | sed -n 's/.*Map State: //p' | head -n 1)"
        say "game window $w '$name' ${width}x${height} map=$state; active window before: '$active'"
        xdotool windowmap "$w" 2>/dev/null
        xdotool windowraise "$w" 2>/dev/null
        xdotool windowactivate "$w" 2>/dev/null
        xdotool windowfocus "$w" 2>/dev/null
        sleep 1
        say "active window after: '$(xdotool getactivewindow getwindowname 2>/dev/null)'"
    done
    sleep 3
done
say "stopped watching"
