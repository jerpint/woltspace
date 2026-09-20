# Wolt digging: product model and experimental roadmap

Digging is a Woltspace feature that lets a wolt request a temporary guest work
session in another colony. Woltspace Wire is only the authenticated, encrypted
transport. Pairing colonies does not grant remote execution.

## Beginner model

- **Home** is the one colony where the wolt exists and keeps its identity,
  memory, configuration, and long-lived session history.
- **Wire** lets paired colonies identify and call one another. It carries dig
  requests, grants, callbacks, and results, but grants no execution authority.
- **Dig** is an explicitly approved temporary visit into a destination. SSH may
  carry that visit, but SSH is an implementation detail rather than the product
  concept.
- **IWCL** lets the temporary visiting session explain and coordinate with the
  destination's resident wolts while it is there.

Digging is not teleportation, installation, migration, or cloning. A visiting
wolt does not become a resident of the destination colony.

## User story

A wolt proposes setting up another colony in a particular way. Its human asks
the destination to allow a dig. After the destination human reviews and allows
the exact visit, Woltspace creates a bounded guest session on the remote server
under the visiting wolt's home identity. The visitor performs only the approved
work and uses local IWCL to explain the resulting setup to the resident/main
wolt. It returns its result home and the guest session is destroyed.

The resident wolt or human may later call the visitor's home colony over Wire.
The original wolt can answer remotely or request a fresh dig. Periodic check-ins
are scheduled Wire callbacks/status exchanges or newly approved short visits,
not a forgotten permanent shell or a dormant copy of the visiting wolt.

## Runnable v0: owner-provisioned SSH

The owner configures ordinary SSH access using their existing OS user,
`~/.ssh/config`, agent, and keys. Woltspace records which wolt may dig to the
exact SSH host/user (and optionally a working directory), invokes normal SSH,
and gives the wolt the same remote shell and Woltspace CLI access that owner has
already authorized. The wolt can inspect or set up the machine, disconnect, and
report what changed.

V0 does not create a guest Unix account, transport private SSH keys over Wire,
require a remote Woltspace agent, or provide IWCL across the colony boundary.
The SSH user's existing authority is the real authority; the Woltspace record
is an understandable local consent and audit boundary, not a sandbox.

V0 must still pin or verify the SSH host key, bind consent to the exact host and
user, handle remote PATH and TTY behavior, distinguish home-wolt context from
remote resident files, report interrupted setup honestly, and provide a clear
way to remove both the Woltspace grant and underlying SSH access.

## V1: paired-colony visit

Wire carries the request, destination approval, callback, and result. The
destination-owned authorization kernel in this branch belongs here. A later V1
may let the visiting session coordinate with the resident/main wolt over local
IWCL. That requires an explicit identity/context bridge so the visitor never
adopts the resident wolt's memory or becomes a remote clone.

## V1 human experience

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

The same lifecycle may begin without Wire: an owner can manually grant a wolt a
temporary visit to a server they control, with SSH or another Woltspace adapter
providing transport. Wire is the preferred paired-colony convenience path, not
a prerequisite for the general digging concept.

## Security invariants

- A Wire peer is a messenger, not a local authority.
- Pairing never implies permission to dig.
- Approval is local, explicit, exact-request, short-lived, and one-use.
- The request body cannot choose its authenticated source identity.
- Targets are destination-relative and later resolve only inside a newly
  created disposable workspace.
- No host path, standing account, SSH private key, relay read capability, or
  permanent shell credential crosses colonies.
- The home colony remains authoritative for the visitor's identity and memory.
  The destination stores only bounded visit/audit records and never materializes
  a second resident wolt.
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

This is not yet a usable remote-access feature. The next implementation gate is
the simpler SSH v0. The Wire state machine remains isolated V1 groundwork until
the SSH setup/access experience and its audit boundary are proven.
