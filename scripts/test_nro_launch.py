#!/usr/bin/env python3
"""Stub-binary tests for Q2 .nro dispatch (game-launcher.sh), ounce-hb doctor --json (doctor.py),
and ounce.run.v1 surfacing (launch_report.py). Uses isolated temp dirs and stub scripts on PATH."""
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "daemon" / "game-launcher.sh"
DOCTOR = ROOT / "daemon" / "doctor.py"
LAUNCH_REPORT = ROOT / "daemon" / "launch_report.py"

sys.path.insert(0, str(ROOT / "daemon"))
launch_report = importlib.import_module("launch_report")


def write_stub(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def make_nro(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = bytearray(0x40)
    data[0x10:0x14] = b"NRO0"
    path.write_bytes(bytes(data))
    return path


def clean_env(home: Path, bin_dir: Path, **extra) -> dict:
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/local/bin:/usr/bin:/bin",
        "TERM": "xterm-256color",
    }
    for k, v in extra.items():
        if v is not None:
            env[k] = str(v)
    return env


def test_nro_dispatch_and_sdmc():
    with tempfile.TemporaryDirectory(prefix="ds_nro_test_") as tmpdir:
        tmp = Path(tmpdir)
        home = tmp / "home"
        bin_dir = tmp / "bin"
        bin_dir.mkdir(parents=True)
        # Note: home has NO Proton and NO umu; .nro dispatch must succeed without either.
        nro_lower = make_nro(tmp / "games" / "hb_app.nro")
        nro_upper = make_nro(tmp / "games" / "DEMO.NRO")
        args_log = tmp / "args.log"

        write_stub(
            bin_dir / "ounce-runtime",
            f'''
printf 'runtime|%s\\n' "$*" >> {json.dumps(str(args_log))}
if [ -n "${{OUNCE_REPORT_JSON:-}}" ] && [ -n "${{STUB_REPORT_JSON:-}}" ]; then
    printf '%s\\n' "$STUB_REPORT_JSON" > "$OUNCE_REPORT_JSON"
fi
exit "${{STUB_EXIT:-0}}"
''',
        )
        write_stub(
            bin_dir / "ounce-hb",
            f'''
printf 'hb|%s\\n' "$*" >> {json.dumps(str(args_log))}
if [ -n "${{STUB_HB_STDOUT:-}}" ]; then
    printf '%s\\n' "$STUB_HB_STDOUT"
fi
if [ -n "${{OUNCE_REPORT_JSON:-}}" ] && [ -n "${{STUB_REPORT_JSON:-}}" ]; then
    printf '%s\\n' "$STUB_REPORT_JSON" > "$OUNCE_REPORT_JSON"
fi
exit "${{STUB_EXIT:-0}}"
''',
        )

        # 1. Default runner -> external `ounce-runtime run "$EXE" --present=gpu --sdmc=<prefix>/sdmc`
        pfx = tmp / "prefixes" / "hb_app_1"
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_lower), str(pfx), "hb_app", "HB App"],
            env=clean_env(home, bin_dir),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        logged = args_log.read_text().splitlines()[-1]
        expected_sdmc = pfx / "sdmc"
        assert logged == f"runtime|run {nro_lower} --present=gpu --sdmc={expected_sdmc}", logged
        assert expected_sdmc.is_dir(), "per-game prefix sdmc dir should be created"
        print("[ok] .nro defaults to external ounce-runtime run <exe> --present=gpu --sdmc=<prefix>/sdmc without Proton/umu")

        # 2. Uppercase .NRO + explicit DECKSTREAM_NRO_SDMC override
        args_log.write_text("")
        custom_sdmc = tmp / "custom_sdmc"
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_upper), str(pfx)],
            env=clean_env(home, bin_dir, DECKSTREAM_NRO_SDMC=custom_sdmc),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        logged = args_log.read_text().splitlines()[-1]
        assert logged == f"runtime|run {nro_upper} --present=gpu --sdmc={custom_sdmc}", logged
        assert custom_sdmc.is_dir()
        print("[ok] .NRO dispatches identically and honors DECKSTREAM_NRO_SDMC")

        # 3. Streamed fuse-overlayfs write layer mapping (stream/writes/<slug>/sdmc)
        args_log.write_text("")
        writes_dir = home / ".local" / "share" / "deckstream" / "stream" / "writes" / "streamed_hb"
        writes_dir.mkdir(parents=True)
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_lower), str(pfx), "streamed_hb", "Streamed HB"],
            env=clean_env(home, bin_dir),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        logged = args_log.read_text().splitlines()[-1]
        assert logged == f"runtime|run {nro_lower} --present=gpu --sdmc={writes_dir / 'sdmc'}", logged
        assert (writes_dir / "sdmc").is_dir()
        print("[ok] streamed .nro maps --sdmc into stream/writes/<slug>/sdmc for fuse-overlayfs/saves.py")

        # 4. DECKSTREAM_NRO_RUNNER=hb -> external `ounce-hb run --json [--sdmc <dir>] "$EXE"`
        args_log.write_text("")
        hb_stdout = json.dumps({
            "schema": "ounce.hb.run.v1",
            "path": str(nro_lower),
            "exit_code": 0,
            "stop": "clean_exit",
            "guest_exit_code": 0,
            "stdout": "",
            "stderr": "",
            "truncated": False,
        })
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_lower), str(pfx)],
            env=clean_env(home, bin_dir, DECKSTREAM_NRO_RUNNER="hb", STUB_HB_STDOUT=hb_stdout),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        logged = args_log.read_text().splitlines()[-1]
        assert logged == f"hb|run --json --sdmc {pfx / 'sdmc'} {nro_lower}", logged

        # Without sdmc configured (DECKSTREAM_NRO_SDMC=none or no prefix)
        args_log.write_text("")
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_lower)],
            env=clean_env(home, bin_dir, DECKSTREAM_NRO_RUNNER="hb", STUB_HB_STDOUT=hb_stdout),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert args_log.read_text().splitlines()[-1] == f"hb|run --json {nro_lower}"

        args_log.write_text("")
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_lower), str(pfx)],
            env=clean_env(home, bin_dir, DECKSTREAM_NRO_RUNNER="hb", DECKSTREAM_NRO_SDMC="none", STUB_HB_STDOUT=hb_stdout),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert args_log.read_text().splitlines()[-1] == f"hb|run --json {nro_lower}"
        print("[ok] DECKSTREAM_NRO_RUNNER=hb dispatches ounce-hb run --json with optional --sdmc")

        # 5. Missing external binary refuses calmly without bundling or fetching
        empty_bin = tmp / "empty_bin"
        empty_bin.mkdir()
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_lower), str(pfx)],
            env={"HOME": str(home), "PATH": str(empty_bin) + ":/bin:/usr/bin"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 1 and "not found on PATH" in (r.stdout + r.stderr), r.stdout + r.stderr
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_lower), str(pfx)],
            env={"HOME": str(home), "PATH": str(empty_bin) + ":/bin:/usr/bin", "DECKSTREAM_NRO_RUNNER": "hb"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 1 and "not found on PATH" in (r.stdout + r.stderr), r.stdout + r.stderr
        print("[ok] missing external ounce-runtime / ounce-hb exits 1 with clear message")

        # 6. game.env overrides (DECKSTREAM_NRO_RUNNER / DECKSTREAM_NRO_SDMC) and .exe non-interference
        args_log.write_text("")
        conf_dir = home / ".config" / "deckstream"
        conf_dir.mkdir(parents=True, exist_ok=True)
        env_sdmc = tmp / "env_sdmc"
        (conf_dir / "game.env").write_text(f"DECKSTREAM_NRO_RUNNER=hb\nDECKSTREAM_NRO_SDMC={env_sdmc}\n", encoding="utf-8")
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro_lower), str(pfx)],
            env=clean_env(home, bin_dir, STUB_HB_STDOUT=hb_stdout),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 0, r.stdout + r.stderr
        assert args_log.read_text().splitlines()[-1] == f"hb|run --json --sdmc {env_sdmc} {nro_lower}"

        fake_exe = tmp / "games" / "win_game.exe"
        fake_exe.write_bytes(b"MZ")
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(fake_exe), str(pfx)],
            env=clean_env(home, bin_dir),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 1 and "No Proton found" in (r.stdout + r.stderr), r.stdout + r.stderr
        print("[ok] game.env configures .nro runner/--sdmc and .exe still goes through Proton checks")


def test_ounce_run_v1_exit_codes_and_launch_file():
    with tempfile.TemporaryDirectory(prefix="ds_nro_rc_") as tmpdir:
        tmp = Path(tmpdir)
        home = tmp / "home"
        bin_dir = tmp / "bin"
        bin_dir.mkdir(parents=True)
        nro = make_nro(tmp / "app.nro")
        pfx = tmp / "pfx"

        write_stub(
            bin_dir / "ounce-runtime",
            '''
if [ -n "${OUNCE_REPORT_JSON:-}" ] && [ -n "${STUB_REPORT_JSON:-}" ]; then
    printf '%s\\n' "$STUB_REPORT_JSON" > "$OUNCE_REPORT_JSON"
fi
exit "${STUB_EXIT:-0}"
''',
        )
        write_stub(
            bin_dir / "ounce-hb",
            '''
if [ -n "${STUB_HB_STDOUT:-}" ]; then
    printf '%s\\n' "$STUB_HB_STDOUT"
fi
exit "${STUB_EXIT:-0}"
''',
        )

        # Canonical ounce.run.v1 codes win over raw runtime exit codes (e.g., runtime exits 1 on frame_limit or fault)
        cases = [
            ("clean_exit", 0, 0, {}),
            ("frame_limit", 0, 1, {"frames_presented": 60, "framebuffer_hash": "fnv1a64:0123456789abcdef"}),
            ("unimplemented_insn", 10, 1, {"pc": 0x80000100, "opcode": 0xDEADBEEF}),
            ("memory_fault", 11, 0, {"pc": 0x80000200, "lr": 0x80000040, "sp": 0x7FFFFFD000, "fault_addr": 0x1000, "message": "unmapped read"}),
            ("svc_break", 13, 1, {"pc": 0x80000300, "break_result": 0xF601}),
            ("unhandled_svc", 14, 1, {"pc": 0x80000400, "svc_id": 0x7F}),
            ("loader_error", 17, 1, {"message": "corrupt NRO0 section"}),
        ]
        for stop_kind, expected_ec, raw_exit, extra_fields in cases:
            report_path = tmp / f"report_{stop_kind}.json"
            launch_file = tmp / f"launch_{stop_kind}.txt"
            launch_file.write_text(f"slug=hb_test\nplay={int(time.time()) - 200}\ndesktop=gamescope\n", encoding="utf-8")
            payload = {
                "schema": "ounce.run.v1",
                "engine": "ounce-runtime",
                "engine_version": "0.6.0",
                "tier": "tier4",
                "silent_fallback": False,
                "stop_kind": stop_kind,
                "exit_code": expected_ec,
                "insns_executed": 4200,
                **extra_fields,
            }
            r = subprocess.run(
                ["bash", str(LAUNCHER), str(nro), str(pfx)],
                env=clean_env(
                    home,
                    bin_dir,
                    STUB_REPORT_JSON=json.dumps(payload),
                    STUB_EXIT=raw_exit,
                    DECKSTREAM_NRO_REPORT_JSON=report_path,
                    DECKSTREAM_LAUNCH_FILE=launch_file,
                ),
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert r.returncode == expected_ec, (stop_kind, r.returncode, r.stdout, r.stderr)
            assert f"stop_kind={stop_kind} exit_code={expected_ec}" in (r.stdout + r.stderr), r.stdout + r.stderr

            # Verify launch_report.summarize() reads the linked ounce.run.v1 report cleanly
            vals = launch_report.parse(launch_file)
            summary = launch_report.summarize(vals)
            assert summary["stop_kind"] == stop_kind, summary
            assert summary["exit_code"] == expected_ec, summary
            assert summary["ounce_engine"] == "ounce-runtime", summary
            if expected_ec != 0:
                assert summary.get("fault_details") and summary.get("fault_summary"), summary

        # Missing report falls back to raw subprocess exit code
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro), str(pfx)],
            env=clean_env(home, bin_dir, STUB_EXIT=42, DECKSTREAM_NRO_REPORT_JSON=tmp / "missing.json"),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 42, r.returncode

        # ounce-hb stdout ounce.hb.run.v1 exit code handling
        hb_fault = json.dumps({
            "schema": "ounce.hb.run.v1",
            "path": str(nro),
            "exit_code": 11,
            "stop": "memory_fault",
            "guest_exit_code": 11,
            "stdout": "",
            "stderr": "Memory access violation at 0x1000",
            "truncated": False,
        })
        r = subprocess.run(
            ["bash", str(LAUNCHER), str(nro), str(pfx)],
            env=clean_env(home, bin_dir, DECKSTREAM_NRO_RUNNER="hb", STUB_HB_STDOUT=hb_fault, STUB_EXIT=1),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert r.returncode == 11, r.returncode
        assert "stop_kind=memory_fault exit_code=11" in (r.stdout + r.stderr), r.stdout + r.stderr
        print("[ok] ounce.run.v1 and ounce.hb.run.v1 exit codes & fault details propagate cleanly")


def test_launch_report_api_and_cli():
    with tempfile.TemporaryDirectory(prefix="ds_lr_test_") as tmpdir:
        tmp = Path(tmpdir)
        report_file = tmp / "hb_fault.ounce.json"
        report_file.write_text(json.dumps({
            "schema": "ounce.run.v1",
            "engine": "ounce-core",
            "engine_version": "0.10.0",
            "tier": "tier3",
            "silent_fallback": False,
            "stop_kind": "memory_fault",
            "exit_code": 11,
            "pc": 0x80000120,
            "lr": 0x80000080,
            "sp": 0x7FFFFFDE00,
            "fault_addr": 0xDEADBEEF,
            "insns_executed": 9001,
            "message": "unmapped store",
            "symbol": "main+0x20",
            "fault_lines": ["#0 0x80000120 in main (src/main.c:42)"],
        }), encoding="utf-8")

        s = launch_report.summarize_ounce_run(report_file)
        assert s["stop_kind"] == "memory_fault" and s["exit_code"] == 11, s
        assert s["fault_details"]["pc"] == "0x0000000080000120", s
        assert s["fault_details"]["fault_addr"] == "0x00000000deadbeef", s
        assert s["fault_details"]["symbol"] == "main+0x20", s
        assert "unmapped store" in s["fault_summary"] and "src/main.c:42" in s["fault_summary"], s

        # CLI `launch_report.py ounce` and `launch_report.py list`
        r = subprocess.run(
            [sys.executable, str(LAUNCH_REPORT), "ounce", str(report_file)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r.returncode == 0, r.stderr
        out = json.loads(r.stdout)
        assert out["stop_kind"] == "memory_fault" and out["exit_code"] == 11, out

        r_ec = subprocess.run(
            [sys.executable, str(LAUNCH_REPORT), "ounce", str(report_file), "--exit-code"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r_ec.returncode == 11, r_ec.returncode

        # Clean exit must keep exit_code=0 (not dropped as falsy) and have no fault_summary
        clean_file = tmp / "clean.ounce.json"
        clean_file.write_text(json.dumps({
            "schema": "ounce.run.v1",
            "engine": "ounce-runtime",
            "stop_kind": "clean_exit",
            "exit_code": 0,
            "insns_executed": 50,
        }), encoding="utf-8")
        sc = launch_report.summarize_ounce_run(clean_file)
        assert sc["exit_code"] == 0 and "fault_summary" not in sc and "fault_details" not in sc, sc

        # `list` surfaces both finished .txt launch + standalone *.ounce.json
        home = tmp / "home"
        launches = home / ".local" / "share" / "deckstream" / "launches"
        launches.mkdir(parents=True)
        (launches / "hb_demo.ounce.json").write_text(clean_file.read_text(), encoding="utf-8")
        txt = launches / "hb_demo.1000.999999.txt"
        txt.write_text(f"slug=hb_demo\nplay=1000\nlaunched=1002\nlast_seen=1010\nounce_report={report_file}\n", encoding="utf-8")
        os.utime(txt, (time.time() - 300, time.time() - 300))
        r_list = subprocess.run(
            [sys.executable, str(LAUNCH_REPORT), "list"],
            env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert r_list.returncode == 0, r_list.stderr
        lines = [ln for ln in r_list.stdout.splitlines() if ln.strip()]
        assert len(lines) == 2 and '"stop_kind": "memory_fault"' in lines[0] and '"stop_kind": "clean_exit"' in lines[1], lines
        print("[ok] launch_report.py surfaces ounce.run.v1 stop_kind, exit_code, and fault details via API and CLI")


def test_doctor_subprocess_with_stub_ounce_hb():
    with tempfile.TemporaryDirectory(prefix="ds_doc_stub_") as tmpdir:
        tmp = Path(tmpdir)
        home = tmp / "home"
        bin_dir = tmp / "bin"
        bin_dir.mkdir(parents=True)
        home.mkdir(parents=True)

        doc_payload = json.dumps({
            "schema": "ounce.doctor.v1",
            "os": "linux",
            "arch": "x86_64",
            "aarch64_hw_replay": False,
            "ounce_core": {"found": True, "path": "/usr/local/bin/ounce-core", "version": "ounce-core 0.10.0"},
            "ounce_runtime": {"found": True, "path": "/usr/local/bin/ounce-runtime", "version": "ounce-runtime 0.6.0"},
            "toolchain": {},
        })
        write_stub(
            bin_dir / "ounce-hb",
            f'''
if [ "${{1:-}}" = "doctor" ] && [ "${{2:-}}" = "--json" ]; then
    printf '%s\\n' {json.dumps(doc_payload)}
    exit 0
fi
exit 2
''',
        )

        # Call check_ounce directly against real stub on PATH (isolated from host Steam/network)
        env_patch = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"}
        orig_env = {k: os.environ.get(k) for k in ("HOME", "PATH", "OUNCE_HB_BIN", "OUNCE_RUNTIME_BIN")}
        try:
            os.environ.update(env_patch)
            os.environ.pop("OUNCE_HB_BIN", None)
            os.environ.pop("OUNCE_RUNTIME_BIN", None)
            doctor = importlib.import_module("doctor")
            importlib.reload(doctor)
            res = doctor.check_ounce()
            assert len(res) == 1 and res[0].status == "ok", res[0].as_dict()
            assert "ounce-core 0.10.0" in res[0].detail and "ounce-runtime 0.6.0" in res[0].detail, res[0].detail
        finally:
            for k, v in orig_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        print("[ok] doctor.py check_ounce executes external `ounce-hb doctor --json` stub on PATH")


if __name__ == "__main__":
    test_nro_dispatch_and_sdmc()
    test_ounce_run_v1_exit_codes_and_launch_file()
    test_launch_report_api_and_cli()
    test_doctor_subprocess_with_stub_ounce_hb()
    print("\nNRO LAUNCH TESTS PASSED")
