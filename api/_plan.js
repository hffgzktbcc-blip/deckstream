// Launch planner: turns a TorBox torrent (name + file list) into a plan the app and the Deck can act on,
// so the Deck does not have to list and scan folders itself.
//
// Pure function, no network. Mirrors the rules already used by the app (streamBlockReason) and the Deck
// (find_best_executable, stream-runner.sh) so all three agree.

const MB = 1024 * 1024;
const GB = 1024 * MB;

// Executables that are not the game itself (same list as daemon/deckdebrid-daemon.py find_best_executable).
const IGNORE_EXE = [
  'unins', 'setup', 'crash', 'reporter', 'dxwebsetup', 'vcredist', 'dotnet', 'easyanticheat', 'battleye',
  'unitycrash', 'redist', 'prerequisites', 'install', 'config', 'benchmark', 'patch', 'handler', 'quicksfv',
];
// Folders whose executables are backups, cracks or helpers: only used when nothing else exists.
const POOR_DIRS = ['_original files', 'original files', '_crack', 'crack', 'backup', '_redist', 'redist',
  '_commonredist', 'directx', 'vcredist', 'support', 'prerequisites', '__installer', 'installer'];

const ARCHIVE_RE = /\.(zip|rar|7z|r\d+|z\d+|tar|gz|xz|\d{3})$/i;
const SWITCH_RE = /\.(nsp|xci|nsz|xcz)$/i;
const DISC_RE = /\.(iso|mdf|mds|cue|nrg)$/i;
const DOC_RE = /\.(docx?|pdf|txt|rtf|url|lnk|nfo|html?|jpe?g|png|gif)$/i;
const REPACK_BIN_RE = /(^|\/)(fg-(\d+|optional[^/]*)|setup-?\d*)\.bin$/i;
const REPACK_NAME_RE = /fitgirl|dodi|elamigos|kaos|repack/i;
const VIDEO_RE = /\.(mkv|mp4|avi|m2ts|mov|wmv|ts|webm)$/i;
const AUDIO_RE = /\.(mp3|flac|m4a|m4b|opus|ogg|wav|aac|wma)$/i;
const BOOK_RE = /\.(epub|mobi|azw3?|cbz|cbr|djvu)$/i;
const NATIVE_INSTALLER_RE = /\.(sh|run|appimage)$/i;
// A release name that describes a video, audio file or book (not a game).
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

// Paths in the plan are relative to the torrent's top folder (what the Deck mounts), e.g. "Game/Game.exe".
function stripTopFolder(files, torrentName) {
  const names = files.map((f) => String(f.name || '').replace(/^\/+/, ''));
  const top = names.length ? names[0].split('/')[0] : '';
  const shared = names.length > 0 && names.every((n) => n.includes('/') && n.split('/')[0] === top);
  if (!shared) return { folder: null, rel: names };
  return { folder: top || torrentName || null, rel: names.map((n) => n.slice(top.length + 1)) };
}

function pickExe(entries, torrentName) {
  const exes = entries.filter((e) => isRealExe(e.rel));
  if (!exes.length) return { exe: null, alternatives: [] };
  const want = alnum(torrentName);
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

/**
 * @param {{name:string,size?:number,files:Array<{name:string,short_name?:string,size:number,infected?:boolean}>}} torrent
 * @param {{mbps?:number}} [opts] assumed download speed in MB/s for time estimates
 */
function buildPlan(torrent, opts = {}) {
  const mbps = Number(opts.mbps) > 0 ? Number(opts.mbps) : 22;
  const name = String(torrent && torrent.name || '');
  const files = Array.isArray(torrent && torrent.files) ? torrent.files : [];
  const warnings = [];

  const isUsenet = Boolean(
    (torrent && (torrent.usenetdownload_id || torrent.usenet_id || torrent._kind === 'usenet' || torrent.type === 'usenet')) ||
    (opts && (opts.isUsenet || opts.type === 'usenet'))
  );

  const { folder, rel } = stripTopFolder(files, name);
  const webdavFolder = folder || (isUsenet ? (name || null) : null);
  const entries = files.map((f, i) => ({ rel: rel[i], size: Number(f.size) || 0, infected: Boolean(f.infected) }));
  const totalBytes = Number(torrent && torrent.size) || entries.reduce((a, e) => a + e.size, 0);
  const relNames = entries.map((e) => e.rel);

  const infected = entries.filter((e) => e.infected).length;
  if (infected) warnings.push(`TorBox flagged ${infected} file(s) in this release as infected.`);
  if (entries.length > 1000) {
    warnings.push('TorBox WebDAV hides releases with over 1,000 files: direct streaming may miss files. Install to SD card is recommended.');
  }

  const realExes = entries.filter((e) => isRealExe(e.rel));
  const hasSetupExe = relNames.some((n) => /(^|\/)setup[^/]*\.exe$/i.test(n));
  const repackBins = relNames.some((n) => REPACK_BIN_RE.test(n));
  const hasArchive = relNames.some((n) => ARCHIVE_RE.test(n));
  const docOnly = entries.length > 0 && entries.every((e) => DOC_RE.test(e.rel));
  const mediaBytes = entries.filter((e) => VIDEO_RE.test(e.rel) || AUDIO_RE.test(e.rel) || BOOK_RE.test(e.rel))
    .reduce((a, e) => a + e.size, 0);
  const mediaMajority = totalBytes > 0 && mediaBytes / totalBytes >= 0.7;
  const looksLikeMedia = MEDIA_NAME_RE.test(name);
  const hasNativeInstaller = relNames.some((n) => NATIVE_INSTALLER_RE.test(n));
  // Only the hypervisor-crack files themselves: any other .sys (anti-cheat, controller or audio drivers shipped with a
  // game) says nothing about whether the game runs, and blocking on it turned playable releases into "unsupported".
  const hasHypervisor = relNames.some((n) => /(^|\/)(_hypervisor crack[^/]*\/|simplesvm\.sys$|hyperkd\.sys$|hyperhv\.dll$|hyperevade\.dll$)/i.test(n));
  const hasDisabledSteamApi = relNames.some((n) => /(^|\/)steam_api(64)?\.dll\.bak$/i.test(n)) &&
    !relNames.some((n) => /(^|\/)steam_api(64)?\.dll$/i.test(n));
  // A crack packed in a .7z/.rar/.zip next to a ready-to-play game folder is a WARNING, not "archive": the game folder
  // still streams (Cyberpunk 2077 GOG-Rip InsaneRamzes streamed on the Deck and was then blocked by treating it as one).
  const crackArchive = relNames.find((n) => /(^|\/)_?crack[^/]*\/.*\.(7z|rar|zip)$/i.test(n)) || '';
  const hasDirectStorage = relNames.some((n) => /(^|\/)dstorage(core)?\.dll$/i.test(n));

  // Row 58 (18 Oct, Elden Ring Steam Rip): the REAL steam_api64.dll next to the exe plus the crack as loose files in a
  // separate Crack/ folder for the user to copy over. As streamed it exits at start (SteamAPI_Init failed). We never
  // apply a crack for the user, so it is labelled, never "Streams".
  const crackFolderRe = /(^|\/)_?crack[^/]*\//i;
  const hasCrackTree = relNames.some((n) => crackFolderRe.test(n) && /\.(dll|exe|ini|cfg)$/i.test(n));
  const steamApiRe = /(^|\/)steam_api(64)?\.dll$/i;
  const hasRealSteamApi = relNames.some((n) => steamApiRe.test(n) && !crackFolderRe.test(n));
  // InsaneRamZes ships the crack ALREADY APPLIED plus a _crack/ backup copy and the retail DLL in _original files/
  // (Spider-Man Remastered and Spider-Man 2): the game-folder steam_api64.dll is the cracked one, same size as the copy
  // in _crack/. Only a game-folder DLL that matches no crack copy (Elden Ring) still needs the manual step.
  const crackApiSizes = new Set(entries.filter((e) => crackFolderRe.test(e.rel) && steamApiRe.test(e.rel) && e.size > 0).map((e) => e.size));
  const crackApplied = entries.some((e) => steamApiRe.test(e.rel) && !crackFolderRe.test(e.rel) && crackApiSizes.has(e.size)) ||
    relNames.some((n) => /(^|\/)_?original files\/(.*\/)?steam_api(64)?\.dll$/i.test(n));
  const needsManualStep = hasCrackTree && hasRealSteamApi && !crackApplied;
  let kind = 'unknown';
  if (!entries.length) kind = 'unknown';
  else if (relNames.some((n) => SWITCH_RE.test(n))) kind = 'switch';
  else if (hasHypervisor) kind = 'unsupported-hypervisor';
  else if (hasDisabledSteamApi) kind = 'unsupported-uncracked';
  else if (needsManualStep) kind = 'needs-manual-step';
  else if (realExes.length && (looksLikeMedia || mediaMajority)) kind = 'suspicious';   // video/book "release" carrying a program
  else if (realExes.length && entries.length === 1 && totalBytes > 500 * MB) kind = 'suspicious'; // one giant .exe is not a game
  else if (mediaMajority || (looksLikeMedia && !realExes.length)) kind = 'not-a-game';
  else if (docOnly || (totalBytes < 20 * MB && !realExes.length)) kind = 'suspicious';
  else if (repackBins || (REPACK_NAME_RE.test(name) && !realExes.length)) kind = 'repack';
  else if (realExes.length) kind = 'plain';
  else if (hasArchive) kind = 'archive';
  else if (hasSetupExe) kind = 'installer';
  else if (relNames.some((n) => DISC_RE.test(n))) kind = 'disc-image';
  else if (hasNativeInstaller) kind = 'linux-installer';

  const picked = kind === 'plain' ? pickExe(entries, name) : { exe: null, alternatives: [] };
  if (kind === 'plain' && crackArchive) {
    warnings.push(`A crack is packed in ${crackArchive}. DRM-free releases (GOG) don't need it; if the game won't start, use Install to SD instead.`);
  }
  // Sony PC ports ship the PlayStation PC SDK runtime as an .msi that Steam runs on first start (installscript.vdf).
  // Nothing here runs it; whether a game needs it under Proton is not measured, so this is a warning, not a block.
  const psSdk = relNames.find((n) => /(^|\/)pspcsdkruntimeinstaller\.msi$/i.test(n));
  if (kind === 'plain' && psSdk) {
    warnings.push(`Ships the PlayStation PC SDK runtime installer (${base(psSdk)}), which DeckDebrid does not install. If the game exits at start, that may be the cause.`);
  }
  if (kind === 'plain' && picked.exe && inPoorDir(picked.exe.rel)) {
    warnings.push('The only executables are in backup/crack/redistributable folders; check the right one is chosen.');
  }

  const fit = kind === 'plain' ? streamFit(totalBytes) : null;
  let recommendation = 'unsupported';
  let reason = '';
  switch (kind) {
    case 'unsupported-hypervisor':
      recommendation = 'unsupported';
      reason = 'This release requires Windows kernel hypervisor drivers (SimpleSvm.sys / hyperkd.sys) to bypass Denuvo, which cannot run under Proton on SteamOS/Linux.';
      warnings.push('Contains Windows kernel hypervisor drivers (.sys) that cannot run on SteamOS / Proton.');
      break;
    case 'unsupported-uncracked':
      recommendation = 'unsupported';
      reason = 'Disabled Steam API (steam_api64.dll.bak) with no active crack or Steam emulator DLL in place.';
      warnings.push('Uncracked retail files: steam_api64.dll.bak is present without an active crack.');
      break;
    case 'needs-manual-step':
      recommendation = 'unsupported';
      reason = 'Needs a manual step: this release ships the original steam_api64.dll and the crack as separate files in a Crack/ folder to copy over by hand. Streamed as is, it exits at start (the Steam API is missing). DeckDebrid does not apply cracks.';
      warnings.push('Real steam_api64.dll plus a separate Crack/ folder: it will not start as streamed.');
      break;
    case 'plain':
      if (hasDirectStorage) {
        recommendation = 'cloudinstall';
        reason = 'This game uses Microsoft DirectStorage (dstorage.dll), which requires local disk access and stalls on network streaming. Copying to SD card is recommended.';
        warnings.push('Contains Microsoft DirectStorage (dstorage.dll): network streaming hangs on SteamOS; install to SD card to play.');
      } else if (fit === 'poor') {
        // Streams; the phone asks once first (user, 12 Oct: one tap, a confirm only for poor fit / > 25 GB). Cyberpunk's
        // 69 GB GOG-Rip streamed to its title screen on 15 Oct (pre-mount fill 9m31s, then ~8 min to the title).
        recommendation = 'stream';
        reason = 'Ready-to-play folder, but large: the first launch fetches what the game reads at start (Cyberpunk, 69 GB: about 18 minutes on 15 Oct). Later launches are quicker. Installing to the SD card is the alternative.';
      } else {
        recommendation = 'stream';
        reason = fit === 'good'
          ? 'Ready-to-play folder and small enough to stream well.'
          : 'Ready-to-play folder. Streaming should work, but the first launch will read a lot.';
      }
      break;
    case 'repack':
      recommendation = 'cloudinstall';
      reason = 'Installer-based repack: it cannot be streamed. Install it onto the SD card straight from TorBox (this can take hours).';
      break;
    case 'installer':
      recommendation = 'cloudinstall';
      reason = 'Looks like an installer (setup) release, not an extracted game, so it cannot be streamed.';
      break;
    case 'archive': {
      // Name what was found, so a surprising "archive" can be checked against the real files instead of guessed at.
      const archives = relNames.filter((n) => ARCHIVE_RE.test(n));
      const sample = base(archives[0] || '');
      recommendation = 'cloudinstall';
      reason = `Compressed archive (${archives.length} archive file${archives.length === 1 ? '' : 's'}, e.g. ${sample}; no game .exe outside them): it has to be unpacked first, so it cannot be streamed.`;
      // A torrent held as one "<name>.zip" was zipped by TorBox, not the uploader: TorBox ignores allow_zip=false when
      // someone else cached it zipped. Re-adding gets the same zip; a different release can come unpacked.
      if (!isUsenet && archives.length === 1 && name && sample.toLowerCase() === `${name.toLowerCase()}.zip`) {
        reason = `TorBox stored this torrent as one zip (${sample}): someone added it zipped before you, and TorBox ignores "don't zip" for torrents it already has, so it cannot be streamed. Install it to the SD card, or search again and pick a release marked "Unpacked in TorBox" (or one not in TorBox yet, which arrives unpacked).`;
      }
      break;
    }
    case 'switch':
      recommendation = 'download';
      reason = 'Nintendo Switch game: auto-delivers to EmuDeck Switch ROMs (/run/media/.../Emulation/roms/switch) for Eden emulator.';
      break;
    case 'disc-image':
      reason = 'Disc image (ISO): not supported yet.';
      break;
    case 'linux-installer':
      recommendation = 'cloudinstall';
      reason = 'Native Linux installer (GOG .sh): installs to the SD card and runs without Proton, lighter on battery. The installer script itself is never run.';
      break;
    case 'not-a-game':
      reason = 'Not a game (video, audio or book).';
      break;
    case 'suspicious':
      if (realExes.length) {
        reason = 'DO NOT RUN: this looks like a video/audio/book release that contains a program. That is a common malware trick.';
        warnings.push('A program inside a media-style release. Delete it from your TorBox library.');
      } else {
        reason = 'This release has no game files (only documents or a tiny download). It may be fake; do not install it.';
        warnings.push('No game executable and almost no data.');
      }
      break;
    default:
      reason = 'Could not recognise the contents of this release.';
  }
  if (infected) recommendation = 'unsupported';

  // Small startup files worth fetching first (executables, libraries, configs).
  const hotAll = entries
    .filter((e) => HOT_EXT_RE.test(e.rel) && e.size <= 64 * MB)
    .sort((a, b) => a.rel.split('/').length - b.rel.split('/').length || a.size - b.size);
  const hot = { count: hotAll.length, bytes: hotAll.reduce((a, e) => a + e.size, 0), files: hotAll.slice(0, 200).map((e) => e.rel) };
  const engine = kind === 'switch'
    ? { name: 'Nintendo Switch', id: 'switch', proton: 'Eden (Switch Emulator) / EmuDeck' }
    : detectEngine(entries);
  const starterProfile = (kind === 'plain' || kind === 'installer' || kind === 'archive')
    ? synthesizeStarterProfile(entries, hot.files, totalBytes)
    : null;
  const switchRom = kind === 'switch' ? entries.find((e) => SWITCH_RE.test(e.rel)) : null;

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
    rom: switchRom ? { name: base(switchRom.rel), rel: switchRom.rel, size: switchRom.size } : null,
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
