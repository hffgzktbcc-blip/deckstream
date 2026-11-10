# Start here: five steps for a first-time reader

You only need a browser for steps 1 to 4.

1. **What it is.** DeckStream plays PC games you already own, stored on your own server (a NAS,
   Nextcloud, anything that speaks WebDAV), on a Steam Deck. Read the [vision](../VISION.md).
   It does not provide or find games.
2. **What is proven.** [LIMITS.md](../LIMITS.md) gives, claim by claim, what has been tested and
   what has not. The README speed table is from one Deck and the predecessor runtime.
3. **Try the app.** `npm start` runs the companion app and the real `api/*.js` against a mock
   library at http://127.0.0.1:8765 (Node 22).
4. **Pick a first task.** Open
   [good first issues](https://github.com/hffgzktbcc-blip/deckstream/issues?q=is%3Aopen+label%3A%22good+first+issue%22).
   Several are tests or docs. [CONTRIBUTING.md](../../CONTRIBUTING.md) explains how to send a change.
5. **Report back.** Ask in [Discussions](https://github.com/hffgzktbcc-blip/deckstream/discussions). If you ran it on a Deck, use the
   [test report](DEVICE_TEST_REPORT.md), including when it failed. A failure with the stage named is
   more useful than a success with no details.
