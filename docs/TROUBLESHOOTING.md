# Troubleshooting

DeckStream ships a checker, `doctor.py` (source: [`daemon/doctor.py`](../daemon/doctor.py)). The
installer puts it at `~/.config/deckstream/doctor.py`. Run it on the Deck first when something does
not work:

```bash
python3 ~/.config/deckstream/doctor.py            # full report
python3 ~/.config/deckstream/doctor.py --brief    # only warnings and failures
python3 ~/.config/deckstream/doctor.py --json     # machine-readable
python3 ~/.config/deckstream/doctor.py --bundle   # also writes a shareable report
```

Each line starts with a status:

| Icon | Status | Meaning |
|---|---|---|
| ✅ | ok | Fine. |
| ⚠️ | warn | Something is missing or degraded. Warnings do not make the doctor fail. |
| ❌ | fail | DeckStream will not work until this is fixed. Any failure makes the exit code `1`. |
| ℹ️ | info | Information only. |

A line with a problem is followed by `→` and the first thing to try. `--bundle` writes
`~/.local/share/deckstream/diag-<date>-<time>.txt` with the report, the last 80 daemon log lines,
disk usage, the mounts under `/run/media` and the save-backup status. The queue password, the
storage key and anything that looks like a token or password are replaced with `***`, so that file
is the one to attach to an issue.

If one of the checks below crashes, the doctor does not stop: it prints a warning naming the check
and `the check itself failed: <error>`, then runs the rest. That message is a bug in the doctor;
please report it with the bundle.

The checks run in this order.

## Python (`check_python`)

**Looks at:** the Python version running the doctor.

| Result | Meaning | First thing to try |
|---|---|---|
| ❌ `DeckStream needs Python 3.9 or newer` | The Python is older than 3.9. | Check `python3 --version` and run the doctor with Python 3.9 or newer. |

## DeckStream config (`check_config`)

**Looks at:** `~/.config/deckstream/daemon_config.json`.

| Result | Meaning | First thing to try |
|---|---|---|
| ❌ DeckStream config: `... is missing or unreadable` | The file does not exist or is not valid JSON. | Run the DeckStream installer again. |
| ❌ Server address | `server_url` is empty or is not `https://` (plain `http://` is only accepted for `127.0.0.1` / `localhost`). | Re-run the installer and give your own https DeckStream address. |
| ❌ Queue password saved: `no` | `queue_secret` is empty. | Re-run the installer and enter the queue password. |

## DNS (`check_dns`)

**Looks at:** whether these names can be looked up: the host in `server_url`, the host in
`STORAGE_WEBDAV_URL` (from `~/.config/deckstream/storage.conf`), and three optional hosts. The DNS
servers in `/etc/resolv.conf` are shown when a lookup fails.

| Result | Meaning | First thing to try |
|---|---|---|
| ❌ `DNS: <your server or storage host>` cannot be looked up | The Deck cannot find your own server. Nothing will work. | Set a public DNS for the Wi-Fi connection (the commands are printed under the line: `nmcli connection modify "WIFI_NAME" ipv4.dns "1.1.1.1 9.9.9.9" ipv4.ignore-auto-dns yes`, then `nmcli connection up "WIFI_NAME"`). |
| ⚠️ `DNS: github.com` | Only affects downloading rclone, umu and Ludusavi. Your DeckStream site can mirror them under `/daemon/tools`. | Same DNS fix, or use the mirror. |
| ⚠️ `DNS: downloads.rclone.org` | Only affects downloading rclone (mirrored). | Same DNS fix. |
| ⚠️ `DNS: repo.steampowered.com` | Only affects umu's game runtime, which is **not** mirrored. Ignore it if you do not use umu. | Same DNS fix. |

## DeckStream server and clock (`check_server`)

**Looks at:** a request to `<server_url>/api/queue?action=status` with the saved queue password,
and the server's `Date` header. Skipped when there is no `server_url`.

| Result | Meaning | First thing to try |
|---|---|---|
| ❌ DeckStream server: `cannot reach ...` | The connection failed. | Check the Wi-Fi connection and the server address. |
| ❌ Queue password: `the server rejected it` | HTTP 401: the saved password is not the server's. | Re-run the installer and enter the current `DECKSTREAM_SECRET`. |
| ⚠️ `DECKSTREAM_SECRET is not set on the server` | HTTP 503. | Set `DECKSTREAM_SECRET` in Vercel and redeploy. |
| ⚠️ `unexpected HTTP <code>` | Any other status. | Check the server's own logs. |
| ❌ Clock: `off by N s` (over 300 s) | The Deck's clock is wrong; secure connections fail. | Fix the date and time in Settings > System. |
| ⚠️ Clock: `off by N s` (61 to 300 s) | Drifting, not yet breaking anything. | Same fix. |

## Storage server (`check_storage`)

**Looks at:** `STORAGE_KEY`, `STORAGE_WEBDAV_URL` and `STORAGE_WEBDAV_USER` (default `deckstream`)
in `~/.config/deckstream/storage.conf`, then a WebDAV `PROPFIND` to that URL with those credentials.

| Result | Meaning | First thing to try |
|---|---|---|
| ⚠️ `not set on this Deck` | The key or the URL is missing. Streaming and installs need them. | Put `STORAGE_KEY`, `STORAGE_WEBDAV_URL` and `STORAGE_WEBDAV_USER` in `storage.conf`. |
| ⚠️ `set, but <url> could not be reached` | The connection failed or the URL is malformed. | Check the URL, the network and that the storage server is running. |
| ❌ `<url> rejected the key` | HTTP 401 or 403. | Put the current key in `storage.conf`. |
| ⚠️ `<url> answered HTTP <code>` | Any status other than 200, 207, 401 or 403. | Check the storage server's logs and [STORAGE_API.md](STORAGE_API.md). |

## rclone (`check_rclone`)

**Looks at:** `rclone` on the `PATH` or in `~/.local/bin`, and the version from `rclone version`.

| Result | Meaning | First thing to try |
|---|---|---|
| ⚠️ `not installed yet` | Normal before the first stream or cloud install. | Nothing; it is installed automatically then. |
| ⚠️ `installed but its version could not be read` | `rclone version` gave no usable output. | Run `rclone version` yourself; if it is broken, delete it so DeckStream reinstalls it. |
| ⚠️ version older than 1.67 | Parallel chunk reads are not available. | Delete it and let DeckStream reinstall the pinned version. |

## FUSE (`check_fuse`)

**Looks at:** whether `/dev/fuse` exists and is readable and writable, and whether `fusermount3` /
`fusermount` and `fuse-overlayfs` are installed.

| Result | Meaning | First thing to try |
|---|---|---|
| ❌ FUSE device: `/dev/fuse missing or not accessible` | Cloud mounts cannot work. | Check `ls -l /dev/fuse`. |
| ❌ fusermount: `not found` | Needed to mount and unmount the cloud folder. | Install FUSE 3 (`fusermount3`). |
| ⚠️ fuse-overlayfs: `not found` | Streaming still runs, but without the local write layer. | Install `fuse-overlayfs` if you want the write layer. |

## Proton (`check_proton`)

**Looks at:** custom Proton builds (a folder with both `proton` and `toolmanifest.vdf`) in
`~/.steam/root/compatibilitytools.d` and `~/.local/share/Steam/compatibilitytools.d`, and Steam's
own `Proton*` folders in `~/.local/share/Steam/steamapps/common`.

| Result | Meaning | First thing to try |
|---|---|---|
| ❌ `no Proton version found` | Windows games cannot start. | Install GE-Proton with ProtonUp-Qt (Discover store), or Proton Experimental in Steam. |

## Drives (`check_disks`)

**Looks at:** free space in your home folder, and every drive mounted under `/run/media/` (SD
cards) with its file system and free space.

| Result | Meaning | First thing to try |
|---|---|---|
| ❌ Internal drive space under 1 GB | Steam and DeckStream need room there. | Free some space. |
| ⚠️ Internal drive space under 3 GB | Getting low. | Free some space. |
| ❌ SD card `<name>`: `exfat`, `vfat`, `ntfs`, `ntfs3` or `fuseblk` | Proton needs ext4; symlinks fail on these formats. | Reformat the card as ext4 in Desktop Mode (this erases it). |
| ℹ️ SD card: `none mounted` | No card; that is fine. | — |

## Services (`check_services`)

**Looks at:** `systemctl --user is-active deckstream`, `systemctl --user is-active
deckstream-saves.timer`, and the last 200 lines of the daemon's journal for `Daemon loop error` or
`Traceback`.

| Result | Meaning | First thing to try |
|---|---|---|
| ❌ DeckStream daemon: not `active` | The daemon is stopped or crashed. | `systemctl --user restart deckstream`, then read `journalctl --user -u deckstream -n 50`. |
| ⚠️ Daily save backup timer: not `active` | Saves are not being backed up daily. | Re-run the installer to set it up. |
| ⚠️ Recent daemon errors: `N in the last 200 log lines` | The daemon hit errors; the latest one is shown. | `journalctl --user -u deckstream -n 80` and look around the error. |

## Save backups (`check_saves`)

**Looks at:** `ludusavi` on the `PATH` or in `~/.local/bin`, and
`~/.local/share/deckstream/saves-status.json`.

| Result | Meaning | First thing to try |
|---|---|---|
| ⚠️ Ludusavi: `not installed` | Save backups cannot run. | Re-run the installer (it needs github.com or your DeckStream mirror). |
| ⚠️ Save backups: `need attention: <games>` | Those games are `no-saves-found` or `error`. (`no-saves-yet` is normal and not listed.) | For a game Ludusavi did not match: `python3 ~/.config/deckstream/saves.py title <game> "Official name"`. |

## System (`check_system`)

**Looks at:** Wi-Fi power saving (`iw dev <interface> get power_save`), whether the SteamOS system
partition is unlocked (`steamos-readonly status`), and shortcuts waiting in
`~/.config/deckstream/pending_shortcuts.json`.

| Result | Meaning | First thing to try |
|---|---|---|
| ℹ️ Wi-Fi power saving: `on` | Streaming makes many small reads and power saving may delay them. Not yet measured on a Deck, so this is a suggestion only. | If streamed games load slowly or stutter, turn Developer Mode on (Settings > System), then Settings > Developer > Wi-Fi power management off, and compare a launch. |
| ⚠️ SteamOS system partition: `unlocked` | SteamOS updates discard what is installed there. | `sudo steamos-readonly enable`. |
| ⚠️ Steam shortcuts waiting | New games are waiting to be added to Steam, which only happens while Steam is closed. | Exit Steam (Desktop Mode: Steam > Exit). |

## Audio output (`check_audio`)

**Looks at:** the default PulseAudio/PipeWire output (`pactl get-default-sink`). Prints nothing if
`pactl` does not answer.

| Result | Meaning | First thing to try |
|---|---|---|
| ⚠️ default output is `...streaming...` | Steam Link / Remote Play made its virtual streaming output the default, so game sound does not reach the speakers. | Turn Remote Play / Steam Link off (Steam > Settings > Remote Play), or `pactl list short sinks`, then `pactl set-default-sink <speaker sink name>`. |

## Stale stream mounts (`check_stale_mounts`)

**Looks at:** each folder in `~/.local/share/deckstream/stream/games/` that cannot be listed
because its mount is gone ("transport endpoint is not connected"). Prints nothing when there are
none.

| Result | Meaning | First thing to try |
|---|---|---|
| ⚠️ Stale stream mounts: `<games>` | Left behind when a game exited. | Wait a minute; the daemon clears them. By hand: `fusermount3 -uz ~/.local/share/deckstream/stream/games/<game>`. |

## Installed files and updates (`check_install`)

**Looks at:** `~/.config/deckstream/manifest.json`, the SHA-256 of every file it lists, and the
server's `<server_url>/daemon/manifest.json` version.

| Result | Meaning | First thing to try |
|---|---|---|
| ℹ️ `no manifest.json` | Installed before checksums existed. | Re-run the installer once. |
| ⚠️ Installed files: `<file> (missing)` / `(changed)` | A DeckStream file was deleted or edited. | Re-run the installer to repair. |
| ⚠️ Update available | The server has a newer version than the Deck. | Run the installer command again (it waits for a running job). |
