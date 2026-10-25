// Web Push subscription management and notification dispatch.
// Stores subscriptions in Redis via saveDoc('push', deviceId, sub).
// Dispatches notifications to mobile browser service workers via Web Push (RFC 8291/8292) and ntfy.sh topics.

const { applyCors, requireSecret } = require('./_lib');
const { loadDoc, saveDoc } = require('./_store');

let webpush = null;
try {
  webpush = require('web-push');
} catch (e) {
  // web-push library optional in test or lean environments
}

const DEVICE_ID_RE = /^[a-z0-9_-]{1,40}$/;
const PUSH_TTL_SEC = 30 * 86400; // 30 days
const NTFY_TOPIC_RE = /^[a-zA-Z0-9_-]{24,64}$/; // >= 24 chars for unguessable privacy

// Internal notification dispatcher callable by api/status.js or HTTP POST
// ntfyTitle/ntfyBody: wording for ntfy that names no game (ntfy topics are public); defaults to the ready message.
async function sendPushNotification({ deviceId, title, body, tag, url, topic, ntfyTitle, ntfyBody }) {
  const safeTitle = String(title || 'DeckDebrid').slice(0, 100);
  const safeBody = String(body || '').slice(0, 300);
  const safeTag = String(tag || 'deckdebrid-status').slice(0, 50);
  const rawUrl = String(url || '/').slice(0, 200);
  const safeUrl = (rawUrl.startsWith('/') && !rawUrl.startsWith('//')) ? rawUrl : '/';

  let webpushOutcome = 'none';
  let ntfyOutcome = 'skipped';

  // 1. Web Push Dispatch
  const sub = await loadDoc('push', deviceId);
  const vapidPublic = process.env.VAPID_PUBLIC_KEY || '';
  const vapidPrivate = process.env.VAPID_PRIVATE_KEY || '';
  const vapidSubject = process.env.VAPID_SUBJECT || 'mailto:deckdebrid@example.com';

  if (sub && sub.endpoint && webpush && vapidPublic && vapidPrivate) {
    try {
      webpush.setVapidDetails(vapidSubject, vapidPublic, vapidPrivate);
      const payload = JSON.stringify({
        title: safeTitle,
        body: safeBody,
        tag: safeTag,
        data: { url: safeUrl }
      });
      await webpush.sendNotification({ endpoint: sub.endpoint, keys: sub.keys }, payload);
      webpushOutcome = 'sent';
    } catch (err) {
      if (err.statusCode === 404 || err.statusCode === 410) {
        webpushOutcome = 'expired';
        await saveDoc('push', deviceId, null, 1);
      } else {
        webpushOutcome = 'failed';
      }
    }
  }

  // 2. ntfy.sh relay (privacy: generic notification without game name leaks)
  const safeTopic = String(topic || '').trim();
  if (safeTopic) {
    if (NTFY_TOPIC_RE.test(safeTopic)) {
      try {
        await fetch(`https://ntfy.sh/${encodeURIComponent(safeTopic)}`, {
          method: 'POST',
          headers: {
            'Title': String(ntfyTitle || 'Your game is ready').slice(0, 100),
            'Priority': 'high',
            'Tags': 'video_game,white_check_mark'
          },
          body: String(ntfyBody || 'Your Steam Deck has finished preparing a game.').slice(0, 300)
        });
        ntfyOutcome = 'sent';
      } catch (e) {
        ntfyOutcome = 'failed';
      }
    } else {
      ntfyOutcome = 'rejected_invalid_topic';
    }
  }

  return { webpush: webpushOutcome, ntfy: ntfyOutcome };
}

module.exports = async (req, res) => {
  res.setHeader('cache-control', 'no-store');
  if (applyCors(req, res)) return;
  if (!requireSecret(req, res)) return;

  const deviceId = String(req.query.device || req.headers['x-device-id'] || 'deck-main').trim().toLowerCase();
  if (!DEVICE_ID_RE.test(deviceId)) return res.status(400).json({ error: 'Invalid device id' });

  try {
    if (req.method === 'GET') {
      const sub = await loadDoc('push', deviceId);
      return res.json({
        success: true,
        subscribed: Boolean(sub && sub.endpoint),
        vapidPublicKey: process.env.VAPID_PUBLIC_KEY || '',
        updatedAt: sub ? sub.subscribedAt : null
      });
    }

    if (req.method === 'POST') {
      const body = req.body && typeof req.body === 'object' ? req.body : {};
      const action = String(body.action || 'subscribe').trim().toLowerCase();

      if (action === 'subscribe') {
        const sub = body.subscription;
        if (!sub || typeof sub !== 'object' || !sub.endpoint) {
          return res.status(400).json({ error: 'Missing subscription endpoint' });
        }

        const subObj = {
          endpoint: String(sub.endpoint).slice(0, 1000),
          keys: sub.keys && typeof sub.keys === 'object' ? {
            p256dh: String(sub.keys.p256dh || '').slice(0, 200),
            auth: String(sub.keys.auth || '').slice(0, 100)
          } : null,
          deviceId,
          subscribedAt: Date.now()
        };

        await saveDoc('push', deviceId, subObj, PUSH_TTL_SEC);
        return res.json({ success: true, subscribed: true, device: deviceId });
      }

      if (action === 'unsubscribe') {
        await saveDoc('push', deviceId, null, 1);
        return res.json({ success: true, subscribed: false });
      }

      if (action === 'notify') {
        const result = await sendPushNotification({
          deviceId,
          title: body.title,
          body: body.body || body.message,
          tag: body.tag,
          url: body.url,
          topic: body.topic
        });

        if (result.ntfy === 'rejected_invalid_topic') {
          return res.status(400).json({
            error: 'ntfy topic must be >= 24 random characters for privacy',
            webpush: result.webpush,
            ntfy: result.ntfy
          });
        }

        return res.json({
          success: true,
          webpush: result.webpush,
          ntfy: result.ntfy
        });
      }

      return res.status(400).json({ error: 'Unknown push action' });
    }

    return res.status(405).json({ error: 'Method not allowed' });
  } catch (err) {
    return res.status(500).json({ error: 'Push service error: ' + (err.message || 'unknown') });
  }
};

module.exports.sendPushNotification = sendPushNotification;
