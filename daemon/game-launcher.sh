#!/usr/bin/env bash
# DeckStream - launches an installed Windows game through Proton/umu, or a homebrew .nro through an external Ounce runner.
# Steam shortcuts point at a tiny per-game script that calls this, so you never
# need to "Force compatibility tool" in Steam.
# Usage: game-launcher.sh "/path/to/Game.exe" "/path/to/prefix_dir" [slug] [title]
#
# Launcher choice (.exe): umu-launcher (Proton plus Valve's Steam Linux Runtime, as Hydra/Lutris do) when umu and
# its runtime are installed, otherwise plain Proton. umu uses the SAME prefix folder, so saves stay put.
# If umu cannot start the game within 10 s the launcher retries with plain Proton.
# Env: DECKSTREAM_LAUNCHER=proton forces plain Proton; DECKSTREAM_GAMEID=umu-<steam appid> picks umu's fixes.
# Homebrew (.nro/.NRO): dispatched before Proton/umu checks to an external subprocess (never bundled):
#   DECKSTREAM_NRO_RUNNER=hb -> external `ounce-hb run --json` (with `--sdmc <dir>` if configured)
#   default                  -> external `ounce-runtime run <exe> --present=gpu` (with `--sdmc=<dir>` if configured)

EXE="${1:-}"
PREFIX="${2:-}"
SLUG="${3:-}"      # optional: used for save backups
TITLE="${4:-}"
LOG_DIR="$HOME/.local/share/deckstream/logs"
mkdir -p "$LOG_DIR"
LOG_TAG="$(basename "${PREFIX:-${SLUG:-${EXE:-launch}}}")"
exec > >(tee -a "$LOG_DIR/play_${LOG_TAG}.log") 2>&1

fail() { echo "[!] $1"; command -v notify-send &>/dev/null && notify-send "DeckStream" "$1"; exit 1; }
[ -f "$EXE" ] || fail "Game executable not found: $EXE (is the SD card inserted?)"
IS_NRO=0
case "$EXE" in
    *.nro|*.NRO) IS_NRO=1 ;;
esac
if [ "$IS_NRO" -eq 1 ]; then
    [ -z "$PREFIX" ] || mkdir -p "$PREFIX" || fail "Cannot create game data directory at $PREFIX"
else
    mkdir -p "$PREFIX" || fail "Cannot create Proton prefix at $PREFIX"
fi

CACHE_KEY="${SLUG:-$(basename "${PREFIX:-${EXE%.*}}")}"
# Deck mode names an installed game by its folder (the phone's installed list), the shortcut by its title: try both.
DECK_MODE_KEYS=""
DM_DIR="$(dirname "$EXE")"
for _ in 1 2 3 4 5 6; do
    [ "$DM_DIR" = "/" ] || [ "$DM_DIR" = "." ] && break
    DECK_MODE_KEYS="$DECK_MODE_KEYS $(basename "$DM_DIR" | tr -c 'A-Za-z0-9_-' '_' | tr 'A-Z' 'a-z' | sed 's/^_*//; s/_*$//' | cut -c1-80)"
    DM_DIR="$(dirname "$DM_DIR")"
done
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
        set -a
        # shellcheck source=/dev/null  # per-game runtime env file generated on the Deck
        . "$HOME/.config/deckstream/deck_mode/$DM_KEY.env"
        set +a
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

resolve_external_bin() {
    local bin="$1"
    case "$bin" in
        */*) [ -f "$bin" ] && [ -x "$bin" ] && { printf '%s\n' "$bin"; return 0; } ;;
        *)   command -v "$bin" 2>/dev/null && return 0 ;;
    esac
    return 1
}

launch_nro() {
    local sdmc_dir=""
    if [ -n "${DECKSTREAM_NRO_SDMC+x}" ]; then
        case "$DECKSTREAM_NRO_SDMC" in
            ""|0|none|NONE|off|OFF) sdmc_dir="" ;;
            *) sdmc_dir="$DECKSTREAM_NRO_SDMC" ;;
        esac
    elif [ -n "$SLUG" ] && [ -d "$HOME/.local/share/deckstream/stream/writes/$SLUG" ]; then
        sdmc_dir="$HOME/.local/share/deckstream/stream/writes/$SLUG/sdmc"
    elif [ -n "$PREFIX" ]; then
        sdmc_dir="$PREFIX/sdmc"
    fi
    if [ -n "$sdmc_dir" ]; then
        mkdir -p "$sdmc_dir" || fail "Cannot create SDMC directory at $sdmc_dir"
        echo "[*] sdmc:/ mapped to $sdmc_dir"
    fi

    local script_dir report_py launches_dir report_slug report_json
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    report_py="$script_dir/launch_report.py"
    [ -f "$report_py" ] || report_py="$HOME/.config/deckstream/launch_report.py"
    launches_dir="$HOME/.local/share/deckstream/launches"
    mkdir -p "$launches_dir"
    report_slug="${CACHE_KEY:-$(basename "${EXE%.*}")}"
    case "$report_slug" in ''|.|..|*/*) report_slug="nro" ;; esac
    report_json="${DECKSTREAM_NRO_REPORT_JSON:-$launches_dir/${report_slug}.$(date +%s).$$.ounce.json}"
    mkdir -p "$(dirname "$report_json")"

    local cmd_rc hb_out=""
    cd "$(dirname "$EXE")" || true
    if [ "${DECKSTREAM_NRO_RUNNER:-runtime}" = "hb" ]; then
        local hb_bin hb_resolved
        hb_bin="${OUNCE_HB_BIN:-ounce-hb}"
        hb_resolved="$(resolve_external_bin "$hb_bin")" || fail "External runner '$hb_bin' not found on PATH (DeckStream never bundles ounce-hb)."
        echo "[*] $(date) launching $EXE with external $hb_bin"
        local -a cmd=("$hb_resolved" run --json)
        [ -n "$sdmc_dir" ] && cmd+=(--sdmc "$sdmc_dir")
        cmd+=("$EXE")
        hb_out="$(mktemp)"
        OUNCE_REPORT_JSON="$report_json" "${cmd[@]}" > "$hb_out"
        cmd_rc=$?
        cat "$hb_out"
    else
        local rt_bin rt_resolved
        rt_bin="${OUNCE_RUNTIME_BIN:-ounce-runtime}"
        rt_resolved="$(resolve_external_bin "$rt_bin")" || fail "External runner '$rt_bin' not found on PATH (DeckStream never bundles ounce-runtime)."
        echo "[*] $(date) launching $EXE with external $rt_bin (--present=gpu)"
        local -a cmd=("$rt_resolved" run "$EXE" --present=gpu)
        [ -n "$sdmc_dir" ] && cmd+=("--sdmc=$sdmc_dir")
        OUNCE_REPORT_JSON="$report_json" "${cmd[@]}"
        cmd_rc=$?
    fi

    local final_rc="$cmd_rc"
    if command -v python3 >/dev/null 2>&1; then
        local parsed_rc
        parsed_rc="$(python3 - "$report_py" "$report_json" "$hb_out" "$cmd_rc" "${DECKSTREAM_LAUNCH_FILE:-}" <<'PY'
import importlib.util
import json
import os
import sys
from pathlib import Path

report_py, report_json, hb_out, cmd_rc_raw, launch_file = sys.argv[1:6]
cmd_rc = int(cmd_rc_raw)
mod = None
if report_py and os.path.isfile(report_py):
    spec = importlib.util.spec_from_file_location("launch_report", report_py)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

def summarize_file_or_text(path_str):
    if not path_str or not os.path.isfile(path_str):
        return None, None
    raw = Path(path_str).read_text(encoding="utf-8", errors="replace").strip()
    if not raw:
        return None, None
    candidates = [raw] + [line.strip() for line in reversed(raw.splitlines()) if line.strip().startswith("{")]
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except ValueError:
            continue
        if isinstance(obj, dict):
            if mod is not None:
                s = mod.summarize_ounce_run(obj, allow_hb=True)
                if s:
                    return s, obj
            elif obj.get("schema") in ("ounce.run.v1", "ounce.hb.run.v1"):
                stop = obj.get("stop_kind") or obj.get("stop")
                ec = obj.get("exit_code")
                if isinstance(stop, str) and stop:
                    s = {"schema": obj["schema"], "stop_kind": stop, "exit_code": ec if isinstance(ec, int) and not isinstance(ec, bool) else None}
                    return s, obj
    return None, None

summary, raw_obj = summarize_file_or_text(report_json)
if summary is None and hb_out:
    summary, raw_obj = summarize_file_or_text(hb_out)
    if summary is not None and raw_obj and raw_obj.get("schema") == "ounce.run.v1" and not os.path.isfile(report_json):
        Path(report_json).write_text(json.dumps(raw_obj, indent=2) + "\n", encoding="utf-8")

if summary is not None:
    ec = summary.get("exit_code")
    msg = f"[ounce] stop_kind={summary['stop_kind']} exit_code={ec if ec is not None else cmd_rc}"
    if summary.get("fault_summary"):
        msg += f" | {summary['fault_summary']}"
    print(msg, file=sys.stderr)
    if launch_file and os.path.isfile(launch_file):
        with open(launch_file, "a", encoding="utf-8") as fh:
            if os.path.isfile(report_json):
                fh.write(f"ounce_report={report_json}\n")
            fh.write(f"stop_kind={summary['stop_kind']}\n")
            if ec is not None:
                fh.write(f"exit_code={ec}\n")
            if summary.get("fault_summary"):
                fh.write(f"ounce_message={summary['fault_summary']}\n")
    print(ec if isinstance(ec, int) else cmd_rc)
else:
    print(cmd_rc)
PY
)" && case "$parsed_rc" in ''|*[!0-9]*) ;; *) final_rc="$parsed_rc" ;; esac
    fi
    [ -n "$hb_out" ] && rm -f "$hb_out"
    return "$final_rc"
}

if [ "$IS_NRO" -eq 1 ]; then
    launch_nro
    exit $?
fi

# ---- umu (preferred) -------------------------------------------------------------------------------
UMU_RUN="$HOME/.local/share/deckstream/umu-test/umu/umu-run"
UMU_PROTON=""
umu_ready() {
    [ "${DECKSTREAM_LAUNCHER:-umu}" = "proton" ] && return 1
    [ -f "$UMU_RUN" ] || return 1
    ls "$HOME/.local/share/umu/steamrt4"/steamrt4_platform_* >/dev/null 2>&1 || return 1   # runtime downloaded
    command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || return 1
    local d
    for root in "$HOME/.steam/root/compatibilitytools.d" "$HOME/.local/share/Steam/compatibilitytools.d"; do
        for d in "$root"/GE-Proton*; do
            [ -f "$d/proton" ] && [ -f "$d/toolmanifest.vdf" ] && UMU_PROTON="$(printf '%s\n%s\n' "$UMU_PROTON" "$d" | sed '/^$/d' | sort -V | tail -n 1)"
        done
    done
    [ -n "$UMU_PROTON" ]
}

PROTON_BIN=""
for p in "$HOME"/.steam/root/compatibilitytools.d/*/proton \
         "$HOME/.local/share/Steam/steamapps/common/Proton - Experimental/proton" \
         "$HOME"/.local/share/Steam/steamapps/common/Proton*/proton; do
    [ -f "$p" ] && { PROTON_BIN="$p"; break; }
done
[ -z "$PROTON_BIN" ] && fail "No Proton found. Install Proton Experimental from Steam (Library > Tools)."

export STEAM_COMPAT_DATA_PATH="$PREFIX"
export STEAM_COMPAT_CLIENT_INSTALL_PATH="$HOME/.local/share/Steam"
cd "$(dirname "$EXE")" || true
echo "[*] $(date) launching $EXE with $(basename "$(dirname "$PROTON_BIN")")"
SAVES_PY="$HOME/.config/deckstream/saves.py"
[ -n "$SLUG" ] && [ -f "$SAVES_PY" ] && python3 "$SAVES_PY" register --slug "$SLUG" --title "${TITLE:-$SLUG}" --prefix "$PREFIX" >/dev/null 2>&1
# Desktop Mode: a running game often shows a blank screen until the window gets focus (pressing the Steam menu button
# fixes it). Bring the game's window forward once it appears. Off with DECKSTREAM_FOCUS_HELPER=0.
if [ "${DECKSTREAM_FOCUS_HELPER:-1}" != "0" ] && [ -f "$HOME/.config/deckstream/focus_game.sh" ]; then
    # Started from $HOME, not the game folder: the stream runner waits while anything uses the game's folder.
    ( cd "$HOME" && exec env -u LD_PRELOAD bash "$HOME/.config/deckstream/focus_game.sh" ) >/dev/null 2>&1 &
fi
# Sony PC ports (Spider-Man 2, 18 Oct) ship the PlayStation PC SDK runtime as two .msi files that Steam installs on
# first start (installscript.vdf -> install_pspc_sdk_runtime.bat quiet). Outside Steam nothing runs it, so do the same
# once per prefix: Manager first, then Installer, as the .bat does. Only when both files sit next to the game exe;
# a failure or timeout is logged and the game still starts. Off with DECKSTREAM_PSPC_SDK=0.
GAME_DIR="$(dirname "$EXE")"
PSPC_MARK="$PREFIX/.deckstream_pspc_sdk"
if [ "${DECKSTREAM_PSPC_SDK:-1}" != "0" ] && [ ! -f "$PSPC_MARK" ] \
   && [ -f "$GAME_DIR/PsPcSdkRuntimeManager.msi" ] && [ -f "$GAME_DIR/PsPcSdkRuntimeInstaller.msi" ]; then
    echo "[*] $(date) installing the PlayStation PC SDK runtime into the prefix (first launch only)"
    PSPC_OK=1
    PSPC_TIMEOUT=""
    command -v timeout >/dev/null 2>&1 && PSPC_TIMEOUT="timeout ${DECKSTREAM_PSPC_SDK_TIMEOUT:-600}"   # macOS test runs lack it
    for msi in PsPcSdkRuntimeManager.msi PsPcSdkRuntimeInstaller.msi; do
        # shellcheck disable=SC2086
        ( cd "$GAME_DIR" && $PSPC_TIMEOUT "$PROTON_BIN" run msiexec /quiet /i "$msi" ) \
            || { echo "[!] $msi did not install (exit $?); starting the game anyway"; PSPC_OK=0; break; }
    done
    [ "$PSPC_OK" -eq 1 ] && date > "$PSPC_MARK" && echo "[*] PlayStation PC SDK runtime installed"
fi
# Deck mode graphics: low settings for a game whose settings file does not exist yet (never overwrites one).
if [ "${DECKSTREAM_LOW_PRESET:-0}" = "1" ] && [ -f "$HOME/.config/deckstream/deck_preset.py" ]; then
    DM_RUNNER=proton; umu_ready && DM_RUNNER=umu
    python3 "$HOME/.config/deckstream/deck_preset.py" "$EXE" "$PREFIX" "$DM_RUNNER" || true
fi
USE_PROTON=1
if umu_ready; then
    echo "[*] $(date) launching $EXE with umu ($(basename "$UMU_PROTON"), steamrt4)"
    START="$(date +%s)"
    # umu applies a game's own fixes only when GAMEID names it (umu-<id>); look it up in GE-Proton's umu database
    # (exact matches only: the GOG id from goggame-<id>.info, else the exact title). Unknown games keep umu-default.
    UMU_GAMEID="${DECKSTREAM_GAMEID:-}"
    if [ -z "$UMU_GAMEID" ] && [ -f "$HOME/.config/deckstream/umu_gameid.py" ]; then
        UMU_GAMEID="$(python3 "$HOME/.config/deckstream/umu_gameid.py" --exe "$EXE" --title "$TITLE" 2>/dev/null | head -n 1)"
    fi
    UMU_GAMEID="${UMU_GAMEID:-umu-default}"
    echo "[*] umu GAMEID: $UMU_GAMEID"
    # Gaming Mode can launch with LC_ALL=C, which breaks non-ASCII paths under Wine; only that case is changed.
    case "${LC_ALL:-}" in C|POSIX) export LC_ALL=C.UTF-8 ;; esac
    WINEPREFIX="$PREFIX" PROTONPATH="$UMU_PROTON" GAMEID="$UMU_GAMEID" python3 "$UMU_RUN" "$EXE"
    RC=$?
    if [ "$RC" -ne 0 ] && [ $(( $(date +%s) - START )) -lt "${DECKSTREAM_UMU_FAST_FAIL_SECS:-10}" ]; then
        echo "[!] umu failed to start the game (exit $RC); retrying with plain Proton"
    else
        USE_PROTON=0
    fi
fi
if [ "$USE_PROTON" -eq 1 ]; then
    "$PROTON_BIN" waitforexitandrun "$EXE"
    RC=$?
fi
# The backup runs detached (systemd-run, outside Steam's process tree): quitting from the Steam overlay ends the
# whole tree of this script, which used to kill a plain background backup before it finished.
if [ -n "$SLUG" ] && [ -f "$SAVES_PY" ]; then
    systemd-run --user --no-block --collect -E LD_PRELOAD= python3 "$SAVES_PY" backup "$SLUG" >/dev/null 2>&1 \
        || { SETSID=""; command -v setsid >/dev/null 2>&1 && SETSID="setsid"; $SETSID nohup env -u LD_PRELOAD python3 "$SAVES_PY" backup "$SLUG" >/dev/null 2>&1 & }
fi
exit $RC
