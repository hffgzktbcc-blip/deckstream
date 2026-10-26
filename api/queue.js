// Job queue & status relay connecting mobile companion and Steam Deck daemon
// Supports polling, enqueueing install jobs, reporting disk space and completed installs.

const { applyCors, requireSecret, isAllowedDownloadUrl } = require('./_lib');
const { loadDevice, saveDevice, claim, persistent, memSize } = require('./_store');
const { buildPlan } = require('./_plan');
const { addWatch, removeWatch, checkWatches, publicWatch, CHECK_EVERY_MS, WATCH_HARD_TTL_MS } = require('./_watch');
const { sendPushNotification } = require('./push');

const MAX_DEVICES = 20;
const MAX_JOBS = 25;
const MAX_HISTORY = 50;
const DEVICE_ID_RE = /^[a-z0-9_-]{1,40}$/;
const TOPIC_RE = /^[A-Za-z0-9_-]{1,64}$/;

const PLAN_KINDS = ['plain', 'archive', 'installer', 'disc-image', 'linux-installer', 'not-a-game', 'suspicious', 'unknown'];
const PLAN_RECS = ['stream', 'cloudinstall', 'download', 'unsupported'];
// Only a few plain fields survive from the app's launch plan; the exe is a path inside the mount.
function cleanPlan(p) {
  if (!p || typeof p !== 'object') return null;
  const exe = p.exe ? safeFolder(p.exe) : '';
  // Small start-up files (exe/dll/config) the Deck can fetch ahead of the first launch: plain relative paths only.
  const rawHot = Array.isArray(p.hot) ? p.hot : (p.hot && Array.isArray(p.hot.files) ? p.hot.files : []);
  const hot = [...new Set(rawHot.slice(0, 400).map(safeFolder).filter(Boolean))].slice(0, 200);
  return {
    kind: PLAN_KINDS.includes(p.kind) ? p.kind : 'unknown',
    recommendation: PLAN_RECS.includes(p.recommendation) ? p.recommendation : 'unsupported',
    exe: exe && exe.length <= 300 ? exe : '',
    hot,
  };
}

async function resolveServerPlan(req, body, webdavPath) {
  if (body.game && typeof body.game === 'object' && Array.isArray(body.game.files)) {
    return cleanPlan(buildPlan(body.game));
  }
  if (Array.isArray(body.files)) {
    return cleanPlan(buildPlan({ name: body.name || webdavPath, files: body.files }));
  }
  let token = String((req.headers && req.headers['authorization']) || body.storageKey || '').trim();
  if (token.startsWith('Bearer ')) token = token.slice(7).trim();
  if (!token || typeof fetch !== 'function') return null;
  const rawId = String(body.storageId ?? body.gameId ?? body.game_id ?? body.id ?? '').trim();
  const id = /^\d{1,12}$/.test(rawId) ? rawId : '';
  const wantName = String(webdavPath || body.name || '').trim().toLowerCase();
  if (!id && !wantName) return null;
  const base = require('./storage').storageBase();
  const matchItem = (data) => {
    const list = Array.isArray(data) ? data : data ? [data] : [];
    if (id) {
      const byId = list.find((t) => String(t.id ?? '') === id);
      if (byId) return byId;
    }
    if (wantName) {
      return list.find((t) => String(t.name || '').toLowerCase() === wantName) || null;
    }
    return null;
  };
  for (const endpoint of ['games/mylist', 'storage/mylist']) {
    try {
      const url = id ? `${base}/${endpoint}?id=${id}&bypass_cache=true` : `${base}/${endpoint}?bypass_cache=true`;
      const r = await fetch(url, { headers: { authorization: `Bearer ${token}`, 'user-agent': 'DeckStream/1.0' } });
      if (!r || !r.ok) continue;
      const j = await r.json().catch(() => null);
      const item = j && j.success !== false ? matchItem(j.data) : null;
      if (item && Array.isArray(item.files) && item.files.length) {
        return cleanPlan(buildPlan(item));
      }
    } catch {}
  }
  return null;
}

// Folder names that end up in a shell command and a path on the Deck: keep them plain.
function safeFolder(s) {
  const v = String(s || '').trim().replace(/^\/+|\/+$/g, '');
  if (!v || v.length > 300 || v.split('/').some(p => p === '..' || p === '.' || p === '') || /[\0\\]/.test(v)) return '';
  return v;
}

function newDevice() {
  return {
    jobs: [],
    status: {
      online: false,
      lastSeen: 0,
      diskSSD: { free: 0, total: 0 },
      diskSD: { free: 0, total: 0 },
      currentJob: null,
      installedGames: []
    },
    history: []
  };
}

// One job record, the same for the app's enqueue and the site's Stream when ready watcher.
function buildJob(body, { mode, downloadUrl, webdavPath, plan, notifyTopic }) {
  const art = body.artwork && typeof body.artwork === 'object' ? body.artwork : null;
  return {
    id: 'job_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
    name: String(body.name || 'Unnamed Game').trim().slice(0, 120),
    storageId: /^\d{1,12}$/.test(String(body.storageId ?? body.gameId ?? '')) ? String(body.storageId ?? body.gameId) : '',
    downloadUrl,
    targetDir: body.targetDir === 'sd' ? 'sd' : (body.targetDir === 'stream' ? 'stream' : 'internal'), // ~/Games, the SD card, or stream mount
    mode, // 'download' | 'stream' (0 GB mount) | 'cloudinstall' (unpack straight from Remote Storage)
    webdavPath,
    extract: body.extract !== false,
    size: Number.isFinite(Number(body.size)) ? Math.max(0, Number(body.size)) : 0,
    artwork: art ? {
      hero: art.hero, grid: art.grid, logo: art.logo, icon: art.icon, header: art.header
    } : null,
    notifyTopic,
    plan,
    status: 'pending',
    progress: 0,
    enqueuedAt: Date.now()
  };
}

// Stream when ready (api/_watch.js): a cached release whose files are an unpacked folder goes on the queue as a stream
// job. Never twice: this watch's own job (fixed id) waiting, running or finished, or a stream job for the same Remote Storage
// folder waiting or running, is left alone.
function enqueueWatchedStream(device, w) {
  const webdavPath = safeFolder(w.webdavPath);
  const plan = cleanPlan(w.plan);
  if (!webdavPath || !plan) return null;
  const cur = device.status.currentJob;
  const mine = (j) => j && w.jobId && j.id === w.jobId;
  const same = (j) => j && j.mode === 'stream' && j.webdavPath === webdavPath;
  const existing = device.jobs.find(j => mine(j) || same(j)) || ((mine(cur) || same(cur)) ? cur : null)
    || (device.history || []).find(mine);
  if (existing) return existing;
  if (device.jobs.length >= MAX_JOBS) return null;
  const notifyTopic = TOPIC_RE.test(String(w.notifyTopic || '')) ? String(w.notifyTopic) : (device.notifyTopic || '');
  const job = buildJob({ name: w.name || w.title, artwork: w.artwork, size: w.size, targetDir: 'stream' },
    { mode: 'stream', downloadUrl: '', webdavPath, plan, notifyTopic });
  if (w.jobId) job.id = w.jobId;
  device.jobs.push(job);
  return job;
}

async function runWatches(device, deviceId, ctx, force, onJob) {
  if (!(device.watches || []).length) return;
  if (!force && Date.now() - (device.watchCheckedAt || 0) < CHECK_EVERY_MS) return;
  // The Deck's poll and an open app can arrive together with the same stored copy: a lock that simply runs out lets
  // one of them check Remote Storage (and queue) per minute. The app's own watch request checks at once, skipping that lock, so
  // what each watch leads to (queued, told) is also claimed once per watch and its job id is fixed by the watch.
  // A failure here never fails the poll or the queue read: the watches wait for the next check.
  try {
    if (!force && !(await claim('watchlock', deviceId, Math.round(CHECK_EVERY_MS / 1000) - 5))) return;
    const changed = await checkWatches(device, {
      force,
      once: (key) => claim('watchonce', `${deviceId}:${key}`, Math.round(WATCH_HARD_TTL_MS / 1000)),
      enqueueStream: (d, w) => { const job = enqueueWatchedStream(d, w); if (job && onJob) onJob(w.gameId, job); return job; },
      // The watch titles name no game, so ntfy (public topics) can carry them; the body with the name goes by web push only.
      notify: ({ title, body, tag, topic }) => sendPushNotification({ deviceId, title, body, tag, url: '/', topic: topic || device.notifyTopic,
        ntfyTitle: title, ntfyBody: 'Stream when ready: open DeckStream for details.' })
    });
    if (changed) ctx.dirty = true;
  } catch (err) {
    console.error('watch check failed', err && err.message);
  }
}

// Poll/heartbeat only persist when lastSeen is stale, to keep storage writes (and cost) low.
const TOUCH_EVERY_MS = 20000;

module.exports = async (req, res) => {
  res.setHeader('cache-control', 'no-store');
  if (applyCors(req, res)) return;
  if (!requireSecret(req, res)) return;

  const deviceId = String(req.query.device || req.headers['x-device-id'] || 'deck-main').trim().toLowerCase();
  if (!DEVICE_ID_RE.test(deviceId)) return res.status(400).json({ error: 'Invalid device id' });

  try {
    let device = await loadDevice(deviceId);
    if (!device) {
      if (!persistent() && memSize() >= MAX_DEVICES) return res.status(429).json({ error: 'Too many devices' });
      device = newDevice();
    }
    // Run the route against a buffer so storage is written BEFORE the response is sent;
    // serverless functions can be frozen the moment the response goes out.
    const out = { code: 200, body: null, status(c) { this.code = c; return this; }, json(o) { this.body = o; return this; }, end() { return this; } };
    const ctx = { dirty: !device.status.lastSeen && !device.jobs.length && !device.history.length };
    await route(req, out, device, deviceId, ctx);
    if (ctx.dirty) await saveDevice(deviceId, device);
    return res.status(out.code).json(out.body);
  } catch (err) {
    console.error('queue error', err);
    return res.status(503).json({ error: 'Queue storage unavailable' });
  }
};

// The phone's warm-up choice while the Deck has not applied it yet, else null.
function warmupPending(device) {
  const w = device.warmupWanted;
  if (!w || typeof w.on !== 'boolean') return null;
  return (Number(device.status.warmup_applied_at) || 0) >= w.at ? null : { on: w.on, at: w.at };
}

async function route(req, res, device, deviceId, ctx) {
  const touch = () => {
    const stale = Date.now() - (device.status.lastSeen || 0) > TOUCH_EVERY_MS;
    device.status.lastSeen = Date.now();
    device.status.online = true;
    if (stale) ctx.dirty = true;
  };
  if (req.method === 'GET') {
    const action = req.query.action || 'status';
    
    if (action === 'poll') {
      // Used by Steam Deck daemon to fetch next pending job
      touch();
      await runWatches(device, deviceId, ctx, false);
      const nextJob = device.jobs.find(j => j.status === 'pending');
      return res.json({
        job: nextJob || null,
        queueLength: device.jobs.length,
        cancelledJobId: device.cancelledJobId || null
      });
    }

    if (action === 'status') {
      // Used by mobile app to view deck status, disk space, and jobs.
      // Download links embed the Remote Storage token, so never echo them back here.
      const redact = ({ downloadUrl, ...rest }) => rest;
      await runWatches(device, deviceId, ctx, false);
      const isOnline = Date.now() - (device.status.lastSeen || 0) < 45000;
      const cur = device.status.currentJob;
      const activeJob = (cur && cur.id !== device.cancelledJobId) ? redact(cur) : null;
      return res.json({
        deviceId,
        online: isOnline,
        status: { ...device.status, online: isOnline, currentJob: activeJob, warmup_wanted: warmupPending(device) },
        jobs: device.jobs.map(redact),
        history: device.history.slice(-10).map(redact),
        watches: (device.watches || []).map(publicWatch)
      });
    }

    return res.status(400).json({ error: 'Unknown GET action' });
  }

  if (req.method === 'POST') {
    let body = req.body;
    if (typeof body === 'string') {
      try { body = JSON.parse(body); } catch {}
    }
    body = body || {};
    const action = body.action || req.query.action;

    if (action === 'watch') {
      // Stream when ready (api/_watch.js): remember a Remote Storage game and check it now; an already-cached one goes at once.
      let token = String((req.headers && req.headers['authorization']) || '').trim();
      if (token.startsWith('Bearer ')) token = token.slice(7).trim();
      const added = addWatch(device, body, token);
      if (added.error) return res.status(400).json({ error: added.error });
      ctx.dirty = true;
      let job = null;
      await runWatches(device, deviceId, ctx, true, (id, j) => { if (id === added.watch.storageId) job = j; });
      const w = (device.watches || []).find(x => x.storageId === added.watch.storageId);
      return res.json({
        success: true,
        state: job ? 'queued' : (w ? w.state : 'stopped'),
        kind: w && w.kind ? w.kind : undefined,
        job: job ? (({ downloadUrl: _o, ...rest }) => rest)(job) : undefined
      });
    }

    if (action === 'unwatch') {
      if (removeWatch(device, body.storageId || body.gameId)) ctx.dirty = true;
      return res.json({ success: true });
    }

    if (action === 'enqueue' && body.mode === 'remove') {
      // Free a streamed game's disk on the Deck: its cache only, or the whole game. The daemon re-checks everything.
      const slug = String(body.slug || '');
      const scope = body.scope === 'game' ? 'game' : (body.scope === 'cache' ? 'cache' : '');
      if (!/^[a-z0-9][a-z0-9_-]{0,119}$/.test(slug) || !scope) {
        return res.status(400).json({ error: 'A valid game and scope (cache or game) are needed' });
      }
      if (device.jobs.length >= MAX_JOBS) {
        return res.status(429).json({ error: 'Queue is full' });
      }
      const job = {
        id: 'job_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
        name: String(body.name || slug).trim().slice(0, 120),
        mode: 'remove', slug, scope,
        status: 'pending', progress: 0, enqueuedAt: Date.now()
      };
      if (body.path && typeof body.path === 'string' && body.path.length < 300) {
        job.path = body.path.trim();
      }
      device.jobs.push(job);
      ctx.dirty = true;
      return res.json({ success: true, job });
    }

    // The Deck's AI clean/heal jobs (daemon/ai_engine.py, 12ef5b5) deleted whole Wine prefixes (game saves) and registry
    // files of any path sent. Refused here so no Deck receives one, whichever daemon version it runs.
    if (action === 'enqueue' && (body.mode === 'ai_heal' || body.mode === 'ai_clean')) {
      return res.status(403).json({ error: 'AI clean/heal jobs are switched off: they could delete game saves.' });
    }

    if (action === 'enqueue') {
      // Mobile app queues a game for download and install
      const downloadUrl = String(body.downloadUrl || '').trim();
      const mode = ['stream', 'cloudinstall'].includes(body.mode) ? body.mode : 'download';
      const webdavPath = safeFolder(body.webdavPath);
      if (mode === 'download' && !isAllowedDownloadUrl(downloadUrl)) {
        return res.status(400).json({ error: 'downloadUrl must be a link from your storage server (STORAGE_API_URL host or ALLOWED_DOWNLOAD_HOSTS)' });
      }
      if (mode !== 'download' && !webdavPath) {
        return res.status(400).json({ error: 'Stream and cloud-install jobs need a valid Remote Storage folder name' });
      }
      let plan = cleanPlan(body.plan);
      if (mode === 'stream' && !plan) {
        plan = await resolveServerPlan(req, body, webdavPath);
      }
      if (mode === 'stream') {
        if (!plan) {
          return res.status(400).json({
            error: 'Stream mode requires a launch plan (or Remote Storage key + game_id to compute one server-side).'
          });
        }
        if (['archive', 'installer', 'disc-image', 'not-a-game', 'suspicious'].includes(plan.kind)) {
          return res.status(400).json({
            error: `Stream mode requires an extracted, ready-to-play folder. Release kind "${plan.kind}" cannot be streamed directly; install or cloud-install instead.`
          });
        }
      }
      if (device.jobs.length >= MAX_JOBS) {
        return res.status(429).json({ error: 'Queue is full' });
      }
      const notifyTopic = TOPIC_RE.test(String(body.notifyTopic || '')) ? String(body.notifyTopic) : (device.notifyTopic || '');
      const job = buildJob(body, { mode, downloadUrl, webdavPath, plan, notifyTopic });

      if (TOPIC_RE.test(String(body.notifyTopic || ''))) {
        device.notifyTopic = String(body.notifyTopic);
      }

      device.jobs.push(job);
      ctx.dirty = true;
      const { downloadUrl: _omit, ...safeJob } = job;
      return res.json({ success: true, job: safeJob });
    }

    if (action === 'update_progress') {
      // Daemon reports download / extract / steam install progress
      const { jobId, status, progress, message, diskSSD, diskSD, installedGames } = body;
      ctx.dirty = true;
      touch();
      if (diskSSD) device.status.diskSSD = diskSSD;
      if (diskSD) device.status.diskSD = diskSD;
      if (installedGames) device.status.installedGames = installedGames;

      // If job was cancelled by user, reply with cancelled flag so daemon stops
      if (device.cancelledJobId && device.cancelledJobId === jobId) {
        if (status === 'completed' || status === 'failed' || status === 'cancelled') {
          device.cancelledJobId = null;
        }
        return res.json({ success: true, cancelled: true, cancelledJobId: jobId });
      }

      const job = device.jobs.find(j => j.id === jobId);
      if (job) {
        if (status) job.status = status;
        if (progress != null) job.progress = progress;
        if (message) job.message = message;

        if (body.installedGames) device.status.installedGames = body.installedGames;
        if (body.games) device.status.games = body.games;
        if (status === 'completed' || status === 'failed') {
          const completedJob = { ...job, finishedAt: Date.now() };
          device.history.push(completedJob);
          if (device.history.length > MAX_HISTORY) device.history.shift();
          device.jobs = device.jobs.filter(j => j.id !== jobId);
          device.status.currentJob = null;

          // Dispatch ntfy push notification
          const topicRaw = job.notifyTopic || device.notifyTopic;
          const topic = TOPIC_RE.test(String(topicRaw || '')) ? topicRaw : '';
          if (topic) {
            try {
              fetch(`https://ntfy.sh/${encodeURIComponent(topic)}`, {
                method: 'POST',
                headers: {
                  'Title': status === 'completed' ? `🎮 ${job.name} Ready to Play!` : `⚠️ ${job.name} Install Failed`,
                  'Priority': status === 'completed' ? 'high' : 'urgent',
                  'Tags': status === 'completed' ? 'video_game,white_check_mark' : 'warning,x'
                },
                body: status === 'completed' 
                  ? `Successfully installed on Steam Deck & added to Steam shortcuts.`
                  : `Installation failed: ${message || 'Unknown error'}`
              }).catch(() => {});
            } catch {}
          }
        } else {
          device.status.currentJob = job;
        }
      }

      return res.json({ success: true });
    }

    if (action === 'heartbeat') {
      // Daemon sends alive signal, power state, warming progress, and disk info
      touch();
      if (body.diskSSD) device.status.diskSSD = body.diskSSD;
      if (body.diskSD) device.status.diskSD = body.diskSD;
      if (body.installedGames) device.status.installedGames = body.installedGames;
      if (body.ac_online != null) device.status.ac_online = Boolean(body.ac_online);
      if (body.battery_pct != null) device.status.battery_pct = Number(body.battery_pct);
      if (body.state) device.status.state = String(body.state);
      if (body.warming !== undefined) device.status.warming = body.warming;
      if (body.active_game !== undefined) {
        // A queue-time prestage that just covered the game's start-up set: tell the phone once (web push / ntfy).
        const prevPhase = device.status.active_game && device.status.active_game.phase;
        device.status.active_game = body.active_game;
        const ag = body.active_game;
        if (ag && typeof ag === 'object' && ag.phase === 'ready' && prevPhase !== 'ready') {
          try {
            await require('./push').sendPushNotification({
              deviceId, title: 'Ready to play', url: '/', topic: device.notifyTopic,
              body: `${String(ag.name || 'Your game').slice(0, 120)} is ready. Press Play on the Deck.`,
              tag: `deck-${deviceId}-ready-${String(ag.slug || 'game').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 60)}`
            });
          } catch (e) {}
        }
      }
      if (body.games !== undefined) device.status.games = body.games;
      if (body.sd_free_gb != null) device.status.sd_free_gb = Number(body.sd_free_gb);
      if (body.version) device.status.version = String(body.version);
      if (body.last_update) device.status.last_update = body.last_update;
      if (body.error !== undefined) device.status.error = body.error;
      if (typeof body.warmup === 'boolean') device.status.warmup = body.warmup;
      if (body.warmup_applied_at != null) device.status.warmup_applied_at = Number(body.warmup_applied_at) || 0;
      ctx.dirty = true;
      return res.json({ success: true, cancelledJobId: device.cancelledJobId || null, warmup_wanted: warmupPending(device) });
    }

    if (action === 'set_warmup') {
      // The phone's overnight warm-up switch. The Deck picks it up from its next heartbeat reply and applies it once;
      // a newer choice made on the Deck (Decky panel) wins because it moves warmup_applied_at past this one.
      if (typeof body.on !== 'boolean') return res.status(400).json({ error: 'on must be true or false' });
      device.warmupWanted = { on: body.on, at: Date.now() };
      ctx.dirty = true;
      return res.json({ success: true, warmup_wanted: device.warmupWanted });
    }

    if (action === 'cancel') {
      // Cancel a queued or active job
      const { jobId } = body;
      const isCurrent = !jobId || (device.status.currentJob && (!jobId || device.status.currentJob.id === jobId));
      if (isCurrent && device.status.currentJob) {
        const active = device.status.currentJob;
        device.cancelledJobId = active.id;
        device.history.push({
          ...active,
          status: 'cancelled',
          finishedAt: Date.now(),
          message: 'Cancelled by user'
        });
        if (device.history.length > MAX_HISTORY) device.history.shift();
        device.status.currentJob = null;
      } else if (jobId) {
        device.cancelledJobId = jobId;
      }
      device.status.error = null;
      if (jobId) {
        device.jobs = device.jobs.filter(j => j.id !== jobId);
      } else {
        device.jobs = [];
      }
      ctx.dirty = true;
      return res.json({ success: true, cancelledJobId: device.cancelledJobId });
    }

    if (action === 'retry') {
      // No job id (an older phone page): retry the newest FAILED job, so "Try again" always has something to re-queue
      let jobId = body.jobId;
      if (!jobId) {
        const lastFailed = [...device.history].reverse().find(h => h.status === 'failed');
        jobId = lastFailed && lastFailed.id;
      }
      if (jobId) {
        const histIdx = device.history.findIndex(h => h.id === jobId);
        if (histIdx !== -1) {
          // 'pending': the only status the Deck's poll hands out ('queued' sat in the list and never ran)
          const jobToRetry = { ...device.history[histIdx], status: 'pending', progress: 0, message: 'Retrying install', finishedAt: undefined };
          device.history.splice(histIdx, 1);
          device.jobs.push(jobToRetry);
        }
      }
      device.status.error = null;
      ctx.dirty = true;
      return res.json({ success: true });
    }

    return res.status(400).json({ error: 'Unknown POST action' });
  }

  res.status(405).json({ error: 'Method not allowed' });
}
