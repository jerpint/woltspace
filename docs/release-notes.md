# Woltspace 0.6.0 (release candidate)

0.6 is a groundwork release. It separates who can use your apps and wolt sites
from who can reach your lodge, keeps what wolts write from acting as you in
your browser, closes the public links that were on by default, and removes code
the platform no longer uses. Telegram now runs as a connector, outside the
lodge. It is a **minor**
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
  `python3 -m http.server $PORT --bind 127.0.0.1`. Check other start commands
  for a loopback bind too: `HOST=127.0.0.1` is only a hint.
- **Your lodge is published and you want it to check its own login.** Run
  `woltspace access set` (see "The lodge checks its own login"). Without it the
  lodge keeps trusting whatever guards its way in, and `woltspace doctor` says
  so.
- **You use Telegram.** It moves to the connector by itself on the first start
  (below): your own chat keeps working as before. The dog is gone, and group
  chats need to be allowed again by hand.
- **A wolt's site uses livereload, or fetches its own files.** Sites now open on
  their own address (below), where both work. Inside the old lodge address they
  are sandboxed and can't.

The migration guide (`container/migrations/v0.6.0.md`) walks a wolt through
each of these with your consent.

## The Mill: apps get their own door

- **A separate app gateway.** Apps are served by their own small server, on the
  lodge port minus 660 (`7117` for a lodge on `7777`), with none of the lodge's
  routes. Being able to open an app no longer means being able to reach your
  lodge. Override the port with `WOLTSPACE_APP_GATEWAY_PORT` or in Settings.
- **Its own domain and its own login.** Remote apps are served at
  `<app>.<your app domain>`, behind their own Cloudflare Access application, so
  people you share an app with never pass the lodge's login. Configure the app
  domain, the gateway port and both Access audiences under **Settings**. An app
  host with an Access application of its own adds that audience to the apps
  audiences (comma-separated).
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

## Wolt sites get their own address

- **Each wolt's site is served through the app gateway on its own origin**,
  like an app: `http://<wolt>.sites.localhost:<gateway port>/` on this machine.
  Old `/wolt/<name>/site` links redirect there, and the wolt page and the split
  view (push-view included) open it directly.
- **Private by default.** Only you see a site. `woltspace site share <wolt>
  <email>` adds a reader (that matters once sites are served remotely).
- **Livereload and same-site fetch work again** on the site's own address. The
  site can't reach the lodge, other sites or apps, and can't install a service
  worker.
- **Not yet remote.** `site-<wolt>.<app domain>` addresses answer "pending
  setup" in this release.

## What wolts write can't act as you

- Anything a wolt authors that is still served on the lodge's address (old
  site links, spark previews, the built-in About / Memory / Settings pages) is
  sandboxed: it has no origin, so its scripts can't use your login to call the
  lodge. The built-in pages also stop loading a wolt's custom CSS and fonts.
- Lodge pages can only be framed by the lodge itself.
- The unused `/tools` routes (an any-method proxy on the lodge's address) are
  gone.

## The lodge checks its own login

- **`woltspace access set`** saves the Cloudflare Access settings (team domain,
  the lodge app's audience, your email), from this machine, with the lodge
  running or stopped, over SSH too. Then the lodge verifies the Access token
  on every remote request and websocket and only lets your email in.
  `woltspace access show` and `woltspace access clear` do the obvious. The
  cloudflare skill now saves these right after creating the Access app.
- **Without them** the lodge keeps trusting whatever guards its way in (a
  tunnel with Access at the edge, a tailnet). `woltspace doctor` warns that
  anyone who reaches the address is treated as you.
- **Other ways in.** `WOLTSPACE_ALLOWED_HOSTS` adds exact host names (a tailnet
  or LAN name) the lodge answers to; doctor names entries that can never match.
- Requests from this machine work as before.
- A broken `woltspace.json` is never overwritten by the lodge anymore (it used
  to be reset on first-run, dropping its settings).

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
- **An app won't start on a port another program already holds** (another
  user's lodge on the same machine, say). Before, the gateway would serve that
  program under your app's name.

## Telegram runs as a connector

Chat apps now run as **connectors**: a separate program that talks to the chat
service and hands messages to the lodge through a small local API, instead of
living inside the lodge. Telegram is the first, and it is the default.

- **Nothing to do for your own chat.** On the first start, the lodge sets the
  connector up from your bot token and `TELEGRAM_ALLOWED_USERS`: each allowed
  user's own chat may talk to every wolt, start, wake and stop sessions, use
  `/apps`, and have voice notes transcribed, as before. It picks up the wolt
  and session the chat was on. `woltspace connector list` shows it.
- **What works as before.** A message with no live session starts one with
  your message in it; replying to a wolt's message goes to its session, quoted;
  `/wolt`, `/new`, `/sessions`, `/kill`, `/apps`, `/help`; text, voice, photos,
  videos and files.
- **Every other chat is allowed on purpose.** `woltspace connector bind`
  allows one sender in one chat, sends their messages to one wolt, and turns
  on only the grants you name: wake a stopped session, pick other wolts, start
  sessions (limited per hour and at once), stop the sessions that chat
  started, `/apps`, transcribe voice notes.
- **Messages wait instead of getting lost.** While the connector or the lodge
  is down or paused, messages queue and are handed over once, when it's back.
  `woltspace connector handoffs` shows what's waiting, with retry and cancel.
- **Voice notes** are transcribed only for chats allowed to, with the lodge's
  `OPENAI_API_KEY`, within a daily budget. The audio is sent to OpenAI.
- **What changed.** The dog is deprecated: no @mention helper in groups, and
  no group chat is carried over (bind it). `/kill` stops only sessions that
  chat started or talked to. The old in-process adapter is still there for
  this release: stop the lodge, then `woltspace connector runtime telegram
  adapter` (and `... connector` to come back).

## Removed

- **The desktop app.** It may come back as its own project.
- The dog (Telegram's group helper), with the move to the connector.
- The first Telegram adapter, a one-off session migration script and
  `CHANGELOG.md` (release notes live here).

## For developers

- **Tests never touch a live lodge.** The test suite runs its own tmux server
  and its own data folder, and refuses to start against a real colony. Tests
  that need a running server are opt-in, and only against a scratch lodge you
  name (`WOLTSPACE_TEST_LIVE_SERVER=1` with `WOLTSPACE_TEST_SERVER_URL`).

## Known limits

- Everything runs as your OS user. Wolts and apps can still read the lodge's
  files and environment (apps no longer get the chat, GitHub-app or Cloudflare
  tokens since 0.5.16, but other keys still pass). Real containment needs
  separate OS users and is planned after 0.6.
- Without lodge-side Access settings, the lodge trusts its way in; that's
  your choice, and doctor says so.
- Remote wolt sites are not served yet; sites on `woltspace.app` come later.
- The app-port check catches a port that's already taken; it doesn't yet prove
  the process answering later is the one the lodge started.
- Only Chromium was used for the browser checks.
- A connector is trusted to say who sent a message. A malicious connector
  can claim to be any sender you allowed and use that chat's grants, so only
  run connectors you trust, and grant each chat only what it needs.

## Upgrade notes

```sh
woltspace update --pre
```

Then check the lodge:

```sh
woltspace doctor
woltspace status
```

Read the migration guide (`container/migrations/v0.6.0.md`) first, or ask a
wolt to walk you through it.
