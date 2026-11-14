# Try DeckStream: help with one real-world result

**No PR required.** We particularly need independent Steam Deck + NAS/WebDAV results, including failures. Use games you own and storage you control; never share game files or credentials.

## Pick a path

| What you have | Do this | Report |
|---|---|---|
| Node 22, no Steam Deck | Run `npm start` and explore the mock-library companion at http://127.0.0.1:8765 | [Discussions](https://github.com/hffgzktbcc-blip/deckstream/discussions) |
| Steam Deck and NAS/WebDAV | Follow the [real-device checklist](DEVICE_TEST_REPORT.md), using the exact same source revision on the site and daemon | [Device tester call #9](https://github.com/hffgzktbcc-blip/deckstream/issues/9) |
| Own a NAS/WebDAV setup | Document your provider's setup, with secrets removed | [NAS guide #10](https://github.com/hffgzktbcc-blip/deckstream/issues/10) |
| Python or JavaScript | Pick a small unit-test task such as [#25](https://github.com/hffgzktbcc-blip/deckstream/issues/25) or [#22](https://github.com/hffgzktbcc-blip/deckstream/issues/22) | Focused PR |

## What to observe on a real Deck

1. Record the exact site/daemon commit, Deck model, SteamOS and storage type (LAN or remote).
2. Run the [doctor checks](../../README.md#setup); note any failures.
3. Queue one authorized game and record the last successful stage: received job → mounted → Proton started → title screen → controls.
4. If possible, compare **cold cache on-demand launch** against **full local transfer** on the same network, with measured times and transferred bytes. Do not estimate or invent missing numbers.
5. Share the result using the [device report template](DEVICE_TEST_REPORT.md) or [the launch-report form](https://github.com/hffgzktbcc-blip/deckstream/issues/new?template=launch-report.yml).

A partial or failed test helps. Redact passwords, tokens, private URLs and personal paths before uploading logs.

## Honest status

Mock-storage automation exists, but the current own-storage flow has **not** been independently verified on a clean Steam Deck, and LAN NAS performance has not been benchmarked. Older predecessor measurements are not proof for this revision. See [limits](../LIMITS.md).

**Maintainer commitment:** constructive feedback and small contributions are welcome; no guaranteed response time.
