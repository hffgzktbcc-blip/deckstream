// In-App AI Assistant for DeckDebrid: Helps players find streamable games,
// understand release layouts, diagnose launch requirements, and optimize their Steam Deck.
// Supports live Google Gemini API with fallback to built-in expert knowledge engine.
const { applyCors, requireSecret } = require('./_lib');

const SYSTEM_PROMPT = `You are Deck AI, the built-in intelligent companion for DeckDebrid on Steam Deck.
You help gamers find games to stream or install, understand release formats, and resolve SteamOS/TorBox issues.

KEY DOMAIN FACTS:
1. Streaming vs Installing:
   - Streams (0 GB install, plays directly off the TorBox WebDAV mount): ONLY releases TorBox holds ALREADY UNPACKED as a game folder with the game's .exe (SteamRIP, pre-installed, portable, InsaneRamZes torrents usually are, unless TorBox zipped them).
   - Installs (cloudinstall: extracts to microSD card on Deck): FitGirl, DODI, ElAmigos, KaOs, GOG offline installers (setup_*.exe + .bin), and scene releases (RUNE, TENOKE, FLT, CODEX with .iso/.rar).
2. The TorBox Zip Rule:
   - TorBox automatically zips torrents containing over 100 files, and it IGNORES allow_zip=false when someone else already cached the torrent as a zip archive.
   - A release showing "Zipped in TorBox" or single .zip file CANNOT be streamed; it must be installed to Deck.
3. The user's own library:
   - Which of the user's games stream or install is given below as "Library" context, worked out from TorBox's real file
     lists. Only name games from that context as streamable; never guess from a title, and never claim a game streams
     because it is cached in TorBox.
   - SteamRIP / SteamGG web downloads arrive in TorBox as a .zip, so they install; only an unpacked game folder streams.
4. Tone & Style:
   - Keep answers direct, friendly, and practical. Use Markdown formatting (bold, bullet points).
   - Suggest direct actions like searching or checking the Library.`;

// Names the phone sends from the user's TorBox library, already judged by their files (index.html classifyItem).
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

// Built-in Expert Engine: answers instantly with zero external dependencies. It states rules, and names games only
// from the user's own library (the context), never from a typed-in list that goes stale.
function expertEngineReply(message, context = {}) {
  const m = String(message || '').toLowerCase();
  const { streamable, install } = libraryLists(context);
  const has = (...words) => words.some(w => m.includes(w));

  // 1. Which games stream? (from the library, judged by files)
  // "Which of my games stream?", "what streams?", "do any of my games stream": a question about streaming, not a how-to
  const asksWhatStreams = /\b(which|what|any)\b[^.?!]*\bstream(s|able|ing)?\b/.test(m) && !/\bwhy\b|\bhow\b/.test(m);
  if (asksWhatStreams || has('which game', 'what game', 'can stream', 'can i stream', 'streamable', 'stream right now', 'stream now')) {
    if (!streamable.length) {
      return {
        reply: `**None of the games in your TorBox library stream yet.**\n\n` +
          `A game streams only when TorBox holds it as an **unpacked game folder** with its .exe.` +
          (install.length ? ` These are in your library but install first: ${install.slice(0, 8).map(n => `*${n}*`).join(', ')}.` : '') +
          `\n\nIn Search, pick a release marked **⚡ Unpacked in TorBox**, or a SteamRIP / Portable / InsaneRamZes torrent TorBox doesn't have yet (it arrives unpacked).`,
        suggestions: ['Why does a game say Compressed archive?', 'SteamRIP vs repacks', 'How do I free up space?']
      };
    }
    return {
      reply: `**In your library, these stream now (0 GB on the Deck):**\n\n` +
        streamable.map(n => `• ${n}`).join('\n') +
        (install.length ? `\n\n**These install first** (zip, repack or installer): ${install.slice(0, 8).map(n => `*${n}*`).join(', ')}.` : '') +
        `\n\n*This comes from TorBox's real file lists, not the release names.*`,
      suggestions: ['Why does a game say Compressed archive?', 'SteamRIP vs repacks', 'How do I free up space?']
    };
  }

  // 2. Archive / zip / "Compressed archive"
  if (has('zip', 'archive', 'compressed', 'needs install', 'unpack')) {
    return {
      reply: `**Why a game says "Compressed archive" or "Needs installing":**\n\n` +
        `• **TorBox zips big torrents.** Over 100 files, it packs them into one .zip. DeckDebrid asks it not to, but when someone else already added that torrent zipped, TorBox hands everyone the zip.\n` +
        `• **Web downloads are zips too.** SteamRIP and SteamGG file-host links arrive as one .zip.\n` +
        `• **A zip can't stream.** The Deck has to unpack it first.\n\n` +
        `**What to do:** tap **Install to Deck** to unpack it onto the microSD card, or pick a release marked **⚡ Unpacked in TorBox** in Search.`,
      suggestions: ['Which games can I stream?', 'SteamRIP vs repacks', 'How do I free up space?']
    };
  }

  // 3. Repack vs SteamRIP
  if (has('repack', 'steamrip', 'steam rip', 'fitgirl', 'dodi', 'elamigos', 'gog')) {
    return {
      reply: `**SteamRIP vs repacks:**\n\n` +
        `• **SteamRIP / Portable / InsaneRamZes torrents:** the game folder, already unpacked. **Streams**, as long as TorBox holds it unpacked (not zipped).\n` +
        `• **FitGirl, DODI, ElAmigos repacks and GOG installers:** compressed installers (setup.exe + .bin). They **install** to the microSD card first.\n` +
        `• **Scene releases (RUNE, TENOKE, FLT…):** .rar archives, often with a disc image inside. They install too.`,
      suggestions: ['Which games can I stream?', 'Why does a game say Compressed archive?']
    };
  }

  // 4. Storage
  if (has('space', 'storage', ' sd', 'microsd', 'clean', 'delete', 'full')) {
    return {
      reply: `**Freeing space on the Deck:**\n\n` +
        `• **Streamed games** keep only a cache of the files the game has read on the card.\n` +
        `• **Deck tab → Free up space:** **Clear cache** frees a streamed game's cache (it streams again next time); **Delete** removes an installed game. Your saves are never touched.`,
      suggestions: ['Which games can I stream?', 'SteamRIP vs repacks']
    };
  }

  // 5. Controller
  if (has('controller', 'gamepad', 'input', 'joystick', 'buttons')) {
    return {
      reply: `**Controller:**\n\n` +
        `• Before a streamed game starts, the runner waits up to 5 seconds for Steam Input's virtual pad ("Microsoft X-Box 360 pad") to appear, then launches either way.\n` +
        `• Launch from **Gaming Mode** so Steam Input applies your layout.`,
      suggestions: ['Which games can I stream?', 'A game crashes at start']
    };
  }

  // 6. Switch
  if (has('switch', 'emudeck', 'nsp', 'xci', 'yuzu', 'ryujinx', 'eden')) {
    return {
      reply: `**Nintendo Switch:**\n\n` +
        `• Releases with .nsp / .xci / .nsz files are Switch games.\n` +
        `• **Send to EmuDeck** downloads them into EmuDeck's Switch ROMs folder (Emulation/roms/switch); Steam ROM Manager can add them to Gaming Mode.`,
      suggestions: ['Which games can I stream?']
    };
  }

  // 7. Crashes
  if (has('crash', 'error', 'dll', 'won\'t start', 'wont start', 'black screen', 'proton')) {
    return {
      reply: `**A game that closes at start:**\n\n` +
        `• When a streamed game exits within 30 seconds, the runner checks the Proton log for known crash signatures and writes what it found to the runner log.\n` +
        `• Check the game's **Deck rating** (ProtonDB) in Search, and try **GE-Proton** if the rating suggests it.\n` +
        `• If a release only installs (zip or repack), use **Install to Deck** instead of streaming.`,
      suggestions: ['Which games can I stream?', 'Why does a game say Compressed archive?']
    };
  }

  // Default
  return {
    reply: `I'm **Deck AI**. I can help with:\n\n` +
      `• **What streams:** ask *"Which games can I stream?"*; I read it from your TorBox library.\n` +
      `• **Release types:** zips, repacks, SteamRIP, Switch.\n` +
      `• **Storage, controllers and crashes** on the Deck.`,
    suggestions: ['Which games can I stream?', 'Why does a game say Compressed archive?', 'How do I free up space?']
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
    signal: AbortSignal.timeout(15000)
  });

  if (!res.ok) {
    const errText = await res.text().catch(() => '');
    throw new Error(`Gemini API error ${res.status}: ${errText.slice(0, 150)}`);
  }

  const json = await res.json();
  const text = json?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!text) throw new Error('Empty response from Gemini API');

  return {
    reply: text.trim(),
    source: 'gemini',
    suggestions: ['Tell me more', 'Which games stream without installing?', 'Search releases']
  };
}

module.exports = async (req, res) => {
  if (applyCors(req, res, 'POST, OPTIONS')) return;
  if (!requireSecret(req, res)) return;

  if (req.method !== 'POST') {
    return res.status(405).json({ error: 'Method not allowed. Use POST.' });
  }

  const { message, context, apiKey } = req.body || {};
  const queryMessage = String(message || '').trim();

  if (!queryMessage) {
    return res.status(400).json({ error: 'Message is required.' });
  }

  const geminiKey = (typeof apiKey === 'string' && apiKey.trim()) || process.env.GEMINI_API_KEY || '';

  // Try live Gemini API if key is present
  if (geminiKey) {
    try {
      const geminiRes = await callGemini(queryMessage, geminiKey, context);
      return res.json({ success: true, ...geminiRes });
    } catch (err) {
      console.warn('Gemini API call failed, falling back to expert engine:', err.message);
    }
  }

  // Fallback to built-in expert engine
  const expertRes = expertEngineReply(queryMessage, context);
  return res.json({
    success: true,
    reply: expertRes.reply,
    source: 'expert_engine',
    suggestions: expertRes.suggestions || []
  });
};
module.exports._expertEngineReply = expertEngineReply;
