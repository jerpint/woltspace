# Migrations

One document per release that needs a hand beyond reinstalling the packages. Patch bumps
never need one.

These ship **inside the wheel**. The directory lands at
`<install_root>/container/migrations/`, where `install_root` is the path `woltspace paths`
prints, so the migration for a version arrives with the code for that version — which is
why the update skill only looks here *after* the install.

## Naming

- `v0.6.0.md` — migration for the v0.6.0 release
- `v1.0.0.md` — migration for the v1.0.0 release

## Shape

A migration is **prose a wolt performs with consent**, not a script it fires blind. Each
document should:

1. Say who it is for and whether it is required or optional.
2. State the preconditions — which version you are coming from, what must exist.
3. Give the steps in order, each one something a wolt can read out before doing it.
4. Name anything only the human can do (a new config key, a credential, a host command).
5. Be idempotent: running it twice changes nothing the second time.

A document may point at a shell script beside it for the mechanical part. The wolt shows
the human what the script does before running it.

## Usage

Ask any wolt to update the lodge. The update skill reads the document for the version it
just installed and walks the steps with you. Nothing runs without your go-ahead.
