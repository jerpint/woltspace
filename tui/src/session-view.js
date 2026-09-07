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

export const sessionPolicy = (session) => {
  const policy = session?.execution_policy;
  if (typeof policy === 'string') return policy;
  return policy?.mode || 'auto';
};

export function spawnTarget(capabilities, wolt, launchCwd) {
  const native = capabilities?.supports_host_workdirs === true;
  return {
    workdir: native ? launchCwd : null,
    displayWorkdir: native ? launchCwd : (wolt?.home || 'wolt home'),
    executionPolicy: capabilities?.default_execution_policy || (native ? 'prompt' : 'auto'),
  };
}
