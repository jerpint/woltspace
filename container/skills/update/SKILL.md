---
name: update
description: "Update the woltspace platform — check what is published, explain what is new, warn about anything breaking, and install with explicit consent. Use when asked to update woltspace."
user_invocable: true
---

# Update Woltspace

You are the lodge's gatekeeper for platform updates. Check what is published upstream,
translate it into plain language, flag anything that could disrupt the lodge, and open the
gates only when the human says go.

**Never update without explicit consent. This is the safety gate — an update stops and
restarts the control plane the lodge is running on.**

The platform is two published packages, not a checkout:

- `woltspace` on PyPI — the control plane, the lodge, the connectors, and the platform
  skills (they ship inside the wheel).
- `@woltspace/tui` on npm — the terminal cockpit and the pty bridge.

They are versioned **independently**. The tui declares the minimum `woltspace` version it
needs and checks it at startup; woltspace does not check the tui's version. Update each on
its own — and if the tui says the lodge is too old, updating the wheel is the fix.

## Step 1 — name the runtime

```bash
printenv WOLTSPACE_ISOLATION   # host = native, external = container
```

If the variable is empty, fall back to the shape of the filesystem: a `/workspace/wolts`
directory means the image's fixed mount, so container; otherwise native.

Say which one you are in before anything else — the rest of this skill forks there.
Native updates itself. A container cannot: docker lives on the human's machine and
nothing inside the box can reach it.

## Step 2 — check

**Installed.** The `woltspace` first on a session's PATH is the bundled thin client; it
execs the real CLI for `--version` and `paths`, so this is accurate in both runtimes:

```bash
woltspace --version
woltspace paths                      # install_root — remember it, Step 6 needs it
woltspace-tui-service --version || npm ls -g @woltspace/tui
```

**Published.**

```bash
curl -s https://pypi.org/pypi/woltspace/json | python3 -c "import json,sys;print(json.load(sys.stdin)['info']['version'])"
npm view @woltspace/tui version
```

If installed and published match, say the lodge is current and stop. No consent question,
no commands.

**Bump type** comes from the numbers alone — `MAJOR.MINOR.PATCH`. Major changed → major,
else minor changed → minor, else patch.

**Release notes** for the published version (public, no auth):

```bash
curl -s https://api.github.com/repos/jerpint/woltspace/releases/tags/v<latest> \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('name',''));print(d.get('body',''))"
```

Read the body and summarize it in plain words — what the lodge gains, what changes under
the human. No commit hashes, no branch names, no diff jargon. Two things you must not
bury: anything the notes mark **breaking**, and whether a migration ships for that
version (Step 6 tells you how to look; the answer arrives with the new code, so before
the install you can only report what the notes claim).

## Step 3 — one status block

Brief, lore-flavoured, risk named but not dramatized. Lead with what is new.

### Already current
> 🟢 **The lodge is current** (0.5.0)
> Nothing to fetch. Both halves — control plane and cockpit — are on the published version.

### Patch
> 🟢 **Patch available** (0.5.0 → 0.5.1)
> [one or two sentences on what it fixes]. Small and safe, no migration. Want me to bring it in?

### Minor
> 🟡 **Minor update available** (0.5.0 → 0.6.0)
> [what the lodge gains]. [If a migration ships: one plain sentence on what it changes and
> what it asks of you.] The lodge blinks for about ten seconds while the control plane
> restarts — live sessions survive it. Want details, or shall I go?

### Major
> 🔴 **Major update available** (0.5.0 → 1.0.0) — big one
> [what it is]. Breaking: [exactly what breaks and what you do about it]. I'd read the
> notes before we move. Want me to walk you through them?

Rules: never say "breaking changes" without saying what breaks and what the human does
about it. Offer detail rather than front-loading it.

## Step 4 — consent

Ask with `AskUserQuestion` and wait. Do not infer a yes. Accept "yes", "go", "do it",
"go ahead", "update". Anything else is a no.

If the human already said "update it, confirm" in their first message, that is the yes —
still show the status block from Step 3 first, so they know what they agreed to.

## Step 5a — native update (on GO)

Each package to its own latest, in this order:

```bash
uv tool install --force 'woltspace[connectors]==<latest>'
npm install -g @woltspace/tui@latest
```

The `connectors` extra carries the Telegram dependencies; dropping it takes chat dark.
The versions need not match — but the tui refuses a lodge older than the minimum it
declares, so an updated tui on a stale wheel is the one pairing that breaks.

Then the restart. **Tell the human before you run it**, because this kills the control
plane your own session is talking to:

```bash
woltspace stop
woltspace start
```

What that costs, in plain words:

- Live sessions **survive** — they live in tmux, which `stop` never touches, and `start`
  re-adopts them from the registry.
- Telegram goes quiet for roughly ten seconds while the connector comes back.
- The lodge and the cockpit are unreachable for the same few seconds.
- Platform skills resync during `start`. There is no separate sync step — do not invent one.

Then confirm it came back:

```bash
woltspace status
```

Report the state (healthy or not) and how many sessions were adopted. If `start` fails,
`woltspace doctor` names the one command that fixes each failed check — read it out
rather than guessing.

## Step 5b — container (on GO)

You cannot do it. Docker is on the human's machine and this box has no reach into it.
Print the two commands for them to run on the host, and say plainly that you are handing
over rather than executing:

```bash
woltspace rebuild --latest
woltspace restart
```

Once the image is published to a registry, that pair becomes `docker compose pull &&
docker compose up -d` — mention it as what is coming, not as what to run today.

A container rebuild ends the running sessions; unlike native, they do not survive it.
Say so before the human commits.

## Step 6 — migration

Migrations ship **inside the wheel**, so the one for the new version only exists after the
install. Look after Step 5a, never before:

```bash
INSTALL_ROOT=$(woltspace paths | awk '$1=="install_root:"{print $2}')
ls "$INSTALL_ROOT/container/migrations/"
```

The file for the version you just installed is `v<latest>.md` (older entries may be bare
`<latest>.md` or `.sh` — check both spellings before concluding there is none).

- **Patch bump:** skip this step entirely. Patches do not carry migrations.
- **Minor or major:** if the file exists, read it, summarize the steps in plain words, and
  ask before performing any of them. The documents are prose a wolt carries out with
  consent — not scripts to fire blind. If one names a shell script beside it, show the
  human what it does before running it. If no file exists, say so; that is a normal answer.

## Step 7 — report

Close with: the version now installed (both halves), whether the control plane came back
healthy, how many sessions were adopted, and any action item the notes or the migration
left open — a new config key, a renamed setting, something the human has to do by hand.

Flag anything you could not verify rather than rounding it up to success.

## Notes

- The platform is an installed package. There is no clone to pull, no branch to track, and
  no platform source on disk to edit — a wolt that finds itself reaching for git here has
  the wrong model of the world.
- Two artifacts, one version. If you ever see them disagree, that is the bug: reinstall the
  pair rather than patching one.
- Skills are read live from the installed bundle, so upgrading the wheel upgrades every
  platform skill at once. Sessions already running keep the bodies they started with.
- When unsure whether something is breaking, flag it. A false alarm costs a sentence; a
  missed one costs the lodge.
