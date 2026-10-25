# 🎮 DeckStream

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Platform: SteamOS / Linux](https://img.shields.io/badge/Platform-SteamOS%20%2F%20Linux-brightgreen.svg)](https://store.steampowered.com/steamos)
[![Handheld: Steam Deck](https://img.shields.io/badge/Device-Steam%20Deck%20%7C%20Handhelds-purple.svg)](https://www.steamdeck.com)

**DeckStream** is an open-source, on-demand virtual filesystem gaming runtime for the Steam Deck and handheld Linux PCs.

Instead of filling limited handheld NVMe or MicroSD cards with 100+ GB game installations, DeckStream mounts your remote game libraries (WebDAV, Nextcloud, TrueNAS/Unraid Samba, S3/MinIO) over a fast FUSE Copy-on-Write (COW) layer, running the game natively on your device while streaming only the file blocks the game actually reads.

---

## ⚡ How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                      SteamOS Gaming Mode                    │
│   (Steam UI Non-Steam Shortcut • Decky Loader QAM Panel)     │
└──────────────────────────────┬──────────────────────────────┘
                               │ launches
┌──────────────────────────────▼──────────────────────────────┐
│                    stream-runner.sh                         │
│   (Proton / GE-Proton / Wine Runtime Execution via UMU)      │
└──────────────────────────────┬──────────────────────────────┘
                               │ accesses files
┌──────────────────────────────▼──────────────────────────────┐
│                  FUSE Copy-on-Write Layer                   │
│  ┌─────────────────────────────────┐ ┌───────────────────┐  │
│  │ Local NVMe / MicroSD Write Path │ │ Rolling Cache     │  │
│  │ (Game saves, settings, logs)    │ │ (Cached blocks)   │  │
│  └─────────────────────────────────┘ └───────────────────┘  │
└──────────────────────────────┬──────────────────────────────┘
                               │ reads un-cached ranges
┌──────────────────────────────▼──────────────────────────────┐
│               Hedged Multi-Stream HTTP/WebDAV                │
│    (Races parallel connections to eliminate seek jitter)    │
└──────────────────────────────┬──────────────────────────────┘
                               │ fetches blocks
┌──────────────────────────────▼──────────────────────────────┐
│            Your Remote Game Storage / Personal NAS          │
│        (WebDAV • Nextcloud • TrueNAS • S3 • Seedbox)        │
└─────────────────────────────────────────────────────────────┘
```

### Key Engineering Innovations:
1. **Demand Profiling & Smart Startup Prefetch**:
   Modern games read less than 5% of their total assets (splash logos, shader cache, main menu UI) to reach the start screen. DeckStream logs microsecond read offsets on first launch. On subsequent cold launches, it prefetches just those startup ranges in parallel, taking cold launches from minutes down to seconds.
2. **Copy-on-Write Upper Layer (`fuse-overlayfs`)**:
   Remote storage remains 100% read-only. Game writes, INI configuration tweaks, local saves, and DirectX/Vulkan shader caches land in an isolated upper layer on your MicroSD card.
3. **Range-Request Hedging**:
   High-latency remote storage can suffer from erratic First-Byte Latency (TTFB). DeckStream's built-in hedge proxy monitors range requests and automatically races redundant connections if a remote chunk stalls past 200 ms.
4. **Decky Loader Companion**:
   Inspect active streaming sessions, check cache usage, toggle overnight warmup pre-caching, and configure per-game battery-saving presets (30 FPS cap + low graphics preset) directly from SteamOS Game Mode.

---

## 🚀 Getting Started

### Prerequisites
- Steam Deck running SteamOS 3.5+ (or any Linux handheld running Bazzite / ChimeraOS / Arch).
- Developer Mode or standard `deck` user terminal access.
- A remote storage server with your game files (WebDAV, Nextcloud, or NAS).

### Installation

Run the one-line installer from Desktop Mode terminal:

```bash
curl -fsSL https://raw.githubusercontent.com/deckstream/deckstream/main/daemon/install.sh | bash -s -- https://deckstream.local deck-main
```

The installer:
- Configures user-level systemd service `deckstream.service`.
- Installs necessary FUSE and compatibility helpers into `~/.local/bin`.
- Syncs the Decky Loader plugin into `~/homebrew/plugins/DeckStream/` if Decky is installed.

### Storage Configuration

Configure your storage credentials in `~/.config/deckstream/storage.conf`:

```bash
# ~/.config/deckstream/storage.conf
STORAGE_WEBDAV_URL="https://nas.local/remote.php/webdav/Games"
STORAGE_WEBDAV_USER="gamer"
STORAGE_PASS="your_secure_password_or_token"
```

---

## 🛠️ Diagnostics & Self-Healing

DeckStream includes a comprehensive self-diagnostic doctor tool:

```bash
python3 ~/.config/deckstream/doctor.py
```

Checks Wi-Fi power-saving toggles, DNS resolution times, rclone VFS responsiveness, SD card filesystem health, and systemd service state.

---

## 🧪 Running Tests

The test suite validates runtime sandboxing, range hedging, save backups, and integrity manifests:

```bash
bash scripts/run_all_tests.sh
```

---

## ⚖️ Legal & Non-Infringement Disclaimer

DeckStream is an open-source virtual filesystem and runtime utility intended strictly for managing personal media, backups, and game libraries on Linux handheld devices. 

- DeckStream does not provide, host, index, or distribute copyrighted video games, ROMs, or digital media.
- DeckStream contains no tools or software designed to circumvent digital rights management (DRM) or technological protection measures.
- Users are solely responsible for ensuring they have lawful access to any remote storage, software, or media mounted through this tool.

---

## 📄 License

Licensed under the [MIT License](LICENSE).
