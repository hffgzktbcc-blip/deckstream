# Contributing to DeckStream

We welcome contributions from the handheld gaming and Linux communities!

## Scope and Principles

DeckStream is dedicated to building high-performance storage and runtime software for Linux handhelds. To protect the project and community:

1. **Storage Agnostic**: All features must operate over standard network protocols (WebDAV, SFTP, S3, SMB, HTTP) without hardcoding private hosts or providers.
2. **Zero Circumvention**: Pull requests containing DRM circumvention, game cracking, or proprietary console ROM runners will be rejected.
3. **No Piracy Indexers**: The core repository must remain completely clean of torrent search engines or warez scrapers.
4. **Performance First**: Handheld devices are constrained by battery and memory bandwidth. All background helpers must respect sleep locks and battery thresholds.

## Set up in five minutes

You need Python 3.12 and Node 22. No Steam Deck is needed for anything below.

```bash
git clone https://github.com/<you>/deckstream && cd deckstream
npm start                     # companion app + real api/*.js + mock library at http://127.0.0.1:8765
npm test                      # the whole suite
npm i --no-save playwright && npx playwright install chromium   # only for the browser test
```

One test is enough while you work, for example `python3 scripts/test_saves.py` or `node scripts/test_api.js`.
CI runs the whole suite on every pull request.

## Ways to help

| You have | You can |
|---|---|
| A browser | Fix docs, try the companion app with `npm start`, review a PR for clarity |
| Python or Node | Pick a [good first issue](https://github.com/hffgzktbcc-blip/deckstream/issues?q=is%3Aopen+label%3A%22good+first+issue%22) (most are unit tests) or a [help wanted](https://github.com/hffgzktbcc-blip/deckstream/issues?q=is%3Aopen+label%3A%22help+wanted%22) issue |
| A NAS or WebDAV server | Write a setup guide for it ([#10](https://github.com/hffgzktbcc-blip/deckstream/issues/10)) |
| A Steam Deck | Send a [launch report](https://github.com/hffgzktbcc-blip/deckstream/issues/new?template=launch-report.yml), success or failure ([#9](https://github.com/hffgzktbcc-blip/deckstream/issues/9)) |

Labels: `good first issue` means scoped, one or two files, no Deck. `help wanted` is open to anyone.
`area: …` says which part of the tree it touches. `claimed` means someone said they are on it.

## Where the daemon code lives

`daemon/deckstream-daemon.py` is the entry point: the main poll loop and the command line. The rest is in parts
next to it, each importing only from the parts above it in this list:

| Part | What it does |
|---|---|
| `ds_core.py` | Settings, paths, download-host checks, job state, the config file |
| `ds_steam.py` | Steam's binary VDF format, finding a game's executable, launcher scripts |
| `ds_shortcuts.py` | Non-Steam shortcuts (pending, live, `shortcuts.vdf`) and artwork |
| `ds_stream.py` | Stream sessions, running cache fills, charger checks |
| `ds_cache.py` | Prestage, start-up profile backup, warm-up, Deck mode, eviction, removing a game |
| `ds_update.py` | Self-update and the Decky plugin update |
| `ds_status.py` | What the phone and the Decky panel show |
| `ds_janitor.py` | The background janitor and warm-up passes |
| `ds_install.py` | Downloading, extracting and installing games |

A new part must also be added to `_PARTS` in `deckstream-daemon.py` and to `SHIPPED` in `scripts/make_manifest.py`;
`scripts/test_daemon_parts.py` checks both.

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
- Stuck? See [SUPPORT.md](SUPPORT.md). Questions and ideas go in [Discussions](https://github.com/hffgzktbcc-blip/deckstream/discussions). Security reports go through the
  [private form](https://github.com/hffgzktbcc-blip/deckstream/security/advisories/new) ([SECURITY.md](SECURITY.md)).
- Be kind: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Roadmap: [docs/ROADMAP.md](docs/ROADMAP.md).

## Working with AI agents

Agents (Claude Code, Gemini, Copilot and others) follow [AGENTS.md](AGENTS.md). They open PRs; a human merges. A PR that
used an agent is judged the same as any other: it must pass the tests and its claims must be true.
