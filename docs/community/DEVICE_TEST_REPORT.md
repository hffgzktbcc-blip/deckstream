# Steam Deck test report

Copy this into a GitHub issue. Share only redacted output and use software and storage you are authorized to access.

## First launch from a clean setup

1. Check out the exact DeckStream commit you want to test. Deploy the companion and install the daemon from that same revision using the [setup guide](../../README.md#setup). Record both revisions if they differ.
2. Start with one authorized PC game folder and a fresh test setup. Follow the [storage guide](../STORAGE_API.md) to expose the folder through the library API and WebDAV. Use default launch options and record the starting cache state.
3. On the Steam Deck, run `python3 ~/.config/deckstream/doctor.py` and note any failed checks. Open the companion, confirm the folder appears, then queue it to the Deck.
4. Note whether the Deck received the job, mounted the folder, started Proton, reached the title screen and accepted input. Stop at the first failing step and capture the relevant error. A partial run is useful.

## Report template

| Field | Observation |
|---|---|
| Site and daemon commit/version | |
| Deck model, SteamOS, Proton and Decky version if relevant | |
| Storage type and access path (LAN or remote, no hostname) | |
| Ready executable folder or install job | |
| Network conditions and approximate throughput | |
| Empty, cold profiled, or warm cache | |
| Optional prefill, early launch or hedge flags | |
| Exact steps from queueing to the observed result | |
| Time to Proton start, title screen, and usable controls, if observed | |
| Bytes fetched and the source of that measurement | |
| First failing stage or last successful stage | |
| Expected and actual behavior | |

Collect service status, a doctor report and relevant log excerpts. Review any bundle before sharing and remove passwords, tokens, private URLs and identifying paths. [Testing and Reproduction](https://github.com/hffgzktbcc-blip/deckstream/wiki/Testing-and-Reproduction)
