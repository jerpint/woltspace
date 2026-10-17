---
name: creatures
description: Register a custom creature (its own name, emoji, model and role on one of the three sizes) or change a wolt's creature, including your own, when the user asks. Always confirm with the user first; a change applies from the wolt's next session.
---

# Creatures: register one, or change a wolt's

The lodge has three sizes: 🦦 otter (quick), 🦫 beaver (builder) and 🦝
raccoon (thinker). A **custom creature** is a new identity on one of them:
its own name and emoji, optionally its own model per engine and a default
role. It behaves exactly like its size: the same site, tier and lore. Only
the name and emoji change, everywhere the lodge shows a wolt (Telegram,
Slack, the lodge, the TUI, its site).

Custom creatures live in the lodge's settings. Change them only with the
commands below, never by editing `woltspace.json` or a `wolt.json` by hand.

## Always confirm first

Before you register, remove or assign a creature, tell the user exactly what
will change and wait for a yes. That includes changing **your own** creature.
For example:

> I'll register ⭐ star (raccoon-sized, codex model gpt-6-astra) and make
> nova a star. Its next session will show ⭐; this session stays as it is.
> Go?

## Commands

```bash
woltspace creature list                     # the sizes and the lodge's custom creatures
woltspace creature add star --emoji ⭐ --size raccoon \
  --model codex=gpt-6-astra --role "Reviewer and planner"
woltspace creature assign nova star         # change a wolt's creature
woltspace creature rm star                   # only when no wolt is one
```

- **name:** lowercase letters, digits and hyphens. Built-in names (raccoon,
  wolf, dog, ...) are taken.
- **--size:** what it behaves like: `otter`, `beaver` or `raccoon`.
- **--model HARNESS=MODEL:** optional, repeatable. The id must be one the
  engine offers (`GET /harnesses` lists them). An engine without one uses the
  size's default.
- **--role:** optional. The default role for a new wolt of this creature.
- `add` never overwrites: to change a creature, `rm` it (no wolt may be one)
  and `add` it again.

## What assign changes, and when

`woltspace creature assign <wolt> <type>` changes only the wolt's creature
type. It prints what the wolt keeps:

- **Its own pins win.** A model or harness set on the wolt itself stays and
  still applies. Then comes the creature's own model, then the size's default.
- **Its role stays.** A creature's role is only a default for new wolts.
- **Next session.** A running session keeps the model and settings it started
  with. Tell the user the change shows from the wolt's next session.
- A starter site that still shows the old creature's emoji switches to the
  new one. A site emoji the wolt chose itself stays.

Wolf and dog wolts keep their creature: assign refuses them.

## Use on yourself

When the user asks you to change your own model or creature: confirm first,
run the command, then say it applies from your next session. To change only
your model, the engine settings in your `wolt.json` (`model`, `harness`) are
enough; see the lodge's switching-models guide.
