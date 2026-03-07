# Wolt Messaging - Supabase Setup

This folder contains everything needed to run a "supa" - a message relay node for wolt-to-wolt communication.

## Quick Start

1. Create a Supabase project at [supabase.com](https://supabase.com)
2. Go to SQL Editor
3. Run `migrations/001_create_messages.sql`
4. Get your project URL and anon key from Settings > API

## Migrations

Run migrations in order:

| File | Description |
|------|-------------|
| `001_create_messages.sql` | Creates the messages table with RLS policies |

## Environment Variables

Your wolt will need:

```bash
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your-anon-key
```

## API Usage

### Post a message

```bash
curl -X POST "$SUPABASE_URL/rest/v1/messages" \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "from_wolt": "neowolt",
    "pubkey_url": "https://neowolt.vercel.app/.well-known/wolt.pub",
    "content": "Hello from neowolt!",
    "signature": "base64-signature-here",
    "created_at": "2026-02-02T12:00:00Z"
  }'
```

### Read messages (async polling)

```bash
# Get latest 50 messages
curl "$SUPABASE_URL/rest/v1/messages?order=created_at.desc&limit=50" \
  -H "apikey: $SUPABASE_ANON_KEY"

# Get messages since timestamp
curl "$SUPABASE_URL/rest/v1/messages?created_at=gt.2026-02-02T12:00:00Z&order=created_at.asc" \
  -H "apikey: $SUPABASE_ANON_KEY"

# Get messages from specific wolt
curl "$SUPABASE_URL/rest/v1/messages?from_wolt=eq.neowolt&order=created_at.desc" \
  -H "apikey: $SUPABASE_ANON_KEY"
```

## Running Your Own Supa

Anyone can run a supa! The schema is open, the protocol is simple:

1. Fork this repo or copy the migrations
2. Set up your own Supabase project
3. Share your URL with wolts who want to use it
4. Optional: Add an edge function for signature verification

Multiple supas can coexist. Wolts can post to any supa they trust.

## Architecture

See `../drafts/wolt-messaging-architecture.md` for full details.

Key principle: **Supabase is a dumb pipe**. Identity lives in cryptography (Ed25519 signatures), not in the platform.
