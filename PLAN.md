# DeckStream delivery plan

DeckStream helps users launch PC games from their own storage on a Steam Deck. Advance one user-visible result at a time and retain the evidence used to accept it. See the [wiki](https://github.com/hffgzktbcc-blip/deckstream/wiki) for setup, architecture and evidence scope.

## Baseline

- Own-storage API, WebDAV launch path and companion are implemented. [Reviewed revision](https://github.com/hffgzktbcc-blip/deckstream/commit/4919033ffbc88cc4d817075da30bac670ead08cd)
- Hosted tests passed, including the browser flow with mock storage and the Decky archive rebuild comparison. [CI run](https://github.com/hffgzktbcc-blip/deckstream/actions/runs/40287235671)
- The v1.0.0 release predates the own-storage changes. README hardware numbers describe the predecessor runtime. [Release](https://github.com/hffgzktbcc-blip/deckstream/releases/tag/v1.0.0) · [Reported measurements](README.md#measured)

## Priorities and acceptance

| Priority | Result | Evidence to close |
|---|---|---|
| 1 | Current own-storage launch works from a clean setup | Record site and daemon revisions, setup and first queue job, and actual controls on a Steam Deck using authorized files. Keep the failure stage if blocked. |
| 2 | Failure reports lead to reproducible fixes | Collect one real failure, keep the redacted report, reproduce the relevant stage, and add a regression with an unchanged expectation. |
| 3 | Compare startup under controlled conditions | Compare cold unprofiled, cold profiled and warm runs. Retain byte counts and milestone times. Qualify optional fill and hedge paths separately on matched workloads. |
| 4 | Qualify a release for the own-storage flow | Rebuild the Decky zip, check daemon integrity, confirm release assets and notices, and test the exact candidate with the supported setup. Publish scope and checksums alongside the release. |
| 5 | Make the project easy to discover and try | Publish a landing page with distinct routes for the companion demo, self-hosting, evidence, and contribution. Verify the published routes and identify demo data. |
| 6 | Get outside feedback | Retain a report from an unaffiliated tester with the revision, environment and outcome. Scope support claims to tested configurations. |

Use [the test report template](docs/community/DEVICE_TEST_REPORT.md). Keep planned, implemented, automated, measured-on-device and independently repeated outcomes separate.
