# Woltspace 0.6.0 (release candidate)

0.6 is a groundwork release. Most of it is invisible: it separates who can use
your apps from who can reach your lodge, closes the public links that were on by
default, and removes code the platform no longer uses. It is a **minor**
release, so some setups need a step after updating. Read **Before you update**
first.

These notes grow with each release candidate. Install a candidate with
`woltspace update --pre`.

## Before you update

Most lodges need to do nothing. You need to act if any of these is true:

- **You open apps from outside this machine** (on your phone, or shared with
  someone). Apps now live on their own domain behind the app gateway. Until
  that domain is set up, remote app links show "Apps are served on the app
  domain" instead of the app. Set it up with the cloudflare skill
  (`app-domain.md`): a separate domain, a wildcard DNS record, one tunnel rule
  and a Cloudflare Access application for apps.
- **An app has `"public": true` in its `woltspace.json`.** That field is now
  ignored, so the app is no longer shared when it starts. Share it on purpose
  instead: add people to its share list, or open an opt-in quick tunnel (below).
- **You run a container lodge with no Cloudflare tunnel configured.** It used
  to get a random public address. It no longer does: the lodge is reachable on
  this machine only, until you set up a named tunnel with Access.
- **You set `WOLTSPACE_SHARING_ENABLED`.** It is replaced by
  `WOLTSPACE_APP_QUICK_TUNNELS`, which is off by default.
- **An app has no `start` command** (a static folder). It can no longer be
  served as an app. Give it a start command, for example
  `python3 -m http.server $PORT --bind 127.0.0.1`.

## The Mill: apps get their own door

- **A separate app gateway.** Apps are served by their own small server, on the
  lodge port minus 660 (`7117` for a lodge on `7777`), with none of the lodge's
  routes. Being able to open an app no longer means being able to reach your
  lodge. Override the port with `WOLTSPACE_APP_GATEWAY_PORT` or in Settings.
- **Its own domain and its own login.** Remote apps are served at
  `<app>.<your app domain>`, behind their own Cloudflare Access application, so
  people you share an app with never pass the lodge's login. Configure the app
  domain, the gateway port and both Access audiences under **Settings**.
- **Share lists.** Each app has a list of email addresses allowed to open it.
  You are always on it. Nobody else is until you add them.
- **Apps know who is visiting.** The gateway passes the verified visitor's
  email to the app in the `X-Woltspace-User` header, and removes any copy a
  visitor tried to send.
- **Apps listen on this machine only.** Apps are started with
  `HOST=127.0.0.1`, which the common dev servers honour, so the gateway is their
  only way in.
- **Old addresses redirect.** `/app/<name>` on the lodge and the old
  `<app>.<lodge domain>` addresses send you to the gateway.
- **Safer by default.** The gateway refuses cross-site form posts and
  websockets from other sites, forwards websockets and streamed responses
  unchanged, and strips Cloudflare credentials before a request reaches an app.
  One consequence: an OAuth provider that answers with a cross-site
  `form_post` is refused. The apps skill explains the workaround.

## The lodge checks its own login

- When Cloudflare Access is configured, the lodge now verifies the Access
  token itself, on every request and websocket, and only lets the owner's email
  in. A request that reaches the lodge without passing Cloudflare is refused.
- Requests from this machine work as before.

## Public links

- **Never for the lodge.** The lodge is only ever published through a named
  Cloudflare tunnel behind Access. The random public "quick tunnel" address for
  the lodge is gone.
- **Opt-in for apps.** A quick tunnel gives one app a random public link with
  no login. It is off unless you set `WOLTSPACE_APP_QUICK_TUNNELS=1`, and each
  one is opened by hand through the share API. Stopping an app closes its
  tunnel. With the setting turned off, any leftover tunnel is closed the next
  time the app starts or the lodge restarts.
- `woltspace doctor` now says when a tunnel token is set but the tunnel URL is
  missing, which used to leave the lodge silently unpublished.

## Removed

- **The desktop app.** It may come back as its own project.
- The first Telegram adapter, a one-off session migration script and
  `CHANGELOG.md` (release notes live here).

## For developers

- **Tests never touch a live lodge.** The test suite runs its own tmux server
  and its own data folder, and refuses to start against a real colony. Tests
  that need a running server are opt-in, and only against a scratch lodge you
  name (`WOLTSPACE_TEST_LIVE_SERVER=1` with `WOLTSPACE_TEST_SERVER_URL`).

## Known limits

- Apps still inherit the lodge's environment, including its tokens. This
  changes when secrets get a proper home, which is planned work for 0.6.
- When the lodge is reachable remotely and Access is not configured, the lodge
  does not yet refuse remote requests the way the app gateway does.

## Upgrade notes

```sh
woltspace update --pre
```

Then check the lodge:

```sh
woltspace doctor
woltspace status
```

The 0.6 migration guide (`container/migrations/v0.6.0.md`) ships with the first
release candidate.
