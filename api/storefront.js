// Live Storefront Metadata & Discovery Relay (Steam, GOG, Epic)
// Ported from OpenGameInstaller (OGI) storefront scrapers to bring native
// store browsing directly to mobile PWAs without needing a desktop PC.
const { applyCors, requireSecret } = require('./_lib');
const aiHandler = require('./_ai');

const STEAM_FEATURED_URL = 'https://store.steampowered.com/api/featuredcategories/?l=english&cc=US';
const STEAM_SUGGEST_URL = 'https://store.steampowered.com/search/suggest';

let featuredCache = { data: null, expires: 0 };
// Steam's featured lists include its own hardware (Steam Deck, Valve Index) as type 0 "apps": never show those as games.
const HARDWARE = /^(steam deck|steam controller|steam link|steam frame|steam machine|valve index)\b/i;
const isGameEntry = (i) => i && Number.isInteger(i.id) && typeof i.name === 'string' && !HARDWARE.test(i.name.trim());

// ---- Discover shelves, the way OpenGameInstaller's Steam add-on builds its catalog: one Steam search per shelf
// (filter=globaltopsellers + a tag or category), each with its own time limit and cache, joined with allSettled so a
// failing shelf is left out instead of breaking the rest. Covers come from IStoreBrowseService/GetItems, because newer
// games store theirs under a hashed file name and BOTH fixed library_600x900 paths 404 for them (checked 2026-10-15).
const STEAM_SEARCH_URL = 'https://store.steampowered.com/search/results/?filter=globaltopsellers&ignore_preferences=1&json=1&hidef2p=1&category1=998';
const STEAM_ITEMS_URL = 'https://api.steampowered.com/IStoreBrowseService/GetItems/v1/';
const STEAM_ASSETS = 'https://shared.akamai.steamstatic.com/store_item_assets/';
// Ids checked live against Steam's search on 2026-10-15. deck_compatibility=3 returned only games Valve's own Deck report
// rates Verified (resolved_category 3, 8 of 8 checked); =2 returned only Playable ones.
const GENRES = [
  { key: 'deck-verified', name: 'Verified on Steam Deck', filter: '&deck_compatibility=3' },
  { key: 'action', name: 'Action', filter: '&tags=19' },
  { key: 'adventure', name: 'Adventure', filter: '&tags=21' },
  { key: 'rpg', name: 'RPG', filter: '&tags=122' },
  { key: 'open-world', name: 'Open world', filter: '&tags=1695' },
  { key: 'soulslike', name: 'Souls-like', filter: '&tags=29482' },
  { key: 'roguelike', name: 'Roguelike', filter: '&tags=1716' },
  { key: 'metroidvania', name: 'Metroidvania', filter: '&tags=1628' },
  { key: 'platformer', name: 'Platformer', filter: '&tags=1625' },
  { key: 'shooter', name: 'Shooter', filter: '&tags=1774' },
  { key: 'fighting', name: 'Fighting', filter: '&tags=1743' },
  { key: 'racing', name: 'Racing', filter: '&tags=699' },
  { key: 'sports', name: 'Sports', filter: '&tags=701' },
  { key: 'horror', name: 'Horror', filter: '&tags=1667' },
  { key: 'survival', name: 'Survival', filter: '&tags=1662' },
  { key: 'strategy', name: 'Strategy', filter: '&tags=9' },
  { key: 'simulation', name: 'Simulation', filter: '&tags=599' },
  { key: 'puzzle', name: 'Puzzle', filter: '&tags=1664' },
  { key: 'story-rich', name: 'Story rich', filter: '&tags=1742' },
  { key: 'indie', name: 'Indie', filter: '&tags=492' },
  { key: 'coop', name: 'Co-op', filter: '&category2=9' },
];
const TOP_SELLERS = { key: 'top-sellers', name: 'Top sellers', filter: '' };
const byKey = (k) => GENRES.find(g => g.key === k);
const SHELVES = [TOP_SELLERS, byKey('deck-verified'), byKey('rpg'), byKey('roguelike'), byKey('indie')];   // Home: short on purpose
const SHELF_SIZE = 50;
const SHELF_TTL_MS = 6 * 3600 * 1000;     // OGI: 6 h
const COVER_TTL_MS = 24 * 3600 * 1000;
let shelfCache = new Map();               // key -> { section, expires }  (kept after expiry: served if Steam is down)
let coverCache = new Map();               // appId -> { url, expires }

const decodeHtml = (v) => String(v).replace(/<[^>]+>/g, '')
  .replace(/&#(x?[0-9a-f]+);/gi, (m, n) => { const c = n[0].toLowerCase() === 'x' ? parseInt(n.slice(1), 16) : parseInt(n, 10); return Number.isFinite(c) ? String.fromCodePoint(c) : m; })
  .replace(/&(amp|lt|gt|quot|apos);/g, (m, n) => ({ amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" })[n])
  .replace(/[<>]/g, '').trim();

const fixedCovers = (id) => [
  `${STEAM_ASSETS}steam/apps/${id}/library_600x900.jpg`,
  `https://steamcdn-a.akamaihd.net/steam/apps/${id}/library_600x900.jpg`,
  `${STEAM_ASSETS}steam/apps/${id}/header.jpg`,
];

async function fetchShelf(shelf) {
  const r = await fetch(STEAM_SEARCH_URL + shelf.filter, { headers: { 'User-Agent': 'DeckStream/1.0' }, signal: AbortSignal.timeout(6000) });
  if (!r.ok) throw new Error(`Steam search ${r.status}`);
  const data = await r.json();
  const seen = new Set();
  const items = [];
  for (const it of Array.isArray(data && data.items) ? data.items : []) {
    if (!it || typeof it.name !== 'string' || typeof it.logo !== 'string') continue;
    const m = it.logo.match(/\/apps\/(\d+)\//);
    if (!m) continue;                      // bundles and packages have no app id
    const appId = Number(m[1]);
    const title = decodeHtml(it.name).replace(/[\u2122\u00ae\u00a9]/g, '').replace(/\s{2,}/g, ' ').trim();   // proper names: no ™ ® ©
    if (!appId || !title || seen.has(appId)) continue;
    seen.add(appId);
    items.push({ appId, title });
    if (items.length >= SHELF_SIZE) break;
  }
  if (!items.length) throw new Error('empty shelf');
  return { key: shelf.key, name: shelf.name, items };
}

async function resolveCovers(appIds) {
  const now = Date.now();
  const todo = [...new Set(appIds)].filter(id => !(coverCache.get(id) && coverCache.get(id).expires > now));
  const batches = [];
  for (let i = 0; i < todo.length; i += 50) batches.push(todo.slice(i, i + 50));
  await Promise.allSettled(batches.map(async (batch) => {
    const input = { ids: batch.map(appid => ({ appid })), context: { language: 'english', country_code: 'US', steam_realm: 1 }, data_request: { include_assets: true } };
    const r = await fetch(`${STEAM_ITEMS_URL}?input_json=${encodeURIComponent(JSON.stringify(input))}`, { headers: { 'User-Agent': 'DeckStream/1.0' }, signal: AbortSignal.timeout(5000) });
    if (!r.ok) throw new Error(`GetItems ${r.status}`);
    const d = await r.json();
    for (const item of (d && d.response && d.response.store_items) || []) {
      const a = item && item.success === 1 && item.assets;
      const file = a && (a.library_capsule_2x || a.library_capsule);
      if (!file || typeof a.asset_url_format !== 'string') continue;
      const path = a.asset_url_format.replace('${FILENAME}', file);
      if (/^https?:\/\//i.test(path) || path.includes('..')) continue;   // only Steam's own asset host
      coverCache.set(item.appid, { url: STEAM_ASSETS + path.replace(/^\/+/, ''), expires: now + COVER_TTL_MS });
    }
  }));
}

async function getShelf(shelf, now) {
  const hit = shelfCache.get(shelf.key);
  if (hit && hit.expires > now) return hit.section;
  try {
    const section = await fetchShelf(shelf);
    shelfCache.set(shelf.key, { section, expires: now + SHELF_TTL_MS });
    return section;
  } catch (err) {
    if (hit) return hit.section;          // stale beats nothing
    throw err;
  }
}

function withCovers(section, limit = SHELF_SIZE) {
  return {
    key: section.key, name: section.name,
    items: section.items.slice(0, limit).map(({ appId, title }) => {
      const exact = coverCache.get(appId);
      const fixed = fixedCovers(appId);
      const capsule = exact ? exact.url : fixed[0];
      return { appId, title, capsule, capsuleFallbacks: fixed.filter(u => u !== capsule), header: fixed[2] };
    }),
  };
}

async function loadShelves(list, coverLimit = SHELF_SIZE) {
  const now = Date.now();
  const results = await Promise.allSettled(list.map(shelf => getShelf(shelf, now)));
  const sections = [], failed = [];
  results.forEach((r, i) => (r.status === 'fulfilled' ? sections.push(r.value) : failed.push(list[i].key)));
  await resolveCovers(sections.flatMap(s => s.items.slice(0, coverLimit).map(i => i.appId))).catch(() => {});
  return { sections: sections.map(s => withCovers(s, coverLimit)), failed };
}

// ---- One game's page: what it is about, from Steam's own store data. Everything reaching the phone is plain text
// (the HTML description is reduced to paragraphs here) or an https image on Steam's servers, so the page can draw it
// without trusting markup. Cached a day per game; a game Steam won't describe is a 404, Steam being down a 502.
const STEAM_APPDETAILS_URL = 'https://store.steampowered.com/api/appdetails';
const STEAM_REVIEWS_URL = 'https://store.steampowered.com/appreviews/';
const DETAILS_TTL_MS = 24 * 3600 * 1000;
let detailsCache = new Map();             // appId -> { data, expires }
const DECK_RATING = { 1: 'unsupported', 2: 'playable', 3: 'verified' };
const STEAM_IMAGE = /^https:\/\/([a-z0-9-]+\.)*(steamstatic\.com|akamaihd\.net|steampowered\.com)\//i;
const steamImage = (u) => (typeof u === 'string' && STEAM_IMAGE.test(u) && !/["'<>\s]/.test(u) ? u.split('?')[0] : '');

function htmlToParagraphs(html, maxChars = 2400) {
  const text = String(html || '')
    .replace(/<\s*(br|\/p|\/li|\/h[1-6]|\/ul|\/div)\s*\/?>/gi, '\n')
    .replace(/<\s*li[^>]*>/gi, '\n• ')
    .replace(/<[^>]+>/g, '');
  const out = [];
  let used = 0;
  for (const raw of decodeHtml(text).split(/\n+/)) {
    const line = raw.replace(/\s+/g, ' ').trim();
    if (!line || line === '•') continue;
    if (used + line.length > maxChars) break;
    out.push(line);
    used += line.length;
  }
  return out;
}

const plainList = (arr, key) => (Array.isArray(arr) ? arr : [])
  .map(x => decodeHtml(typeof x === 'string' ? x : (x && x[key]) || '')).filter(Boolean).slice(0, 8);

function storageNeeded(req) {
  const m = String((req && req.minimum) || '').replace(/<[^>]+>/g, ' ').match(/(?:storage|hard (?:disk|drive)(?: space)?)\s*:?\s*([\d.,]+)\s*(GB|MB|TB)/i);
  return m ? `${m[1].replace(',', '.')} ${m[2].toUpperCase()}` : '';
}

async function fetchDetails(appId) {
  const input = { ids: [{ appid: appId }], context: { language: 'english', country_code: 'US', steam_realm: 1 }, data_request: { include_assets: true, include_platforms: true } };
  const [details, items, reviews] = await Promise.allSettled([
    fetch(`${STEAM_APPDETAILS_URL}?appids=${appId}&l=english&cc=US`, { headers: { 'User-Agent': 'DeckStream/1.0' }, signal: AbortSignal.timeout(6000) })
      .then(r => { if (!r.ok) throw new Error(`appdetails ${r.status}`); return r.json(); }),
    fetch(`${STEAM_ITEMS_URL}?input_json=${encodeURIComponent(JSON.stringify(input))}`, { headers: { 'User-Agent': 'DeckStream/1.0' }, signal: AbortSignal.timeout(5000) })
      .then(r => { if (!r.ok) throw new Error(`GetItems ${r.status}`); return r.json(); }),
    fetch(`${STEAM_REVIEWS_URL}${appId}?json=1&language=all&purchase_type=all&num_per_page=0&filter=summary`, { headers: { 'User-Agent': 'DeckStream/1.0' }, signal: AbortSignal.timeout(5000) })
      .then(r => { if (!r.ok) throw new Error(`appreviews ${r.status}`); return r.json(); }),
  ]);
  if (details.status !== 'fulfilled') throw new Error('Steam store unavailable');
  const entry = details.value && details.value[String(appId)];
  if (!entry || entry.success !== true || !entry.data || typeof entry.data.name !== 'string') return null;
  const d = entry.data;

  let capsule = '', hero = '', deck = 'unknown';
  const item = items.status === 'fulfilled' && items.value && items.value.response && Array.isArray(items.value.response.store_items) ? items.value.response.store_items[0] : null;
  if (item && item.success === 1) {
    const a = item.assets;
    const at = (file) => (a && file && typeof a.asset_url_format === 'string' ? a.asset_url_format.replace('${FILENAME}', file) : '');
    const safePath = (p) => (p && !/^https?:\/\//i.test(p) && !p.includes('..') ? STEAM_ASSETS + p.replace(/^\/+/, '') : '');
    capsule = safePath(at(a && (a.library_capsule_2x || a.library_capsule)));
    hero = safePath(at(a && (a.library_hero_2x || a.library_hero)));
    deck = DECK_RATING[item.platforms && item.platforms.steam_deck_compat_category] || 'unknown';
  }
  // Steam's player rating ("Very Positive", 92% of 612,000 reviews); left out when Steam has none or doesn't answer.
  let rating = null;
  const q = reviews.status === 'fulfilled' && reviews.value && reviews.value.success === 1 ? reviews.value.query_summary : null;
  if (q && Number.isInteger(q.total_reviews) && q.total_reviews > 0 && Number.isInteger(q.total_positive) && typeof q.review_score_desc === 'string') {
    const label = decodeHtml(q.review_score_desc);
    if (label && !/no user reviews/i.test(label)) rating = { label, percent: Math.round((100 * Math.min(q.total_positive, q.total_reviews)) / q.total_reviews), total: q.total_reviews };
  }
  const fixed = fixedCovers(appId);
  const release = d.release_date && typeof d.release_date === 'object' ? d.release_date : {};
  const score = d.metacritic && Number.isInteger(d.metacritic.score) && d.metacritic.score > 0 && d.metacritic.score <= 100 ? d.metacritic.score : null;
  return {
    appId,
    title: decodeHtml(d.name).replace(/[™®©]/g, '').replace(/\s{2,}/g, ' ').trim(),
    summary: decodeHtml(d.short_description || ''),
    about: htmlToParagraphs(d.about_the_game || d.detailed_description || ''),
    genres: plainList(d.genres, 'description'),
    developers: plainList(d.developers),
    publishers: plainList(d.publishers),
    released: decodeHtml(release.date || ''),
    comingSoon: release.coming_soon === true,
    metacritic: score,
    reviews: rating,
    controller: d.controller_support === 'full' ? 'full' : (d.controller_support === 'partial' ? 'partial' : ''),
    storage: storageNeeded(d.pc_requirements),
    deck,
    capsule: capsule || fixed[0],
    capsuleFallbacks: fixed.filter(u => u !== capsule),
    hero: hero || `${STEAM_ASSETS}steam/apps/${appId}/library_hero.jpg`,
    background: steamImage(d.background_raw) || steamImage(d.background) || steamImage(d.header_image) || fixed[2],
    header: steamImage(d.header_image) || fixed[2],
    screenshots: (Array.isArray(d.screenshots) ? d.screenshots : [])
      .map(x => ({ thumb: steamImage(x && x.path_thumbnail), full: steamImage(x && x.path_full) }))
      .filter(x => x.thumb && x.full).slice(0, 8),
  };
}

async function getDetails(appId) {
  const now = Date.now();
  const hit = detailsCache.get(appId);
  if (hit && hit.expires > now) return hit.data;
  try {
    const data = await fetchDetails(appId);
    if (detailsCache.size > 500) detailsCache = new Map();
    detailsCache.set(appId, { data, expires: now + (data ? DETAILS_TTL_MS : 3600 * 1000) });
    return data;
  } catch (err) {
    if (hit) return hit.data;             // stale beats nothing
    throw err;
  }
}

const buildCatalog = () => loadShelves(SHELVES);

module.exports = async (req, res) => {
  // Deck AI chat (api/_ai.js) rides on this function: Vercel's Hobby plan deploys at most 12 functions, and a 13th
  // (api/ai.js, 6e76d76) made every deployment fail. It does its own CORS (POST) and queue-password check.
  if (req.query && req.query.action === 'ai') return aiHandler(req, res);
  if (applyCors(req, res, 'GET, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  const action = req.query.action || 'featured';
  const query = String(req.query.q || req.query.query || '').trim();

  res.setHeader('cache-control', 'public, max-age=1800'); // Cache for 30 minutes

  if (action === 'genres') {
    try {
      const { sections, failed } = await loadShelves(GENRES, 3);
      if (!sections.length) return res.status(502).json({ error: 'Steam genres unavailable', failed });
      return res.json({ success: true, failed, genres: sections.map(s => ({ key: s.key, name: s.name, previews: s.items })) });
    } catch (err) {
      return res.status(502).json({ error: 'Steam genres unavailable' });
    }
  }

  if (action === 'genre') {
    const genre = byKey(String(req.query.key || ''));
    if (!genre) return res.status(400).json({ error: 'Unknown genre' });
    try {
      const { sections } = await loadShelves([genre]);
      if (!sections.length) return res.status(502).json({ error: `Steam's ${genre.name} list is unavailable right now` });
      return res.json({ success: true, section: sections[0] });
    } catch (err) {
      return res.status(502).json({ error: `Steam's ${genre.name} list is unavailable right now` });
    }
  }

  if (action === 'details') {
    const appId = Number(req.query.appId);
    if (!Number.isInteger(appId) || appId <= 0 || appId > 99999999) return res.status(400).json({ error: 'appId must be a Steam app number' });
    try {
      const game = await getDetails(appId);
      if (!game) return res.status(404).json({ error: 'Steam has no store page for this game' });
      return res.json({ success: true, game });
    } catch (err) {
      return res.status(502).json({ error: 'Steam store details are unavailable right now' });
    }
  }

  if (action === 'catalog') {
    try {
      const catalog = await buildCatalog();
      if (!catalog.sections.length) return res.status(502).json({ error: 'Steam shelves unavailable', failed: catalog.failed });
      return res.json({ success: true, source: 'steam-search', ...catalog });
    } catch (err) {
      return res.status(502).json({ error: 'Steam shelves unavailable' });
    }
  }

  if (action === 'suggest' || (action === 'search' && query)) {
    if (!query) return res.status(400).json({ error: 'Query parameter "q" required' });
    try {
      const sRes = await fetch(`${STEAM_SUGGEST_URL}?term=${encodeURIComponent(query)}&f=games&cc=US&l=english`, {
        headers: { 'User-Agent': 'DeckStream/1.0' },
        signal: AbortSignal.timeout(6000)
      });
      if (!sRes.ok) return res.status(502).json({ error: 'Steam search suggestion unavailable' });
      const html = await sRes.text();
      const matches = [...html.matchAll(/data-ds-appid="(\d+)"[^>]*>([\s\S]*?)<\/a>/gi)];
      const suggestions = matches.slice(0, 15).map(m => {
        const id = Number(m[1]);
        const nameMatch = m[2].match(/class="match_name[^"]*"[^>]*>([\s\S]*?)<\/div>/i);
        const name = nameMatch ? nameMatch[1].replace(/<[^>]+>/g, '').trim() : 'Game';
        return {
          appId: id,
          title: name,
          capsule: `https://steamcdn-a.akamaihd.net/steam/apps/${id}/library_600x900.jpg`,
          hero: `https://steamcdn-a.akamaihd.net/steam/apps/${id}/library_hero.jpg`,
          header: `https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/${id}/header.jpg`
        };
      });
      return res.json({ success: true, count: suggestions.length, suggestions });
    } catch (err) {
      return res.status(502).json({ error: 'Could not fetch Steam suggestions' });
    }
  }

  // Default: Featured storefront categories (top sellers, specials, new releases)
  try {
    if (!featuredCache.data || featuredCache.expires < Date.now()) {
      const fRes = await fetch(STEAM_FEATURED_URL, {
        headers: { 'User-Agent': 'DeckStream/1.0' },
        signal: AbortSignal.timeout(7000)
      });
      if (fRes.ok) {
        const data = await fRes.json();
        const mapItems = (items) => (items || []).filter(i => (i.type === 0 || !i.type) && isGameEntry(i)).filter((i, n, all) => all.findIndex(x => x.id === i.id) === n).slice(0, 16).map(i => ({
          appId: i.id,
          title: i.name.replace(/[\u2122\u00ae\u00a9]/g, '').replace(/\s{2,}/g, ' ').trim(),
          capsule: `https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/${i.id}/library_600x900.jpg`,
          capsuleFallbacks: [`https://steamcdn-a.akamaihd.net/steam/apps/${i.id}/library_600x900.jpg`, `https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/${i.id}/header.jpg`],
          hero: `https://steamcdn-a.akamaihd.net/steam/apps/${i.id}/library_hero.jpg`,
          header: i.header_image || `https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/${i.id}/header.jpg`,
          discount: i.discount_percent || 0,
          originalPrice: i.original_price ? (i.original_price / 100).toFixed(2) : null,
          finalPrice: i.final_price ? (i.final_price / 100).toFixed(2) : null,
          currency: i.currency || 'USD'
        }));

        featuredCache = {
          data: {
            topSellers: mapItems(data.top_sellers?.items),
            specials: mapItems(data.specials?.items),
            newReleases: mapItems(data.new_releases?.items)
          },
          expires: Date.now() + 3600000 // 1 hour TTL
        };
      }
    }

    if (featuredCache.data) {
      return res.json({
        success: true,
        source: 'steam-storefront',
        categories: featuredCache.data
      });
    }

    return res.status(502).json({ error: 'Could not fetch featured categories' });
  } catch (err) {
    return res.status(502).json({ error: 'Steam Storefront relay error' });
  }
};

module.exports._resetCatalogCache = () => { shelfCache = new Map(); coverCache = new Map(); detailsCache = new Map(); featuredCache = { data: null, expires: 0 }; };
