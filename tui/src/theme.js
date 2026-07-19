// Forest palette + lore voice. Fun is core, never at the expense of clarity.

export const color = {
  terra: '#E07B4A', // warm terracotta, brightened for dark terminals
  amber: '#C98B2A',
  green: '#7FA98A', // alive, calm
  dim: '#8A8378', // bark-grey secondary text
};

export const glyph = {
  otter: '🦦',
  beaver: '🦫',
  raccoon: '🦝',
  wolf: '🐺',
  dog: '🐶',
  rodent: '🐭',
};

export const creatureGlyph = (creature) => glyph[creature] || glyph.rodent;

export const adapterTag = { lodge: 'ld', telegram: 'tg', slack: 'sk', create: 'cr' };

export const lore = {
  loading: 'gnawing…',
  emptyAlive: 'the lodge is quiet - no wolts stirring',
  emptyAll: 'no sessions in the journal yet',
  emptyMatch: 'nothing in the lodge matches',
  stopConfirm: (slug) => `send ${slug} to sleep? (y/N)`,
  stopped: (slug) => `${slug} is asleep`,
  spawned: (slug) => `${slug} scampers into the lodge`,
  sent: (slug) => `message tucked into ${slug}'s burrow`,
  spawnTitle: 'wake a wolt',
  sendTitle: (slug) => `message → ${slug}`,
};

export function age(epochSeconds) {
  if (!epochSeconds) return '';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function clock(date) {
  return date.toTimeString().slice(0, 8);
}
