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
| Storage integration | automated localhost storage API, real rclone WebDAV/VFS and cache-prefill check | Generated fixture bytes only, no clean-device game launch. |
| Self-update | implemented, off until you set a signing key | Not exercised by anyone outside the project. |
| Local NAS latency | not benchmarked | The README says so; nothing here claims it. |
| Which games work | not claimed | There is no compatibility list. Report yours with the [test report](community/DEVICE_TEST_REPORT.md). |

If a claim you read anywhere (README, a post, a reply) is stronger than this table, this table wins.
Please open an issue.
