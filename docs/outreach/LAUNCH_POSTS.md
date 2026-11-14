# DeckStream launch and tester recruitment — November 2026

**Public positioning:** **DeckStream — Steam Deck NAS + WebDAV Game Launcher**

DeckStream is an **experimental, open-source, self-hosted file-on-demand game launcher**, not remote video streaming. PC games run locally on the Deck via Proton. DeckStream does not supply game files or defeat DRM. See the [project homepage](https://hffgzktbcc-blip.github.io/deckstream/), [setup](../../README.md#setup), [evidence limits](../LIMITS.md), and **[tester call #9](https://github.com/hffgzktbcc-blip/deckstream/issues/9)**.

**Current readiness caveat:** API/browser tests pass with mock storage. The current own-storage revision has **not** been independently verified on a clean Deck. Published device timings were recorded with a predecessor runtime and must not be represented as current compatibility data. The published v1.0.0 release predates own-storage. Do not use “works with game X” or performance figures as unqualified promises.

## Primary conversion path

A curious visitor should see: **what it does → what has been verified → how to try it → one link to share findings**.

- **Steam Deck + storage testers:** [Issue #9 — call for testers](https://github.com/hffgzktbcc-blip/deckstream/issues/9) → [launch report form](https://github.com/hffgzktbcc-blip/deckstream/issues/new?template=launch-report.yml).
- **Developers without a Deck:** [run the mock-library app](../../README.md#development) → [beginner issues](https://github.com/hffgzktbcc-blip/deckstream/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22).
- **Questions:** [Discussions](https://github.com/hffgzktbcc-blip/deckstream/discussions), with one welcome topic to be posted/pinned by the owner.

## Outreach order and rules (verify again immediately before posting)

| Priority | Community | Why and guardrail | Action |
|---|---|---|---|
| 1 | [r/alphaandbetausers](https://www.reddit.com/r/alphaandbetausers/) | Explicit early testers; confirm current subreddit rules and use a tester request, not a launch-as-production claim | Publish the beta-tester draft below as author |
| 2 | [Steam Hardware Lemmy community](https://lemmy.world/c/steamdeck%40sopuli.xyz) | Deck-focused; community has rules against piracy, accepts Deck-relevant technical discussion; use `[Deck]` title prefix | Publish Steam Deck draft, adapt to their format |
| 3 | [r/SteamDeckHQ](https://www.reddit.com/r/SteamDeckHQ/) or [r/SteamDeck](https://www.reddit.com/r/SteamDeck/) | Reach device owners; check current self-promotion limits and whether mod permission is needed | One thoughtful launch/tester post only if permitted |
| 4 | [r/coolgithubprojects](https://www.reddit.com/r/coolgithubprojects/) | Interested in open-source repositories; check current rules | Share project and beginner issues with a specific ask |
| Later | [r/selfhosted](https://www.reddit.com/r/selfhosted/) | Community guidance expects promoted apps to be production-ready and documented. **Not appropriate as a production launch yet** | Wait for an independently recorded own-storage test and properly qualified release, or ask mods if an early tester request fits |
| Later | [Show HN guidelines](https://news.ycombinator.com/showhn.html) | Only when visitors can actually try the project. **HN says not to use AI-written submission text and not to automate posting** | Owner writes their own submission with working demo and stays to answer comments |
| Avoid for now | [r/homelab](https://www.reddit.com/r/homelab/) | Moderators adopted tighter 2026 controls for software/AI-generated promotion | No unsolicited mass cross-post; seek moderator approval first |

No bots, automated DMs, fake testimonials, upvote requests or repeated posting across communities. Make an original post from the maintainer account, disclose that this is your project and acknowledge AI assistance where the community requests it. Don't state that a post is live until its public URL is recorded.

## Ready-to-adapt: early beta testers (r/alphaandbetausers)

**Title:** Looking for Steam Deck + NAS/WebDAV testers for my open-source launcher (early build)

**Body:**

I’m building DeckStream, an open-source project for people with a Steam Deck and PC game files on storage they control.

The idea: pick a game through a phone-friendly companion. The Deck mounts the game folder over WebDAV, fetches files on demand, and launches locally through Proton. This is not a cloud gaming/video streaming service, and the project doesn't provide games or bypass DRM.

I'm looking for a small group of testers with different storage setups (NAS, Nextcloud, other WebDAV servers) to follow the setup guide and tell me **the first step that works or breaks**.

I'm keeping the claim small: the current own-storage flow passes automated tests against mock storage, but I still need a clean Deck test and independent reports. The older timings in the README are from a predecessor runtime, not a performance guarantee for this build.

If you're interested, here's the tester checklist and report link: https://github.com/hffgzktbcc-blip/deckstream/issues/9

A failure report with your SteamOS version and where the process stopped is genuinely useful. No special access or paid plan is required.

## Ready-to-adapt: Steam Deck audience

**Title:** [Deck] Building a self-hosted NAS/WebDAV game launcher for Steam Deck — looking for real-world testers

**Body:**

I've been working on DeckStream, a small open-source experiment: choose a PC game folder from your own NAS/WebDAV storage on your phone, then have the Deck mount it and run the game locally through Proton rather than copying the entire folder first.

To be clear, it's **file-on-demand launching**, not remote gameplay video streaming. It doesn't include games or work around DRM. I don't have a validated compatibility list for this build.

The phone companion, queue and Deck launch pipeline are implemented and the current flow has automated tests with a mock storage library. But I haven't yet got a verified clean-device setup on the current revision from someone outside the project. That's the gap I'd like to close, including failures.

If you've got a Deck and a NAS or Nextcloud/WebDAV server, could you try the documented flow and report the first stage where it succeeds or fails?

GitHub: https://github.com/hffgzktbcc-blip/deckstream
Testing instructions: https://github.com/hffgzktbcc-blip/deckstream/issues/9

Happy to answer technical questions and fix issues that people can reproduce.

## Ready-to-adapt: GitHub/open-source audience

**Title:** DeckStream: a self-hosted Steam Deck NAS/WebDAV launcher seeking testers and beginner contributors

**Body:**

DeckStream is an MIT-licensed, open-source experiment that queues and launches authorized PC game files from your own WebDAV storage on Steam Deck through Proton.

I've documented the architecture and the current limitations; this is not a compatibility announcement. The pipeline passes mock-storage CI, but clean Deck results on this revision are still needed.

Developers without a Deck can help with small, scoped tests and documentation tasks. If you have a Deck, a NAS or Nextcloud setup, even a failed first run is a useful report.

- Repo: https://github.com/hffgzktbcc-blip/deckstream
- Beginner issues: https://github.com/hffgzktbcc-blip/deckstream/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22
- Steam Deck testers: https://github.com/hffgzktbcc-blip/deckstream/issues/9

## Ready-to-adapt: short social announcement

I've been building **DeckStream**: an open-source Steam Deck launcher for PC game files on your own NAS/WebDAV server. Queue from your phone; the Deck mounts files on demand and runs locally via Proton. Experimental current build; looking for real setup reports, including failures. https://github.com/hffgzktbcc-blip/deckstream/issues/9 #SteamDeck #SelfHosted

## GitHub Discussions welcome prompt (owner to post and pin)

**Title:** Welcome to DeckStream — what storage are you running?

**Body:** DeckStream is a self-hosted, early-stage Steam Deck NAS/WebDAV launcher. The current own-storage path has automated mock-storage tests but still needs clean-device verification. Tell us your storage type (NAS/Nextcloud/other WebDAV, local or remote), Deck model and what you'd most like to test. Please don't post keys or private URLs. If you try the software, file a [device report](https://github.com/hffgzktbcc-blip/deckstream/issues/new?template=launch-report.yml), even if it fails. Developers without a Deck can take a [good first issue](https://github.com/hffgzktbcc-blip/deckstream/issues?q=is%3Aopen+is%3Aissue+label%3A%22good+first+issue%22).

## Tracking

For each external post, record the **actual public URL**, date, community, questions received and tester reports linked back to [#9](https://github.com/hffgzktbcc-blip/deckstream/issues/9). Only count a tester when a report names an environment and outcome; count an outside contribution only when an unaffiliated account contributes a substantive issue, reproduction or PR.

### Status, 10 November 2026
- Drafts and GitHub paths ready; outreach has **not** been posted externally by the repository automation.
- An updated tester call is live at [issue #9](https://github.com/hffgzktbcc-blip/deckstream/issues/9).
- Branding artwork for GitHub's social preview requires upload in **Repository Settings → General → Social preview**; public APIs in this workflow do not configure that setting.
