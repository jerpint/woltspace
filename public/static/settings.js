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

  function refreshFollowingStatuses(defaultHarness) {
    document.querySelectorAll('[data-wolt-status][data-following="true"]').forEach(status => {
      status.textContent = `Follows lodge · ${defaultHarness}`;
    });
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
      refreshFollowingStatuses(data.default);
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

  document.querySelectorAll('[data-wolt-select]').forEach(select => {
    select.addEventListener('change', async () => {
      const row = select.closest('[data-wolt-row]');
      const status = row.querySelector('[data-wolt-status]');
      const previous = row.dataset.savedValue;
      const requested = select.value || null;
      select.disabled = true;
      setGlobalState('saving', `Saving ${row.dataset.wolt}…`);
      try {
        const data = await save(`/wolts/${encodeURIComponent(row.dataset.wolt)}/harness`, { harness: requested });
        row.dataset.savedValue = requested || '';
        status.dataset.following = String(!data.pinned);
        status.textContent = data.pinned ? `Pinned · ${data.harness}` : `Follows lodge · ${data.harness}`;
        setGlobalState('saved', `${row.dataset.wolt} saved`);
        showToast(`${row.dataset.wolt} will use ${harnessLabels.get(data.harness) || data.harness}.`);
      } catch (error) {
        select.value = previous;
        setGlobalState('error', 'Could not save');
        showToast(error.message, 'error');
      } finally {
        select.disabled = false;
      }
    });
  });
}
