const tabs = document.querySelectorAll('.analysis-tab');
const panels = document.querySelectorAll('.analysis-panel');

tabs.forEach((tab) => {
  tab.addEventListener('click', () => {
    tabs.forEach((item) => {
      item.classList.remove('is-active');
      item.setAttribute('aria-selected', 'false');
    });
    panels.forEach((panel) => {
      panel.classList.remove('is-active');
      panel.hidden = true;
    });

    tab.classList.add('is-active');
    tab.setAttribute('aria-selected', 'true');
    const panel = document.getElementById(tab.dataset.target);
    panel.hidden = false;
    requestAnimationFrame(() => panel.classList.add('is-active'));
  });
});

function isParkingOption(value) {
  const raw = typeof value === 'object' ? (value.label || value.value) : value;
  const normalized = String(raw || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, ' ')
    .trim();
  return /\bparking\b/.test(normalized)
    || /^(lot|garage)\b/.test(normalized)
    || normalized.includes('park and ride');
}

function replaceOptions(select, values = [], placeholder) {
  select.innerHTML = '';
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = placeholder;
  select.appendChild(empty);

  const visibleValues = values.filter((value) => !isParkingOption(value));
  visibleValues.forEach((value) => {
    const option = document.createElement('option');
    option.value = typeof value === 'object' ? value.value : value;
    option.textContent = typeof value === 'object' ? value.label : value;
    select.appendChild(option);
  });
  select.disabled = visibleValues.length === 0;
}

function setLoading(select, label) {
  replaceOptions(select, [], label);
  select.disabled = true;
}

function updateSubmitState(form) {
  const requiredSelects = [...form.querySelectorAll('select')];
  form.querySelector('.submit-analysis').disabled = !requiredSelects.every((select) => select.value);
}

const baseballOptionsCache = new Map();
async function loadBaseballOptions(place) {
  if (!place) return { games: [], multi_sections: [], sections_by_game: {} };
  if (!baseballOptionsCache.has(place)) {
    const requestPromise = fetch(`${baseballOptionsUrl}?venue=${encodeURIComponent(place)}`, {
      headers: { Accept: 'application/json' },
    }).then(async (response) => {
      if (!response.ok) throw new Error(`Options request failed (${response.status})`);
      return response.json();
    }).catch((error) => {
      baseballOptionsCache.delete(place);
      throw error;
    });
    baseballOptionsCache.set(place, requestPromise);
  }
  return baseballOptionsCache.get(place);
}

const formOptions = new WeakMap();
document.querySelectorAll('.selection-form').forEach((form) => {
  const placeSelect = form.querySelector('.place-select');
  const gameSelect = form.querySelector('.game-select');
  const sectionSelect = form.querySelector('.section-select');

  placeSelect.addEventListener('change', async () => {
    const place = placeSelect.value;
    formOptions.delete(form);
    if (gameSelect) setLoading(gameSelect, place ? 'Loading games…' : 'Select a game');
    setLoading(sectionSelect, place ? 'Loading sections…' : 'Select a section');
    updateSubmitState(form);
    if (!place) return;

    form.setAttribute('aria-busy', 'true');
    try {
      const options = await loadBaseballOptions(place);
      if (placeSelect.value !== place) return;
      formOptions.set(form, options);
      if (gameSelect) {
        replaceOptions(gameSelect, options.games || [], 'Select a game');
        replaceOptions(sectionSelect, [], 'Select a section');
      } else {
        replaceOptions(sectionSelect, options.multi_sections || [], 'Select a section');
      }
    } catch (_error) {
      if (placeSelect.value !== place) return;
      if (gameSelect) setLoading(gameSelect, 'Unable to load games');
      setLoading(sectionSelect, 'Unable to load sections');
    } finally {
      form.removeAttribute('aria-busy');
      updateSubmitState(form);
    }
  });

  if (gameSelect) {
    gameSelect.addEventListener('change', () => {
      const options = formOptions.get(form) || {};
      const sections = (options.sections_by_game || {})[gameSelect.value] || [];
      replaceOptions(sectionSelect, sections, 'Select a section');
      updateSubmitState(form);
    });
  }

  sectionSelect.addEventListener('change', () => updateSubmitState(form));

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const place = placeSelect.value;
    const section = sectionSelect.value;
    if (!place || !section) return;

    const params = new URLSearchParams({ event: place, section });
    if (form.dataset.analysis === 'game') {
      params.set('game', gameSelect.value);
      params.set('mode', 'single');
      window.location.assign(`/graph?${params.toString()}`);
    } else if (form.dataset.analysis === 'timing') {
      window.location.assign(`/predict?${params.toString()}`);
    } else {
      window.location.assign(`/graph?${params.toString()}`);
    }
  });
});
