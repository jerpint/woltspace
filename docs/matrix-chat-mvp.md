# Matrix chat MVP

## Question

Does Woltspace feel like the right beginner product when it opens as a calm,
familiar messenger and keeps the terminal as an optional advanced view?

## First runnable slice

- one human Matrix account across Element and the Woltspace PWA
- one lodge-owned wolt Matrix account
- one private encrypted room
- a native Woltspace room timeline and composer
- send and receive plain text through Matrix
- persistent browser and wolt crypto stores across restart
- a small `/` palette with `/help` and `open terminal`
- Element interoperability proof

The room is the durable conversation. A Woltspace session is the current agent
runtime behind the wolt participant; it may restart without creating a new
chat.

## Explicitly deferred

- room creation and administration
- a complete settings UI
- attachments, voice, reactions, threads, search, presence, and calls
- multi-wolt rooms
- push notifications
- federation policy and public hosting
- account recovery UX beyond documenting the proof credentials and stores
- native desktop/mobile wrappers

## Security boundary

- The homeserver must see ciphertext, not message bodies.
- The human PWA and each wolt are distinct Matrix accounts/devices.
- A wolt account is scoped to the room it serves; it is not a device on the
  human account.
- Matrix access tokens and crypto stores are private local state and never
  enter Git, logs, URLs, or rendered HTML.
- A Matrix message can communicate intent but does not grant tool authority.
- The proof may use disposable local accounts and infrastructure. Public
  hosting, DNS, tunnels, and live-lodge changes require separate approval.

## Acceptance proof

1. Create two disposable Matrix accounts and an encrypted room.
2. Verify the human PWA device and wolt device are the only participants.
3. Send from Woltspace; read the same ciphertext-backed message in Element.
4. Send from Element; route it to a wolt session and return the reply to the
   same room.
5. Restart the browser and connector; decrypt the room without creating new
   device identities.
6. Confirm the homeserver database/logs contain no plaintext bodies or access
   tokens and that the terminal remains reachable from the chat.
