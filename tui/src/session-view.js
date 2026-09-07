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
