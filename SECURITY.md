# Security policy

## Reporting a vulnerability

Use the repository's [private vulnerability reporting form](https://github.com/hffgzktbcc-blip/deckstream/security/advisories/new).
Please do not open a public issue for a suspected vulnerability. Include the affected revision, component, conditions
and a minimal reproduction. Use mock values or redacted excerpts for sensitive configuration.

DeckStream is maintained by volunteers, so responses are best effort. We aim to acknowledge a report within a week,
agree a fix and disclosure date with you, and credit you in the advisory unless you prefer otherwise.

## Supported versions

Only the latest release and the `main` branch receive security fixes.

## Scope

In scope: the site API (`api/`), the reference storage server (`scripts/storage_server.js`), the daemon (`daemon/`)
and the Decky panel (`decky/`). Report anything involving:

- the shared queue credential (`DECKSTREAM_SECRET`) or storage credentials;
- the storage relay, download host allowlist, or any URL the server fetches or posts to (server-side request forgery);
- job authorization, push subscriptions and notification content;
- update and install integrity (the daemon manifest, Decky zip, release checksums).

Use public issues for routine setup and launch failures. Third-party services (storage hosts, push services, ntfy)
should be reported to their own maintainers.

## What we do

- Dependabot checks npm packages and GitHub Actions weekly (`.github/dependabot.yml`).
- CodeQL scans JavaScript, Python and workflows on every PR and weekly (`.github/workflows/codeql.yml`).
- URLs the server sends requests to are limited to an allowlist (downloads) or to https on public addresses (push
  subscription endpoints), checked by `api/_lib.js` and tested in `scripts/test_api.js` and `scripts/test_push.js`.
- Release files come with SHA-256 checksums (`docs/RELEASING.md`).
