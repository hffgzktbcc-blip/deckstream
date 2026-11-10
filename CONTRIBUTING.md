# Contributing to DeckStream

We welcome contributions from the handheld gaming and Linux communities!

## Scope and Principles

DeckStream is dedicated to building high-performance storage and runtime software for Linux handhelds. To protect the project and community:

1. **Storage Agnostic**: All features must operate over standard network protocols (WebDAV, SFTP, S3, SMB, HTTP) without hardcoding private hosts or providers.
2. **Zero Circumvention**: Pull requests containing DRM circumvention, game cracking, or proprietary console ROM runners will be rejected.
3. **No Piracy Indexers**: The core repository must remain completely clean of torrent search engines or warez scrapers.
4. **Performance First**: Handheld devices are constrained by battery and memory bandwidth. All background helpers must respect sleep locks and battery thresholds.

## Development Workflow

1. Fork the repository and create your branch from `main`.
2. Ensure any new features include unit tests under `scripts/`.
3. If files in `daemon/` are modified, regenerate the integrity manifest:
   ```bash
   python3 scripts/make_manifest.py
   ```
4. Verify the test suite passes cleanly:
   ```bash
   bash scripts/run_all_tests.sh
   ```
5. Submit your pull request with a clear description of the improvement and telemetry evidence where applicable.

## Your first contribution

- New here? Read [docs/community/START_HERE.md](docs/community/START_HERE.md), then pick a
  [good first issue](https://github.com/hffgzktbcc-blip/deckstream/issues?q=is%3Aopen+label%3A%22good+first+issue%22). Comment "I'll take this" so two people
  do not collide; if nothing happens for a week the issue is free again.
- You do not need a Steam Deck for tests, docs or the companion app. `npm start` runs the app against a mock library.
- Have a Deck? A [launch report](https://github.com/hffgzktbcc-blip/deckstream/issues/new?template=launch-report.yml) is a contribution, including when it failed.
- Say in the PR what you tested and what you could not. Do not claim a device result you did not measure
  ([docs/LIMITS.md](docs/LIMITS.md)).
- Questions and ideas go in [Discussions](https://github.com/hffgzktbcc-blip/deckstream/discussions). Security reports go through the
  [private form](https://github.com/hffgzktbcc-blip/deckstream/security/advisories/new) ([SECURITY.md](SECURITY.md)).
- Be kind: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md).

## Working with AI agents

Agents (Claude Code, Gemini, Copilot and others) follow [AGENTS.md](AGENTS.md). They open PRs; a human merges. A PR that
used an agent is judged the same as any other: it must pass the tests and its claims must be true.
