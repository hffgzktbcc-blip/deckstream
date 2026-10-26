#!/usr/bin/env bash
# Build the Decky Loader plugin and package it as decky/DeckStream.zip (a DeckStream/ folder with plugin.json,
# package.json, main.py and dist/index.js: the layout Decky's "Install plugin from URL/ZIP" expects).
# The site serves it at /decky/DeckStream.zip. Rebuild after changing decky/src or decky/main.py.
set -euo pipefail
cd "$(dirname "$0")/../decky"
pnpm install --frozen-lockfile --silent
rm -rf dist && pnpm run build >/dev/null
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/DeckStream/dist"
cp plugin.json package.json main.py "$stage/DeckStream/"
cp dist/index.js "$stage/DeckStream/dist/"
rm -f DeckStream.zip
(cd "$stage" && TZ=UTC find DeckStream -exec touch -d '2026-01-01 00:00:00' {} + && zip -qrX "$OLDPWD/DeckStream.zip" DeckStream)
echo "decky/DeckStream.zip: $(du -h DeckStream.zip | cut -f1)"
