# Matrix chat MVP

## Question

Does Woltspace feel like the right beginner product when it opens as a calm,
familiar messenger and keeps the terminal as an optional advanced view?

## First runnable slice

- one human Matrix account used through the Woltspace PWA
- one lodge-owned wolt Matrix account
- one private encrypted room
- a native Woltspace room timeline and composer
- send and receive plain text through Matrix
- persistent browser and wolt crypto stores across restart
- a small `/` palette with `/help` and `open terminal`
- optional Element interoperability proof

The room is the durable conversation. A Woltspace session is the current agent
runtime behind the wolt participant; it may restart without creating a new
chat.

## Explicitly deferred

- invisible account/device/room bootstrap (required before this leaves draft)
- a complete settings UI
- attachments, voice, reactions, threads, search, presence, and calls
- multi-wolt rooms
- push notifications
- federation policy and public hosting
- account recovery UX
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

## Human acceptance result

The disposable proof completed a real encrypted human-to-wolt-to-human round
trip on a phone. The human opened one Woltspace Chat link, sent ordinary text,
and received n00b's replies without entering a homeserver URL, Matrix ID,
password, room ID, or invitation. The test also exposed and fixed two browser
bugs: decrypted events require their own callback, and visible messages must be
rendered in canonical Matrix timeline order rather than decryption-completion
order.

The temporary proof used a random HTTPS tunnel solely because a phone cannot
reach Mac loopback and browser crypto requires a secure context. That tunnel,
its bearer, disposable accounts, server database, crypto stores, and build
cache are not part of this branch. Product integration must serve Chat and a
Matrix client proxy from Woltspace's existing authenticated origin.

## Draft exit gate

The connector and UI foundation in this PR are intentionally not a default-on
product. Before enabling them for owners, Woltspace must invisibly provision
the human device, wolt account/device, encrypted room, and exact trust pins;
own a loopback SQLite Synapse lifecycle; and land inbound Matrix routing with
`notify --matrix` as one integrated path. The desired owner experience is only:
open Chat, choose a wolt, and send.
