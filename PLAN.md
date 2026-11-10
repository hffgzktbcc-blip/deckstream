# DeckStream delivery plan

DeckStream helps users launch PC games from their own storage on a Steam Deck. Advance one user-visible result at a time and retain the evidence used to accept it. See the [wiki](https://github.com/hffgzktbcc-blip/deckstream/wiki) for setup, architecture and evidence scope.

## Baseline

- Own-storage API, WebDAV launch path and companion are implemented. [Reviewed revision](https://github.com/hffgzktbcc-blip/deckstream/commit/4919033ffbc88cc4d817075da30bac670ead08cd)
- Hosted tests passed, including the browser flow with mock storage and the Decky archive rebuild comparison. [CI run](https://github.com/hffgzktbcc-blip/deckstream/actions/runs/40287235671)
- The v1.0.0 release predates the own-storage changes. README hardware numbers describe the predecessor runtime. [Release](https://github.com/hffgzktbcc-blip/deckstream/releases/tag/v1.0.0) · [Reported measurements](README.md#measured)

## Order of work

Owner = the maintainer (only they hold a Deck, repo settings and release rights). Agent = an AI session working on a topic branch; it opens PRs and never merges. Community = outside contributors.

| Step | Work | Owner | Done when |
|---|---|---|---|
| 0 | **Tidy the public repo.** Merge the privacy PR (#32). Rewriting Git history and deleting stray `refs/handoff/*` refs is a force push: only the owner does it, after a backup clone. | Owner | A fresh clone and `git log --all` show no personal names or home paths. |
| 1 | **First real launch (priority 1).** Fresh install on a Deck with authorized files and your own storage; keep the revision, doctor bundle and the stage where it first failed. | Owner | A redacted report on issue #9, success or failure. Nothing is released before this. |
| 2 | **Fix what step 1 finds (priority 2).** Each fix lands with a test that failed before it. | Agent | Failing-then-passing test per fix, linked from the report. |
| 3 | **Review the open draft PRs in this order:** #29 (CodeQL, coverage, draft-only release workflow), #30 (daemon split), #31 (landing page). Run `bash scripts/run_all_tests.sh` on each. | Owner | Merged, or closed with a reason. #30 only after step 1, since it touches shipped daemon files and needs a Deck run. |
| 4 | **Release candidate (priority 4).** Rebuild the Decky zip, refresh the manifest, publish checksums and notes that claim only what step 1 and 5 measured. | Agent prepares, Owner publishes | Tagged pre-release with checksums; `docs/LIMITS.md` updated to match. |
| 5 | **Startup comparison (priority 3).** Cold unprofiled, cold profiled and warm runs on one game, with byte counts and milestone times. | Owner runs, Agent writes up | A table in the repo with conditions and the raw numbers. |
| 6 | **Outside feedback (priority 6).** Point issue #9 at the release candidate; post one hand-written tester call where early projects are allowed (`docs/outreach/VISIBILITY_CHECKLIST.md`). | Owner | One report from someone outside the project. |

Running in parallel, not blocking anything above:

- **Contributors.** Keep at least 3 open `good first issue` items (the weekly workflow warns below that). Reply to a newcomer within 2 days. Create the Projects board and pin the Discussions welcome post (owner, in the browser). Run the *Sync labels* workflow once.
- **Code health.** Type-check the Decky panel (#26) and run shellcheck (#27) in CI; add tests (#5 to #7, #22 to #25). Either call `assertPublicHttpUrl` where the site fetches a user-supplied URL, or delete it.
- **Dependencies.** TypeScript 7 (#16) needs its own check; #11 needs a workflow run to prove it.
- **Hygiene.** No personal names or secrets in anything public, and use the login `hffgzktbcc-blip` for credit. Use `scripts/handoff.sh` so work survives a session ending.

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
