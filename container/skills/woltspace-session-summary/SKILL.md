---
name: woltspace-session-summary
description: Write a session summary to disk so the Telegram bot can answer "what'd you make?" with real context.
---

# Session Summary Skill

Write a short summary of what happened this session so the bot has context between sessions.

## When to use

Run at the end of a session — or anytime something meaningful was made (artifact, decision, significant change).

## What to write

Reflect honestly on the session. Answer:
- What was the main thing that happened or got built?
- Did anything come out of it (link, file, decision)?
- Why did it matter or what drove it?

Keep it fluid — 1-3 sentences max. No bullet lists. Write like you'd tell someone what you did.

## Format

```json
{
  "id": "sess_<timestamp>",
  "t": <unix seconds>,
  "title": "<3-6 word label>",
  "artifact": { "type": "<type>", "url": "<url>", "label": "<label>" },
  "summary": "<1-3 sentence prose>",
  "tags": ["tag1", "tag2"]
}
```

- `artifact` is optional — omit entirely if nothing linkable came out
- `artifact.type`: `spotify_playlist`, `digest`, `html_page`, `code`, `config`, `decision`
- `tags`: 2-5 lowercase words (e.g. `music`, `bot`, `infra`, `memory`, `site`)

## How to write it

Run this node one-liner, filling in the fields:

```bash
node -e "
const {appendFileSync, mkdirSync} = require('fs');
const woltDir = process.env.WOLT_DIR || '/workspace/wolt';
const stateDir = woltDir + '/.state';
mkdirSync(stateDir, {recursive: true});
const entry = {
  id: 'sess_' + Date.now().toString(36),
  t: Math.floor(Date.now() / 1000),
  title: 'YOUR TITLE HERE',
  artifact: { type: 'TYPE', url: 'URL', label: 'LABEL' },
  summary: 'YOUR SUMMARY HERE.',
  tags: ['tag1', 'tag2']
};
appendFileSync(stateDir + '/sessions.jsonl', JSON.stringify(entry) + '\n');
console.log('session summary written:', entry.id);
"
```

Drop the `artifact` key if there's nothing to link.

## Example

```bash
node -e "
const {appendFileSync, mkdirSync} = require('fs');
const woltDir = process.env.WOLT_DIR || '/workspace/wolt';
const stateDir = woltDir + '/.state';
mkdirSync(stateDir, {recursive: true});
const entry = {
  id: 'sess_' + Date.now().toString(36),
  t: Math.floor(Date.now() / 1000),
  title: 'morning building playlist',
  artifact: {
    type: 'spotify_playlist',
    url: 'https://open.spotify.com/playlist/abc123',
    label: \"Arthur's Morning Build\"
  },
  summary: 'made a 12-track playlist for focused building — ólafur arnalds, nils frahm, boards of canada. anchored around texture not rhythm, nothing with words. jerpint mentioned he builds best with ambient instrumental.',
  tags: ['music', 'playlist', 'morning', 'focus']
};
appendFileSync(stateDir + '/sessions.jsonl', JSON.stringify(entry) + '\n');
console.log('written:', entry.id);
"
```

## Notes

- No need to write one every session — only when something meaningful happened
- If you're not sure what to write, ask yourself: "if jerpint asked the bot tomorrow what happened, what would be useful to know?"
- The bot reads the last 5 entries by default
