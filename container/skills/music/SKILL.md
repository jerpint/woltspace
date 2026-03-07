---
name: music
description: Curate a Spotify playlist as a story — genre deep-dives, artist journeys, scene snapshots. Hyperpersonalized music discovery.
---

# Music Curation Skill

Curate a Spotify playlist for jerpint — not a list of songs, but a **story**. Each playlist is a guided exploration: a genre deep-dive, an artist journey, a scene you didn't know existed. The playlist has a narrative arc and comes with context that makes listening richer.

## Usage

- `/music` in chat — interactive, propose a concept first
- Digest pipeline — automated, picks a theme based on mood/day
- Telegram (future) — pitch a concept, jerpint vibes checks, then cook

## Thesis

Algorithmic curation is impersonal — collaborative filtering optimizes for the average listener in your cluster, not for you. A wolt that knows your taste from real collaboration can do something Spotify can't: **teach you about music while introducing you to it.** Not "because you listened to X" — more like a friend who knows the scene saying "ok sit down, let me tell you about this thing."

## The format: Playlist as story

Every playlist has a **concept** — a reason it exists beyond "songs you might like."

### Concept types

- **Genre deep-dive:** "Today we're exploring Tuareg guitar — how desert blues went from Tinariwen playing in refugee camps to Mdou Moctar shredding on a homemade guitar." Tracks follow the arc.
- **Artist journey:** "A deep-dive on King Gizzard — they've made thrash metal, microtonal Turkish psych, jazz, and a concept album about a sentient river. Here's the roadmap." Key tracks across eras + fun facts.
- **Scene snapshot:** "NYC 2001-2004 — The Strokes dropped Is This It and suddenly every band in lower Manhattan had a record deal. Here's what that moment sounded like beyond the big names."
- **Lineage/influence:** "The line from Fela Kuti to Khruangbin — how Afrobeat grooves traveled through funk, dub, and psychedelia to land in a Houston trio."
- **Mood/energy:** "3pm on a Friday, brain is fried, need something that moves but doesn't demand attention." Less narrative, more vibes — but still curated with intention.
- **Cross-pollination:** "What happens when you put Turkish psychedelia next to Saharan guitar next to Ethiopian jazz? Turns out they're all doing the same thing from different continents."

### Playlist structure

A good playlist has an arc, not just a shuffled bag of songs:

1. **Opener** — sets the tone, signals what kind of ride this is
2. **Build** — deeper into the theme, energy rises or narrative develops
3. **Discovery peak** — the tracks jerpint definitely hasn't heard, the reason this playlist exists
4. **Anchor** — 1-2 familiar-adjacent tracks so it doesn't feel alien
5. **Closer** — lands it, leaves you wanting to explore more

10-15 tracks. Long enough to be a real session, short enough to stay focused.

### The writeup

Each playlist comes with a short writeup (3-5 paragraphs) that covers:
- What the concept is and why it's interesting
- Key context: history, connections, fun facts, why these artists matter
- How the tracks connect to jerpint's existing taste (the bridge from known to unknown)
- 1-2 specific tracks to pay attention to and why

This writeup lives in the digest (if generated for a cron) or gets sent via Telegram/chat.

## How it works

### Step 1: Choose a concept

Pick based on:
- **Taste profile** — what's in jerpint's orbit that hasn't been explored yet
- **Unexplored queue** — genres/scenes/artists flagged for future exploration
- **Mood signals** — time of day, day of week, explicit requests
- **Recent history** — don't repeat themes, build on what landed

When interactive (chat or Telegram), **pitch the concept first:**
> "I'm thinking a deep-dive on the Daptone Records universe — Sharon Jones, Charles Bradley, Budos Band, Menahan Street Band. Analog soul/funk recorded on vintage equipment in Brooklyn. Connects to the Khruangbin groove but rawer. Interested?"

If jerpint says yes, cook. If not, pitch another.

### Step 2: Research

This is what makes it better than haiku picking from memory:
- **Web search** for the concept — get real context, history, key albums, deep cuts
- **"If you like X" searches** — find the connections algorithms miss
- **Cross-reference with Spotify** — verify tracks exist, check for deep cuts vs obvious picks
- **Check against recent playlists** — zero overlap with last 5 playlists

### Step 3: Build the playlist

```
# Spotify API flow:
# 1. Refresh token (access tokens expire hourly)
# 2. Search each track: GET /v1/search?q={artist}+{title}&type=track&limit=3
# 3. VERIFY artist name matches intended artist (Spotify search can return wrong matches)
# 4. Create playlist: POST /v1/users/{userId}/playlists
# 5. Add tracks: POST /v1/playlists/{id}/tracks
```

Spotify user ID: `uxroktcqj7luuc0nqwtmqrhh1`
Credentials: `SPOTIFY_ID`, `SPOTIFY_SECRET`, `SPOTIFY_REFRESH_TOKEN` from `.env`

**Artist verification is critical.** After search, check that `result.artists[0].name` roughly matches the intended artist. The "King Gizzard → Everlast" incident happened because we trusted the first search result blindly.

### Step 4: Write up + deliver

- Write the narrative (3-5 paragraphs)
- In digest: embed as Spotify iframe + writeup in nw's section
- In chat: share playlist link + writeup
- In Telegram (future): share link + condensed version

### Step 5: Capture feedback

When jerpint reacts ("this was great", "not feeling this one", "more like this"):
- Update `wolt/memory/music-taste.md` immediately
- Note which concept type landed, which specific tracks got mentioned
- Adjust the unexplored queue accordingly

## Taste profile

Full profile lives at `wolt/memory/music-taste.md`. Key points:

**Genre gravity** (the core orbit):
- Post-punk revival / garage rock (Strokes, Arctic Monkeys, Interpol, Yeah Yeah Yeahs)
- Desert/stoner rock (QOTSA, Jack White)
- Psychedelic funk/soul (Khruangbin)
- Indie rock (Bloc Party, Franz Ferdinand, TV on the Radio)

**Confirmed hits** (from nw-curated playlists):
- Post-punk: Protomartyr, Shame, IDLES, Dry Cleaning, Viagra Boys, Fontaines D.C.
- Desert guitar: Mdou Moctar
- Math/prog: Black Midi
- Funk: Vulfpeck
- Electronic with riffs: Soulwax, Moderat, Ratatat, Justice, Daft Punk
- Dance-punk: LCD Soundsystem

**What doesn't land:**
- Generic ambient/chill (overserved — Nils Frahm, Jon Hopkins, Olafur Arnalds every time)
- "Spotify Discover Weekly" energy — safe, obvious, impersonal

**Unexplored queue** (concepts to pitch):
- Afrobeat lineage (Fela Kuti → Antibalas → Kokoroko → Ezra Collective)
- Krautrock (Can, Neu!, Faust) — the motorik beat connection
- Turkish/Middle Eastern psych (Baris Manco, Altin Gun, Khruangbin's influences)
- Japanese noise/experimental (Boredoms, Melt-Banana, Guitar Wolf)
- South American psych (Os Mutantes, Stereolab's Brazilian side)
- Daptone Records universe (Sharon Jones, Charles Bradley, Budos Band)
- Ethiopian jazz (Mulatu Astatke, The Ethiopiques series)
- Surf rock → garage (Thee Oh Sees, Ty Segall, together Pangea)
- No wave NYC (DNA, James Chance, Teenage Jesus)
- Post-punk class of 2020s (deeper: Squid, Black Country New Road, Yard Act, Geese)

## Telegram flow (future)

```
nw → jerpint: "thinking about a deep-dive on the Daptone Records
universe today — analog soul/funk from Brooklyn. Sharon Jones,
Charles Bradley, Budos Band. Connects to the Khruangbin groove
but rawer. interested?"

jerpint → nw: "ya sounds good"

nw → jerpint: "cooking..."

[nw researches, builds playlist, writes up]

nw → jerpint: "playlist ready — 12 tracks, starts with Sharon Jones
'100 Days 100 Nights' and goes deep from there.
https://open.spotify.com/playlist/xxx
writeup: [condensed narrative]"
```

The key: **jerpint approves the concept before nw invests in building it.** No wasted playlists on themes that don't land.

## Integration with digest

When called from the digest cron:
- Pick a concept autonomously (no approval step)
- Lean toward mood-appropriate choices (morning vs afternoon)
- Include the writeup in the nw reflection section of the digest
- Embed the playlist as a Spotify iframe
- Playlist name: `nw · {date} — {concept short title}`

## Notes

- This is a showcase for the wolt thesis: relationship > algorithm
- Every playlist should feel like it came from someone who knows you, not a system that tracked you
- Quality over frequency — one great themed playlist beats three generic ones
- The writeup is as important as the playlist — it's what makes this curation, not aggregation
