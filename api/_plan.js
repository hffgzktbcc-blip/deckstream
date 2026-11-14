// Launch planner: turns a remote storage game (name + file list) into an execution plan
// for SteamOS and the DeckStream companion app.
//
// Pure function, no network dependencies.
const MB = 1024 * 1024;
const GB = 1024 * MB;

// Executables that are helpers/installers rather than the main game engine.
const IGNORE_EXE = [
  'unins', 'setup', 'crash', 'reporter', 'dxwebsetup', 'vcredist', 'dotnet', 'easyanticheat', 'battleye',
  'unitycrash', 'redist', 'prerequisites', 'install', 'config', 'benchmark', 'patch', 'handler', 'quicksfv',
];

const POOR_DIRS = ['_redist', 'redist', '_commonredist', 'directx', 'vcredist', 'support', 'prerequisites', '__installer', 'installer'];

const ARCHIVE_RE = /\.(zip|rar|7z|tar|gz|xz)$/i;
const DISC_RE = /\.(iso|mdf|mds|cue|nrg)$/i;
const DOC_RE = /\.(docx?|pdf|txt|rtf|url|lnk|html?|jpe?g|png|gif)$/i;
const VIDEO_RE = /\.(mkv|mp4|avi|m2ts|mov|wmv|ts|webm)$/i;
const AUDIO_RE = /\.(mp3|flac|m4a|m4b|opus|ogg|wav|aac|wma)$/i;
const BOOK_RE = /\.(epub|mobi|azw3?|cbz|cbr|djvu)$/i;
const NATIVE_INSTALLER_RE = /\.(sh|run|appimage)$/i;
const MEDIA_NAME_RE = /\b(s\d{2}e\d{2}|1080p|720p|2160p|4k|web-?dl|webrip|blu-?ray|bdrip|hdtv|x26[45]|h\.?26[45]|hevc|epub|mobi|flac|mp3|m4b|audiobook)\b/i;
const HOT_EXT_RE = /\.(exe|dll|ini|cfg|json|xml|toml|lua)$/i;

const lower = (s) => String(s || '').toLowerCase();
const base = (p) => String(p).split('/').pop();
const alnum = (s) => lower(s).replace(/[^a-z0-9]/g, '');

function isRealExe(path) {
  const b = lower(base(path));
  if (!b.endsWith('.exe')) return false;
  return !IGNORE_EXE.some((x) => b.includes(x));
}

function inPoorDir(path) {
  const dirs = lower(path).split('/').slice(0, -1);
  return dirs.some((d) => POOR_DIRS.includes(d));
}

function stripTopFolder(files, gameName) {
  const names = files.map((f) => String(f.name || '').replace(/^\/+/, ''));
  const top = names.length ? names[0].split('/')[0] : '';
  const shared = names.length > 0 && names.every((n) => n.includes('/') && n.split('/')[0] === top);
  if (!shared) return { folder: null, rel: names };
  return { folder: top || gameName || null, rel: names.map((n) => n.slice(top.length + 1)) };
}

function pickExe(entries, gameName) {
  const exes = entries.filter((e) => isRealExe(e.rel));
  if (!exes.length) return { exe: null, alternatives: [] };
  const want = alnum(gameName);
  const score = (e) => {
    const depth = e.rel.split('/').length;
    const rawBn = base(e.rel).replace(/\.exe$/i, '');
    const cleanBn = rawBn.replace(/[-_]?(win64|win32|shipping|x64|x86)$/i, '');
    const bn = alnum(cleanBn || rawBn);
    const nameMatch = bn.length >= 4 && want.includes(bn) ? 1 : 0;
    return [inPoorDir(e.rel) ? 1 : 0, depth, -nameMatch, -e.size, e.rel.length];
  };
  const cmp = (a, b) => {
    const x = score(a), y = score(b);
    for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] - y[i];
    return 0;
  };
  const sorted = [...exes].sort(cmp);
  return { exe: sorted[0], alternatives: sorted.slice(1, 4).map((e) => e.rel) };
}

function detectEngine(entries) {
  const rels = entries.map((e) => lower(e.rel));
  if (rels.some((r) => r.endsWith('.ucas') || r.endsWith('.utoc') || r.endsWith('.pak') || r.includes('binaries/win64/'))) {
    return { name: 'Unreal Engine', id: 'unreal', proton: 'Proton Experimental / GE-Proton (DirectX 12 / Vulkan)' };
  }
  if (rels.some((r) => r.includes('_data/') || r.endsWith('unityplayer.dll'))) {
    return { name: 'Unity', id: 'unity', proton: 'Proton 9.0+ / GE-Proton' };
  }
  if (rels.some((r) => r.endsWith('.pck'))) {
    return { name: 'Godot', id: 'godot', proton: 'Native / Proton 8.0+' };
  }
  if (rels.some((r) => r.endsWith('.vpk'))) {
    return { name: 'Source', id: 'source', proton: 'Proton 8.0+ / Native Linux' };
  }
  return { name: 'Custom / Native', id: 'custom', proton: 'GE-Proton / Proton Experimental' };
}

function synthesizeStarterProfile(entries, hotFiles, totalBytes) {
  const BIG_THRESHOLD = 256 * MB;
  const EDGE_BYTES = 2 * MB;
  const ranges = [];
  let profileBytes = 0;

  for (const h of (hotFiles || [])) {
    const entry = entries.find((e) => e.rel === h);
    if (entry && entry.size > 0) {
      ranges.push({ t: 0.0, f: entry.rel, o: 0, n: entry.size });
      profileBytes += entry.size;
    }
  }

  const bigEntries = entries.filter((e) => e.size >= BIG_THRESHOLD).sort((a, b) => b.size - a.size);
  for (const b of bigEntries) {
    const headSize = Math.min(EDGE_BYTES, b.size);
    ranges.push({ t: 1.0, f: b.rel, o: 0, n: headSize });
    profileBytes += headSize;
    if (b.size > headSize) {
      const tailOffset = Math.max(headSize, b.size - EDGE_BYTES);
      const tailSize = b.size - tailOffset;
      if (tailSize > 0) {
        ranges.push({ t: 1.0, f: b.rel, o: tailOffset, n: tailSize });
        profileBytes += tailSize;
      }
    }
  }

  return {
    version: 1,
    synthetic: true,
    totalBytes: profileBytes,
    ratio: totalBytes > 0 ? Number((profileBytes / totalBytes).toFixed(4)) : 0,
    rangeCount: ranges.length,
    ranges: ranges.slice(0, 500),
  };
}

function streamFit(bytes) {
  if (bytes <= 8 * GB) return 'good';
  if (bytes <= 25 * GB) return 'fair';
  return 'poor';
}

function buildPlan(game, opts = {}) {
  const mbps = Number(opts.mbps) > 0 ? Number(opts.mbps) : 22;
  const name = String(game && game.name || '');
  const files = Array.isArray(game && game.files) ? game.files : [];
  const warnings = [];

  const { folder, rel } = stripTopFolder(files, name);
  const webdavFolder = folder || name || null;
  const entries = files.map((f, i) => ({ rel: rel[i], size: Number(f.size) || 0 }));
  const totalBytes = Number(game && game.size) || entries.reduce((a, e) => a + e.size, 0);
  const relNames = entries.map((e) => e.rel);

  const realExes = entries.filter((e) => isRealExe(e.rel));
  const hasSetupExe = relNames.some((n) => /(^|\/)setup[^/]*\.exe$/i.test(n));
  const hasArchive = relNames.some((n) => ARCHIVE_RE.test(n));
  const docOnly = entries.length > 0 && entries.every((e) => DOC_RE.test(e.rel));
  const mediaBytes = entries.filter((e) => VIDEO_RE.test(e.rel) || AUDIO_RE.test(e.rel) || BOOK_RE.test(e.rel))
    .reduce((a, e) => a + e.size, 0);
  const mediaMajority = totalBytes > 0 && mediaBytes / totalBytes >= 0.7;
  const looksLikeMedia = MEDIA_NAME_RE.test(name);
  const hasNativeInstaller = relNames.some((n) => NATIVE_INSTALLER_RE.test(n));
  const hasDirectStorage = relNames.some((n) => /(^|\/)dstorage(core)?\.dll$/i.test(n));

  let kind = 'unknown';
  if (!entries.length) kind = 'unknown';
  else if (realExes.length && (looksLikeMedia || mediaMajority)) kind = 'suspicious';
  else if (realExes.length && entries.length === 1 && totalBytes > 500 * MB) kind = 'suspicious';
  else if (mediaMajority || (looksLikeMedia && !realExes.length)) kind = 'not-a-game';
  else if (docOnly || (totalBytes < 20 * MB && !realExes.length)) kind = 'suspicious';
  else if (realExes.length) kind = 'plain';
  else if (hasArchive) kind = 'archive';
  else if (hasSetupExe) kind = 'installer';
  else if (relNames.some((n) => DISC_RE.test(n))) kind = 'disc-image';
  else if (hasNativeInstaller) kind = 'linux-installer';

  const picked = kind === 'plain' ? pickExe(entries, name) : { exe: null, alternatives: [] };
  const fit = kind === 'plain' ? streamFit(totalBytes) : null;
  let recommendation = 'unsupported';
  let reason = '';

  switch (kind) {
    case 'plain':
      if (hasDirectStorage) {
        recommendation = 'cloudinstall';
        reason = 'Uses Microsoft DirectStorage: local disk access recommended for best performance on Steam Deck.';
        warnings.push('Contains Microsoft DirectStorage (dstorage.dll): local caching recommended.');
      } else {
        recommendation = 'stream';
        reason = fit === 'good'
          ? 'Ready-to-stream game folder with optimized startup footprint.'
          : 'Ready-to-stream game folder over chunk-cached VFS.';
      }
      break;
    case 'installer':
      recommendation = 'cloudinstall';
      reason = 'Game installer package. Install to microSD card first.';
      break;
    case 'archive':
      recommendation = 'cloudinstall';
      reason = 'Compressed archive. Extract to remote storage or install to microSD card.';
      break;
    case 'linux-installer':
      recommendation = 'cloudinstall';
      reason = 'Native Linux installer script or package.';
      break;
    case 'disc-image':
      reason = 'Disc image (ISO). Mount or extract before streaming.';
      break;
    case 'not-a-game':
      reason = 'Media file (video, audio or book).';
      break;
    case 'suspicious':
      reason = 'Non-game file structure.';
      break;
    default:
      reason = 'Unrecognized file structure.';
  }

  const hotAll = entries
    .filter((e) => HOT_EXT_RE.test(e.rel) && e.size <= 64 * MB)
    .sort((a, b) => a.rel.split('/').length - b.rel.split('/').length || a.size - b.size);
  const hot = { count: hotAll.length, bytes: hotAll.reduce((a, e) => a + e.size, 0), files: hotAll.slice(0, 200).map((e) => e.rel) };
  const engine = detectEngine(entries);
  const starterProfile = (kind === 'plain' || kind === 'installer' || kind === 'archive')
    ? synthesizeStarterProfile(entries, hot.files, totalBytes)
    : null;

  return {
    name,
    kind,
    recommendation,
    reason,
    streamFit: fit,
    hasDirectStorage: Boolean(hasDirectStorage),
    webdavFolder,
    exe: picked.exe ? picked.exe.rel : null,
    exeAlternatives: picked.alternatives,
    engine,
    starterProfile,
    sizeBytes: totalBytes,
    fileCount: entries.length,
    fetchMinutesAtLineSpeed: Math.round(totalBytes / (mbps * 1e6) / 60),
    assumedMBps: mbps,
    hot,
    warnings,
  };
}

module.exports = { buildPlan, isRealExe, streamFit };
