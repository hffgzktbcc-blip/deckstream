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
