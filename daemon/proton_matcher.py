#!/usr/bin/env python3
"""Proton Crash Signature Matcher and Log Manager for Streamed Steam Deck Games.

Parses Proton trace logs (steam-<appid>.log) to isolate actionable root causes
for game crashes (DXVK, VKD3D, OOM, Access Violations, Anti-Cheat, missing libraries)
and rotates/caps log files to prevent disk exhaustion.
"""
import os
import re
import shutil

SIGNATURES = [
    {
        "category": "DXVK Vulkan Failure",
        "key": "dxvk",
        "pattern": re.compile(r"failed to initialize (?:dxvk|vulkan loader)", re.IGNORECASE),
        "cause": "DXVK initialization failed: Vulkan loader or driver issue",
    },
    {
        "category": "VKD3D DirectX 12 Failure",
        "key": "vkd3d",
        "pattern": re.compile(r"failed to find vkd3d-proton d3d12core interfaces|vkd3d_create_instance", re.IGNORECASE),
        "cause": "DirectX 12 / VKD3D failure: D3D12 interfaces not available in Proton prefix",
    },
    {
        "category": "OOM (Out of Memory)",
        "key": "oom",
        "pattern": re.compile(r"(?:unhandled exception|status) 0xc0000017", re.IGNORECASE),
        "cause": "Out of memory in Wine / VC_redist (status 0xc0000017)",
    },
    {
        "category": "Access Violation Crash",
        "key": "access_violation",
        "pattern": re.compile(r"unhandled exception:? c0000005|access violation", re.IGNORECASE),
        "cause": "Crash in game executable (Access Violation 0xc0000005)",
    },
    {
        "category": "Direct3D 11 Creation Failure",
        "key": "d3d11",
        "pattern": re.compile(r"d3d11:\s*failed to create device", re.IGNORECASE),
        "cause": "Direct3D 11 device creation failed: display or renderer mismatch",
    },
    {
        "category": "Wine Prefix Timeout",
        "key": "prefix_timeout",
        "pattern": re.compile(r"wine_kernel_init boot event wait timed out", re.IGNORECASE),
        "cause": "Wine prefix creation timed out or corrupted",
    },
    {
        "category": "Anti-Cheat Incompatible",
        "key": "anticheat",
        "pattern": re.compile(
            r"(?:easyanticheat|battleye|eac_server)[^\n]*(?:error|violation|failed|denied|untrusted|integrity|not found|cannot initialize|not installed|service error)"
            r"|(?:failed to (?:initialize|start)[^\n]*(?:easyanticheat|battleye)|security violation|anti-cheat error)",
            re.IGNORECASE
        ),
        "cause": "Anti-cheat failure: EasyAntiCheat / BattlEye is not supported in streamed Proton",
    },
    {
        "category": "Steam API Not Found",
        "key": "steam_api",
        "pattern": re.compile(r"steamapi_init\(?\)? failed|steamapi_init failed|steam_api6?4?\.dll[^\n]*(?:not found|failed to load|c0000135)", re.IGNORECASE),
        "cause": "The game could not start the Steam API: this release needs a manual step (a crack or steam_api replacement)",
    },
]

# The only values that ever leave the Deck as a launch's exit_reason (the phone shows the words, never log text).
EXIT_REASONS = [s["key"] for s in SIGNATURES] + ["unknown_early"]


def match_signature(log_content):
    """Scan log content (typically the last 100 KB) against known Proton error signatures.

    Returns dict with {"category": str, "cause": str, "matched": str} or None.
    """
    if not log_content:
        return None

    # Focus on the tail where crash stack traces and exit codes are logged
    lines = log_content.splitlines()
    tail = lines[-1000:] if len(lines) > 1000 else lines
    text_to_scan = "\n".join(tail)

    for sig in SIGNATURES:
        match = sig["pattern"].search(text_to_scan)
        if match:
            return {
                "category": sig["category"],
                "key": sig["key"],
                "cause": sig["cause"],
                "matched": match.group(0),
            }
    return None


def inspect_proton_log(log_path):
    """Read and evaluate a Proton log file on disk."""
    if not os.path.isfile(log_path):
        return None
    try:
        size = os.path.getsize(log_path)
        # Read at most the last 256 KB
        read_size = min(size, 256 * 1024)
        with open(log_path, "r", errors="replace") as f:
            if size > read_size:
                f.seek(size - read_size)
            content = f.read()
        return match_signature(content)
    except OSError:
        return None


def _keep_tail(path, max_bytes):
    """Cut a file down to its last `max_bytes` bytes (in place, streamed in 8 MB blocks)."""
    size = os.path.getsize(path)
    if size <= max_bytes:
        return
    tmp = path + ".tail"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        src.seek(size - max_bytes)
        shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
    os.replace(tmp, path)


def cap_logs_in(log_dir, max_bytes=25 * 1024 * 1024, keep=5):
    """Rotate every Proton log (`steam-*.log`) in a directory that is over `max_bytes`. Returns how many were rotated."""
    import glob
    n = 0
    for path in glob.glob(os.path.join(log_dir, "steam-*.log")):
        try:
            big = os.path.getsize(path) > max_bytes
        except OSError:
            continue
        if big:
            rotate_and_cap_log(path, max_bytes, keep)
            n += 1
    return n


def rotate_and_cap_log(log_path, max_bytes=25 * 1024 * 1024, keep=5):
    """Rotate log file if it exceeds max_bytes, keeping up to `keep` historical files."""
    if not os.path.isfile(log_path):
        return

    try:
        size = os.path.getsize(log_path)
        if size <= max_bytes:
            return

        # Shift existing rotated files: log.4 -> log.5, log.3 -> log.4 ...
        for i in range(keep - 1, 0, -1):
            src = f"{log_path}.{i}"
            dst = f"{log_path}.{i+1}"
            if os.path.exists(src):
                if i + 1 > keep:
                    try:
                        os.remove(src)
                    except OSError:
                        pass
                else:
                    shutil.move(src, dst)

        # Move current log to .1, then keep only its last `max_bytes` (a 1 GB log must not live on as a 1 GB .1)
        target_1 = f"{log_path}.1"
        shutil.move(log_path, target_1)
        _keep_tail(target_1, max_bytes)

        # Recreate empty log
        with open(log_path, "w") as f:
            f.write(f"# Log rotated at {size} bytes\n")
    except OSError:
        pass


def main():
    import sys
    if len(sys.argv) < 2:
        print("Usage: proton_matcher.py <path_to_proton_log> [--rotate]", file=sys.stderr)
        sys.exit(1)

    log_path = sys.argv[1]
    if "--key" in sys.argv:                  # the runner's exit summary: one enum word, or "none"
        d = inspect_proton_log(log_path)
        print(d["key"] if d else "none")
        sys.exit(0)
    if "--rotate-only" in sys.argv:          # every exit: cap the log, no diagnosis
        rotate_and_cap_log(log_path)
        sys.exit(0)
    if "--rotate" in sys.argv:
        rotate_and_cap_log(log_path)

    diagnosis = inspect_proton_log(log_path)
    if diagnosis:
        print(f"[!] Proton Diagnostic: {diagnosis['category']} - {diagnosis['cause']}")
        print(f"    Matched: {diagnosis['matched']}")
        sys.exit(2)
    else:
        print("[✓] No known crash signature matched.")
        sys.exit(0)


if __name__ == "__main__":
    main()
