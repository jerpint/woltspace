import { spawnTarget } from './session-view.js';

export const woltTypes = ['raccoon', 'beaver', 'otter'];

export function validateWoltName(value) {
  const name = (value || '').trim().toLowerCase();
  if (!name) return 'name is required';
  if (name.length > 20) return 'name must be 20 characters or less';
  if (!/^[a-z][a-z0-9-]*$/.test(name)) {
    return 'use lowercase letters, numbers, and hyphens; start with a letter';
  }
  return '';
}

export function createWoltAction(name, type, capabilities, launchCwd) {
  const error = validateWoltName(name);
  if (error) throw new Error(error);
  if (!woltTypes.includes(type)) throw new Error(`unsupported wolt type: ${type}`);
  const target = spawnTarget(capabilities, null, launchCwd);
  return {
    type: 'create',
    name: name.trim().toLowerCase(),
    woltType: type,
    workdir: target.workdir,
    executionPolicy: target.executionPolicy,
    isolation: capabilities?.isolation,
  };
}
