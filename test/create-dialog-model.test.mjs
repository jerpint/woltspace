import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';

const lodgeSource = readFileSync(
  new URL('../public/static/lodge.js', import.meta.url),
  'utf8',
);

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  if (start === -1) throw new Error(`${name} is not in lodge.js`);
  const body = source.indexOf('{', start);
  let depth = 0;
  for (let i = body; i < source.length; i += 1) {
    if (source[i] === '{') depth += 1;
    if (source[i] === '}') depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  throw new Error(`${name} is incomplete`);
}

const entry = (id, label = id) => ({ id, label });

// The shape /harnesses answers with: tier defaults in `models`, the list a
// card's dropdown offers in `catalog`.
const HARNESSES = [
  {
    id: 'claude',
    label: 'Claude Code',
    models: { raccoon: 'opus', beaver: 'sonnet', otter: 'haiku' },
    catalog: [entry('opus', 'Opus'), entry('sonnet', 'Sonnet'), entry('haiku', 'Haiku'), entry('fable', 'Fable')],
  },
  {
    id: 'codex',
    label: 'Codex',
    models: { raccoon: 'gpt-5.5', beaver: 'gpt-5.6-terra', otter: 'gpt-5.6-luna' },
    catalog: [entry('gpt-5.5'), entry('gpt-5.6-terra'), entry('gpt-5.6-luna')],
  },
  // Offers claude's sonnet and fable too, but neither is one of its defaults.
  {
    id: 'bridge',
    label: 'Bridge',
    models: { raccoon: 'gpt-5.5', beaver: 'gpt-5.6-terra', otter: 'gpt-5.6-luna' },
    catalog: [entry('sonnet'), entry('fable'), entry('gpt-5.5'), entry('gpt-5.6-terra'), entry('gpt-5.6-luna')],
  },
  // Takes any typed model name: its catalog is only suggestions.
  {
    id: 'typed',
    label: 'Typed',
    freeform_model: true,
    models: { raccoon: 'fable', beaver: 'sonnet', otter: 'haiku' },
    catalog: [entry('fable'), entry('sonnet'), entry('haiku')],
  },
  {
    id: 'bare',
    label: 'Bare',
    models: { raccoon: 'fable', beaver: 'sonnet', otter: 'haiku' },
    catalog: [],
  },
];

// A <select> as far as the dialog can tell it from a real one: its value is the
// selected option's, a value no option carries selects nothing, and a list
// with nothing selected shows its first option.
class Select {
  constructor() {
    this.options = [];
    this.selectedOption = null;
    this.hidden = true;
    this.dataset = {};
  }
  get value() { return this.selectedOption ? this.selectedOption.value : ''; }
  set value(value) {
    this.selectedOption = this.options.find(option => option.value === String(value)) || null;
  }
  replaceChildren(...options) {
    this.options = [];
    this.selectedOption = null;
    options.forEach(option => this.appendChild(option));
  }
  appendChild(option) {
    this.options.push(option);
    if (!this.selectedOption) this.selectedOption = this.options[0];
    return option;
  }
  prepend(option) {
    this.options.unshift(option);
    if (!this.selectedOption) this.selectedOption = this.options[0];
  }
}

class ClassList {
  constructor() { this.names = new Set(); }
  add(name) { this.names.add(name); }
  remove(name) { this.names.delete(name); }
  toggle(name, on) { if (on) this.names.add(name); else this.names.delete(name); }
  contains(name) { return this.names.has(name); }
}

class Card {
  constructor(type, pace) {
    this.dataset = { type };
    this.classList = new ClassList();
    this.select = new Select();
    this.parts = {
      '.type-card-hint': { dataset: { pace }, textContent: pace },
      '.type-card-model': { textContent: '', hidden: false },
      '.type-card-select': this.select,
    };
  }
  querySelector(selector) { return this.parts[selector] || null; }
}

// Everything else the dialog touches: the name field, the buttons, the engine
// menu. None of it decides a model.
function plainElement() {
  return {
    value: '',
    textContent: '',
    hidden: false,
    disabled: false,
    type: '',
    style: {},
    dataset: {},
    classList: new ClassList(),
    children: [],
    setAttribute() {},
    focus() {},
    append(...nodes) { this.children.push(...nodes); },
    appendChild(node) { this.children.push(node); return node; },
    replaceChildren(...nodes) { this.children = nodes; },
  };
}

function loadCreateDialog() {
  const cards = [new Card('otter', 'Quick'), new Card('beaver', 'Builder'), new Card('raccoon', 'Thinker')];
  const elements = {};
  const context = {
    harnessList: HARNESSES,
    harnessDefault: 'claude',
    createSelectedType: null,
    createSelectedHarness: '',
    // Not about models: the name line, the engine menu's own drawing.
    showCreateNameWarning: () => {},
    startCreateNameIdeas: () => {},
    updateCreatePreview: () => {},
    renderCreateHarnessTrigger: () => {},
    closeCreateHarnessMenu: () => {},
    engineIcon: () => plainElement(),
    lodgeElement: () => plainElement(),
    setTimeout: () => 0,
    document: {
      createElement: () => ({ value: '', textContent: '' }),
      getElementById: id => (elements[id] ||= plainElement()),
      querySelectorAll: selector => {
        if (selector !== '.type-card') throw new Error(`unexpected selector ${selector}`);
        return cards;
      },
      querySelector: selector => {
        const match = /^\.type-card\[data-type="([a-z]+)"\]$/.exec(selector);
        if (!match) throw new Error(`unexpected selector ${selector}`);
        return cards.find(card => card.dataset.type === match[1]) || null;
      },
    },
  };
  const source = [
    'harnessInfo',
    'modelFor',
    'modelLabelFor',
    'openCreateWolt',
    'pickType',
    'renderCreateHarnessOptions',
    'selectCreateHarness',
    'renderCreateHarness',
    'createSelectedModel',
  ].map(name => functionSource(lodgeSource, name)).join('\n');
  runInNewContext(
    `${source}
    this.dialog = {
      open: () => openCreateWolt(),
      pickEngine: id => selectCreateHarness(id),
      pickCard: card => pickType(card),
      model: () => createSelectedModel(),
    };`,
    context,
  );

  const card = type => cards.find(c => c.dataset.type === type);
  const dialog = {
    open: context.dialog.open,
    pickEngine: context.dialog.pickEngine,
    // What Create would send as `model`: '' means the request leaves it out.
    model: context.dialog.model,
    select: type => card(type).select,
    shown: type => card(type).select.value,
    shownEverywhere: () => Object.fromEntries(cards.map(c => [c.dataset.type, c.select.value])),
    pickCard: type => context.dialog.pickCard(card(type)),
    // Picking an option in a card's dropdown: the browser sets the value, then
    // the select's change handler picks that card.
    choose(type, model) {
      card(type).select.value = model;
      assert.equal(card(type).select.value, model, `${type} offers ${model}`);
      context.dialog.pickCard(card(type));
    },
  };
  dialog.open();
  return dialog;
}

test('the dialog opens on the usual model of each card, with nothing to pin', () => {
  const dialog = loadCreateDialog();

  assert.deepEqual(dialog.shownEverywhere(), { otter: 'haiku', beaver: 'sonnet', raccoon: 'opus' });
  assert.equal(dialog.model(), '');
});

test('a model chosen on a card survives picking the same engine again', () => {
  const dialog = loadCreateDialog();

  dialog.choose('raccoon', 'fable');
  dialog.pickEngine('claude');

  assert.equal(dialog.shown('raccoon'), 'fable');
  assert.equal(dialog.model(), 'fable');
});

test('the cards left alone stay on their usual model and pin nothing', () => {
  const dialog = loadCreateDialog();

  dialog.choose('raccoon', 'fable');
  dialog.pickEngine('claude');

  assert.equal(dialog.shown('beaver'), 'sonnet');
  assert.equal(dialog.shown('otter'), 'haiku');
  dialog.pickCard('beaver');
  assert.equal(dialog.model(), '');
});

test('a chosen model follows to another engine that also offers it', () => {
  const dialog = loadCreateDialog();

  dialog.choose('raccoon', 'fable');
  dialog.pickEngine('bridge');

  assert.equal(dialog.shown('raccoon'), 'fable');
  assert.equal(dialog.model(), 'fable');
});

test('an engine that does not offer the chosen model shows its usual one instead', () => {
  const dialog = loadCreateDialog();

  dialog.choose('raccoon', 'fable');
  dialog.pickEngine('codex');

  assert.equal(dialog.shown('raccoon'), 'gpt-5.5');
  assert.equal(dialog.model(), '');
});

test('a card left on its usual model never turns into a pin on another engine', () => {
  const dialog = loadCreateDialog();

  // The beaver shows claude's sonnet because nobody touched it. The next
  // engine offers sonnet as well, but not as the beaver's usual model.
  dialog.pickCard('beaver');
  assert.equal(dialog.shown('beaver'), 'sonnet');
  dialog.pickEngine('bridge');

  assert.equal(dialog.shown('beaver'), 'gpt-5.6-terra');
  assert.equal(dialog.model(), '');
});

test('reopening the dialog starts from the usual models again', () => {
  const dialog = loadCreateDialog();

  dialog.choose('raccoon', 'fable');
  dialog.open();

  assert.deepEqual(dialog.shownEverywhere(), { otter: 'haiku', beaver: 'sonnet', raccoon: 'opus' });
  dialog.pickCard('raccoon');
  assert.equal(dialog.model(), '');
});

for (const engine of ['typed', 'bare']) {
  test(`an engine without a fixed list (${engine}) hides the dropdowns and pins nothing`, () => {
    const dialog = loadCreateDialog();

    dialog.choose('raccoon', 'fable');
    dialog.pickEngine(engine);

    for (const type of ['otter', 'beaver', 'raccoon']) {
      assert.equal(dialog.select(type).hidden, true, `${type} dropdown is hidden`);
      assert.equal(dialog.shown(type), '');
    }
    assert.equal(dialog.model(), '');
  });
}
