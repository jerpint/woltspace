# Lodge usage

How many LLM tokens the lodge's wolts use, per day, wolt, session, provider and
model, with the providers' plan limits. Lodge-private, read-only for everyone but
the wolf.

- **Page:** `/usage` in the lodge (charts, tables, an activity calendar). It counts
  fresh when opened, when filters change, and every 2 minutes while visible.
  Each count scans the transcripts. The page requests compact chart and table
  totals with at most five session details and hides numbers older than 150 s.
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
operator's own use: it is not counted and not listed. Operator activity started
in a wolt's folder counts for that wolt on a native lodge. A transcript visible in
several places counts once: Claude keeps the maximum observed value of each usage counter per wolt and
message ID. Codex deduplicates by its engine
session (the longest copy wins).

One count runs at a time; concurrent requests share it. A count reads the
saved history and the transcripts and writes nothing.

## History

`<wolts_dir>/.space/usage/history.json` (0600, in a 0700 folder, with a sibling
lock) keeps per-day rows after the engines delete old transcripts. Only the
wolf writes it: the built-in `usage-save` job at 23:55 local time, and once at
start-up when the wolf was down at 23:55 (journaled under the owner `_lodge`;
see `woltspace wolf runs`). The save merges under an exclusive lock and replaces
the file atomically. Live counts and saves retain the larger of the saved and fresh
amount for each row. If a row shrinks or disappears, even with the same
transcript sources, the day is flagged as incomplete and the result is a
high-water estimate. History is part
of every backup; a restore may drop the lock, which the next save recreates.

Days are calendar days in the zone history was started in: the lodge's local
zone (the wolf's clock) at the first save, or UTC when the machine's zone has no
IANA name. The zone is recorded in history and kept, so saved days never shift:
if the machine's zone later differs, counting continues in the recorded zone and
the difference shows as a `reporting_zone_differs` coverage gap. A history with
days but no recorded zone is refused (`history_zone_mismatch`) rather than
guessed; add its zone by hand.

## Plan limits

Codex limits come from its transcripts. Claude plan limits are on by default
for lodge Claude sessions without a custom status line. Set
`usage.claude_limits.enabled` to `false` in the lodge's `woltspace.json` to
disable the built-in feed. Existing wolts pick it up at next start or resume.
An existing status-line setting, including a disabled one, takes precedence.

The dashboard shows the latest observed account reading, with its timestamp,
when Claude supplies a supported window. The status-line helper stores only
the observation time and supported rate-limit windows in
`<wolts_dir>/.space/usage/claude-limits.json`. An independent custom feed can
still supply this reading with the built-in feed off. Producers sharing the
snapshot need to coordinate through the same writer for newest-wins behavior.
