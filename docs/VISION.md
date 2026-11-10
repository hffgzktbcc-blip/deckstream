# Vision

**Play the PC games you already own, from the server you already run, on a Steam Deck, without
copying the whole library across first. Show the evidence when it works and say plainly when it does not.**

## Who it is for
- Owners of a Steam Deck (or another SteamOS handheld) with a NAS, Nextcloud or any WebDAV-capable
  server holding games they have the right to use.
- Self-hosters who want a phone-friendly queue instead of file copying.
- Contributors who like storage, Proton and handheld-performance problems.

## What it will not be
- A game source. It does not provide, host, index or search for games.
- A DRM or protection workaround.
- A claim machine: a launch that works on one Deck is one launch on one Deck.

## Steps (see [PLAN.md](../PLAN.md) and [ROADMAP.md](ROADMAP.md))
1. A clean setup launches a game from your own storage, with the failing stage recorded when it does not.
2. A real failure report turns into a reproduced fix and a regression test.
3. Cold, profiled and warm starts are compared under stated conditions.
4. A release is qualified for the own-storage flow, with checksums.
5. Outsiders can find it, try it and report back.

Every public number states its conditions and whether it was measured on a device. See
[LIMITS.md](LIMITS.md).
