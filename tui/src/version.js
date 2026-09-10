export const packageName = '@woltspace/tui';
export const packageVersion = '0.5.1';

// One package, two bins. Both answer `--version --json` with the same name and
// version and say which bin they are, so the Python side can tell a real
// @woltspace/tui bin from something else wearing the name. It does not check
// the version: compatibility runs the other way now.
export const versionRecord = (binary = 'woltspace-tui') => ({
  name: packageName,
  version: packageVersion,
  binary,
});

// The client declares what it needs. The lodge never checks the tui — the tui
// checks the lodge, here, against this one number. Raise it only when the tui
// starts depending on something a lodge below it cannot serve.
export const minLodgeVersion = '0.5.0';

// A lodge that reports no version is a pre-0.5.1 one: /health gained the field
// in 0.5.1, so "missing" means the last release without it.
export const ASSUMED_LODGE_VERSION = '0.5.0';

// [major, minor, patch]; pre-release and build suffixes ("0.6.0-rc.1",
// "0.6.0rc1", "0.6.0+dev") are ignored, so a release candidate counts as the
// release. Anything unparseable reads as 0.0.0 — old, not compatible.
export const parseVersion = (version) => {
  const match = /^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?/.exec(String(version ?? ''));
  if (!match) return [0, 0, 0];
  return [match[1], match[2], match[3]].map((part) => (part === undefined ? 0 : Number(part)));
};

// Is `lodgeVersion` at least `min`? Missing lodge version → ASSUMED_LODGE_VERSION.
export const lodgeSatisfies = (lodgeVersion, min = minLodgeVersion) => {
  const have =
    lodgeVersion === undefined || lodgeVersion === null || String(lodgeVersion).trim() === ''
      ? ASSUMED_LODGE_VERSION
      : lodgeVersion;
  const left = parseVersion(have);
  const right = parseVersion(min);
  for (let i = 0; i < 3; i++) {
    if (left[i] !== right[i]) return left[i] > right[i];
  }
  return true;
};

// One line, or null. Two cases and no more: the lodge is below what this tui
// needs, or the lodge has moved a release line ahead of the tui.
export const versionBanner = (lodgeVersion, tuiVersion = packageVersion) => {
  if (!lodgeSatisfies(lodgeVersion)) {
    const seen = lodgeVersion || `pre-${ASSUMED_LODGE_VERSION}`;
    return `this tui needs woltspace >= ${minLodgeVersion}, lodge is ${seen} — run: uv tool install 'woltspace[connectors]'`;
  }
  const [lodgeMajor, lodgeMinor] = parseVersion(lodgeVersion || ASSUMED_LODGE_VERSION);
  const [tuiMajor, tuiMinor] = parseVersion(tuiVersion);
  if (lodgeMajor > tuiMajor || (lodgeMajor === tuiMajor && lodgeMinor > tuiMinor)) {
    return `lodge ${lodgeVersion} is newer than this tui — npm i -g @woltspace/tui@latest`;
  }
  return null;
};
