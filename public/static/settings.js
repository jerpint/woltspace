const root = document.querySelector('[data-settings-root]');

if (root) {
  const globalState = document.querySelector('[data-global-state]');
  const globalMessage = document.querySelector('[data-global-message]');
  const toast = document.querySelector('[data-toast]');
  const toastIcon = document.querySelector('[data-toast-icon]');
  const toastMessage = document.querySelector('[data-toast-message]');
  const harnessLabels = new Map(
    [...document.querySelectorAll('[name="default-harness"]')].map(input => [
      input.value,
      input.closest('.ds-choice-card').querySelector('.ds-choice-title').textContent.trim(),
    ]),
  );
  let toastTimer;

  function setGlobalState(kind, message) {
    globalState.classList.remove('saving', 'saved', 'error');
    if (kind) globalState.classList.add(kind);
    globalMessage.textContent = message;
  }

  function showToast(message, kind = 'saved') {
    clearTimeout(toastTimer);
    toast.classList.toggle('error', kind === 'error');
    toastIcon.textContent = kind === 'error' ? '!' : '✓';
    toastMessage.textContent = message;
    toast.classList.add('visible');
    toastTimer = setTimeout(() => toast.classList.remove('visible'), 2600);
  }

  async function save(endpoint, body) {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'The lodge could not save that change.');
    return data;
  }

  async function saveAccess(value) {
    const form = document.querySelector('[data-access-form]');
    const controls = [...form.querySelectorAll('input, button')];
    controls.forEach(control => { control.disabled = true; });
    setGlobalState('saving', 'Saving Access verification…');
    try {
      const data = await save('/settings/access', { access: value });
      const saved = data.access || {};
      form.dataset.savedValue = JSON.stringify(saved);
      form.querySelectorAll('[data-access-field]').forEach(input => { input.value = saved[input.dataset.accessField] || ''; });
      setGlobalState('saved', 'Access verification saved');
      showToast(data.access ? 'Remote requests will be verified.' : 'Access verification is disabled.');
    } catch (error) {
      const previous = JSON.parse(form.dataset.savedValue || '{}');
      form.querySelectorAll('[data-access-field]').forEach(input => { input.value = previous[input.dataset.accessField] || ''; });
      setGlobalState('error', 'Could not save');
      showToast(error.message, 'error');
    } finally {
      controls.forEach(control => { control.disabled = false; });
    }
  }

  document.querySelector('[data-access-save]')?.addEventListener('click', () => {
    const values = {};
    document.querySelectorAll('[data-access-field]').forEach(input => { values[input.dataset.accessField] = input.value.trim(); });
    saveAccess(values);
  });
  document.querySelector('[data-access-clear]')?.addEventListener('click', () => saveAccess(null));

  document.querySelector('[data-default-form]')?.addEventListener('change', async event => {
    const input = event.target.closest('[name="default-harness"]');
    if (!input) return;
    const group = input.closest('[data-default-form]');
    const previous = group.dataset.savedValue;
    group.disabled = true;
    setGlobalState('saving', 'Saving lodge default…');
    try {
      const data = await save('/harness/default', { harness: input.value });
      group.dataset.savedValue = data.default;
      root.dataset.defaultHarness = data.default;
      setGlobalState('saved', 'Lodge default saved');
      showToast(`${harnessLabels.get(data.default) || data.default} is now the lodge default.`);
    } catch (error) {
      const previousInput = group.querySelector(`[value="${CSS.escape(previous)}"]`);
      if (previousInput) previousInput.checked = true;
      setGlobalState('error', 'Could not save');
      showToast(error.message, 'error');
    } finally {
      group.disabled = false;
    }
  });

  document.querySelector('[data-expiry-select]')?.addEventListener('change', async event => {
    const select = event.currentTarget;
    const row = select.closest('[data-expiry-row]');
    const previous = row.dataset.savedValue;
    const value = select.value ? Number(select.value) : null;
    select.disabled = true;
    setGlobalState('saving', 'Saving session policy…');
    try {
      const data = await save('/settings/session-expiry', { idle_timeout_seconds: value });
      row.dataset.savedValue = data.idle_timeout_seconds ?? '';
      setGlobalState('saved', 'Session policy saved');
      showToast(value ? `Idle sessions will rest after ${select.options[select.selectedIndex].text}.` : 'Idle sessions will stay open.');
    } catch (error) {
      select.value = previous;
      setGlobalState('error', 'Could not save');
      showToast(error.message, 'error');
    } finally {
      select.disabled = false;
    }
  });

  document.querySelector('[data-apps-domain-save]')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    const row = button.closest('[data-apps-domain-row]');
    const input = row.querySelector('[data-apps-domain-input]');
    button.disabled = input.disabled = true;
    setGlobalState('saving', 'Saving app addresses…');
    try {
      const data = await save('/settings/apps-domain', { apps_domain: input.value.trim() || null });
      input.value = data.apps_domain || '';
      row.dataset.savedValue = input.value;
      setGlobalState('saved', 'App addresses saved');
      showToast(data.apps_domain ? `Apps will use *.${data.apps_domain}.` : 'Apps will keep their current addresses.');
    } catch (error) {
      input.value = row.dataset.savedValue;
      setGlobalState('error', 'Could not save');
      showToast(error.message, 'error');
    } finally {
      button.disabled = input.disabled = false;
    }
  });
}
