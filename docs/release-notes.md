# Woltspace 0.5.8

Python **0.5.8** secures the local control plane, replaces the browser's
separate Node terminal bridge with an in-process Python PTY, and ships the
owner-only Slack Agent View added on `main` since 0.5.7. The separately
distributed **@woltspace/tui remains at 0.5.2**; no npm release is required.

## Security and browser terminal

- Restrict the lodge HTTP API to loopback and configured Woltspace tunnel
  authorities. DNS-rebinding hosts, foreign browser writes and cross-site
  fetches now fail closed, and lodge responses no longer publish wildcard CORS.
- Apply the same Host/Origin boundary to the browser terminal and proxied app
  WebSockets before accepting a connection.
- Move the browser's tmux attachment into the Python control plane. The old
  `woltspace-tui-service` child and its unauthenticated `API_PORT + 1` listener
  are no longer started.
- Preserve real tmux input/output, resize, reconnect and concurrent attachment
  behavior without bundling Node, npm, `node-pty` or `@woltspace/tui` into the
  beginner macOS runtime. Tiny transient terminal sizes are ignored, split
  UTF-8 sequences decode correctly, and disconnecting a browser leaves the
  underlying tmux session alive.
- Stop treating a read-only site request as permission to create files. Site
  scaffolding remains an explicit state-changing action.

The public lodge remains available through its configured Cloudflare Tunnel
and Access policy. Direct LAN, Tailscale or custom-host access that does not use
loopback or the configured tunnel hostname now receives HTTP 403. A future
explicit host allowlist may support deliberate private-network access; there is
no wildcard compatibility escape hatch in this release.

## Slack Agent View

- Run Slack as a native supervised connector for one immutable owner, using
  Socket Mode and owner DMs only. Non-owner, channel, multiparty-DM, bot,
  subtype, retry and duplicate events fail closed before agent work.
- Open **Choose your wolt** for every fresh top-level DM and atomically deliver
  the original message once after selection. Existing Slack threads retain
  their original Woltspace session.
- Use Slack's native Agent View session lifecycle and activity state, with the
  exact Woltspace slug as the stable session title and an authenticated
  **Open session** link.
- Remove the detached legacy progress implementation and custom Slack adapter
  overrides. Legacy token environment variables remain migration inputs to the
  canonical `channels.slack` configuration.
- Bundle the `setup-slack` skill and a minimal owner-controlled Slack app
  manifest. Native Stop interruption, richer media and hosted OAuth remain
  follow-up work.

## Upgrade notes

No colony-data migration is required. Native upgrades stop and restart the
control plane while preserving tmux sessions; the new runtime re-adopts those
sessions and starts Telegram, Slack and Wolf as configured. A recorded legacy
Node terminal bridge is stopped with the outgoing control plane or reaped by
the incoming runtime and is not respawned, even if stale TUI configuration or
`WOLTSPACE_TUI_SERVICE_BIN` remains.

After upgrading, verify the lodge is healthy and that no process listens on the
former bridge port (normally API port + 1):

```sh
woltspace status
lsof -nP -iTCP:7778 -sTCP:LISTEN
```

The second command should print nothing for a lodge on port 7777. The advanced
interactive `woltspace tui` remains a separate optional npm installation; only
the browser terminal no longer depends on it.

Two follow-ups are deliberately outside this patch release: a machine-wide
warning for manually started or record-less legacy bridge processes, and
loopback-by-default bindings for separately managed app development servers.
