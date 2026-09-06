export const packageName = '@woltspace/tui';
export const packageVersion = '0.5.0-rc.2';

// One package, two bins. Both answer `--version --json` with the same name and
// version and say which bin they are, so the Python side can match exactly.
export const versionRecord = (binary = 'woltspace-tui') => ({
  name: packageName,
  version: packageVersion,
  binary,
});
