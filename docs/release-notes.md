# Woltspace 0.6.0

Python **0.6.0** redesigns the lodge around your wolts: a sidebar you can read at
a glance, a page per wolt, and sessions that rest and wake without losing their
place. It also closes a terminal exposure on app addresses. The separately
distributed **@woltspace/tui remains at 0.5.2**; no npm release is required.

## The lodge

- **A sidebar of wolts.** Each wolt shows as online (a dot) or offline (dimmed,
  with when it was last seen). With more than eight wolts the sidebar shows the
  recent ones; "All N wolts" opens the full list. On a phone the drawer paints
  straight away from what it saw last and then refreshes.
- **The Wolts page.** Search, an "Online" section outlined in green above
  "Offline", each wolt's recent sessions one tap away, and a quick "+" to start
  a chat.
- **A page per wolt.** Its site, its sessions and its settings in one place.
  Harness, model and other settings are editable from the lodge.
- **Apps, connectors and the terminal** have their own lodge controls.
- **Two session states.** Online means the session's terminal is open; offline
  means everything else. Every lodge page uses the same definition, so two
  browsers always agree.

## Sessions

- **Rest and wake.** Opening an offline session wakes it where it left off, for
  claude and codex alike. Opening an online session whose agent has exited
  resumes it before attaching.
- **Codex conversations stay separate.** A codex conversation is claimed by
  exactly one session, even when two sessions of the same wolt start together,
  and a session that missed its id at start recovers it later.
- **Idle policy.** The lodge setting for closing idle sessions defaults to 24
  hours ("Never" is available). Container installs apply it today; native
  lodges will apply it in a later release.
- **A light session list.** Lodge pages ask for a compact view (open sessions,
  the last day, and a few per wolt) instead of every session ever recorded.

## Security

- **App addresses can't reach the lodge terminal.** Websockets on an app
  address are routed only to that app; the terminal and livereload sockets
  refuse app addresses.
- **Backend data stays data.** App names, keepers, spark titles, harness labels
  and wolf statuses are rendered as text, never as markup or script.
- **Opening a terminal link is read-only.** Waking a session goes through the
  lodge's origin-checked request, so a link opened from another site can't wake
  an agent.

## Upgrade notes

No colony-data migration is required. Existing sessions, sites, apps and wolves
keep working. Session states now read "online" and "offline" instead of
"working", "awake" and "resting". Native upgrades stop and restart the control
plane while preserving tmux sessions.

After upgrading, verify the lodge is healthy:

```sh
woltspace status
```
