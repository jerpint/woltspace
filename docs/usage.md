# Lodge usage

How many LLM tokens the lodge's wolts use, per day, wolt, session, provider and
model, with the providers' plan limits. Lodge-private, read-only for everyone but
the wolf.

- **Page:** `/usage` in the lodge (charts, tables, an activity calendar). It counts
  fresh when opened and every 2 minutes while visible, and never shows numbers
  older than 150 s.
- **CLI:** `woltspace usage summary|table|limits` with `--by`, `--since`,
  `--until`, `--wolt`, `--provider`, `--model`, `--metric`, `--top`, `--format
  table|json|csv`. A thin client of the API; the `usage` platform skill teaches
  wolts to use it.
- **API:** `GET /api/usage/data` (the page's dashboard JSON) and
  `GET /api/usage?by=wolt&since=7d` (the CLI's queries). The query string is
  validated before any count; errors are fixed codes. App subdomains and shared
  app addresses never reach these routes.

## Where the numbers come from

`container/lib/usage_collector.py` reads the engines' own transcripts for every
wolt (a folder holding `wolt/wolt.json`): `<wolt>/.claude/projects` and
`<wolt>/.codex/sessions` when they physically lie inside the wolt's folder, and
on a native lodge the operator's shared engine home (`~/.claude/projects` or
`$CLAUDE_CONFIG_DIR/projects`, `$CODEX_HOME/sessions`). A wolt engine folder that
resolves anywhere else (a link to a shared home) is read like a shared home.
A shared transcript belongs to a wolt only when the working folder it recorded
at its start lies inside that wolt's folder (real paths, compared by whole
components), or, failing that, when the lodge's own session records name its
engine session. Everything else there, malformed records included, is the
operator's own use: it is not counted and not listed. A transcript visible in
several places counts once: Claude by its message ids, Codex by its engine
session (the longest copy wins).

One count runs at a time; concurrent requests share it. A count reads the
saved history and the transcripts and writes nothing.

## History

`<wolts_dir>/.space/usage/history.json` (0600, in a 0700 folder, with a sibling
lock) keeps per-day rows after the engines delete old transcripts. Only the
wolf writes it: the built-in `usage-save` job at 23:55 local time, and once at
start-up when the wolf was down at 23:55 (journaled under the owner `_lodge`;
see `woltspace wolf runs`). The save merges under an exclusive lock and replaces
the file atomically. A day whose transcripts shrank or broke since the last save
keeps, row by row, the larger of the saved and the fresh amount. History is part
of every backup; a restore may drop the lock, which the next save recreates.

Days are calendar days in the zone history was started in: the lodge's local
zone (the wolf's clock) at the first save, or UTC when the machine's zone has no
IANA name. The zone is recorded in history and kept, so saved days never shift:
if the machine's zone later differs, counting continues in the recorded zone and
the difference shows as a `reporting_zone_differs` coverage gap. A history with
days but no recorded zone is refused (`history_zone_mismatch`) rather than
guessed; add its zone by hand.

## Plan limits

Codex limits come from its transcripts. Claude limits are optional: the lodge
reads `<wolts_dir>/.space/usage/claude-limits.json` when something provides it
(`{"read_at": ISO time, "rate_limits": {"five_hour": {"used_percentage": n,
"resets_at": unix seconds}, "seven_day": {...}}}`, the shape Claude Code hands a
status line). Woltspace never writes Claude settings to set that up; without the
file, Claude limits show as unavailable.
