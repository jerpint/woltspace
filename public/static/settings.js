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

  // Per-engine catalogs + merged tier defaults, rendered by the settings route.
  const harnessData = JSON.parse(document.querySelector('[data-harness-data]')?.textContent || '[]');
  const harnessById = new Map(harnessData.map(h => [h.id, h]));

  function fillTierModels(modelSelect, harnessId, selected) {
    const catalog = harnessById.get(harnessId)?.catalog || [];
    modelSelect.innerHTML = '';
    catalog.forEach(m => {
      const option = document.createElement('option');
      option.value = m.id;
      option.textContent = m.label || m.id;
      if (m.id === selected) option.selected = true;
      modelSelect.appendChild(option);
    });
  }

  function refreshFollowingTiers(defaultHarness) {
    document.querySelectorAll('[data-tier-row]').forEach(row => {
      const engineSelect = row.querySelector('[data-tier-harness]');
      if (engineSelect.value) return; // pinned tier, unaffected
      const tier = row.dataset.tier;
      const model = harnessById.get(defaultHarness)?.models?.[tier] || '';
      row.dataset.savedModel = model;
      fillTierModels(row.querySelector('[data-tier-model]'), defaultHarness, model);
      row.querySelector('[data-tier-status]').textContent = `Follows lodge · ${defaultHarness} · ${model}`;
    });
  }

  document.querySelectorAll('[data-tier-row]').forEach(row => {
    const tier = row.dataset.tier;
    const engineSelect = row.querySelector('[data-tier-harness]');
    const modelSelect = row.querySelector('[data-tier-model]');
    const status = row.querySelector('[data-tier-status]');

    async function pushTier(body, revert) {
      engineSelect.disabled = true;
      modelSelect.disabled = true;
      setGlobalState('saving', `Saving ${tier} default…`);
      try {
        const data = await save('/harness/tiers', { tier, ...body });
        row.dataset.savedHarness = data.pinned ? data.harness : '';
        row.dataset.savedModel = data.model || '';
        // keep the page-load map fresh so lodge-default flips show current models
        const engineMeta = harnessById.get(data.harness);
        if (engineMeta?.models) engineMeta.models[tier] = data.model;
        fillTierModels(modelSelect, data.harness, data.model);
        status.textContent = `${data.pinned ? 'Pinned' : 'Follows lodge'} · ${data.harness} · ${data.model}`;
        setGlobalState('saved', `${tier} default saved`);
        showToast(`New ${tier}s follow ${harnessById.get(data.harness)?.label || data.harness} · ${data.model}.`);
      } catch (error) {
        revert();
        setGlobalState('error', 'Could not save');
        showToast(error.message, 'error');
      } finally {
        engineSelect.disabled = false;
        modelSelect.disabled = false;
      }
    }

    engineSelect.addEventListener('change', () => {
      const previous = row.dataset.savedHarness || '';
      pushTier({ harness: engineSelect.value || null }, () => { engineSelect.value = previous; });
    });

    modelSelect.addEventListener('change', () => {
      const previous = row.dataset.savedModel || '';
      pushTier({ model: modelSelect.value }, () => { if (previous) modelSelect.value = previous; });
    });
  });

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
      refreshFollowingTiers(data.default);
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
