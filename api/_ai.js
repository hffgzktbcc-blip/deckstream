// In-App AI Assistant for DeckStream: Helps players stream PC games from their remote storage,
// understand VFS caching, diagnose Proton requirements, and optimize their Steam Deck.
// Supports live Google Gemini API with fallback to built-in expert knowledge engine.
const { applyCors, requireSecret } = require('./_lib');

const SYSTEM_PROMPT = `You are Deck AI, the built-in intelligent companion for DeckStream on Steam Deck.
You help gamers stream and play PC games from their remote storage (NAS, WebDAV, TrueNAS, Nextcloud, S3) on SteamOS.

KEY FACTS:
1. Virtual FUSE Streaming:
   - Games stream on demand over WebDAV/S3 via rclone VFS and fuse-overlayfs.
   - The remote storage stays strictly read-only; all game saves, settings, and DXVK caches write to local MicroSD.
2. Smart Startup Profiler:
   - On the first cold launch, DeckStream records the byte ranges the game engine accesses during boot.
   - Subsequent launches fetch those ranges in 16 parallel streams, cutting boot time from minutes to under 90 seconds.
3. Handheld Optimization:
   - Deck Mode caps framerates at 30 FPS and applies low graphics presets, boosting battery life up to 130%.
4. Saves & Integrity:
   - Game saves are backed up after every session using Ludusavi.
5. Tone:
   - Helpful, concise, gamer-friendly, and technical when asked.`;

// Plain strings only, capped, so a reply can quote them without trusting anything else in the request.
function libraryLists(context) {
  const clean = (list) => (Array.isArray(list) ? list : [])
    .map(x => String(x == null ? '' : x).replace(/[\u0000-\u001f<>*_`\[\]]/g, '').trim().slice(0, 120))
    .filter(Boolean).slice(0, 30);
  return { streamable: clean(context && context.streamable), install: clean(context && context.install) };
}

function libraryLine(context) {
  const { streamable, install } = libraryLists(context);
  return `Library: streams = ${streamable.length ? streamable.join('; ') : 'none'}; installs = ${install.length ? install.join('; ') : 'none'}.`;
}

// Built-in Expert Engine: answers instantly with zero external dependencies.
function expertEngineReply(message, context = {}) {
  const m = String(message || '').toLowerCase();
  const { streamable, install } = libraryLists(context);
  const has = (...words) => words.some(w => m.includes(w));

  // 1. Which games stream?
  const asksWhatStreams = /\b(which|what|any)\b[^.?!]*\bstream(s|able|ing)?\b/.test(m) && !/\bwhy\b|\bhow\b/.test(m);
  if (asksWhatStreams || has('which game', 'what game', 'can stream', 'can i stream', 'streamable', 'stream right now', 'stream now')) {
    if (!streamable.length) {
      return {
        reply: `**No streamable games detected in your remote storage yet.**\n\n` +
          `A game streams instantly when your remote storage holds it as an **unpacked game folder** with its main Windows executable (.exe).` +
          (install.length ? ` These are in your library but need downloading or extracting first: ${install.slice(0, 8).map(n => `*${n}*`).join(', ')}.` : '') +
          `\n\nMount your NAS, Nextcloud, TrueNAS, or S3 bucket in Settings to populate your cartridge shelf.`,
        suggestions: ['How does VFS streaming work?', 'How do I add a game folder?', 'How do I free up space?']
      };
    }
    return {
      reply: `**In your remote library, these stream now (0 GB on the Deck):**\n\n` +
        streamable.map(n => `• ${n}`).join('\n') +
        (install.length ? `\n\n**These need installation or extracting first**: ${install.slice(0, 8).map(n => `*${n}*`).join(', ')}.` : '') +
        `\n\n*Streamed directly over chunk-cached VFS.*`,
      suggestions: ['How does VFS streaming work?', 'How do I free up space?']
    };
  }

  // 2. VFS / Streaming explanation
  if (has('vfs', 'how it works', 'how does streaming work', 'fuse', 'overlayfs', 'streaming work')) {
    return {
      reply: `**How DeckStream VFS Streaming Works:**\n\n` +
        `• **Zero Local Footprint:** Games remain on your remote storage (NAS, WebDAV, S3, or TrueNAS).\n` +
        `• **Chunk-Level Caching:** When the game requests a texture or audio file, DeckStream streams only those specific byte ranges into a fast local VFS cache.\n` +
        `• **Copy-on-Write Saves:** Remote storage is strictly read-only. Game saves and config files are written locally to your microSD card and synced with Ludusavi.\n` +
        `• **Proton Compatibility:** Games run locally on the Steam Deck's GPU via Proton with native controller input.`,
      suggestions: ['Which games can I stream?', 'How do I add a game folder?', 'How do I free up space?']
    };
  }

  // 3. Storage & Space
  if (has('space', 'storage', ' sd', 'microsd', 'clean', 'delete', 'full', 'cache')) {
    return {
      reply: `**Managing Storage & VFS Cache on Steam Deck:**\n\n` +
        `• **Streamed games** only take space for files the game has actually read (usually 10-20% of full game size).\n` +
        `• **Deck tab → Free up space:** Tap **Clear cache** to purge a game's local read cache (saves remain 100% intact, game re-streams next time).\n` +
        `• **Delete:** Removes an installed game package from the microSD card.`,
      suggestions: ['Which games can I stream?', 'How does VFS streaming work?']
    };
  }

  // 4. Controller & Input
  if (has('controller', 'gamepad', 'input', 'joystick', 'buttons', 'steam input')) {
    return {
      reply: `**Steam Deck Controller Setup:**\n\n` +
        `• DeckStream automatically connects to Steam Input's virtual gamepad on launch.\n` +
        `• Always launch games from **Steam Gaming Mode** so your custom controller configurations and gyro settings apply seamlessly.`,
      suggestions: ['Which games can I stream?', 'A game crashes at start']
    };
  }

  // 5. Crashes & Proton
  if (has('crash', 'error', 'dll', 'won\'t start', 'wont start', 'black screen', 'proton', 'ge-proton')) {
    return {
      reply: `**Troubleshooting Game Launch:**\n\n` +
        `• If a game closes quickly at start, inspect the Proton log in DeckStream's Recent jobs card.\n` +
        `• Check the game's **ProtonDB rating** in the app. For games requiring specific media codecs, switch Proton version to **GE-Proton** in Steam Game Properties.\n` +
        `• Verify that all game prerequisites (DirectX, VC++ redistributables) are present in the game directory.`,
      suggestions: ['Which games can I stream?', 'How does VFS streaming work?']
    };
  }

  // Default
  return {
    reply: `I'm **Deck AI**. I can help you with:\n\n` +
      `• **Streamable games:** Ask *"Which games can I stream?"* to check your remote library.\n` +
      `• **Streaming architecture:** Ask *"How does VFS streaming work?"*.\n` +
      `• **Storage, Steam Input, and Proton compatibility** on your Steam Deck.`,
    suggestions: ['Which games can I stream?', 'How does VFS streaming work?', 'How do I free up space?']
  };
}

// Call the live Google Gemini API (gemini-2.5-flash)
async function callGemini(message, apiKey, context = {}) {
  const url = `https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=${encodeURIComponent(apiKey)}`;
  const contextStr = context ? `User context: Current tab=${String(context.currentTab || 'unknown').slice(0, 20)}, Query=${String(context.query || 'none').slice(0, 100)}, Library games=${Number(context.libraryCount) || 0}. ${libraryLine(context)}` : '';
  const body = {
    system_instruction: {
      parts: [{ text: SYSTEM_PROMPT + '\n' + contextStr }]
    },
    contents: [
      { role: 'user', parts: [{ text: message }] }
    ],
    generationConfig: {
      temperature: 0.7,
      maxOutputTokens: 800
    }
  };

  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(12000)
  });

  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`Gemini API error (${res.status}): ${errText.slice(0, 150)}`);
  }

  const data = await res.json();
  const candidate = data && data.candidates && data.candidates[0];
  const reply = candidate && candidate.content && candidate.content.parts && candidate.content.parts[0] && candidate.content.parts[0].text;
  if (!reply) throw new Error('Gemini API returned an empty response');

  return { reply: reply.trim(), suggestions: ['Which games can I stream?', 'How does VFS streaming work?'] };
}

module.exports = async (req, res) => {
  if (applyCors(req, res, 'POST, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  const { message, apiKey, context } = req.body || {};
  if (!message || typeof message !== 'string') {
    return res.status(400).json({ error: 'Missing message parameter' });
  }

  try {
    if (apiKey && typeof apiKey === 'string' && apiKey.trim().length > 10) {
      try {
        const geminiRes = await callGemini(message, apiKey.trim(), context);
        return res.json({ ...geminiRes, source: 'gemini' });
      } catch (err) {
        console.warn('Gemini API call failed, falling back to expert engine:', err.message);
      }
    }

    const expertRes = expertEngineReply(message, context);
    return res.json({ ...expertRes, source: 'expert' });
  } catch (err) {
    return res.status(500).json({ error: err.message || 'Internal AI error' });
  }
};
