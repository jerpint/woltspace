# The Mill

The Mill serves wolt sites, each on an origin of its own, so a page a wolt wrote never runs with the lodge's origin. On the lodge's origin, its script could call the lodge's API as the owner.

## How a site is reached

- **The address:** `http://<wolt>.sites.localhost:7777/`, on the lodge's one port. Browsers resolve `*.localhost` to this machine with no DNS setup.
- **The forwarding:** the lodge recognises a site host and forwards it to the Mill. It never runs a lodge route for it.
- **The Mill itself:** its own process, supervised like the other connectors. It listens only on a unix socket, `<data root>/.space/run/mill/mill.sock`, whose folder is the owner's alone (`0700`). It has no TCP port.
- **If the Mill is down:** sites answer `502` and the lodge keeps working.

The old `/wolt/<name>/site/` address keeps working for now. Remote sites (through a tunnel) come later.

## Who may see a site

For now, only the owner. The lodge decides, and tells the Mill in one header after stripping any copy the visitor sent:

- **Lodge auth off** (the default): a visitor on this machine who came straight in, not through a tunnel. This is the same rule as the old site address.
- **Lodge auth on** (`WOLTSPACE_LODGE_AUTH=1`): a site has its own origin, so the lodge's cookie never reaches it.
  1. Open the site from the lodge, at `/sites/open?wolt=<name>&path=/page.html`. This needs the paired browser or the owner key.
  2. The lodge hands over a one-time grant, good for one minute and for that site only.
  3. The site's host turns the grant into a cookie of its own, valid for 12 hours.
  4. `woltspace pair --forget` closes those site sessions too.

The Mill forwards only content-negotiation, caching, range and browser-provenance headers. No cookie, `Authorization` or lodge key ever reaches it.

## What a site can do

- **Requests:** `GET` and `HEAD`, for files under the wolt's real `wolt/site/` folder. A symlinked site folder, symlinked files leading outside it, `..` and encoded separators all answer `404`.
- **Response headers:** every response carries `nosniff`, `Content-Security-Policy: sandbox allow-scripts allow-same-origin`, and `Cache-Control: private, no-store`.
- **Service workers:** refused.
- **Live reload:** owner pages get a reload client. Its socket, `/_woltspace/livereload`, only accepts the site's own origin.

## Naming

How a site host is spelled depends on the way in, so naming is a list (`server/content_hosts.py`). The local scheme is `<wolt>.sites.localhost`. Remote ways in add theirs when they start serving sites. A site label is a DNS label, so a wolt whose name has `_` or is over 63 characters has no site host.
