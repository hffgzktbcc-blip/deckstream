# DeckStream delivery plan

DeckStream helps users launch PC games from their own storage on a Steam Deck. Advance one user-visible result at a time and retain the evidence used to accept it. See the [wiki](https://github.com/hffgzktbcc-blip/deckstream/wiki) for setup, architecture and evidence scope.

## Baseline

- Own-storage API, WebDAV launch path and companion are implemented. [Reviewed revision](https://github.com/hffgzktbcc-blip/deckstream/commit/4919033ffbc88cc4d817075da30bac670ead08cd)
- Hosted tests passed, including the browser flow with mock storage and the Decky archive rebuild comparison. [CI run](https://github.com/hffgzktbcc-blip/deckstream/actions/runs/40287235671)
- The v1.0.0 release predates the own-storage changes. README hardware numbers describe the predecessor runtime. [Release](https://github.com/hffgzktbcc-blip/deckstream/releases/tag/v1.0.0) · [Reported measurements](README.md#measured)

## Standing priorities

Until the first independent test result exists, work is ranked: **security, reliability, real user problems, independent testing**. Deferred by default: speculative features, refactors that fix no known problem, duplicate documentation, outreach, and any task that exists only to keep an agent busy. A deferred item needs a stated user problem or a security reason to jump the queue.

Every problem someone touches is closed only when it is **fixed, a test fails without the fix and passes with it, and `bash scripts/run_all_tests.sh` passes**. A fix without that evidence stays open. Bots and agents follow the same rule: open a PR, run the tests, report what was and was not run, never merge.

## Order of work

Owner = the maintainer (holds a Deck, repo settings and release rights). Agent = an AI session on a topic branch.

| Step | Work | Who | Done when |
|---|---|---|---|
| 1 | **First real launch (priority 1).** Fresh install on a Deck with authorized files and your own storage; keep the revision, doctor bundle and the stage where it first failed. | Owner | A redacted report on issue #9, success or failure. No release before this. |
| 2 | **Fix what step 1 finds (priority 2).** | Agent | Each fix has a failing-then-passing test linked from the report. |
| 3 | **Security follow-up.** Remove leftover refs and branches that still carry personal data (`refs/handoff/*`, `clean-*`); either call `assertPublicHttpUrl` where the site fetches a user-supplied URL or delete it; run the CodeQL results through and fix real findings. | Owner removes refs, Agent fixes code | `git ls-remote origin` shows no stray refs; CodeQL has no open high findings; the URL check is used or gone. |
| 4 | **Reliability checks in CI.** Type-check the Decky panel (#26) and run shellcheck (#27). Fix what they find. | Agent | Both run in CI and pass; any fix has a test. |
| 5 | **Startup comparison (priority 3).** Cold unprofiled, cold profiled and warm runs on one game, byte counts and milestone times. | Owner runs, Agent writes up | A table in the repo with conditions and raw numbers. |
| 6 | **Release candidate (priority 4).** Only after steps 1 to 4. Rebuild the Decky zip, refresh the manifest, publish checksums and notes that claim only what was measured. | Agent prepares, Owner publishes | Tagged pre-release; `docs/LIMITS.md` matches. |
| 7 | **Independent testing (priority 6).** Point issue #9 at the release candidate. A hand-written tester call is the owner's choice, in one community that allows it. | Owner | One report from someone outside the project. |

Kept alive, not expanded: reply to newcomers within 2 days and keep the existing `good first issue` items (#5 to #7, #22 to #25) accurate. No new outreach material, landing-page work or Projects board until step 1 is done. Dependency PRs merge only with passing tests. Keep personal names and secrets out of anything public; credit uses the login `hffgzktbcc-blip`.

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
