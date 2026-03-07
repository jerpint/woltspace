#!/usr/bin/env node
// container/cron/digest.mjs
// Daily digest pipeline — fetch → select (LLM) → render (template) → push
// Phase 1: parallel JS fetches (~5s)
// Phase 2: claude picks items + writes reflection (~10s, ONE turn, no tools)
// Phase 3: JS renders HTML template + writes spark (instant)

import { readFileSync, existsSync, readdirSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';
import { request as httpRequest } from 'node:http';
import { randomBytes } from 'node:crypto';
import { spawn as spawnProcess } from 'node:child_process';

const __dirname = dirname(fileURLToPath(import.meta.url));
const WOLT_DIR   = process.env.WOLT_DIR || '/workspace/wolt';
const MEMORY_DIR = join(WOLT_DIR, 'wolt', 'memory');
const SPARKS_DIR = join(WOLT_DIR, 'wolt', 'sparks');
const WOLT_NAME  = process.env.WOLT_NAME || 'wolt';
const HUMAN_NAME = process.env.HUMAN_NAME || '';

// -- Spotify API --

const SPOTIFY_ID     = process.env.SPOTIFY_ID;
const SPOTIFY_SECRET = process.env.SPOTIFY_SECRET;
const SPOTIFY_USER   = process.env.SPOTIFY_USER;
let spotifyAccessToken  = process.env.SPOTIFY_ACCESS_TOKEN;
let spotifyRefreshToken = process.env.SPOTIFY_REFRESH_TOKEN;

async function refreshSpotifyToken() {
  if (!SPOTIFY_ID || !SPOTIFY_SECRET || !spotifyRefreshToken) {
    console.log('[spotify] missing credentials, skipping');
    return null;
  }
  const basic = Buffer.from(SPOTIFY_ID + ':' + SPOTIFY_SECRET).toString('base64');
  const body = 'grant_type=refresh_token&refresh_token=' + encodeURIComponent(spotifyRefreshToken);
  const res = await fetch('https://accounts.spotify.com/api/token', {
    method: 'POST',
    headers: {
      'Authorization': 'Basic ' + basic,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body,
    signal: AbortSignal.timeout(10000),
  });
  if (!res.ok) {
    console.error('[spotify] token refresh failed:', res.status, await res.text());
    return null;
  }
  const data = await res.json();
  spotifyAccessToken = data.access_token;
  if (data.refresh_token) spotifyRefreshToken = data.refresh_token;
  console.log('[spotify] token refreshed');
  return spotifyAccessToken;
}

async function searchSpotifyTrack(artist, title) {
  if (!spotifyAccessToken) return null;
  const q = encodeURIComponent(artist + ' ' + title);
  const res = await fetch('https://api.spotify.com/v1/search?q=' + q + '&type=track&limit=3', {
    headers: { 'Authorization': 'Bearer ' + spotifyAccessToken },
    signal: AbortSignal.timeout(8000),
  });
  if (!res.ok) return null;
  const data = await res.json();
  const items = data.tracks?.items;
  if (!items?.length) return null;
  const normalize = s => (s || '').toLowerCase().replace(/the\s+/g, '').trim();
  const target = normalize(artist);
  const best = items.find(t => normalize(t.artists?.[0]?.name).includes(target.slice(0, 8))) || items[0];
  return { uri: best.uri, id: best.id, name: best.name, artist: best.artists?.[0]?.name };
}

async function createSpotifyPlaylist(name, description) {
  if (!spotifyAccessToken || !SPOTIFY_USER) return null;
  const res = await fetch('https://api.spotify.com/v1/users/' + SPOTIFY_USER + '/playlists', {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + spotifyAccessToken,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ name, public: true, description }),
    signal: AbortSignal.timeout(10000),
  });
  if (!res.ok) {
    console.error('[spotify] playlist creation failed:', res.status);
    return null;
  }
  return res.json();
}

async function addTracksToPlaylist(playlistId, uris) {
  if (!spotifyAccessToken || !uris.length) return;
  await fetch('https://api.spotify.com/v1/playlists/' + playlistId + '/tracks', {
    method: 'POST',
    headers: {
      'Authorization': 'Bearer ' + spotifyAccessToken,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ uris }),
    signal: AbortSignal.timeout(10000),
  });
}

// -- Montreal time --

function montrealHour() {
  return parseInt(new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Montreal', hour: 'numeric', hour12: false,
  }).format(new Date()));
}

function montrealDateStr() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Montreal',
    weekday: 'long', month: 'long', day: 'numeric', year: 'numeric',
  }).format(new Date());
}

function montrealShort() {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Montreal', month: 'short', day: 'numeric', year: 'numeric',
  }).format(new Date()).toLowerCase();
}

function greeting() {
  const h = montrealHour();
  const name = HUMAN_NAME || 'there';
  if (h < 5)  return "still dark out";
  if (h < 9)  return "good morning " + name;
  if (h < 12) return "morning " + name;
  if (h < 14) return "good afternoon " + name;
  if (h < 18) return "afternoon " + name;
  if (h < 21) return "good evening " + name;
  return "late night " + name;
}

// -- Memory --

function loadMemory() {
  return ['identity.md']
    .map(f => {
      const p = join(MEMORY_DIR, f);
      if (!existsSync(p)) return '';
      return readFileSync(p, 'utf8').slice(0, 2000);
    })
    .join('\n');
}

function loadTasteProfile() {
  const p = join(MEMORY_DIR, 'music-taste.md');
  if (!existsSync(p)) return '';
  return readFileSync(p, 'utf8');
}

function getRecentPlaylistArtists(n = 5) {
  if (!existsSync(SPARKS_DIR)) return [];
  const files = readdirSync(SPARKS_DIR)
    .filter(f => f.endsWith('.json'))
    .sort((a, b) => b.localeCompare(a))
    .slice(0, n * 3);

  const artists = [];
  let found = 0;
  for (const f of files) {
    if (found >= n) break;
    try {
      const d = JSON.parse(readFileSync(join(SPARKS_DIR, f), 'utf8'));
      if (d.type === 'spark' && d.title?.includes('digest') && d.html) {
        const fromTags = [...d.html.matchAll(/music-artist[^>]*>([^<]+)/g)].map(m => m[1].trim());
        artists.push(...fromTags);
        found++;
      }
    } catch { /* skip */ }
  }
  return [...new Set(artists)];
}

function pickMusicConcept() {
  const tasteProfile = loadTasteProfile();
  const queueMatch = tasteProfile.match(/## Unexplored queue[\s\S]*?(?=\n## |\n$|$)/);
  if (!queueMatch) return null;

  const lines = queueMatch[0].split('\n').filter(l => l.startsWith('- '));
  if (lines.length === 0) return null;

  const dayOfYear = Math.floor((Date.now() - new Date(new Date().getFullYear(), 0, 0)) / 86400000);
  const idx = dayOfYear % lines.length;
  const concept = lines[idx].replace(/^-\s*/, '').trim();

  return concept;
}

// -- Recent sparks (dedup) --

function recentSparks(n = 4) {
  if (!existsSync(SPARKS_DIR)) return [];
  const files = readdirSync(SPARKS_DIR)
    .filter(f => f.endsWith('.json'))
    .sort((a, b) => b.localeCompare(a))
    .slice(0, n);

  const items = [];
  for (const f of files) {
    try {
      const d = JSON.parse(readFileSync(join(SPARKS_DIR, f), 'utf8'));
      if (d.type === 'spark' && d.title?.includes('digest') && d.html) {
        const headings = [...d.html.matchAll(/<card-title[^>]*>([^<]+)/g)].map(m => m[1].trim());
        const artists  = [...d.html.matchAll(/music-artist[^>]*>([^<]+)/g)].map(m => m[1].trim());
        headings.slice(0, 6).forEach(h => items.push(h));
        artists.forEach(t => items.push(t));
      }
    } catch { /* skip */ }
  }
  return items;
}

// -- Push to right pane --

function pushToPane(sparkId) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ url: '/history/' + sparkId });
    const req = httpRequest({
      host: 'localhost', port: 3000, path: '/current', method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, res => { res.resume(); res.on('end', resolve); });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

// -- Phase 1: Parallel fetching (no LLM) --

async function fetchJSON(url) {
  const res = await fetch(url, {
    headers: { 'User-Agent': 'Woltspace/1.0' },
    signal: AbortSignal.timeout(10000),
  });
  return res.json();
}

async function fetchText(url) {
  const res = await fetch(url, {
    headers: { 'User-Agent': 'Woltspace/1.0' },
    signal: AbortSignal.timeout(10000),
  });
  return res.text();
}

function extractOG(html) {
  const get = (prop) => {
    const m = html.match(new RegExp('<meta[^>]+property=["\']og:' + prop + '["\'][^>]+content=["\']([^"\']+)["\']', 'i'))
           || html.match(new RegExp('<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:' + prop + '["\']', 'i'));
    return m ? m[1] : null;
  };
  return { og_title: get('title'), og_description: get('description'), og_image: get('image') };
}

async function fetchHN() {
  console.log('[fetch] HN top stories...');
  try {
    const ids = await fetchJSON('https://hacker-news.firebaseio.com/v0/topstories.json');
    const top30 = ids.slice(0, 30);
    const stories = await Promise.allSettled(
      top30.map(id => fetchJSON('https://hacker-news.firebaseio.com/v0/item/' + id + '.json'))
    );
    return stories
      .filter(r => r.status === 'fulfilled' && r.value && r.value.url)
      .map(r => ({
        source: 'hn',
        title: r.value.title,
        url: r.value.url,
        score: r.value.score,
        hn_id: r.value.id,
      }));
  } catch (e) {
    console.error('[fetch] HN failed:', e.message);
    return [];
  }
}

async function fetchLobsters() {
  console.log('[fetch] Lobsters...');
  try {
    const stories = await fetchJSON('https://lobste.rs/hottest.json');
    return stories.slice(0, 20).map(s => ({
      source: 'lobsters',
      title: s.title,
      url: s.url || s.comments_url,
      score: s.score,
      tags: (s.tags || []).join(', '),
      description: s.description || '',
    }));
  } catch (e) {
    console.error('[fetch] Lobsters failed:', e.message);
    return [];
  }
}

async function fetchHFPapers() {
  console.log('[fetch] HuggingFace papers...');
  try {
    const html = await fetchText('https://huggingface.co/papers');
    const paperIds = [...new Set([...html.matchAll(/\/papers\/([\d]+\.[\d]+)/g)].map(m => m[1]))];
    console.log('[fetch] found ' + paperIds.length + ' HF paper IDs');

    const papers = await Promise.allSettled(
      paperIds.slice(0, 10).map(async id => {
        const txt = await fetchText('https://arxiv-txt.org/abs/' + id);
        const titleMatch = txt.match(/^#\s*Title\s*\n(.+?)(?:\n|$)/im) || txt.match(/Title:\s*(.+?)(?:\n|$)/i) || txt.match(/^(.+?)(?:\n|$)/);
        const abstractMatch = txt.match(/^#\s*Abstract\s*\n([\s\S]+?)(?:\n\n|\n#)/im) || txt.match(/Abstract[:\s]*\n?([\s\S]+?)(?:\n\n|\n[A-Z])/i);
        return {
          source: 'hf_paper',
          title: titleMatch ? titleMatch[1].trim() : 'Paper ' + id,
          abstract: abstractMatch ? abstractMatch[1].trim().slice(0, 400) : txt.slice(0, 400),
          url: 'https://huggingface.co/papers/' + id,
          arxiv_url: 'https://arxiv.org/abs/' + id,
          paper_id: id,
        };
      })
    );
    return papers
      .filter(r => r.status === 'fulfilled')
      .map(r => r.value);
  } catch (e) {
    console.error('[fetch] HF papers failed:', e.message);
    return [];
  }
}

async function enrichWithOG(items) {
  console.log('[fetch] OG metadata for ' + items.length + ' items...');
  const enriched = await Promise.allSettled(
    items.map(async item => {
      if (!item.url || item.source === 'hf_paper') return item;
      try {
        const html = await fetchText(item.url);
        const og = extractOG(html);
        return { ...item, ...og };
      } catch {
        return item;
      }
    })
  );
  return enriched.map(r => r.status === 'fulfilled' ? r.value : r.reason);
}

// -- HTML Template --

function renderHTML(data) {
  const { dateStr, greeting, picks, papers, playlistId, musicWriteup, conceptTitle, reflection } = data;

  const esc = s => (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

  const cardHTML = picks.map(p => {
    const imgPart = p.og_image
      ? '<img class="card-img" src="' + esc(p.og_image) + '" alt="" loading="lazy">'
      : '';
    const tagClass = p.source === 'lobsters' ? 'lobsters' : p.source === 'hf_paper' ? 'paper' : 'hn';
    return '<div class="card"><a href="' + esc(p.url) + '" target="_blank" rel="noopener">'
      + imgPart
      + '<div class="card-body"><div class="card-meta"><span class="tag ' + tagClass + '">' + esc(p.source) + '</span></div>'
      + '<div class="card-title">' + esc(p.title) + '</div>'
      + (p.description ? '<div class="card-desc">' + esc(p.description) + '</div>' : '')
      + '</div></a></div>';
  }).join('\n    ');

  const paperHTML = papers.map(p =>
    '<div class="paper-card"><a href="' + esc(p.url) + '" target="_blank" rel="noopener">'
    + '<div class="card-meta" style="margin-bottom:8px"><span class="tag paper">paper</span></div>'
    + '<div class="paper-title">' + esc(p.title) + '</div>'
    + '<div class="paper-abstract">' + esc((p.abstract || '').slice(0, 200)) + '</div>'
    + '</a></div>'
  ).join('\n    ');

  const musicIframe = playlistId
    ? '<iframe style="border-radius:12px" src="https://open.spotify.com/embed/playlist/'
      + esc(playlistId) + '?utm_source=generator&theme=0" width="100%" height="152" frameBorder="0"'
      + ' allow="autoplay;clipboard-write;encrypted-media;fullscreen;picture-in-picture" loading="lazy"></iframe>'
    : '';

  const musicHTML = musicIframe
    ? '<div class="music-section">'
      + (conceptTitle ? '<div class="music-concept">' + esc(conceptTitle) + '</div>' : '')
      + musicIframe
      + (musicWriteup ? '<div class="music-writeup">' + esc(musicWriteup) + '</div>' : '')
      + '</div>'
    : '';

  return '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>' + esc(WOLT_NAME) + ' digest</title>\n<style>\n'
+ `*{box-sizing:border-box;margin:0;padding:0}
body{background:#0e1621;color:#cdd9e5;font-family:'SF Mono','Fira Code','Courier New',monospace;min-height:100vh;padding-bottom:60px}
@keyframes fadeIn{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:translateY(0)}}
.topbar{padding:22px 32px;border-bottom:1px solid #1e2d3d;display:flex;justify-content:space-between;align-items:center;animation:fadeIn .35s ease}
.topbar .date{color:#7ec89a;font-size:.82rem;letter-spacing:.08em}
.topbar .greeting{color:#cdd9e5;opacity:.5;font-size:.82rem}
.container{max-width:860px;margin:0 auto;padding:40px 24px}
.section{margin-bottom:48px}
.section:nth-child(1){animation:fadeIn .4s .1s ease both}
.section:nth-child(2){animation:fadeIn .4s .2s ease both}
.section:nth-child(3){animation:fadeIn .4s .3s ease both}
.section:nth-child(4){animation:fadeIn .4s .4s ease both}
.section-label{font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:#7ec89a;margin-bottom:16px;opacity:.75}
.card{background:#131f2e;border:1px solid #1e2d3d;border-radius:6px;margin-bottom:10px;overflow:hidden;transition:border-color .2s}
.card:hover{border-color:#7ec89a55}
.card a{text-decoration:none;color:inherit;display:flex}
.card-img{width:120px;min-width:120px;height:80px;object-fit:cover;display:block;background:#0e1621}
.card-body{padding:13px 15px;flex:1}
.card-meta{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.tag{font-size:.62rem;letter-spacing:.1em;text-transform:uppercase;padding:2px 6px;border-radius:3px;background:#0e1621;color:#7ec89a;border:1px solid #7ec89a44}
.tag.lobsters{color:#e8a87c;border-color:#e8a87c44}
.tag.paper{color:#a67ce8;border-color:#a67ce844}
.card-title{font-size:.88rem;line-height:1.4;color:#cdd9e5}
.card-desc{font-size:.73rem;color:#cdd9e5;opacity:.45;margin-top:4px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.paper-card{background:#131f2e;border:1px solid #1e2d3d;border-left:3px solid #a67ce8;border-radius:6px;padding:16px;margin-bottom:10px;transition:border-color .2s}
.paper-card:hover{border-color:#a67ce855;border-left-color:#a67ce8}
.paper-card a{text-decoration:none;color:inherit;display:block}
.paper-title{font-size:.85rem;color:#cdd9e5;margin-bottom:8px;line-height:1.4}
.paper-abstract{font-size:.72rem;color:#cdd9e5;opacity:.45;line-height:1.5}
.nw-section{border-top:1px solid #1e2d3d;padding-top:32px}
.nw-quote{font-size:.88rem;line-height:1.75;color:#cdd9e5;opacity:.8;font-style:italic;max-width:640px}
.nw-byline{margin-top:14px;color:#7ec89a;font-size:.78rem}
.music-section{padding:20px 24px;animation:fadeIn .4s .05s ease both}
.music-concept{font-size:.72rem;letter-spacing:.12em;text-transform:uppercase;color:#e8a87c;margin-bottom:12px;opacity:.85}
.music-writeup{font-size:.78rem;line-height:1.65;color:#cdd9e5;opacity:.6;margin-top:14px;max-width:640px;font-style:italic}
`
+ '</style>\n</head>\n<body>\n\n<div class="topbar">\n  <span class="date">' + esc(dateStr) + '</span>\n  <span class="greeting">' + esc(greeting) + '</span>\n</div>\n\n'
+ (musicHTML ? musicHTML + '\n' : '')
+ '<div class="container">\n\n'
+ '  <div class="section">\n    <div class="section-label">from the web</div>\n    ' + cardHTML + '\n  </div>\n\n'
+ (papers.length ? '  <div class="section">\n    <div class="section-label">research</div>\n    ' + paperHTML + '\n  </div>\n\n' : '')
+ '  <div class="section nw-section">\n    <div class="nw-quote">' + esc(reflection) + '</div>\n    <div class="nw-byline">&mdash; ' + esc(WOLT_NAME) + '</div>\n  </div>\n\n'
+ '</div>\n</body>\n</html>';
}

// -- Main --

async function runDigest() {
  const timeStr    = montrealDateStr();
  const shortDate  = montrealShort();
  const hello      = greeting();
  const memory     = loadMemory();
  const recent     = recentSparks(4);
  const sparkId    = 'digest-' + randomBytes(4).toString('hex');

  console.log('[digest] starting — ' + timeStr);
  const t0 = Date.now();

  // Phase 1: Parallel fetch
  console.log('[digest] phase 1: fetching sources...');

  const [hn, lobsters, papers] = await Promise.all([
    fetchHN(),
    fetchLobsters(),
    fetchHFPapers(),
  ]);

  console.log('[digest] fetched: ' + hn.length + ' HN, ' + lobsters.length + ' lobsters, ' + papers.length + ' papers');

  const topHN = hn.sort((a, b) => b.score - a.score).slice(0, 10);
  const topLobsters = lobsters.sort((a, b) => b.score - a.score).slice(0, 8);
  const enriched = await enrichWithOG([...topHN, ...topLobsters]);

  const fetchTime = ((Date.now() - t0) / 1000).toFixed(1);
  console.log('[digest] phase 1 done in ' + fetchTime + 's');

  const slim = (items) => items.map((item, i) => ({
    idx: i,
    source: item.source,
    title: item.title,
    url: item.url,
    score: item.score,
    description: (item.og_description || item.description || '').slice(0, 120),
    og_image: item.og_image || null,
  }));
  const slimPapers = papers.slice(0, 5).map((p, i) => ({
    idx: i,
    title: p.title,
    url: p.url,
    abstract: (p.abstract || '').slice(0, 250),
  }));

  const allItems = {
    hn: slim(enriched.filter(i => i.source === 'hn')),
    lobsters: slim(enriched.filter(i => i.source === 'lobsters')),
    papers: slimPapers,
  };

  // Phase 2: Parallel — articles (Haiku) + music curation (Sonnet)
  console.log('[digest] phase 2: selecting articles + curating music...');

  const cleanEnv = { ...process.env };
  delete cleanEnv.CLAUDECODE;
  delete cleanEnv.CLAUDE_CODE_ENTRYPOINT;

  function spawnClaude(promptText, model = 'claude-haiku-4-5-20251001', timeout = 90, maxTurns = 1) {
    return new Promise((resolve, reject) => {
      let out = '';
      const child = spawnProcess('claude', [
        '-p', promptText,
        '--max-turns', String(maxTurns),
        '--output-format', 'text',
        '--model', model,
        '--dangerously-skip-permissions',
      ], {
        env: cleanEnv,
        cwd: WOLT_DIR,
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: timeout * 1000,
      });
      child.stdout.on('data', d => { out += d.toString(); });
      child.stderr.on('data', d => process.stderr.write(d));
      child.on('exit', code => {
        if (code !== 0) reject(new Error('claude exited ' + code));
        else resolve(out);
      });
      child.on('error', reject);
    });
  }

  function parseJSON(raw) {
    let jsonStr = raw.replace(/^```json?\s*\n?/gm, '').replace(/```\s*$/gm, '').trim();
    const start = jsonStr.indexOf('{');
    const end = jsonStr.lastIndexOf('}');
    if (start >= 0 && end > start) jsonStr = jsonStr.slice(start, end + 1);
    try {
      return JSON.parse(jsonStr);
    } catch {
      let cleaned = raw.replace(/```json?\s*\n?/g, '').replace(/```/g, '');
      const s = cleaned.indexOf('{'), e2 = cleaned.lastIndexOf('}');
      if (s >= 0 && e2 > s) {
        cleaned = cleaned.slice(s, e2 + 1).replace(/[\x00-\x1f]/g, ' ');
        return JSON.parse(cleaned);
      }
      throw new Error('Could not parse JSON from LLM output');
    }
  }

  const articlePrompt = [
    'You are ' + WOLT_NAME + '. ' + memory,
    '',
    'Avoid repeating: ' + (recent.join(', ') || 'none'),
    '',
    'Sources (indexed):\n' + JSON.stringify(allItems, null, 2),
    '',
    'Return ONLY valid JSON (no markdown fences, no extra text):',
    '{"hn":[0,2,5],"lobsters":[1,3],"papers":[0,1],"reflection":"..."}',
    '',
    '- hn/lobsters/papers: arrays of idx from sources. Pick 5-8 total, at least 1 lobsters.',
    '- papers: 1-2 indices.',
    '- reflection: 2-4 sentences, genuine, in your voice.',
    '- ONLY output the JSON object.',
  ].join('\n');

  const tasteProfile = loadTasteProfile();
  const recentArtists = getRecentPlaylistArtists(5);
  const concept = pickMusicConcept();
  const h = montrealHour();
  const dayName = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Montreal', weekday: 'long',
  }).format(new Date()).toLowerCase();

  console.log('[music] concept: ' + (concept || 'general'));
  console.log('[music] excluding ' + recentArtists.length + ' recent artists');

  const humanRef = HUMAN_NAME || 'the listener';
  const musicPrompt = [
    'You are ' + WOLT_NAME + ', curating a playlist for ' + humanRef + '.',
    '',
    '## Taste profile',
    tasteProfile || 'Post-punk, garage rock, psych funk, electronic with riffs. No generic ambient.',
    '',
    '## Today\'s concept',
    concept
      ? 'Theme: ' + concept + '. Build the playlist around this concept — follow the lineage, find deep cuts, tell a story through the track order.'
      : 'Pick a theme that fits ' + dayName + ' ' + (h < 12 ? 'morning' : h < 17 ? 'afternoon' : 'evening') + ' energy. Something with intention, not a random grab bag.',
    '',
    '## Rules',
    '- 10-12 tracks. Each one must be a REAL song by a REAL artist.',
    '- DO NOT include any of these recently served artists: ' + (recentArtists.join(', ') || 'none'),
    '- Follow a playlist arc: opener (sets the tone) → build (deeper) → discovery peak (tracks they haven\'t heard) → anchor (familiar-adjacent) → closer (leaves you wanting more).',
    '- Lean into deep cuts and lesser-known tracks. The point is discovery, not confirmation.',
    '- No generic ambient/chill (Nils Frahm, Jon Hopkins, Olafur Arnalds, Tycho, Sigur Ros).',
    '',
    '## Output',
    'Return ONLY valid JSON (no markdown fences):',
    '{"concept_title":"short concept name","tracks":[{"artist":"...","title":"..."}],"writeup":"3-5 sentences: what this concept is, why it matters, how it connects to the listener\'s taste. Written in ' + WOLT_NAME + '\'s voice — direct, knowledgeable, not formal."}',
    '- ONLY output the JSON object.',
  ].join('\n');

  const [articleStdout, musicStdout] = await Promise.allSettled([
    spawnClaude(articlePrompt, 'claude-haiku-4-5-20251001', 90),
    spawnClaude(musicPrompt, 'claude-sonnet-4-6', 120, 8),
  ]);

  const selectTime = ((Date.now() - t0) / 1000).toFixed(1);
  console.log('[digest] phase 2 done in ' + selectTime + 's');

  let selection;
  try {
    if (articleStdout.status !== 'fulfilled') throw new Error('Article selection failed: ' + articleStdout.reason);
    selection = parseJSON(articleStdout.value);
  } catch (e) {
    console.error('[digest] article selection failed:', e.message);
    process.exit(1);
  }

  let musicSelection = null;
  try {
    if (musicStdout.status !== 'fulfilled') throw new Error('Music curation failed: ' + musicStdout.reason);
    musicSelection = parseJSON(musicStdout.value);
    console.log('[music] concept: ' + (musicSelection.concept_title || 'untitled') + ', ' + (musicSelection.tracks?.length || 0) + ' tracks');
  } catch (e) {
    console.error('[music] curation failed, will skip playlist:', e.message);
  }

  console.log('[digest] selection:', JSON.stringify(selection));

  // Phase 3: Resolve data + Spotify playlist + render
  console.log('[digest] phase 3: resolving + building playlist...');

  const hnItems = allItems.hn;
  const lobItems = allItems.lobsters;
  const paperItems = allItems.papers;

  const picks = [
    ...(selection.hn || []).map(i => hnItems[i]).filter(Boolean),
    ...(selection.lobsters || []).map(i => lobItems[i]).filter(Boolean),
  ];
  const resolvedPapers = (selection.papers || []).map(i => paperItems[i]).filter(Boolean);

  let playlistId = null;
  let musicWriteup = '';
  let conceptTitle = '';
  const musicTracks = musicSelection?.tracks || [];

  if (SPOTIFY_ID && SPOTIFY_SECRET && spotifyRefreshToken && SPOTIFY_USER && musicTracks.length > 0) {
    conceptTitle = musicSelection.concept_title || '';
    musicWriteup = musicSelection.writeup || '';

    await refreshSpotifyToken();

    if (spotifyAccessToken) {
      console.log('[spotify] searching ' + musicTracks.length + ' tracks...');
      const searchResults = await Promise.allSettled(
        musicTracks.map(async m => {
          const result = await searchSpotifyTrack(m.artist, m.title);
          if (!result) return null;
          const intended = m.artist.toLowerCase().replace(/the\s+/g, '');
          const found = result.artist.toLowerCase().replace(/the\s+/g, '');
          if (!found.includes(intended.slice(0, 8)) && !intended.includes(found.slice(0, 8))) {
            console.log('[spotify] artist mismatch: wanted "' + m.artist + '", got "' + result.artist + '" — skipping');
            return null;
          }
          return result;
        })
      );
      const foundTracks = searchResults
        .filter(r => r.status === 'fulfilled' && r.value)
        .map(r => r.value);
      console.log('[spotify] found ' + foundTracks.length + '/' + musicTracks.length + ' tracks (verified)');

      if (foundTracks.length >= 3) {
        const playlistName = conceptTitle
          ? WOLT_NAME + ' \u00b7 ' + shortDate + ' \u2014 ' + conceptTitle
          : WOLT_NAME + ' digest \u00b7 ' + shortDate;
        const playlist = await createSpotifyPlaylist(playlistName, 'curated by ' + WOLT_NAME + ' — ' + (conceptTitle || 'daily mix'));
        if (playlist) {
          playlistId = playlist.id;
          await addTracksToPlaylist(playlistId, foundTracks.map(t => t.uri));
          console.log('[spotify] playlist created: ' + playlistId + ' with ' + foundTracks.length + ' tracks');
        }
      }
    }
  } else {
    console.log('[spotify] no credentials or no music picks, skipping playlist');
  }

  const html = renderHTML({
    dateStr: timeStr,
    greeting: hello,
    picks,
    papers: resolvedPapers,
    playlistId,
    musicWriteup,
    conceptTitle,
    reflection: selection.reflection || '',
  });

  const spark = {
    id: sparkId,
    type: 'spark',
    title: WOLT_NAME + ' digest \u00b7 ' + shortDate,
    timestamp: new Date().toISOString(),
    html,
  };

  const sparkFile = join(SPARKS_DIR, sparkId + '.json');
  writeFileSync(sparkFile, JSON.stringify(spark));

  try {
    await pushToPane(sparkId);
  } catch (err) {
    console.error('[digest] pushed spark but /current failed:', err.message);
  }

  const totalTime = ((Date.now() - t0) / 1000).toFixed(1);
  console.log('[digest] done in ' + totalTime + 's — ' + sparkId);
  console.log('[digest] picks: ' + picks.length + ' items, ' + resolvedPapers.length + ' papers, playlist: ' + (playlistId || 'none'));
}

runDigest().catch(err => {
  console.error('[digest] fatal:', err);
  process.exit(1);
});
