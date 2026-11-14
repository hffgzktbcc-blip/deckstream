# Storage API

DeckStream does not host or find games. It reads a library of game folders you already own from a storage server you
run, through a small HTTP API, and the Deck streams the same folders over WebDAV.

```
phone / browser ──> your Vercel site ──/api/storage──> STORAGE_API_URL   (library, file links)
Steam Deck ──────────────────────────────rclone──────> WebDAV            (the game files)
```

## Reference server

`scripts/storage_server.js` implements the API with no dependencies. Every folder directly inside the games directory
is one game.

```bash
STORAGE_KEY=change-me node scripts/storage_server.js --dir /srv/games --port 8780 --public-url http://nas.lan:8780
rclone serve webdav /srv/games --addr :8781 --user deckstream --pass change-me
```

Set on the site (Vercel project settings, Environment Variables):

| Variable | Value |
| :--- | :--- |
| `STORAGE_API_URL` | `http://nas.lan:8780/v1/api` (must be reachable from Vercel: a public host or a tunnel) |
| `ALLOWED_DOWNLOAD_HOSTS` | optional, comma-separated extra hosts for file links (https only) |

On the Deck the installer asks for the WebDAV address and the storage key, and saves them in
`~/.config/deckstream/storage.conf` (`STORAGE_WEBDAV_URL`, `STORAGE_WEBDAV_USER`, `STORAGE_KEY`).

## Contract

Every request carries `Authorization: Bearer <storage key>`. Responses are JSON `{ "success": true, "data": ... }`, or
`{ "success": false, "error": "..." }` with a 4xx/5xx status. The site's relay forwards only these four endpoints.

| Endpoint | Returns |
| :--- | :--- |
| `GET games/mylist` | every game: `[{ id, name, size, files: [{ id, name, short_name, size }], cached, download_finished, progress, download_state, created_at, updated_at }]` |
| `GET games/mylist?id=ID` | one game, or 404 |
| `GET games/requestdl?id=ID[&file_id=FID]` | a URL the Deck can `GET` (with Range) for one file; the largest file when `file_id` is left out |
| `POST games/mount` `{ "path": "Folder name" }` | `{ id, name }` of that folder, or 400/404 |
| `GET user/me` | anything, with 200 when the key is right |

- `id` is a stable integer per game (1 to 12 digits). The reference server derives it from the folder name.
- `files[].name` is the path inside the WebDAV root, starting with the game's folder (`Hades II/Ship/Hades2.exe`).
- `cached: false` means the game is not ready yet (still syncing); the app shows its `progress` (0 to 1).
- File links must be on the `STORAGE_API_URL` host (http is allowed there) or on an `ALLOWED_DOWNLOAD_HOSTS` host (https).

## What streams

The planner (`api/_plan.js`) reads the file list. A ready folder with the game's own `.exe` streams. Installers
(`setup*.exe` plus `.bin` parts) and archives are installed on the Deck once (`cloud-install.sh`). Disc images and media
are not sent.
