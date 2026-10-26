// DeckStream in Gaming Mode's quick-access menu, as close to the phone app as the panel allows: the Deck daemon's status,
// the install queue (progress, waiting jobs, recent results with the failure text, Cancel and Try again), the game being
// prepared, the streamed games with Play, installed games, drive space with the full-card warning, Free up space (Clear
// cache, Delete), Refresh, your storage library (the phone's groups and Stream / Install to Deck), and Search with the
// phone's one-tap buttons (the planner reads a game's files: a game folder streams, anything else installs).
// Play opens the game's own Steam shortcut (made by the daemon), the same as the library.
import { ButtonItem, Field, Navigation, PanelSection, PanelSectionRow, ProgressBarWithInfo, TextField, ToggleField, staticClasses } from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { Fragment, useEffect, useRef, useState } from "react";
import { FaCloudDownloadAlt } from "react-icons/fa";

type Game = { slug: string; name: string; cache_gb: number; appid: number | null };
type Space = { used_gb: number; total_gb: number; free_gb: number } | null;
type ActiveGame = {
  slug?: string; name?: string; phase?: "preparing" | "ready" | "starting" | "playing"; phase_label?: string;
  progress?: number; bytes_done?: number; bytes_total?: number; eta_s?: number;
} | null;   // daemon live_activity(): the same status line the phone's Home card shows
type LastLaunch = {
  slug: string; stage: "preparing" | "filling" | "starting" | "playing" | "ended"; ago_s?: number | null;
  fill_seconds?: number; play_to_launch_seconds?: number; exit_seconds?: number; exit_text?: string;
} | null;
type Status = {
  installed: boolean;
  error?: string;
  running?: boolean | null;
  version?: string;
  ac_online?: boolean | null;
  battery_pct?: number | null;
  active_game?: ActiveGame;
  last_launch?: LastLaunch;
  games?: Game[];
  installed_games?: { name: string; path?: string; slug?: string; skipped?: { folder: string; bytes: number } }[];
  space?: { internal: Space; card: Space };
  warmup?: boolean;
  deck_mode?: Record<string, boolean>;
  space_warning?: { drive: string; pct: number; free_gb?: number; name?: string; gb?: number } | null;
};
type Job = { id: string; name: string; mode: string; status?: string; progress?: number; message?: string; size_gb?: number | null; finished?: number | null };
type Queue = {
  ok: boolean; why?: string; current?: Job | null; jobs?: Job[]; history?: Job[];
  error?: { message: string; can_retry: boolean; job_id: string } | null;
};

const getStatus = callable<[refresh: boolean], Status>("get_status");
const getQueue = callable<[], Queue>("get_queue");
const setWarmup = callable<[on: boolean], { ok: boolean; on: boolean; text: string }>("set_warmup");
const setDeckMode = callable<[slug: string, on: boolean], { ok: boolean; on: boolean; text: string }>("set_deck_mode");
// Per game, the phone's Deck tab uses the same words: 30 fps and low graphics (Unreal games without a settings file yet).
const DECK_MODE_LABEL = "Deck mode: 30 fps, low graphics";
// The phone's Settings → On the Deck card uses the same words.
const WARMUP_HELP = "While the Deck is charging and idle, it fills your recently played stream games so they start fast.";
// The phone's confirm() words for Clear cache and Delete.
const CLEAR_ASK = "It stays in your library and streams again the next time you play it (the first start is slow again).";
const DELETE_ASK = "Its files, launcher and Steam tile go. Your saves stay. You can send it again later.";
const queueAction = callable<[action: string, jobId: string], { ok: boolean; text: string }>("queue_action");
type Result = { id: string; title: string; group: string; size_gb: number | null; sendable: boolean };
const searchGames = callable<[query: string], { ok: boolean; why?: string; results?: Result[] }>("search");
const getLibrary = callable<[], { ok: boolean; why?: string; items?: LibItem[] }>("library");
const sendLibrary = callable<[storageId: string, name: string], { ok: boolean; text: string }>("send_library");
const removeGame = callable<[slug: string, scope: string, name: string, path: string], { ok: boolean; text: string }>("remove_game");
const getSkipped = callable<[path: string], { ok: boolean; text: string }>("get_skipped");
const SKIPPED_WHY = "Other-language files were left out to install faster. Get them if the game asks for a missing file or you play in another language.";
// The phone's search headings and buttons, in its order: streams now, installs, then Steam store titles (details only).
const GROUPS: Record<string, [string, string]> = {
  "Streams from your storage": ["A game folder: streams to the Deck, nothing to install", "Stream to Deck"],
  "Install on the Deck": ["Installers and archives: installed on the Deck once", "Install to Deck"],
  "On the Steam store": ["Not in your storage: add the game folder there first", ""],
};

// The phone's displayGameName (index.html), so a job reads the same on both: 'Hades.II.v1.0' -> 'Hades II'.
function displayGameName(raw?: string): string {
  const original = String(raw || "").trim();
  let t = original
    .replace(/\[[^\]]*\]|\([^)]*\)|\{[^}]*\}/g, " ")
    .replace(/[™®©]/g, "")
    .replace(/\s*\|.*$/, "")
    .replace(/\.(iso|zip|rar|7z)$/i, "")
    .replace(/[._]?\b(build|update|patch)[ ._]?\d[\d.]*/gi, " ")
    .replace(/[._ ]v\.?\d+(?:[._]\d+)*[a-z]?\b/gi, " ");
  if (!/\s/.test(original) || (original.match(/[._]/g) || []).length >= 3) t = t.replace(/[._]+/g, " ");
  t = t.replace(/\s+-\s+/g, " - ").replace(/\s+/g, " ").trim();
  const tail = /(?:^|[\s-])(?:deluxe|ultimate|complete|goty|edition|remastered|v\d+(\.\d+)*)$/i;
  for (let i = 0; i < 8 && tail.test(t); i++) t = t.replace(tail, "").replace(/[\s\-:,+]+$/, "").trim();
  t = t.replace(/\bdirectors? cut\b/i, "Director's Cut");
  const letters = t.replace(/[^A-Za-z]/g, "");
  if (letters.length > 6 && letters === letters.toUpperCase()) {
    t = t.toLowerCase().replace(/\b[a-z][a-z']*/g, (w) =>
      /^(i|ii|iii|iv|v|vi|vii|viii|ix|x|xi|xii|xiii|xv|xvi|hd|dlc|goty|vr)$/.test(w) ? w.toUpperCase() : w[0].toUpperCase() + w.slice(1));
  }
  t = t.replace(/^[\s\-:,+]+|[\s\-:,+]+$/g, "").replace(/\s+/g, " ").trim();
  return t || original.replace(/[\[\]()_.-]+/g, " ").trim() || "Game";
}

// The phone's Recent results line: what it was, or why it failed.
function resultText(h: Job): string {
  if (h.status === "completed") return h.mode;
  if (h.status === "cancelled") return "Cancelled";
  return /^Install failed: /.test(h.message || "") ? (h.message || "") : "Failed" + (h.message ? ": " + h.message : "");
}

function spaceText(s: Space): string {
  return s ? `${s.used_gb} of ${s.total_gb} GB used · ${s.free_gb} GB free` : "";
}

// An older daemon sends a fill without phase_label: the same words from the numbers.
function fillText(a: NonNullable<ActiveGame>): string {
  return `Getting ready: ${((a.bytes_done || 0) / 1e9).toFixed(1)} of ${((a.bytes_total || 0) / 1e9).toFixed(1)} GB${eta(a.eta_s) ? ", " + eta(a.eta_s) : ""}`;
}

function eta(seconds?: number): string {
  if (!seconds || seconds <= 0) return "";
  const m = Math.round(seconds / 60);
  return m < 60 ? `${Math.max(1, m)} min left` : `${Math.floor(m / 60)} h ${m % 60} min left`;
}

function ago(seconds?: number | null): string {
  if (seconds == null) return "";
  if (seconds < 90) return "just now";
  const m = Math.round(seconds / 60);
  return m < 90 ? `${m} min ago` : `${Math.round(m / 60)} h ago`;
}

function dur(seconds: number): string {
  return seconds < 120 ? `${seconds} s` : `${Math.round(seconds / 60)} min`;
}

function lastLaunchText(l: NonNullable<LastLaunch>): string {
  if (l.exit_seconds != null && l.exit_text) return `Exited after ${dur(l.exit_seconds)}: ${l.exit_text}`;
  if (l.play_to_launch_seconds != null) return `Started ${dur(l.play_to_launch_seconds)} after Play`;
  return "Ended";
}

function play(appid: number) {
  const w = window as any;
  const overview = w.appStore?.GetAppOverviewByAppID?.(appid);
  if (overview?.m_gameid && w.SteamClient?.Apps?.RunGame) {
    w.SteamClient.Apps.RunGame(overview.m_gameid, "", -1, 100);
    Navigation.CloseSideMenus();
  } else {
    Navigation.Navigate(`/library/app/${appid}`);   // the game's page: one more tap on Play
    Navigation.CloseSideMenus();
  }
}

// The phone's Library tab: what is in your storage, in its groups, with its one tap. Read once when the panel opens (and on
// Refresh), not on the 5 s timer: a big library is a big response.
type LibItem = { id: string; name: string; size_gb: number | null; group: string; pct: number };
const LIB_GROUPS: Record<string, [string, string]> = {
  "Ready to play": ["Pre-installed: streams to the Deck", "Stream to Deck"],
  "Needs installing": ["Installers and archives: installed on the Deck once", "Install to Deck"],
  "Still caching": ["Your storage is still syncing these", ""],
  "Other files": ["Disc images, media and the rest", ""],
};
const LIB_SHOWN = 5;

function LibrarySection() {
  const [res, setRes] = useState<{ ok: boolean; why?: string; items?: LibItem[] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState("");
  const [open, setOpen] = useState<string[]>([]);
  const load = () => {
    setBusy(true);
    getLibrary().then(setRes).catch(() => setRes({ ok: false, why: "Could not reach the panel's backend." })).finally(() => setBusy(false));
  };
  useEffect(load, []);
  const send = (it: LibItem) => {
    if (sending) return;
    setSending(it.id);
    const name = displayGameName(it.name);
    toaster.toast({ title: "DeckStream", body: `Sending ${name} to your Deck…`, duration: 3000 });
    sendLibrary(it.id, name)
      .then((v) => toaster.toast({ title: "DeckStream", body: v.text, duration: 6000 }))
      .catch(() => toaster.toast({ title: "DeckStream", body: "Could not reach the panel's backend.", duration: 4000 }))
      .finally(() => setSending(""));
  };
  const items = res?.items || [];
  return (
    <PanelSection title={items.length ? `Your library (${items.length})` : "Your library"}>
      {res && !res.ok && <PanelSectionRow><Field label="Library unavailable" description={res.why} /></PanelSectionRow>}
      {res?.ok && items.length === 0 && (
        <PanelSectionRow><Field label="Your library is empty." description="Add game folders to your storage server, then Refresh." /></PanelSectionRow>
      )}
      {Object.keys(LIB_GROUPS).map((g) => {
        const all = items.filter((it) => it.group === g);
        if (!all.length) return null;
        const shown = open.includes(g) ? all : all.slice(0, LIB_SHOWN);
        const [hint, action] = LIB_GROUPS[g];
        return (
          <Fragment key={g}>
            <PanelSectionRow><Field label={`${g} (${all.length})`} description={hint} /></PanelSectionRow>
            {shown.map((it) => (
              <PanelSectionRow key={it.id}>
                <ButtonItem layout="below" label={displayGameName(it.name)} disabled={!action || !!sending}
                  description={[it.size_gb ? `${it.size_gb} GB` : "", g === "Still caching" ? `${it.pct}%` : ""].filter(Boolean).join(" · ")}
                  onClick={() => action && send(it)}>
                  {g === "Still caching" ? "Still syncing" : !action ? "Send from the phone" : sending === it.id ? "Sending…" : action}
                </ButtonItem>
              </PanelSectionRow>
            ))}
            {all.length > shown.length && (
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={() => setOpen([...open, g])}>{`Show ${all.length - shown.length} more`}</ButtonItem>
              </PanelSectionRow>
            )}
          </Fragment>
        );
      })}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={load}>{busy ? "Loading your library…" : "Refresh the library"}</ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

// The phone's Search tab: type a game (Steam's on-screen keyboard), pick your copy, Stream / Install to Deck.
function SearchSection() {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<{ ok: boolean; why?: string; results?: Result[] } | null>(null);
  const [sending, setSending] = useState("");
  const run = () => {
    if (!text.trim() || busy) return;
    setBusy(true);
    searchGames(text.trim())
      .then(setRes)
      .catch(() => setRes({ ok: false, why: "Could not reach the panel's backend." }))
      .finally(() => setBusy(false));
  };
  const send = (r: Result) => {
    if (sending) return;
    if (!r.sendable || !r.id) return;
    setSending(r.id);
    const name = displayGameName(r.title);
    toaster.toast({ title: "DeckStream", body: `Sending ${name} to your Deck…`, duration: 3000 });
    sendLibrary(r.id, name)
      .then((v) => toaster.toast({ title: "DeckStream", body: v.text, duration: 6000 }))
      .catch(() => toaster.toast({ title: "DeckStream", body: "Could not reach the panel's backend.", duration: 4000 }))
      .finally(() => setSending(""));
  };
  const order = Object.keys(GROUPS);
  const list = (res?.results || []).map((r, i) => ({ r, i })).sort((a, b) => order.indexOf(a.r.group) - order.indexOf(b.r.group) || a.i - b.i);
  return (
    <PanelSection title="Search">
      <PanelSectionRow>
        <TextField label="Find a game" value={text} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") run(); }} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || !text.trim()} onClick={run}>{busy ? "Searching…" : "Search"}</ButtonItem>
      </PanelSectionRow>
      {res && !res.ok && <PanelSectionRow><Field label="Search unavailable" description={res.why} /></PanelSectionRow>}
      {res?.ok && list.length === 0 && <PanelSectionRow><Field label="No results" description="Try a shorter name." /></PanelSectionRow>}
      {list.map(({ r, i }, k) => (
        <Fragment key={i}>
          {(k === 0 || list[k - 1].r.group !== r.group) && (
            <PanelSectionRow><Field label={r.group} description={GROUPS[r.group]?.[0]} /></PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem layout="below" label={displayGameName(r.title)} disabled={!r.sendable || !!sending}
              description={r.size_gb ? `${r.size_gb} GB` : ""}
              onClick={() => send(r)}>
              {!r.sendable ? "Details on the phone" : sending === r.id ? "Sending…" : GROUPS[r.group]?.[1] || "Send to Deck"}
            </ButtonItem>
          </PanelSectionRow>
        </Fragment>
      ))}
    </PanelSection>
  );
}

function Content() {
  const [st, setSt] = useState<Status | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [q, setQ] = useState<Queue | null>(null);
  const [armed, setArmed] = useState("");   // Cancel needs a second press: an install can take hours
  const alive = useRef(true);
  const load = (refresh = false) => {
    getQueue().then((v) => alive.current && setQ(v)).catch(() => alive.current && setQ({ ok: false, why: "Could not read the queue." }));
    return getStatus(refresh)
      .then((s) => { if (alive.current) { setSt(s); setFailed(false); } })
      .catch(() => alive.current && setFailed(true));
  };
  const act = (action: "cancel" | "retry", id: string) => {
    if (action === "cancel" && armed !== id) { setArmed(id); return; }
    setArmed("");
    queueAction(action, id)
      .then((r) => toaster.toast({ title: "DeckStream", body: r.ok ? r.text : `Couldn't ${action === "cancel" ? "cancel" : "retry"}: ${r.text}`, duration: 4000 }))
      .catch(() => toaster.toast({ title: "DeckStream", body: "Could not reach the panel's backend.", duration: 4000 }))
      .finally(() => load());
  };
  // The phone's Free up space: Clear cache or Delete, each with a second press (the phone asks first, with these words).
  const remove = (scope: "cache" | "game", slug: string, name: string, path = "") => {
    const key = `${scope}:${slug}`;
    if (armed !== key) { setArmed(key); return; }
    setArmed("");
    removeGame(slug, scope, name, path)
      .then((r) => toaster.toast({ title: "DeckStream", body: r.text, duration: 4000 }))
      .catch(() => toaster.toast({ title: "DeckStream", body: "Could not reach the panel's backend.", duration: 4000 }))
      .finally(() => load());
  };
  // The phone's "Get skipped files": copies only what the rip install left out.
  const fetchSkipped = (path: string) => {
    getSkipped(path)
      .then((r) => toaster.toast({ title: "DeckStream", body: r.text, duration: 4000 }))
      .catch(() => toaster.toast({ title: "DeckStream", body: "Could not reach the panel's backend.", duration: 4000 }))
      .finally(() => load());
  };
  const refreshNow = () => { setBusy(true); load(true).finally(() => alive.current && setBusy(false)); };

  useEffect(() => {
    alive.current = true;
    load();
    const t = setInterval(() => load(), 5000);   // only while the panel is open
    return () => { alive.current = false; clearInterval(t); };
  }, []);

  if (failed) return <PanelSection><PanelSectionRow><Field label="DeckStream">Could not read the Deck's status.</Field></PanelSectionRow></PanelSection>;
  if (!st) return <PanelSection><PanelSectionRow><Field label="DeckStream">Loading…</Field></PanelSectionRow></PanelSection>;
  if (!st.installed) {
    return (
      <PanelSection title="DeckStream">
        <PanelSectionRow><Field label="Not installed" description="Install the DeckStream daemon from the app's Deck tab first." /></PanelSectionRow>
      </PanelSection>
    );
  }
  if (st.error) return <PanelSection title="DeckStream"><PanelSectionRow><Field label="Problem">{st.error}</Field></PanelSectionRow></PanelSection>;

  const running = st.running === true ? "Running" : st.running === false ? "Stopped" : "Unknown";
  const power = st.ac_online ? "Charging" : st.battery_pct != null ? `${st.battery_pct}% battery` : "";
  const a = st.active_game;
  const games = st.games || [];
  const deckMode = (slug: string) => (
    <PanelSectionRow>
      <ToggleField label={DECK_MODE_LABEL} checked={!!st.deck_mode?.[slug]}
        onChange={(on: boolean) => setDeckMode(slug, on)
          .then((r) => toaster.toast({ title: "DeckStream", body: r.text, duration: 3000 }))
          .catch(() => toaster.toast({ title: "DeckStream", body: "Could not reach the panel's backend.", duration: 4000 }))
          .finally(() => load())} />
    </PanelSectionRow>
  );
  const installed = st.installed_games || [];
  const sp = st.space;
  const warn = st.space_warning;
  const cur = q?.ok ? q.current : null;
  const showCur = cur && !(cur.mode === "Stream" && a && a.bytes_total);   // a stream's fill already shows below

  return (
    <>
      {a && (
        <PanelSection title="Now">
          <PanelSectionRow>
            {a.phase === "preparing" && a.bytes_total ? (
              <ProgressBarWithInfo label={a.name || "Game"} nProgress={Math.max(0, Math.min(100, a.progress || 0))}
                sOperationText={a.phase_label || fillText(a)} />
            ) : (
              <Field label={a.name || "Game"} description={a.phase_label || ""} />
            )}
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Deck">
        <PanelSectionRow><Field label="DeckStream" description={st.version ? `Version ${st.version}` : undefined}>{running}</Field></PanelSectionRow>
        {power && <PanelSectionRow><Field label="Power">{power}</Field></PanelSectionRow>}
        {sp?.internal && <PanelSectionRow><Field label="Internal drive" description={spaceText(sp.internal)} /></PanelSectionRow>}
        {sp?.card && <PanelSectionRow><Field label="microSD card" description={spaceText(sp.card)} /></PanelSectionRow>}
        {warn && (
          <PanelSectionRow>
            <Field label={`${warn.drive} ${warn.pct}% full`}
              description={warn.name ? `${warn.name} is using ${warn.gb} GB of cache: clearing it is the quickest way to free space.` : "Free space before the next install."} />
          </PanelSectionRow>
        )}
        {st.warmup != null && (
          <PanelSectionRow>
            <ToggleField label="Overnight warm-up" checked={st.warmup}
              description={st.warmup && st.ac_online === false ? `${WARMUP_HELP} Starts on the charger.` : WARMUP_HELP}
              onChange={(on: boolean) => setWarmup(on)
                .then((r) => { if (!r.ok) toaster.toast({ title: "DeckStream", body: r.text, duration: 4000 }); })
                .catch(() => toaster.toast({ title: "DeckStream", body: "Could not reach the panel's backend.", duration: 4000 }))
                .finally(() => load())} />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={refreshNow}>{busy ? "Refreshing…" : "Refresh"}</ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {q?.ok && q.error && (
        <PanelSection title="The Deck needs you">
          <PanelSectionRow><Field label={q.error.message} /></PanelSectionRow>
          {q.error.can_retry && (
            <PanelSectionRow><ButtonItem layout="below" onClick={() => act("retry", q.error!.job_id)}>Try again</ButtonItem></PanelSectionRow>
          )}
        </PanelSection>
      )}

      <PanelSection title="Queue">
        {q && !q.ok && <PanelSectionRow><Field label="Queue unavailable" description={q.why} /></PanelSectionRow>}
        {q?.ok && !showCur && !a && <PanelSectionRow><Field label="Nothing is installing or streaming." /></PanelSectionRow>}
        {showCur && cur && (
          <>
            <PanelSectionRow>
              <ProgressBarWithInfo label={displayGameName(cur.name)} nProgress={cur.progress || 0}
                sOperationText={cur.message || cur.mode} sTimeRemaining={`${cur.progress || 0}%`} />
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => act("cancel", cur.id)}>{armed === cur.id ? "Press again to cancel" : "Cancel"}</ButtonItem>
            </PanelSectionRow>
          </>
        )}
        {(q?.ok ? q.jobs || [] : []).map((j) => (
          <PanelSectionRow key={j.id}>
            <ButtonItem layout="below" label={displayGameName(j.name)}
              description={[j.mode, j.size_gb ? `${j.size_gb} GB` : ""].filter(Boolean).join(" · ")}
              onClick={() => act("cancel", j.id)}>
              {armed === j.id ? "Press again to cancel" : "Cancel"}
            </ButtonItem>
          </PanelSectionRow>
        ))}
      </PanelSection>

      {q?.ok && (q.history || []).length > 0 && (
        <PanelSection title="Recent">
          {(q.history || []).map((h) => (
            <PanelSectionRow key={h.id + String(h.finished)}>
              {h.status === "failed" ? (
                <ButtonItem layout="below" label={displayGameName(h.name)} description={resultText(h)} onClick={() => act("retry", h.id)}>Try again</ButtonItem>
              ) : (
                <Field label={displayGameName(h.name)} description={resultText(h)}>
                  {h.finished ? ago(Math.max(0, Math.floor(Date.now() / 1000) - h.finished)) : ""}
                </Field>
              )}
            </PanelSectionRow>
          ))}
        </PanelSection>
      )}


      <SearchSection />
      <LibrarySection />

      {st.last_launch && st.last_launch.stage === "ended" && (
        <PanelSection title="Last launch">
          <PanelSectionRow>
            <Field label={st.last_launch.slug.replace(/_/g, " ")} description={ago(st.last_launch.ago_s)}>{lastLaunchText(st.last_launch)}</Field>
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Your streamed games">
        {games.length === 0 && <PanelSectionRow><Field label="None yet" description="Send a game from the DeckStream app on your phone." /></PanelSectionRow>}
        {games.map((g) => (
          <Fragment key={g.slug}>
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                disabled={!g.appid}
                label={g.name}
                description={g.cache_gb ? `${g.cache_gb} GB cached on this Deck` : "Nothing cached yet"}
                onClick={() => g.appid && play(g.appid)}
              >
                {g.appid ? "Play" : "No Steam shortcut yet"}
              </ButtonItem>
            </PanelSectionRow>
            {deckMode(g.slug)}
          </Fragment>
        ))}
      </PanelSection>

      {games.length > 0 && (
        <PanelSection title="Free up space">
          <PanelSectionRow>
            <Field description="Clear a game's cache to free card space (it streams again next time), or delete the game. Saves are never touched." />
          </PanelSectionRow>
          {games.map((g) => (
            <Fragment key={g.slug}>
              <PanelSectionRow>
                <ButtonItem layout="below" label={g.name} description={armed === `cache:${g.slug}` ? CLEAR_ASK : g.cache_gb >= 0.1 ? `${g.cache_gb.toFixed(1)} GB cached` : "nothing cached"}
                  onClick={() => remove("cache", g.slug, g.name)}>{armed === `cache:${g.slug}` ? "Press again to clear" : "Clear cache"}</ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" description={armed === `game:${g.slug}` ? DELETE_ASK : undefined}
                  onClick={() => remove("game", g.slug, g.name)}>{armed === `game:${g.slug}` ? "Press again to delete" : "Delete"}</ButtonItem>
              </PanelSectionRow>
            </Fragment>
          ))}
        </PanelSection>
      )}

      {installed.length > 0 && (
        <PanelSection title="Installed on the Deck">
          {installed.map((g) => (
            <Fragment key={g.path || g.name}>
              <PanelSectionRow>
                {g.slug && g.path ? (
                  <ButtonItem layout="below" label={displayGameName(g.name)} description={armed === `game:${g.slug}` ? DELETE_ASK : undefined}
                    onClick={() => remove("game", g.slug!, g.name, g.path)}>{armed === `game:${g.slug}` ? "Press again to delete" : "Delete"}</ButtonItem>
                ) : (
                  <Field label={displayGameName(g.name)}>Installed</Field>
                )}
              </PanelSectionRow>
              {g.slug && deckMode(g.slug)}
              {g.skipped && g.path && (
                <PanelSectionRow>
                  <ButtonItem layout="below" description={SKIPPED_WHY} onClick={() => fetchSkipped(g.path!)}>
                    {`Get skipped files (${(g.skipped.bytes / 1e9).toFixed(1)} GB)`}</ButtonItem>
                </PanelSectionRow>
              )}
            </Fragment>
          ))}
        </PanelSection>
      )}
    </>
  );
}

// While Steam's launch spinner turns for one of our stream shortcuts, show the fill's progress as a toast. Polls the
// backend every 5 s ONLY until the fill ends (no polling when idle: power), at most 30 minutes, a toast per ~10 %.
function watchFill(appid: number, stop: { v: boolean }) {
  let lastPct = -100;
  let seen = false;
  const started = Date.now();
  const tick = async () => {
    if (stop.v || Date.now() - started > 30 * 60 * 1000) return;
    try {
      const st = await getStatus(false);
      const a = st.active_game;
      const mine = (st.games || []).some((g) => g.appid === (appid >>> 0));
      if (mine && a && a.bytes_total) {
        seen = true;
        const pct = Math.round(a.progress || 0);
        if (pct - lastPct >= 10 || lastPct < 0) {
          lastPct = pct;
          toaster.toast({
            title: a.name || "DeckStream",
            body: a.phase_label || fillText(a),
            duration: 4000,
          });
        }
      } else if (seen || (mine && (!a || a.phase === "playing"))) {
        return;   // the fill finished (or never started): stop polling
      }
    } catch (_) { /* backend busy: try again */ }
    setTimeout(tick, 5000);
  };
  tick();
}

export default definePlugin(() => {
  const stop = { v: false };
  let reg: { unregister?: () => void } | undefined;
  try {
    reg = (window as any).SteamClient?.GameSessions?.RegisterForAppLifetimeNotifications?.((n: { unAppID: number; bRunning: boolean }) => {
      if (n && n.bRunning) watchFill(n.unAppID, stop);
    });
  } catch (_) { /* older client: no toasts, the panel still works */ }
  return {
    name: "DeckStream",
    titleView: <div className={staticClasses.Title}>DeckStream</div>,
    content: <Content />,
    icon: <FaCloudDownloadAlt />,
    onDismount() { stop.v = true; try { reg?.unregister?.(); } catch (_) { /* ignore */ } },
  };
});
