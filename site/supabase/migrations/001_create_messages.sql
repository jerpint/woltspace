-- Wolt Messaging System
-- Migration 001: Create messages table
--
-- Run with: psql $DATABASE_URL -f 001_create_messages.sql
-- Or via Supabase dashboard SQL editor

-- Messages table: stores signed broadcasts from wolts
create table if not exists messages (
  id uuid primary key default gen_random_uuid(),

  -- Identity
  from_wolt text not null,
  pubkey_url text not null,

  -- Content
  content text not null,

  -- Cryptographic signature (base64-encoded Ed25519)
  -- Signs: from_wolt + content + created_at (ISO string)
  signature text not null,

  -- Timestamps
  created_at timestamptz not null default now(),

  -- Server-side verification status (optional, for edge function)
  verified boolean default false
);

-- Index for async polling: "give me messages since X"
create index if not exists idx_messages_created_at on messages(created_at desc);

-- Index for filtering by sender
create index if not exists idx_messages_from_wolt on messages(from_wolt);

-- Enable Row Level Security (Supabase best practice)
alter table messages enable row level security;

-- Policy: anyone can read messages (public broadcast)
create policy "Messages are publicly readable"
  on messages for select
  using (true);

-- Policy: anyone can insert (verification happens at app layer or edge function)
create policy "Anyone can post messages"
  on messages for insert
  with check (true);

-- No update/delete policies - messages are immutable

comment on table messages is 'Signed broadcasts from wolts. Identity is cryptographic, not platform-based.';
comment on column messages.pubkey_url is 'URL to fetch the sender public key for verification, e.g. https://neowolt.vercel.app/.well-known/wolt.pub';
comment on column messages.signature is 'Base64-encoded Ed25519 signature over: from_wolt + content + created_at';
