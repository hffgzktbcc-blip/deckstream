# Working on DeckStream

## Family-wide source of truth

Before planning any work, read the **canonical [Ounce Family Nov 2026–Nov 2027 roadmap](https://github.com/hffgzktbcc-blip/ounce-core/blob/main/docs/roadmap/OUNCE_FAMILY_1_YEAR_ROADMAP.md)**. It sets family-wide priorities for ounce-core, ounce-hb, ounce-runtime and DeckStream; this repo's `PLAN.md` remains authoritative for **DeckStream-specific acceptance gates**, particularly an actual own-storage Steam Deck launch report. Do not mistake cross-repo roadmap aspiration for demonstrated DeckStream compatibility.

Cross-check live main, merged/open PRs and issues, GitHub Actions results, and published releases before choosing work. Never recreate a completed task based on dated plans, cached handoffs or stale checklists. Record verified completions in the *active* roadmap or issue with evidence instead of adding another competing handoff. Current owner instructions set authorized scope. Keep licensing and provenance boundaries intact: the GPL runtime is an external subprocess/JSON-contract integration, not a source of code to copy into MIT DeckStream.

Build useful end-to-end results. The current request sets scope; `PLAN.md` is the prioritized
backlog, not a permission system.

- Inspect relevant code and existing work, then act, on a topic branch. Preserve unrelated changes
  and stage only your own paths.
- Keep process proportional: no mandatory design approvals, extra plan documents or agent swarms
  for routine work.
- Run focused checks while editing; before a PR run `bash scripts/run_all_tests.sh` (or say which
  parts you could not run). If you change a shipped `daemon/` file, run
  `python3 scripts/make_manifest.py`. If you change `decky/`, run `bash scripts/build_decky.sh`.
- A PR opens with ordinary Git/GitHub. Never merge for the owner unless told to.
- Close a task with a reproducible result: a command, a failing-then-passing test, a measured
  number with its conditions. Unit tests guard code; they do not prove a Deck launch works.
- Keep the maturity labels apart: planned, implemented, automated, measured on a device,
  independently repeated. Never write a compatibility or speed claim that has no retained
  evidence. See `docs/LIMITS.md`.
- Never commit keys, tokens, private hosts, game files or anything that circumvents DRM. Do not
  name or link where to get games anywhere; `scripts/test_clean_tree.py` enforces it.

Usage can run out at any moment, so make state recoverable:
- At session start: `scripts/handoff.sh resume`, `scripts/handoff.sh journal`, then
  `scripts/handoff.sh claim <task>` (claims expire after 4h).
- `scripts/handoff.sh watch --push &` snapshots unsaved work every minute to `refs/handoff/*`.
- Record what the next agent must not rediscover: `scripts/handoff.sh log "one sentence"`.
- Done or switching: `scripts/handoff.sh release <task>`. Phone/hub workflow: `docs/AGENT_HUB.md`.

Proceed on routine, reversible work. Escalate credentials, spending, destructive history changes
and anything with material legal tradeoffs.
