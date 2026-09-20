Python **0.5.7** adds owner-approved SSH Digging for bounded remote setup and
bootstrap work. The separately distributed **@woltspace/tui remains at 0.5.2**.

- Approve a named destination with `woltspace dig grant`, inspect approvals
  with `dig list`, connect with `dig connect`, and remove Woltspace consent
  with `dig revoke`.
- Reuse the host's existing OpenSSH configuration, agent, keys, certificates,
  and aliases. Dig does not copy or manage SSH credentials.
- Pin the resolved hostname, Unix user, and port at grant time, re-resolve the
  alias before every connection, and refuse changes.
- Require strict host-key checking, preserve remote command argument boundaries,
  and keep a private local audit of connection outcomes without storing command
  bodies or credentials.
- Bundle the `digging` skill so wolts prefer the Dig wrapper over raw SSH, ask
  immediately before entering a destination unless the current instruction
  explicitly waives that prompt, begin with bounded probes, and leave remote
  handoff material under `.woltspace/bootstrap` rather than another wolt's
  private memory.

Dig v0 is a consent, discoverability, target-pinning, and audit layer over the
machine owner's ordinary SSH authority. Wolts sharing the same Unix user can
bypass it by invoking SSH directly, and `dig revoke` does not remove the Unix
account, key, agent, certificate, or server-side authorization. There is no
Wire authorization, Cloudflare transport, guest account, remote IWCL, or live
agent integration in this release.

No migration is required. Configure and verify ordinary SSH access first, then
grant the exact existing alias. Review `docs/digging-v0.md` before connecting to
a real machine.
