// Deck status endpoint for mobile phone card and daemon heartbeat.
// Supports reporting power (AC/battery), warming & prep progress (bytes, ETA),
// active game phase, per-game readiness pills, self-update status, and human-readable errors.

const { applyCors, requireSecret } = require('./_lib');
const { loadDoc, saveDoc, loadDevice, saveDevice } = require('./_store');
const { sendPushNotification } = require('./push');

const DEVICE_ID_RE = /^[a-z0-9_-]{1,40}$/;
const STATUS_TTL_SEC = 600; // 10 minutes TTL in Redis

function timeAgo(ms) {
  if (!ms || isNaN(ms)) return 'never';
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 10) return 'just now';
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

module.exports = async (req, res) => {
  res.setHeader('cache-control', 'no-store');
  if (applyCors(req, res)) return;
  if (!requireSecret(req, res)) return;

  const deviceId = String(req.query.device || req.headers['x-device-id'] || 'deck-main').trim().toLowerCase();
  if (!DEVICE_ID_RE.test(deviceId)) return res.status(400).json({ error: 'Invalid device id' });

  try {
    if (req.method === 'GET') {
      let doc = await loadDoc('status', deviceId);
      let dev = null;

      if (!doc) {
        dev = await loadDevice(deviceId);
        if (dev && dev.status) {
          doc = {
            time: dev.status.lastSeen || 0,
            ac_online: dev.status.ac_online ?? null,
            battery_pct: dev.status.battery_pct ?? null,
            state: dev.status.state || (dev.status.currentJob ? 'installing' : 'idle'),
            // The daemon's heartbeat (api/queue.js) carries the live line: getting ready, ready to play, streaming.
            // An install job outranks it: the daemon sends no heartbeat while a job runs, so that line would be stale.
            active_game: (dev.status.active_game && !(dev.status.currentJob && dev.status.currentJob.mode !== 'stream' && dev.status.currentJob.targetDir !== 'stream'))
              ? dev.status.active_game : (dev.status.currentJob ? {
              name: dev.status.currentJob.name,
              phase: dev.status.currentJob.status === 'downloading' ? 'preparing' : dev.status.currentJob.status,
              phase_label: dev.status.currentJob.message || '',
              progress: dev.status.currentJob.progress || 0
            } : null),
            warming: dev.status.warming || null,
            games: dev.status.games || [],
            sd_free_gb: dev.status.diskSD?.free ? Number((dev.status.diskSD.free / (1024**3)).toFixed(1)) : 0,
            version: dev.status.version || '',
            last_update: dev.status.last_update || null,
            error: dev.status.error || null
          };
        }
      }

      const lastSeen = doc ? Number(doc.time || 0) : 0;
      const msAgo = Date.now() - (lastSeen > 1e12 ? lastSeen : lastSeen * 1000);
      const isOnline = lastSeen > 0 && msAgo < 60_000; // seen within 60 seconds

      return res.json({
        success: true,
        device: deviceId,
        online: isOnline,
        lastSeen,
        lastSeenAgo: timeAgo(lastSeen > 1e12 ? lastSeen : lastSeen * 1000),
        ac_online: doc ? doc.ac_online : null,
        battery_pct: doc ? doc.battery_pct : null,
        state: doc ? (doc.state || 'idle') : 'offline',
        active_game: doc ? doc.active_game : null,
        warming: doc ? doc.warming : null,
        games: doc && Array.isArray(doc.games) ? doc.games : [],
        sd_free_gb: doc ? doc.sd_free_gb : 0,
        version: doc ? (doc.version || '') : '',
        last_update: doc ? doc.last_update : null,
        error: doc ? doc.error : null
      });
    }

    if (req.method === 'POST') {
      const body = req.body && typeof req.body === 'object' ? req.body : {};
      const nowMs = Date.now();

      const statusObj = {
        time: nowMs,
        ac_online: body.ac_online != null ? Boolean(body.ac_online) : null,
        battery_pct: typeof body.battery_pct === 'number' ? Math.max(0, Math.min(100, Math.round(body.battery_pct))) : null,
        state: String(body.state || 'idle').trim().toLowerCase(), // 'idle' | 'waiting_for_charger' | 'warming' | 'playing' | 'installing' | 'ready' | 'done' | 'error'
        active_game: body.active_game && typeof body.active_game === 'object' ? {
          slug: String(body.active_game.slug || '').trim().slice(0, 100),
          name: String(body.active_game.name || '').trim().slice(0, 120),
          phase: String(body.active_game.phase || 'queued').trim().toLowerCase(), // 'queued' | 'preparing' | 'ready' | 'playing' | 'done'
          phase_label: String(body.active_game.phase_label || '').trim().slice(0, 150),
          progress: Number(body.active_game.progress) || 0,
          bytes_done: Number(body.active_game.bytes_done) || 0,
          bytes_total: Number(body.active_game.bytes_total) || 0,
          eta_s: Number(body.active_game.eta_s) || 0,
          control_eta_min: Number(body.active_game.control_eta_min) || 0
        } : null,
        warming: body.warming && typeof body.warming === 'object' ? {
          slug: String(body.warming.slug || '').trim().slice(0, 100),
          name: String(body.warming.name || '').trim().slice(0, 120),
          bytes_done: Number(body.warming.bytes_done) || 0,
          bytes_total: Number(body.warming.bytes_total) || 0,
          started: Number(body.warming.started) || 0,
          eta_s: Number(body.warming.eta_s) || 0
        } : null,
        games: Array.isArray(body.games) ? body.games.slice(0, 50).map(g => ({
          slug: String(g.slug || '').trim().slice(0, 100),
          name: String(g.name || '').trim().slice(0, 120),
          cache_gb: Number(g.cache_gb) || 0,
          profile_gb: Number(g.profile_gb) || 0,
          warm: Boolean(g.warm),
          warmed_at: Number(g.warmed_at) || 0,
          control_eta_min: Number(g.control_eta_min) || 0
        })) : [],
        sd_free_gb: Number(body.sd_free_gb) || 0,
        version: String(body.version || '').trim().slice(0, 40),
        last_update: body.last_update && typeof body.last_update === 'object' ? {
          from: String(body.last_update.from || '').slice(0, 40),
          to: String(body.last_update.to || '').slice(0, 40),
          result: String(body.last_update.result || '').slice(0, 40),
          time: Number(body.last_update.time) || 0
        } : null,
        error: body.error && typeof body.error === 'object' ? {
          code: String(body.error.code || '').slice(0, 50),
          message: String(body.error.message || '').slice(0, 300),
          action: String(body.error.action || '').slice(0, 50), // 'wake_deck' | 'clear_cache' | 'retry'
          can_retry: Boolean(body.error.can_retry),
          job_id: String(body.error.job_id || '').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 60)
        } : null
      };

      // Dispatch background push notifications on phase transitions (Ready/Done) or new error
      try {
        const prevDoc = await loadDoc('status', deviceId);
        const prevPhase = prevDoc?.active_game?.phase;
        const currPhase = statusObj.active_game?.phase;

        if (currPhase && currPhase !== prevPhase && (currPhase === 'ready' || currPhase === 'done')) {
          const gameName = statusObj.active_game.name || 'Your game';
          const notifTitle = currPhase === 'ready' ? '🎮 Game Ready to Play' : '✅ Game Finished Installing';
          const notifBody = currPhase === 'ready'
            ? `${gameName} is ready to play on your Steam Deck!`
            : `${gameName} has finished installing.`;
          sendPushNotification({
            deviceId,
            title: notifTitle,
            body: notifBody,
            tag: `deck-${deviceId}-${currPhase}-${statusObj.active_game.slug || 'game'}`,
            url: '/'
          }).catch(() => {});
        } else if (statusObj.error && statusObj.error.message && (!prevDoc?.error || prevDoc.error.message !== statusObj.error.message)) {
          sendPushNotification({
            deviceId,
            title: '⚠️ Steam Deck Alert',
            body: String(statusObj.error.message || '').slice(0, 150),
            tag: `deck-${deviceId}-error`,
            url: '/'
          }).catch(() => {});
        }
      } catch (e) {}

      await saveDoc('status', deviceId, statusObj, STATUS_TTL_SEC);

      // Sync with device queue status
      try {
        const dev = await loadDevice(deviceId);
        if (dev) {
          dev.status.lastSeen = nowMs;
          dev.status.online = true;
          dev.status.ac_online = statusObj.ac_online;
          dev.status.battery_pct = statusObj.battery_pct;
          dev.status.state = statusObj.state;
          dev.status.version = statusObj.version;
          dev.status.last_update = statusObj.last_update;
          dev.status.error = statusObj.error;
          await saveDevice(deviceId, dev);
        }
      } catch (e) {}

      return res.json({ success: true, saved: true, device: deviceId });
    }

    return res.status(405).json({ error: 'Method not allowed' });
  } catch (err) {
    return res.status(500).json({ error: 'Status service error: ' + (err.message || 'unknown') });
  }
};
