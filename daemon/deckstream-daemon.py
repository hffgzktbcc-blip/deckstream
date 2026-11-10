#!/usr/bin/env python3
"""
DeckStream Daemon for Steam Deck (SteamOS)
Monitors DeckStream queue, downloads games from RemoteStorage directly to Steam Deck,
extracts archives, detects the game executable, configures Proton compatibility,
adds the game as a Non-Steam shortcut, and fetches SteamGridDB artwork.
"""

import os
import sys
import time
import json
import shutil
import subprocess
import re
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

# The daemon is split into ds_*.py parts installed next to this file. They are loaded fresh from this folder each
# time this file is loaded: the Decky panel loads it again after a self-update (and with its own $HOME), so a copy
# cached from an earlier load must not be reused. The settings run_daemon sets at start-up (QUEUE_SECRET, SERVER_URL,
# DEVICE_ID, ALLOWED_DOWNLOAD_HOSTS) live in ds_core and are read as ds_core.NAME everywhere else.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARTS = ('ds_core', 'ds_steam', 'ds_shortcuts', 'ds_stream', 'ds_cache',
          'ds_update', 'ds_status', 'ds_janitor', 'ds_install')
for _name in _PARTS:
    sys.modules.pop(_name, None)
sys.path.insert(0, _HERE)
try:
    import ds_core
    from ds_core import (CONFIG_FILE, DEFAULT_DOWNLOAD_HOSTS, GAMES_DIR_INTERNAL, HOME, cap_proton_logs,
        clear_current_job, consume_restart_request, get_disk_usage, get_micro_sd_path, load_config, queue_request,
        safe_dir_name, set_current_job)
    from ds_shortcuts import (_cef_target, add_non_steam_shortcut, apply_pending_shortcuts, cef_flag_file, live_enabled,
        request_shortcut, set_live_shortcuts)
    from ds_stream import discard_failed_download
    from ds_cache import (_GAMES_CACHE, apply_deck_mode_wanted, apply_warmup_wanted, remove_game_data, restore_profile,
        start_prestage)
    from ds_status import deck_status, streamed_games
    from ds_janitor import start_janitor
    from ds_install import (ChargerGate, DOWNLOAD_FAILURES, _cloud_rate, deliver_rom_or_executable, download_file,
        extract_archive, fail_job, folder_size, idle_poll_interval, list_installed_games, looks_like_archive,
        report_cloud_progress, report_job, resumed_from_sleep, run_cloud_install, run_resume_hook)
finally:
    sys.path.remove(_HERE)

# The Decky panel and the tests load this file and call what they need on it (d.deck_status(), d.installed_version()
# and so on), so every name from the parts stays reachable here as it was before the split.
for _name in _PARTS:
    globals().update((k, v) for k, v in vars(sys.modules[_name]).items() if not k.startswith("__"))


def run_daemon():
    cfg = load_config()
    server_url = cfg["server_url"].rstrip("/")
    device_id = cfg["device_id"]
    poll_sec = max(3, int(cfg.get("poll_interval_sec", 10)))
    ds_core.QUEUE_SECRET = cfg.get("queue_secret", "")
    ds_core.SERVER_URL = server_url
    ds_core.DEVICE_ID = device_id
    ds_core.ALLOWED_DOWNLOAD_HOSTS = DEFAULT_DOWNLOAD_HOSTS + [
        str(h).lower() for h in cfg.get("allowed_download_hosts", []) if h
    ]
    parsed_server = urllib.parse.urlparse(server_url)
    local_dev = parsed_server.hostname in ("localhost", "127.0.0.1", "::1")
    if not server_url or parsed_server.scheme not in ("https", "http") or (parsed_server.scheme == "http" and not local_dev):
        print(f"[!] server_url must be your own https DeckStream URL (got '{server_url}').")
        print(f"    Fix it in {CONFIG_FILE} (the queue password is only ever sent over https).")
        sys.exit(1)
    if not ds_core.QUEUE_SECRET:
        print("[!] No queue_secret in config - the server will reject this Deck.")
        print(f"    Add \"queue_secret\" to {CONFIG_FILE} (same value as DECKSTREAM_SECRET).")

    print("==========================================")
    print("   🎮 DeckStream Daemon - Steam Deck    ")
    print("==========================================")
    print(f"Server:    {server_url}")
    print(f"Device ID: {device_id}")
    print(f"Home Dir:  {HOME}")
    GAMES_DIR_INTERNAL.mkdir(parents=True, exist_ok=True)
    print(f"Games Dir: {GAMES_DIR_INTERNAL}")
    print("------------------------------------------")

    last_heartbeat = 0.0
    clear_current_job()
    start_janitor()                                   # stream sessions are finished even while a long job runs
    idle_polls = 0                                    # consecutive polls with no job: drives idle_poll_interval()
    while True:
        try:
            clear_current_job()                       # the previous job (if any) has finished
            if consume_restart_request():
                print("[*] An update was installed while a job was running; restarting now to use it.")
                sys.exit(0)                           # systemd (Restart=always) starts the new version
            apply_pending_shortcuts()
            # 1. Report heartbeat & disk space (every 30s; polling also keeps the Deck marked online)
            ssd_stat = get_disk_usage(GAMES_DIR_INTERNAL)
            sd_path = get_micro_sd_path()
            sd_stat = get_disk_usage(sd_path) if sd_path else {"free": 0, "total": 0}

            if time.time() - last_heartbeat >= 30:
                cap_proton_logs()
                installed = list_installed_games(GAMES_DIR_INTERNAL)
                heartbeat_payload = {
                    "action": "heartbeat",
                    "diskSSD": ssd_stat,
                    "diskSD": sd_stat,
                    "installedGames": installed,
                    **deck_status(),
                }
                req = queue_request(
                    f"{server_url}/api/queue?device={device_id}",
                    data=json.dumps(heartbeat_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as hb:
                    try:   # the reply carries the phone's warm-up and Deck mode choices; an older site sends none
                        reply = json.loads(hb.read().decode("utf-8") or "{}")
                        if apply_warmup_wanted(reply.get("warmup_wanted")) | bool(apply_deck_mode_wanted(reply.get("deck_mode_wanted"))):
                            last_heartbeat = 0.0   # report the new state on the next pass, not in 30 s
                        else:
                            last_heartbeat = time.time()
                    except (ValueError, AttributeError, OSError):
                        last_heartbeat = time.time()

            # 2. Poll for pending job
            poll_req = queue_request(f"{server_url}/api/queue?action=poll&device={device_id}")
            with urllib.request.urlopen(poll_req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                job = data.get("job")
            idle_polls = 0 if job else idle_polls + 1

            if job and job.get("status") == "pending" and job.get("mode") == "remove":
                # Free a streamed game's disk (cache, or the whole game) on request from the app; nothing is downloaded.
                job_id = job["id"]
                ok, freed, msg = remove_game_data(str(job.get("slug") or ""), str(job.get("scope") or ""), target_path=job.get("path"), game_name=job.get("name"))
                print(f"[{'+' if ok else '!'}] Remove '{job.get('slug')}' ({job.get('scope')}): {msg}")
                _GAMES_CACHE["at"] = 0.0
                _GAMES_CACHE["games"] = []
                report_job(server_url, device_id, job_id, "completed" if ok else "failed", 100 if ok else 0, msg, freedBytes=freed, games=streamed_games(), installedGames=list_installed_games(GAMES_DIR_INTERNAL))
                time.sleep(2)
                continue

            if job and job.get("status") == "pending" and job.get("mode") in ("ai_heal", "ai_clean"):
                # switched off: the 17 Oct versions deleted Wine prefixes (saves) and registry files; the queue refuses them too
                report_job(server_url, device_id, job["id"], "failed", 0, "AI clean/heal is switched off (it could delete saves)")
                time.sleep(2)
                continue

            if job and job.get("status") == "pending":
                job_id = job["id"]
                game_name = safe_dir_name(job.get("name"))
                dl_url = job.get("downloadUrl", "")
                storage_target = job.get("targetDir", "internal")
                dest_root = sd_path if (storage_target == "sd" and sd_path) else GAMES_DIR_INTERNAL

                print(f"\n[+] Processing Job {job_id}: '{game_name}'")
                set_current_job(job_id, game_name)

                # Update progress: downloading
                def report_progress(pct, cur, total):
                    update = {
                        "action": "update_progress",
                        "jobId": job_id,
                        "status": "downloading",
                        "progress": pct,
                        "message": f"Downloading: {round(cur / 1048576)}MB / {round(total / 1048576)}MB"
                    }
                    try:
                        r = queue_request(
                            f"{server_url}/api/queue?device={device_id}",
                            data=json.dumps(update).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        with urllib.request.urlopen(r, timeout=8) as resp:
                            res_json = json.loads(resp.read().decode("utf-8"))
                            if res_json.get("cancelled") or res_json.get("cancelledJobId") == job_id:
                                raise InterruptedError("Cancelled by user")
                    except InterruptedError:
                        raise
                    except Exception:
                        pass

                job_mode = job.get("mode", "download") # "download" or "stream"

                # If cloud stream mode, configure stream launcher shortcut (0 GB local SSD)
                if job_mode == "stream":
                    print(f"[*] Cloud Stream Mode requested for '{game_name}'. Configuring 0-GB Stream Runner...")
                    stream_runner_path = Path.home() / ".config" / "deckstream" / "stream-runner.sh"
                    
                    # Create desktop launch script
                    desktop_launcher = Path.home() / ".local" / "share" / "deckstream" / "launchers"
                    desktop_launcher.mkdir(parents=True, exist_ok=True)
                    clean_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', game_name).lower()
                    launcher_file = desktop_launcher / f"stream_{clean_slug}.sh"
                    
                    webdav_folder = (job.get("webdavPath") or "").strip().strip("/")
                    plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
                    plan_kind = str(plan.get("kind") or "").strip().lower()
                    if not plan or not plan_kind:
                        err_msg = "Stream job has no launch plan. Only verified ready-to-play folders can be streamed."
                        print(f"[!] {err_msg}")
                        r = queue_request(
                            f"{server_url}/api/queue?device={device_id}",
                            data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": err_msg}).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        try:
                            urllib.request.urlopen(r, timeout=10)
                        except Exception:
                            pass
                        time.sleep(poll_sec)
                        continue

                    if plan_kind in ("archive", "installer", "disc-image", "not-a-game", "suspicious"):
                        err_msg = f"Cannot stream release of type '{plan_kind}'. It must be extracted or installed first."
                        print(f"[!] {err_msg}")
                        r = queue_request(
                            f"{server_url}/api/queue?device={device_id}",
                            data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": err_msg}).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        try:
                            urllib.request.urlopen(r, timeout=10)
                        except Exception:
                            pass
                        time.sleep(poll_sec)
                        continue

                    if not webdav_folder:
                        err_msg = "Stream job has no RemoteStorage folder name. Re-send it from the updated web app."
                        print(f"[!] {err_msg}")
                        r = queue_request(
                            f"{server_url}/api/queue?device={device_id}",
                            data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": err_msg}).encode("utf-8"),
                            headers={"Content-Type": "application/json"}
                        )
                        urllib.request.urlopen(r, timeout=10)
                        time.sleep(poll_sec)
                        continue

                    def sh_quote(s):
                        return "'" + str(s).replace("'", "'\"'\"'") + "'"

                    # Credentials are NOT embedded here; stream-runner reads ~/.config/deckstream/storage.conf
                    with open(launcher_file, "w") as lf:
                        lf.write("#!/usr/bin/env bash\n")
                        plan = job.get("plan") if isinstance(job.get("plan"), dict) else {}
                        plan_exe = str(plan.get("exe") or "").strip().strip("/")
                        if any(part in ("", ".", "..") for part in plan_exe.split("/")) or len(plan_exe) > 300:
                            plan_exe = ""   # only a clean path inside the mount is passed on
                        extra = f" {sh_quote(plan_exe)}" if plan_exe else ""
                        lf.write(f"exec bash {sh_quote(stream_runner_path)} {sh_quote(clean_slug)} {sh_quote(game_name)} {sh_quote(webdav_folder)}{extra}\n")
                    os.chmod(launcher_file, 0o755)

                    # Add non-steam shortcut pointing to the stream launcher
                    applied, app_id = request_shortcut(f"{game_name} (Cloud Stream)", str(launcher_file), job.get("artwork"))
                    if shutil.which("notify-send"):
                        try:
                            subprocess.run(["notify-send", f"⚡ {game_name} Stream Ready", "Added to Steam. Warming cache..."], timeout=10)
                        except Exception:
                            pass
                    # Start warming the cache now, while the user walks to the Deck (off until enabled; never fatal)
                    try:
                        # A game learned on an earlier card or install starts warm again: get its profile back first.
                        restore_profile(clean_slug, webdav_folder)
                        start_prestage(clean_slug, webdav_folder, (plan or {}).get("hot"))
                    except Exception as e:
                        print(f"[!] Prestage not started: {e}")

                    # Report completed
                    done_update = {
                        "action": "update_progress",
                        "jobId": job_id,
                        "status": "completed",
                        "progress": 100,
                        "message": ("⚡ Stream Ready! Added to your Steam library with 0 GB SSD usage." if applied
                                    else "⚡ Stream ready. Close Steam (Desktop Mode: Steam > Exit) to add it to your library."),
                        "installedGames": list_installed_games(GAMES_DIR_INTERNAL)
                    }
                    r = queue_request(
                        f"{server_url}/api/queue?device={device_id}",
                        data=json.dumps(done_update).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(r, timeout=10)
                    time.sleep(poll_sec)
                    continue

                # Cloud install: unpack an installer/zip straight from the storage mount onto the SD card
                if job_mode == "cloudinstall":
                    webdav_folder = (job.get("webdavPath") or "").strip().strip("/")
                    script = Path.home() / ".config" / "deckstream" / "cloud-install.sh"
                    if not webdav_folder:
                        fail_job(server_url, device_id, job_id, "Cloud install job has no RemoteStorage folder name.")
                        time.sleep(poll_sec)
                        continue
                    if not script.exists():
                        fail_job(server_url, device_id, job_id, "cloud-install.sh missing on the Deck. Re-run the DeckStream installer.")
                        time.sleep(poll_sec)
                        continue
                    if storage_target == "sd" and not sd_path and job.get("keepAll") is not True:
                        fail_job(server_url, device_id, job_id, "No MicroSD card found. Insert one, or choose internal storage.")
                        time.sleep(poll_sec)
                        continue
                    dest_game_folder = (dest_root / game_name).resolve()
                    keep_all = job.get("keepAll") is True
                    if keep_all:
                        # "Get skipped files": the folder of a rip install that left files out, as the app listed it
                        hit = [g for g in list_installed_games() if g["path"] == str(job.get("path") or "") and g.get("skipped")]
                        if not hit or hit[0]["skipped"]["folder"] != webdav_folder:
                            fail_job(server_url, device_id, job_id, "Nothing was skipped in that game, or it is not installed here.")
                            time.sleep(poll_sec)
                            continue
                        dest_game_folder = Path(hit[0]["path"])
                        dest_root = dest_game_folder.parent
                    if dest_root.resolve() not in dest_game_folder.parents:
                        fail_job(server_url, device_id, job_id, "Refusing to write outside the games folder")
                        time.sleep(poll_sec)
                        continue
                    free = (sd_stat if (storage_target == "sd" and sd_path) else ssd_stat)["free"]
                    if job.get("size", 0) and free < job["size"]:
                        fail_job(server_url, device_id, job_id,
                                 f"Not enough free space: {round(free / 1073741824, 1)}GB free, release is {round(job['size'] / 1073741824, 1)}GB.")
                        time.sleep(poll_sec)
                        continue

                    report_job(server_url, device_id, job_id, "installing", 5, "Reading release from RemoteStorage...")
                    ok, msg = run_cloud_install(
                        script, re.sub(r"[^a-zA-Z0-9_-]", "_", game_name).lower(), webdav_folder, dest_game_folder,
                        lambda last: report_cloud_progress(server_url, device_id, job_id, dest_game_folder, job.get("size", 0), last),
                        size_fn=lambda: folder_size(dest_game_folder),
                        awake_why=f"DeckStream installing {game_name}", paused_fn=ChargerGate().should_pause, keep_all=keep_all,
                    )
                    _cloud_rate.pop(job_id, None)
                    if not ok:
                        report_job(server_url, device_id, job_id, "failed", 0, f"Install failed: {msg}")
                        time.sleep(poll_sec)
                        continue
                    if keep_all:      # the game is already in Steam: only the left-out files were added
                        report_job(server_url, device_id, job_id, "completed", 100, "Got the skipped files",
                                   installedGames=list_installed_games())
                        time.sleep(poll_sec)
                        continue

                    deliver_rom_or_executable(game_name, dest_game_folder, job, server_url, device_id, job_id)
                    time.sleep(poll_sec)
                    continue

                # Check disk space for regular downloads
                required_space = job.get("size", 0) * 1.5  # archive + extracted
                avail_space = ssd_stat["free"] if storage_target != "sd" else sd_stat["free"]
                if required_space > 0 and avail_space < required_space:
                    err_msg = f"Insufficient disk space. Needed ~{round(required_space / 1073741824, 1)}GB, Free {round(avail_space / 1073741824, 1)}GB"
                    print(f"[!] {err_msg}")
                    r = queue_request(
                        f"{server_url}/api/queue?device={device_id}",
                        data=json.dumps({"action": "update_progress", "jobId": job_id, "status": "failed", "message": err_msg}).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(r, timeout=10)
                    time.sleep(poll_sec)
                    continue

                # Download
                dest_game_folder = (dest_root / game_name).resolve()
                if dest_root.resolve() not in dest_game_folder.parents:
                    fail_job(server_url, device_id, job_id, "Refusing to write outside the games folder")
                    time.sleep(poll_sec)
                    continue
                folder_existed = dest_game_folder.exists()
                dest_game_folder.mkdir(parents=True, exist_ok=True)
                temp_file = dest_game_folder / "download.pkg"

                try:
                    download_file(dl_url, temp_file, report_progress)
                except DOWNLOAD_FAILURES as ex:
                    # Rejected URL, cancellation, or failed transfer: clean up immediately
                    fail_msg = "Download cancelled by user" if isinstance(ex, InterruptedError) else f"Download failed: {ex}"
                    print(f"[!] {fail_msg}")
                    fail_job(server_url, device_id, job_id, fail_msg)
                    discard_failed_download(dest_game_folder, temp_file, folder_existed)
                    clear_current_job()
                    time.sleep(poll_sec)
                    continue

                if not temp_file.exists() or temp_file.stat().st_size == 0:
                    err_msg = "Download was empty (0 bytes received from CDN). Check RemoteStorage link."
                    print(f"[!] {err_msg}")
                    fail_job(server_url, device_id, job_id, err_msg)
                    discard_failed_download(dest_game_folder, temp_file, folder_existed)
                    time.sleep(poll_sec)
                    continue

                # Extract if archive
                extract_update = {
                    "action": "update_progress",
                    "jobId": job_id,
                    "status": "extracting",
                    "progress": 95,
                    "message": "Extracting game files..."
                }
                try:
                    r = queue_request(
                        f"{server_url}/api/queue?device={device_id}",
                        data=json.dumps(extract_update).encode("utf-8"),
                        headers={"Content-Type": "application/json"}
                    )
                    urllib.request.urlopen(r, timeout=8)
                except Exception:
                    pass

                if looks_like_archive(temp_file):
                    try:
                        extract_archive(temp_file, dest_game_folder)
                    except Exception as ex:
                        # A truncated or corrupt archive used to be treated as "a single file": the job went on, found an
                        # .exe among the half-extracted files and reported the game as installed. Fail instead.
                        print(f"[!] Extraction failed: {ex}")
                        fail_job(server_url, device_id, job_id, f"Extraction failed (the download may be damaged): {ex}")
                        discard_failed_download(dest_game_folder, temp_file, folder_existed)
                        time.sleep(poll_sec)
                        continue
                else:
                    print("[*] Not an archive: keeping the downloaded file as it is")
                    pass

                deliver_rom_or_executable(game_name, dest_game_folder, job, server_url, device_id, job_id)
                time.sleep(poll_sec)
                continue

        except urllib.error.URLError as e:
            # Server temporarily unreachable
            pass
        except Exception as e:
            print(f"[!] Daemon loop error: {e}")

        slept_for = idle_poll_interval(poll_sec, idle_polls)
        t_before_sleep = time.time()
        time.sleep(slept_for)
        # OFF unless "suspend_hook": true in config.json (no Deck run yet): recover cloud mounts that hung across a suspend
        if cfg.get("suspend_hook") and resumed_from_sleep(t_before_sleep, time.time(), slept_for):
            print("[*] Resumed from sleep: checking the cloud mounts.")
            run_resume_hook()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--live-shortcuts" and sys.argv[2] in ("on", "off", "status"):
        if sys.argv[2] != "status":
            set_live_shortcuts(sys.argv[2] == "on")
        on, flag = live_enabled(), cef_flag_file().exists()
        try:
            reachable = bool(_cef_target())
        except Exception:
            reachable = False
        print(f"Live shortcuts: {'ON' if on else 'off'}; Steam debug flag file: {'present' if flag else 'absent'}; "
              f"Steam API reachable now: {'yes' if reachable else 'no'}")
        if on and not reachable:
            print("Restart Steam once (or reboot) so it opens its debugging interface; new games then appear without a restart.")
        if on:
            print("Note: while this is on, any program running as this user can control Steam through 127.0.0.1:8080.")
        sys.exit(0)
    if len(sys.argv) == 4 and sys.argv[1] == "--add-shortcut":
        # Repair/re-create the Steam shortcut for an already-installed game:
        #   deckstream-daemon.py --add-shortcut "Game Name" "/path/to/Game.exe"
        # Close Steam first (it rewrites shortcuts.vdf when it exits).
        sys.exit(0 if add_non_steam_shortcut(sys.argv[2], sys.argv[3]) else 1)
    run_daemon()
