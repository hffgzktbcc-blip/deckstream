# Changelog

Notable changes to DeckStream.

## Unreleased

### Added
- Clean-Linux launch rig (`scripts/test_launch_rig.sh`, run in CI, scenarios `basic`, `conditions` and `gfx`): the real runner and launcher end to end against a local WebDAV server, FUSE mount and probe exes built from this repo's source, under Wine and Xvfb, with an emulated SD card, slow link, controller entry and a Direct3D 11 read-back. `scripts/rig_real_game.sh` runs the same path for a game you own on your own machine and writes a report.
- Project foundations: vision, limits, roadmap and milestones (`docs/`), agent working rules (`AGENTS.md`, `CLAUDE.md`,
  `GEMINI.md`, Copilot instructions) with a git-based handoff tool (`scripts/handoff.sh`), code of conduct, CODEOWNERS,
  issue forms, Dependabot and a first-PR welcome.
- CodeQL scanning, a fuller `SECURITY.md`, line coverage reporting in CI (`scripts/coverage.sh`), push notification
  tests (`scripts/test_push.js`), a version check (`scripts/check_version.py`) and a release workflow that builds the
  Decky zip and daemon bundle with checksums as a draft release (`docs/RELEASING.md`).

### Fixed
- `scripts/build_decky.sh` stamps files with `touch -t`, so the Decky zip can be rebuilt on macOS as well as Linux
  (#55, #57; patch first posted by @awais-ur-rehman, PR by @bfs2021).

### Security
- Push subscriptions must use an https endpoint on a public address; the server refuses to store or send to
  localhost, cloud metadata or home-network hosts.

## 1.0.0
First release; predates the own-storage changes. See the [release page](https://github.com/hffgzktbcc-blip/deckstream/releases/tag/v1.0.0).
