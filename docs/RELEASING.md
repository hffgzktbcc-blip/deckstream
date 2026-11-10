# Releasing

DeckStream uses [semantic versioning](https://semver.org). The site (`package.json`) and the Decky panel
(`decky/package.json`) share one version; the daemon's `daemon/manifest.json` carries a content hash, not a version.
`python3 scripts/check_version.py` (part of `scripts/run_all_tests.sh`) keeps them and `CHANGELOG.md` consistent.

## While developing

Add a line under `## Unreleased` in `CHANGELOG.md` for anything a user or contributor would notice.

## Cutting a release

1. **Run it on a real Steam Deck first.** A release needs a launch on a device, recorded with its conditions
   (see `docs/LIMITS.md`). Unit tests and CI do not count as a Deck run.
2. Move the `## Unreleased` entries into a new `## X.Y.Z` section (keep an empty `## Unreleased` above it). Note what
   was measured on a device and what was not.
3. Set `"version": "X.Y.Z"` in `package.json` and `decky/package.json`, then run `bash scripts/build_decky.sh`,
   `python3 scripts/make_manifest.py` and `bash scripts/run_all_tests.sh`. Merge that PR.
4. Tag the merge commit: `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. The `release` workflow checks the tag against the version and changelog, builds `DeckStream-decky-X.Y.Z.zip` and
   `deckstream-daemon-X.Y.Z.tar.gz`, writes `SHA256SUMS`, and opens a **draft** release with the changelog section as
   notes. It never publishes.
6. Review the draft (files, checksums, notes), then publish it by hand.

To try the build without tagging, run the `release` workflow manually from the Actions tab; the files appear as a run
artifact and nothing is published.

Users can verify a download with `sha256sum -c SHA256SUMS`.
