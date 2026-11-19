#!/usr/bin/env python3
"""Launch timings and engine run reports for streamed and homebrew games.

stream-runner.sh writes one key=value file per launch in ~/.local/share/deckstream/launches/ (Play, fill start/end,
mount, game start, cache size at launch and every 30 s while the game runs). game-launcher.sh also records
ounce.run.v1 JSON reports for .nro homebrew launches. Steam ends the runner when the game exits, so this module, run
by the daemon, turns FINISHED launches into a short summary and sends them to the user's own DeckStream site
(/api/launch, behind the queue password), so every launch is measured without anyone watching.

Usage: launch_report.py list            # summaries of finished launches (and .nro reports) not yet sent
       launch_report.py send            # send them (uses ~/.config/deckstream/daemon_config.json)
       launch_report.py ounce FILE      # parse and summarize an ounce.run.v1 JSON report
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HOME", str(Path.home())))
LAUNCH_DIR = HOME / ".local" / "share" / "deckstream" / "launches"
CONFIG = HOME / ".config" / "deckstream" / "daemon_config.json"
QUIET_SECONDS = 120          # no stats line for 2 minutes and the runner gone = the session is over
KEEP_SENT = 50


def parse(path):
    """The last value of each key (the stats loop appends cache_mb_last/last_seen every 30 s)."""
    vals = {}
    try:
        for line in Path(path).read_text(errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep and k:
                vals[k.strip()] = v.strip()
    except OSError:
        pass
    return vals


def _num(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _int_val(v):
    """Parse an integer scalar (decimal or 0x-hex), rejecting booleans."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            return int(s, 16) if s.lower().startswith(("0x", "-0x")) else int(s)
        except ValueError:
            return None
    return None


def _fmt_hex(v, width=0):
    n = _int_val(v)
    if n is None:
        return None
    return f"0x{n:0{width}x}" if width else f"0x{n:x}"


EXIT_REASONS = ("dxvk", "vkd3d", "oom", "access_violation", "d3d11", "prefix_timeout", "anticheat", "steam_api",
                "unknown_early")        # same list as proton_matcher.EXIT_REASONS and api/launch.js

# Canonical ounce.run.v1 stop_kind -> exit_code table (ounce-core docs/OUNCE_RUN_V1_SCHEMA.md).
OUNCE_STOP_CODES = {
    "clean_exit": 0,
    "window_closed": 0,
    "frame_limit": 0,
    "unimplemented_insn": 10,
    "memory_fault": 11,
    "cpu_error": 12,
    "svc_break": 13,
    "unhandled_svc": 14,
    "cpu_halted": 15,
    "budget_exhausted": 16,
    "loader_error": 17,
}
_HB_STOP_ALIASES = {
    "unimplemented_instruction": "unimplemented_insn",
    "break": "svc_break",
}
_CLEAN_STOP_KINDS = ("clean_exit", "window_closed", "frame_limit")


def _load_json_source(source):
    if isinstance(source, dict):
        return source
    if isinstance(source, Path):
        try:
            return json.loads(source.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
    if isinstance(source, str):
        s = source.strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except ValueError:
                return None
        try:
            return json.loads(Path(source).read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
    return None


def parse_ounce_run(source, allow_hb=False):
    """Parse an `ounce.run.v1` JSON object (dict, JSON text, or file path). Returns normalized fields or None."""
    data = _load_json_source(source)
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if schema == "ounce.run.v1":
        stop_kind = data.get("stop_kind")
    elif allow_hb and schema == "ounce.hb.run.v1":
        raw_stop = data.get("stop")
        stop_kind = _HB_STOP_ALIASES.get(raw_stop, raw_stop)
    else:
        return None
    if not isinstance(stop_kind, str) or not stop_kind.strip():
        return None
    stop_kind = stop_kind.strip()

    exit_code = _int_val(data.get("exit_code"))
    if exit_code is None:
        exit_code = OUNCE_STOP_CODES.get(stop_kind)

    out = {
        "schema": schema,
        "stop_kind": stop_kind,
        "exit_code": exit_code,
    }
    for key in ("engine", "engine_version", "tier", "framebuffer_hash", "audio_hash", "message", "symbol"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()
    if isinstance(data.get("silent_fallback"), bool):
        out["silent_fallback"] = data["silent_fallback"]
    for key in ("pc", "lr", "sp", "opcode", "fault_addr", "svc_id", "break_result",
                "insns_executed", "frames_presented", "audio_frames_mixed", "guest_exit_status"):
        val = _int_val(data.get(key))
        if val is not None:
            out[key] = val
    for key in ("fault_lines", "frames", "backtrace"):
        val = data.get(key)
        if isinstance(val, list) and val:
            lines = [str(item).strip() for item in val if item is not None and str(item).strip()]
            if lines:
                out[key] = lines
    if allow_hb and schema == "ounce.hb.run.v1":
        for stream in ("stderr", "stdout"):
            txt = data.get(stream)
            if isinstance(txt, str) and txt.strip() and "message" not in out and exit_code not in (None, 0):
                out["message"] = txt.strip().splitlines()[-1][:240]
                break
    return out


def ounce_fault_details(report):
    """Extract structured fault details from a parsed `ounce.run.v1` dict."""
    if not isinstance(report, dict):
        return {}
    stop_kind = report.get("stop_kind", "")
    exit_code = report.get("exit_code")
    is_fault = stop_kind not in _CLEAN_STOP_KINDS or (exit_code is not None and exit_code != 0)
    details = {}
    for reg, width in (("pc", 16), ("lr", 16), ("sp", 16), ("fault_addr", 16), ("opcode", 8), ("svc_id", 2), ("break_result", 8)):
        if report.get(reg) is not None:
            details[reg] = _fmt_hex(report[reg], width)
    for key in ("message", "symbol", "fault_lines", "frames", "backtrace"):
        if report.get(key):
            details[key] = report[key]
    if is_fault and stop_kind == "budget_exhausted" and report.get("insns_executed") is not None:
        details["insns_executed"] = report["insns_executed"]
    return details if (is_fault or "message" in details) else {}


def format_ounce_fault(report):
    """Build a single concise diagnostic string for an abnormal `ounce.run.v1` stop, or None on clean exit."""
    if not isinstance(report, dict):
        return None
    stop_kind = report.get("stop_kind", "")
    exit_code = report.get("exit_code")
    details = ounce_fault_details(report)
    if not details and stop_kind in _CLEAN_STOP_KINDS and (exit_code is None or exit_code == 0):
        return None
    header = f"{stop_kind} (exit {exit_code})" if exit_code is not None else stop_kind
    parts = []
    if details.get("message"):
        parts.append(details["message"])
    if details.get("symbol"):
        parts.append(f"symbol={details['symbol']}")
    if details.get("opcode"):
        parts.append(f"opcode={details['opcode']}")
    if details.get("fault_addr"):
        parts.append(f"fault_addr={details['fault_addr']}")
    if details.get("svc_id"):
        parts.append(f"svc={details['svc_id']}")
    if details.get("break_result"):
        parts.append(f"result={details['break_result']}")
    if details.get("insns_executed") is not None:
        parts.append(f"insns={details['insns_executed']}")
    for reg in ("pc", "lr", "sp"):
        if details.get(reg):
            parts.append(f"{reg}={details[reg]}")
    for seq_key in ("fault_lines", "frames", "backtrace"):
        if details.get(seq_key):
            parts.append(" | ".join(details[seq_key][:3]))
            break
    return f"{header}: {', '.join(parts)}" if parts else header


def summarize_ounce_run(source, allow_hb=False):
    """Return a concise dict with `stop_kind`, `exit_code`, and fault details for an `ounce.run.v1` report."""
    report = parse_ounce_run(source, allow_hb=allow_hb)
    if not report:
        return None
    out = {
        "schema": report["schema"],
        "stop_kind": report["stop_kind"],
        "exit_code": report["exit_code"],
    }
    for key in ("engine", "engine_version", "tier", "insns_executed", "frames_presented", "framebuffer_hash"):
        if key in report:
            out[key] = report[key]
    details = ounce_fault_details(report)
    if details:
        out["fault_details"] = details
    fault_summary = format_ounce_fault(report)
    if fault_summary:
        out["fault_summary"] = fault_summary
    return out


def _inline_ounce_dict(vals):
    stop_kind = vals.get("stop_kind") or vals.get("ounce_stop_kind")
    if not stop_kind:
        return None
    raw = {
        "schema": "ounce.run.v1",
        "stop_kind": stop_kind,
        "exit_code": vals.get("exit_code") if "exit_code" in vals else vals.get("ounce_exit_code"),
        "engine": vals.get("ounce_engine") or vals.get("engine"),
        "tier": vals.get("ounce_tier") or vals.get("tier"),
        "pc": vals.get("pc"),
        "lr": vals.get("lr"),
        "sp": vals.get("sp"),
        "opcode": vals.get("opcode"),
        "fault_addr": vals.get("fault_addr"),
        "svc_id": vals.get("svc_id"),
        "break_result": vals.get("break_result"),
        "insns_executed": vals.get("insns_executed"),
        "message": vals.get("ounce_message") or vals.get("message"),
        "symbol": vals.get("symbol"),
    }
    return summarize_ounce_run({k: v for k, v in raw.items() if v is not None})


def summarize(vals):
    """A summary with plain numbers (seconds, MB) and optional `ounce.run.v1` fields; missing milestones are left out."""
    n = {k: _num(v) for k, v in vals.items()}
    out = {"slug": vals.get("slug", ""), "play": n.get("play"),
           "mode": "gaming" if "gamescope" in vals.get("desktop", "").lower() else
                   ("desktop" if vals.get("desktop", "unknown") != "unknown" else "unknown")}

    def diff(a, b):
        return n[b] - n[a] if n.get(a) is not None and n.get(b) is not None and n[b] >= n[a] else None

    fields = {
        "fill_seconds": diff("fill_start", "fill_end"),
        "play_to_launch_seconds": diff("play", "launched"),
        "session_seconds": diff("launched", "last_seen"),
        "fill_mb": diff("cache_mb_before_fill", "cache_mb_at_launch"),
        "downloaded_while_playing_mb": diff("cache_mb_at_launch", "cache_mb_last"),
        "fill_rc": n.get("fill_rc"),
        "exit_seconds": n.get("exit_seconds"),
        "exit_reason": vals.get("exit_reason") if vals.get("exit_reason") in EXIT_REASONS else None,
    }
    out.update({k: v for k, v in fields.items() if v is not None})

    ounce = None
    if vals.get("ounce_report"):
        ounce = summarize_ounce_run(Path(vals["ounce_report"]), allow_hb=True)
    if ounce is None:
        ounce = _inline_ounce_dict(vals)
    if ounce:
        out["stop_kind"] = ounce["stop_kind"]
        if ounce.get("exit_code") is not None:
            out["exit_code"] = ounce["exit_code"]
        if ounce.get("engine"):
            out["ounce_engine"] = ounce["engine"]
        if ounce.get("fault_details"):
            out["fault_details"] = ounce["fault_details"]
        if ounce.get("fault_summary"):
            out["fault_summary"] = ounce["fault_summary"]
    return out


def ounce_reports(launch_dir=None):
    """Return [(Path, summary), ...] for standalone `*.ounce.json` reports in `launch_dir`."""
    d = Path(launch_dir) if launch_dir else LAUNCH_DIR
    out = []
    for p in sorted(d.glob("*.ounce.json")) if d.is_dir() else []:
        s = summarize_ounce_run(p, allow_hb=True)
        if s:
            out.append((p, s))
    return out


def _runner_alive(path):
    """The runner's pid is the last part of the file name: <slug>.<epoch>.<pid>.txt."""
    try:
        pid = int(Path(path).name.rsplit(".", 2)[-2])
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"stream-runner.sh" in f.read()
    except (ValueError, OSError, IndexError):
        return False


def finished(launch_dir=None, now=None, alive=_runner_alive):
    """(path, summary) of launches whose session is over, oldest first."""
    d = Path(launch_dir) if launch_dir else LAUNCH_DIR
    now = now if now is not None else time.time()
    out = []
    for p in sorted(d.glob("*.txt")) if d.is_dir() else []:
        vals = parse(p)
        last = _num(vals.get("last_seen")) or _num(vals.get("launched")) or _num(vals.get("play")) or 0
        if alive(p) or now - max(last, p.stat().st_mtime) < QUIET_SECONDS:
            continue
        s = summarize(vals)
        if s["slug"] and s.get("play"):
            out.append((p, s))
        else:
            p.rename(p.with_suffix(".bad"))           # never retried
    return out


def send(server_url, secret, device_id, launch_dir=None, opener=urllib.request.urlopen, now=None):
    """Post each finished launch; a sent file is renamed .sent (the newest KEEP_SENT are kept). Returns the count."""
    sent = 0
    for p, s in finished(launch_dir, now):
        body = json.dumps({"device": device_id, "launch": s}).encode()
        req = urllib.request.Request(server_url.rstrip("/") + "/api/launch", data=body,
                                     headers={"Content-Type": "application/json", "x-deckstream-secret": secret})
        try:
            with opener(req, timeout=10) as r:
                r.read(4096)
        except Exception:
            break                                     # offline or old site: try again next time
        p.rename(p.with_suffix(".sent"))
        sent += 1
    d = Path(launch_dir) if launch_dir else LAUNCH_DIR
    for old in sorted(d.glob("*.sent"), key=lambda q: q.stat().st_mtime)[:-KEEP_SENT] if d.is_dir() else []:
        old.unlink(missing_ok=True)
    return sent


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        for p, s in finished():
            print(p.name, json.dumps(s))
        for p, s in ounce_reports():
            print(p.name, json.dumps(s))
        return 0
    if cmd == "ounce":
        if len(argv) < 3:
            print("Usage: launch_report.py ounce REPORT.json [--exit-code]", file=sys.stderr)
            return 2
        s = summarize_ounce_run(Path(argv[2]), allow_hb=True)
        if not s:
            return 2
        print(json.dumps(s))
        if "--exit-code" in argv[3:] and isinstance(s.get("exit_code"), int):
            return s["exit_code"]
        return 0
    if cmd == "send":
        try:
            cfg = json.loads(CONFIG.read_text())
        except (OSError, ValueError):
            return 2
        if not cfg.get("server_url"):
            return 2
        print(f"sent {send(cfg['server_url'], cfg.get('queue_secret', ''), cfg.get('device_id', 'deck-main'))}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
