# Wolt digging v0: local experimental boundary

Digging is a Woltspace feature that lets a wolt request a temporary guest work
session in another colony. Woltspace Wire is only the authenticated, encrypted
transport. Pairing colonies does not grant remote execution.

This first slice freezes the destination-owned authorization lifecycle. It does
not open SSH, expose a listener, start a remote agent, or protect production
secrets.

## Human experience

1. Alice asks to dig to Bobeaver Colony with a short task description and a
   destination-relative disposable workspace.
2. Wire delivers the signed and encrypted request from Alice's pinned colony
   identity.
3. Bobeaver's human sees the source colony, source wolt, task, target, and short
   expiry. They explicitly allow or reject this one dig.
4. Allow creates a random, short-lived, one-use grant bound to the verified
   source colony and exact request. The grant returns over Wire.
5. Alice redeems it over the same pinned Wire relationship. Only then may
   Woltspace create a constrained guest session.
6. Completion, expiry, or revocation closes the dig. Results and an inert audit
   summary may return over Wire.

## Security invariants

- A Wire peer is a messenger, not a local authority.
- Pairing never implies permission to dig.
- Approval is local, explicit, exact-request, short-lived, and one-use.
- The request body cannot choose its authenticated source identity.
- Targets are destination-relative and later resolve only inside a newly
  created disposable workspace.
- No host path, standing account, SSH private key, relay read capability, or
  permanent shell credential crosses colonies.
- The destination can revoke before or during a dig. Active-session termination
  is a required integration gate, not yet implemented by the kernel.
- The guest session receives a purpose-built policy and cannot inherit the
  destination wolt's normal Auto grant.
- Logs and status omit bearer capabilities and message bodies by default.

## Current implementation

`container/lib/digging.py` provides strict request parsing and a private durable
state machine: `pending -> approved -> active -> completed`, with `revoked` and
`expired` terminal paths. Capabilities are stored only as SHA-256 digests and
removed on redemption. `verified_peer` is an input from the future Wire adapter,
not a claim accepted from the request body.

This is not yet a usable remote-access feature. The next gate is a disposable
two-colony runner with a fake/in-memory Wire adapter. Only after lifecycle,
termination, audit redaction, and hostile-request tests pass should an actual
private transport be considered.
