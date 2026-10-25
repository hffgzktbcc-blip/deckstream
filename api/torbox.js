// Relays TorBox API requests to bypass browser CORS restrictions and format parameters correctly.
const { applyCors, requireSecret } = require('./_lib');

const ALLOWED_ENDPOINTS = new Set([
  'torrents/mylist',
  'torrents/requestdl',
  'torrents/checkcached',
  'torrents/createtorrent',
  'usenet/mylist',
  'usenet/requestdl',
  'usenet/checkcached',
  'usenet/createusenetdownload',
  'webdl/mylist',
  'webdl/requestdl',
  'webdl/checkcached',
  'webdl/createwebdownload',
  'user/me'
]);

const MULTIPART_ALLOWED_ENDPOINTS = new Set([
  'torrents/createtorrent',
  'usenet/createusenetdownload',
  'webdl/createwebdownload'
]);

module.exports = async (req, res) => {
  if (applyCors(req, res, 'GET, POST, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  const TORBOX_API = process.env.TORBOX_API_URL || 'https://api.torbox.app/v1/api';

  let token = req.headers['authorization'] || '';
  if (token.startsWith('Bearer ')) token = token.slice(7).trim();

  // Parse path and query
  let rawPath = String(req.query.p || req.query.path || req.url.replace(/^\/api\/torbox(\?.*)?$/, '')).replace(/^\/+/, '');
  if (!rawPath) {
    return res.status(400).json({ error: 'Missing path parameter' });
  }

  // Handle case where path contains query string or duplicate ?
  const [basePath, existingQuery] = rawPath.split('?');
  // Only TorBox API method paths like torrents/mylist; no traversal or odd characters.
  if (!/^[A-Za-z0-9_\/-]{1,80}$/.test(basePath) || basePath.split('/').some(p => p === '' || p === '..')) {
    return res.status(400).json({ error: 'Invalid path' });
  }

  // Enforce explicit endpoint allowlist
  if (!ALLOWED_ENDPOINTS.has(basePath)) {
    return res.status(403).json({ error: 'Endpoint not allowed' });
  }

  const urlObj = new URL(`${TORBOX_API}/${basePath}`);

  if (existingQuery) {
    new URLSearchParams(existingQuery).forEach((v, k) => urlObj.searchParams.set(k, v));
  }
  for (const [k, v] of Object.entries(req.query)) {
    if (k !== 'p' && k !== 'path' && k !== 'token' && k !== 'tkn') {
      urlObj.searchParams.set(k, v);
    }
  }

  // For requestdl endpoint, TorBox STRICTLY requires token as a URL query parameter
  if (basePath.includes('requestdl') && token) {
    urlObj.searchParams.set('token', token);
  }

  const targetUrl = urlObj.toString();

  try {
    const headers = {};
    if (token) {
      headers['authorization'] = `Bearer ${token}`;
    }

    let body = undefined;
    let isMultipart = false;
    if (req.method === 'POST') {
      const incomingCt = req.headers['content-type'] || req.headers['Content-Type'] || '';
      isMultipart = typeof incomingCt === 'string' && incomingCt.toLowerCase().includes('multipart/form-data');

      // Multipart payloads are strictly restricted to upload endpoints
      if (isMultipart && !MULTIPART_ALLOWED_ENDPOINTS.has(basePath)) {
        return res.status(403).json({ error: 'Multipart uploads are not permitted for this endpoint' });
      }

      let parsed = req.body;
      if (parsed === undefined && !isMultipart && req && typeof req[Symbol.asyncIterator] === 'function') {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        if (chunks.length) {
          parsed = Buffer.concat(chunks);
        }
      }
      if (typeof parsed === 'string') {
        try { parsed = JSON.parse(parsed); } catch {}
      }
      if (Buffer.isBuffer(parsed)) {
        try { parsed = JSON.parse(parsed.toString('utf-8')); } catch {}
      }

      // 1. Direct multipart passthrough (e.g. browser FormData upload with boundary)
      if (isMultipart) {
        headers['content-type'] = incomingCt;
        if (req.headers['content-length']) {
          headers['content-length'] = req.headers['content-length'];
        }
        if (Buffer.isBuffer(req.body) || typeof req.body === 'string' || (req.body && typeof req.body.pipe === 'function')) {
          body = req.body;
        } else if (req && typeof req.pipe === 'function') {
          body = req;
        } else {
          body = req.body;
        }
      } else if (basePath === 'usenet/createusenetdownload') {
        // TorBox /usenet/createusenetdownload strictly requires multipart/form-data
        if (Buffer.isBuffer(parsed) || (typeof parsed === 'string' && parsed.trim().startsWith('<'))) {
          parsed = { file: parsed };
        }
        const fd = new FormData();
        const linkVal = (parsed && parsed.link) || req.query.link || '';
        if (linkVal) {
          fd.append('link', String(linkVal));
        }

        if (parsed && parsed.file) {
          let fileBytes = parsed.file;
          if (typeof fileBytes === 'string') {
            const trimmed = fileBytes.trim();
            if (!trimmed.startsWith('<') && !trimmed.startsWith('<?xml')) {
              try { fileBytes = Buffer.from(fileBytes, 'base64'); } catch {}
            }
          }
          const filename = (parsed && (parsed.filename || parsed.name)) || 'upload.nzb';
          const blob = new Blob([fileBytes], { type: 'application/x-nzb' });
          fd.append('file', blob, filename.endsWith('.nzb') ? filename : `${filename}.nzb`);
        }

        const passVal = (parsed && parsed.password) || req.query.password || '';
        if (passVal) {
          fd.append('password', String(passVal));
        }

        const postVal = (parsed && parsed.post_processing != null)
          ? parsed.post_processing
          : (req.query.post_processing != null ? req.query.post_processing : '-1');
        if (postVal != null) {
          fd.append('post_processing', String(postVal));
        }

        const nameVal = (parsed && parsed.name) || req.query.name || '';
        if (nameVal) {
          fd.append('name', String(nameVal));
        }

        if (parsed && typeof parsed === 'object' && !Buffer.isBuffer(parsed)) {
          const handled = new Set(['link', 'file', 'filename', 'password', 'post_processing', 'name']);
          for (const [k, v] of Object.entries(parsed)) {
            if (!handled.has(k) && v != null) {
              fd.append(k, String(v));
            }
          }
        }

        body = fd;
        delete headers['content-type'];
      } else if (basePath === 'torrents/createtorrent') {
        const fd = new FormData();
        const magnetVal = (parsed && parsed.magnet) || req.query.magnet || (typeof parsed === 'string' && parsed.includes('magnet:') ? parsed : '');
        if (magnetVal) {
          fd.append('magnet', String(magnetVal));
        } else if (parsed && typeof parsed === 'object' && !Buffer.isBuffer(parsed)) {
          for (const [k, v] of Object.entries(parsed)) {
            if (v != null) fd.append(k, String(v));
          }
        }
        fd.append('seed', String((parsed && parsed.seed) || '3'));
        // Force allow_zip=false server-side: zipped releases break Range streaming
        fd.append('allow_zip', 'false');
        body = fd;
        delete headers['content-type'];
      } else if (basePath === 'webdl/createwebdownload') {
        const fd = new FormData();
        const linkVal = (parsed && parsed.link) || req.query.link || '';
        fd.append('link', String(linkVal));
        body = fd;
        delete headers['content-type'];
      } else if (parsed && typeof parsed === 'object' && !Buffer.isBuffer(parsed)) {
        headers['content-type'] = 'application/json';
        body = JSON.stringify(parsed);
      } else {
        body = parsed || req.body;
      }
    }

    const fetchOpts = {
      method: req.method,
      headers,
      body
    };
    if (body && (typeof body.pipe === 'function' || isMultipart)) {
      fetchOpts.duplex = 'half';
    }

    const r = await fetch(targetUrl, fetchOpts);

    res.status(r.status);
    const ct = r.headers.get('content-type');
    if (ct) res.setHeader('content-type', ct);

    const text = await r.text();
    res.send(text);
  } catch (err) {
    // Never leak targetUrl or query tokens in 502 error messages
    res.status(502).json({ error: 'Could not reach TorBox: ' + (err.name || 'network failure') });
  }
};
