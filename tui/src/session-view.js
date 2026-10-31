// Is there an agent in there to talk to?
//
// The lodge reports two kinds of liveness: `tmux_alive` (the tmux session
// exists) and `agent_alive` (an agent process is in its tree). Only the second
// one means enter can attach — a died or failed-to-respawn agent leaves a bare
// login shell holding the tmux session open, and attaching to that drops the
// human into a shell wearing their session's name. `alive` already carries the
// agent answer from a current lodge; the fallbacks keep an older lodge (which
// only ever sent tmux presence) working as it did.
export const agentAlive = (session) => {
  if (typeof session?.agent_alive === 'boolean') return session.agent_alive;
  return session?.alive === true;
};

// How many rows the default filter is hiding. Shown as a footer hint so the
// list never looks empty when there is history one keystroke away.
export const inactiveCount = (sessions) =>
  (sessions || []).filter((s) => !agentAlive(s)).length;

export const sessionWorkdir = (session) =>
  session?.target?.canonical_workdir || session?.workdir || session?.dir || '';

// A record without version 2 is from before Bypass had its name: its "auto"
// (or no policy at all) ran with no checks, so it shows as bypass*.
export const sessionPolicy = (session) => {
  const policy = session?.execution_policy;
  const mode = (typeof policy === 'string' ? policy : policy?.mode) || 'auto';
  const legacy = typeof policy !== 'object' || policy === null || policy.policy_version !== 2;
  return legacy && mode === 'auto' ? 'bypass*' : mode;
};

// The levels a spawn can ask for; '' leaves the choice to the server.
export const LEVELS = [
  ['', 'server default: Bypass, or Ask on a host without a grant'],
  ['prompt', 'Ask: you approve its actions'],
  ['auto', 'Auto: the engine approves routine actions and asks for the rest'],
  ['bypass', 'Bypass: no checks at all'],
];

// Only the levels this wolt's engine offers here, when the lodge says so.
export const spawnLevels = (capabilities, wolt) => {
  const offered = capabilities?.levels?.[wolt?.harness];
  return LEVELS.filter(([mode]) => !mode || !offered || mode in offered);
};

// Where a newly woken wolt starts.
//
// Every spawn is rooted at the wolt's own home, native and container alike. A
// null workdir is the API's "use the wolt home" — start_session resolves it
// against the wolt, which is the only place that knows it. Native used to
// substitute the directory `woltspace tui` happened to be launched from, which
// meant a den woken from the platform checkout booted with none of its own
// project-scoped skills, and a den woken from another wolt's directory booted
// rooted inside that wolt's tree.
//
// `supportsHostWorkdirs` is passed through so callers can see the host can do
// it; spawning into a host directory is a deliberate opt-in still to be built,
// and until then nothing picks one implicitly. `launchCwd` is kept in the
// signature for that future opt-in.
export function spawnTarget(capabilities, wolt, launchCwd, level = '') {
  const native = capabilities?.supports_host_workdirs === true;
  return {
    workdir: null,
    displayWorkdir: wolt?.home || 'wolt home',
    // No pick: let the server apply exact-target grants.
    executionPolicy: level || undefined,
    supportsHostWorkdirs: native,
  };
}
