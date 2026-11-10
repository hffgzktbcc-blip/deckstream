# Launch posts (drafts for the owner to publish)

Rules: post as yourself and say you are the author; lead with what it does for the reader; ask for one specific thing (a Deck owner with a NAS to run it and report); answer every reply; do not ask for stars; do not post the same text in several places on the same day. Every claim must be in [LIMITS.md](../LIMITS.md).

## Show HN
**Title:** Show HN: DeckStream, play games from your own server on a Steam Deck without copying them first
**Text:**
I built DeckStream. You pick a game on your phone; the Steam Deck mounts the game folder from your own WebDAV server (NAS, Nextcloud, anything), fetches the parts the game reads at start-up first, and starts it through Proton. It does not provide or find games: it plays files you already own on storage you control, and it contains nothing that circumvents protection.
Limits: the speed table in the README is from one Deck on home Wi-Fi with the earlier runtime; the current version has not yet been run on a clean Deck by anyone but me; a NAS on your own LAN has not been benchmarked. I am looking for a Steam Deck owner with a NAS who will run the setup and tell me which step broke. https://github.com/hffgzktbcc-blip/deckstream

## r/SteamDeck / r/linux_gaming
**Title:** I made an open-source launcher that streams games from your own NAS to the Deck on demand (looking for testers)
Short version of the Show HN text, with the README hero image. State the limits in the first paragraph. Check each subreddit's self-promotion rules first.

## Decky / Steam Deck communities
The Gaming Mode panel is a Decky plugin installed from a URL, so post in the Decky Loader community only as a plugin announcement, per their rules.

## Mastodon / Bluesky / X thread (4 posts)
1. Playing a game from your own NAS on a Steam Deck usually means copying it first. DeckStream mounts it over WebDAV and fetches what the game reads at start-up first.
2. Pick the game on your phone; the Deck does the rest through Proton. Open source, MIT.
3. Limits: measured on one Deck, earlier runtime; the current build needs outside testers.
4. If you have a Deck and a NAS: github.com/hffgzktbcc-blip/deckstream (docs/community/START_HERE.md). #SteamDeck #selfhosted

## Where else (one at a time, after the first reply cycle)
- up-for-grabs.net: add \`_data/projects/deckstream.yml\` once there are 3+ open good first issues.
- goodfirstissue.dev and the GitHub topic pages: topics are set; they pick the project up on their own.
- r/selfhosted, r/homelab: the self-hosting angle, with the storage API doc.
- awesome lists for Steam Deck or self-hosted: open a PR only once an outside user exists.

## What not to do
Do not name or link game sources, do not post screenshots of copyrighted game content or library names that suggest where files came from, do not claim a game "works" without a retained report, do not quote speeds without the conditions, and do not count stars or views as adoption.
