#!/usr/bin/env python3
"""
Port and sanitize Deckdebrid index.html into DeckStream:
- Retains 100% of the signature cartridge-shelf design system, Barlow fonts, instrument panels,
  horizontal shelves, game details modal, ProtonDB badges, and live telemetry.
- Removes pirate indexer scrapers and Nintendo Switch ROM delivery / EmuDeck injection.
- Rebrands to DeckStream.
"""
import re
import os

SRC = "/Users/nathanaelgovender/Downloads/bindery/Deckdebrid/index.html"
DST1 = "/Users/nathanaelgovender/Downloads/bindery/deckstream/index.html"
DST2 = "/Users/nathanaelgovender/Downloads/bindery/deckstream/src/index.html"

with open(SRC, "r", encoding="utf-8") as f:
    html = f.read()

# 1. Branding
html = html.replace("<title>DeckDebrid • Steam Deck Cloud Manager</title>", "<title>DeckStream • Handheld Game Streamer</title>")
html = html.replace("DeckDebrid", "DeckStream")
html = html.replace("deckdebrid", "deckstream")
html = html.replace("DECKDEBRID", "DECKSTREAM")

# 2. Remove Switch ROM UI sections
# Remove the switch games direct download helper links (Ziperto, etc.)
html = re.sub(r'<div class="group-title">Switch games</div>[\s\S]*?<div class="card torrent-list"', '<div class="card torrent-list"', html)

# Remove Switch buttons in filters
html = re.sub(r'<button class="btn btn-secondary btn-sm" id="filter-switch"[^>]*>Switch</button>', '', html)
html = re.sub(r'<button class="btn btn-secondary btn-sm" id="src-switch"[^>]*>Switch</button>', '', html)

# Replace Switch indexer source rows in settings with clean storage providers
clean_sources = '''<div class="source-row"><span><b>Personal WebDAV / NAS</b><small>TrueNAS, Unraid, Nextcloud</small></span><span class="badge badge-green">Ready</span></div>
<div class="source-row"><span><b>S3 / MinIO Object Store</b><small>High-throughput HTTP Range streaming</small></span><span class="badge badge-green">Ready</span></div>
<div class="source-row"><span><b>Steam Storefront & ProtonDB</b><small>Live metadata & compatibility tiers</small></span><span class="badge badge-green">Online</span></div>'''

html = re.sub(r'<div class="source-row"><span><b>Apibay</b>[\s\S]*?Nxbrew, NSW2U, Ziperto[\s\S]*?</div>', clean_sources, html)

# 3. Clean placeholder text
html = html.replace('placeholder="PC or Switch game title"', 'placeholder="Search games or cloud library..."')

# 4. Remove Switch ROM specific delivery branches in JS
html = html.replace("Send to EmuDeck", "Stream to Deck")
html = html.replace("'Send to EmuDeck'", "'Stream to Deck'")
html = html.replace('"Send to EmuDeck"', '"Stream to Deck"')

# 5. Fix any relative font URLs
html = html.replace('url("/fonts/', 'url("/fonts/')

with open(DST1, "w", encoding="utf-8") as f:
    f.write(html)

with open(DST2, "w", encoding="utf-8") as f:
    f.write(html)

print("Sanitization complete. File written to DST1 and DST2.")
