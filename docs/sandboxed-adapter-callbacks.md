# Sandboxed adapter callbacks

The Slack DM acceptance test exposed a general delivery boundary: an agent can
produce a valid reply while its harness sandbox refuses the localhost HTTP call
used by `notify`. Per-message elevation is not a durable solution, and adapter
tokens or unrestricted channel authority must not move into the agent sandbox.

## Proposed boundary

Make `notify` a client of a lodge-owned local callback broker. The connector or
control-plane process runs the broker outside agent sandboxes and retains all
network credentials. A session submits only a bounded delivery intent through a
private Unix socket or atomic spool directory that its sandbox can access.

The lodge creates a per-session endpoint or capability and binds it to the
session's registry record. The broker derives the adapter, owner, destination,
and thread from that record; a caller cannot select another session's route.
Before delivery it validates that the session is alive, the route is still
owned by that session, and the connector's owner policy permits the callback.
It also enforces message-size and rate limits.

The durable form should use atomic enqueue, an idempotency key, explicit
accepted/delivered/failed state, bounded retry, a dead-letter state, and a
metadata-only audit trail. A Unix socket is the simplest primary transport; a
mode-0700 lodge-owned spool is a portable fallback where a harness can write
files but cannot open localhost sockets. The spool needs no-follow handling,
atomic rename, private per-session directories, and broker-created capabilities
so one same-lodge session cannot forge another session's callback.

`notify` can retain HTTP as a compatibility path for trusted contexts, while
sandboxed sessions default to the broker. This keeps the security invariant:
the agent communicates reply data and intent, while all delivery authority,
credentials, routing, and retry policy remain local to the lodge.

This is follow-up architecture only. The Slack DM MVP does not implement the
broker or change the current control-plane API.
