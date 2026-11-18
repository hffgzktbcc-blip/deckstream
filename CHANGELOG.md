# Changelog

Notable changes to DeckStream.

## Unreleased

### Added
- Project foundations: vision, limits, roadmap and milestones (`docs/`), agent working rules (`AGENTS.md`, `CLAUDE.md`,
  `GEMINI.md`, Copilot instructions) with a git-based handoff tool (`scripts/handoff.sh`), code of conduct, CODEOWNERS,
  issue forms, Dependabot and a first-PR welcome.
- CodeQL scanning, a fuller `SECURITY.md`, line coverage reporting in CI (`scripts/coverage.sh`), push notification
  tests (`scripts/test_push.js`), a version check (`scripts/check_version.py`) and a release workflow that builds the
  Decky zip and daemon bundle with checksums as a draft release (`docs/RELEASING.md`).

### Fixed
- `scripts/build_decky.sh` stamps files with `touch -t`, so the Decky zip can be rebuilt on macOS as well as Linux
  (#55, #57; thanks to @bfs2021).

### Security
- Push subscriptions must use an https endpoint on a public address; the server refuses to store or send to
  localhost, cloud metadata or home-network hosts.

## 1.0.0
First release; predates the own-storage changes. See the [release page](https://github.com/hffgzktbcc-blip/deckstream/releases/tag/v1.0.0).
