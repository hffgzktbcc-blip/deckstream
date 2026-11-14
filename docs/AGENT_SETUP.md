# Agent setup (Claude Code, Antigravity, Copilot and others)

One rule source: root [AGENTS.md](../AGENTS.md). Backlog: [PLAN.md](../PLAN.md).

| Agent | Reads | Branch prefix |
|---|---|---|
| Claude Code | `CLAUDE.md` (imports AGENTS.md) | `claude/` |
| Any other agent | `AGENTS.md` | its own name, e.g. `agentname/` |
| Antigravity / Gemini | `AGENTS.md`, `GEMINI.md` | `antigravity/` |
| Copilot | `.github/copilot-instructions.md` | `copilot/` |

## Environment
Python 3.12, Node 22, pnpm 10 (only to rebuild the Decky plugin). `npm test` runs the whole suite;
the browser test needs `npm i --no-save playwright && npx playwright install chromium`.

## Coordination
- One agent per topic branch; stage only your own paths; never rewrite another agent's branch.
- Claim a task with `scripts/handoff.sh claim <task>`; name the branch after it.
- Open a PR with ordinary Git/GitHub; the owner merges.
