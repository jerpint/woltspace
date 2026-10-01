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

  document.querySelectorAll('[data-tier-select]').forEach(select => {
    select.addEventListener('change', async () => {
      const row = select.closest('[data-tier-row]');
      const previous = row.dataset.savedValue;
      const { harness, harnessLabel, tier, tierLabel } = select.dataset;
      select.disabled = true;
      setGlobalState('saving', 'Saving default model…');
      try {
        await save('/harness/tiers', { harness, tiers: { [tier]: select.value } });
        row.dataset.savedValue = select.value;
        setGlobalState('saved', 'Default model saved');
        const chosen = select.options[select.selectedIndex].text;
        showToast(`${harnessLabel} ${tierLabel.toLowerCase()}s now start on ${chosen}.`);
      } catch (error) {
        select.value = previous;
        setGlobalState('error', 'Could not save');
        showToast(error.message, 'error');
      } finally {
        select.disabled = false;
      }
    });
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
}
