# What the evidence does and does not show

Maturity labels, in order: **planned**, **implemented**, **automated** (CI passes), **measured on a
device** (a person ran it on a Deck and kept numbers), **independently repeated** (someone outside
the project got the same result and reported it).

| Claim | Where it stands | What it does not show |
|---|---|---|
| Own-storage library, queue and WebDAV launch path | implemented, automated (mock storage, browser flow) | A launch on a clean Deck from this revision. Priority 1 in [PLAN.md](../PLAN.md). |
| Hardware numbers in the README "Measured" table | measured on one Deck, over home Wi-Fi, October 2026 | They come from the predecessor runtime (the v1.0.0 line), a cloud WebDAV store and a handful of games. They are not a promise for your setup and have not been repeated by anyone else. |
| Start-up profile (prefetch of the ranges a game reads) | implemented, unit tested | Not compared against unprofiled starts on the current code ([PLAN.md](../PLAN.md) priority 3). |
| Hedge proxy | implemented, off by default | Measured only in a replay rig, not on a Deck. |
| Replay rig and early-launch calculator | automated local synthetic WebDAV/VFS replay and model checks | No hardware launch, Wi-Fi, SD-card, kernel/FUSE or gameplay evidence; timings and over-fetch depend on workload and flags. |
| Headless smoke harness | automated with mock SSH (preflight, launch/process failures, successful cleanup) | No fresh clean Deck run; process presence alone does not prove playability or controller support (#9/#39). |
| Clean-Linux launch rig (`scripts/test_launch_rig.sh`, four scenarios, in CI) | automated: the real `stream-runner.sh` and `game-launcher.sh` against a local WebDAV server, rclone FUSE mount, fuse-overlayfs and Windows probe exes built from this repo's own source, run under Wine in a virtual display. `basic`: window appears, payload hash read through the mount is exact, clean shutdown. `conditions`: the read cache lands on a loop-mounted ext4 "SD card", reads still succeed through a shaping proxy (emulated 3000 KB/s, 40 ms per request) and the controller wait logic sees a Steam-virtual-pad entry. `gfx`: a Direct3D 11 clear and exact pixel read-back on Wine's own renderer over software GL. `proton`: the same probe through real GE-Proton10-34 (pinned release, SHA-512 checked) via the launcher's plain-Proton path, with DXVK confirmed running on software Vulkan (llvmpipe) | Outside the `proton` scenario Wine stands in for Proton. Even there: no umu and no Steam Linux Runtime container (umu cannot self-install in the CI or sandbox network), software Vulkan only, no GPU or performance, and a Direct3D 11 clear is not a game. The "SD card" is a file-backed ext4 image, not card hardware or its speed. The shaped link is not Wi-Fi or a NAS and its numbers are not measurements. The controller check reads a device-list file; no input device exists and no input reaches a game. No SteamOS, Gaming Mode/gamescope, Decky or real game. |
| Real-game rig (`scripts/rig_real_game.sh`) | implemented; run by the owner on the machine that holds a game. One launch of one game: FUSE mount, exe process seen, clean shutdown | Process presence is not playability. A report from a non-Deck host is not a Deck result. Its output is retained evidence only if the report file is kept. |
| Storage integration | automated localhost storage API, real rclone WebDAV/VFS and cache-prefill check | Generated fixture bytes only, no clean-device game launch. |
| Self-update | implemented, off until you set a signing key | Not exercised by anyone outside the project. |
| Local NAS latency | not benchmarked | The README says so; nothing here claims it. |
| Which games work | not claimed | There is no compatibility list. Report yours with the [test report](community/DEVICE_TEST_REPORT.md). |

If a claim you read anywhere (README, a post, a reply) is stronger than this table, this table wins.
Please open an issue.
