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

function replaceNflOptions(select, values = [], placeholder) {
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

const nflOptionsCache = new Map();
async function loadNflOptions(team) {
  if (!team) return { games: [], sections_by_game: {} };
  if (!nflOptionsCache.has(team)) {
    const requestPromise = fetch(`${nflOptionsUrl}?team=${encodeURIComponent(team)}`, {
      headers: { Accept: 'application/json' },
    }).then(async (response) => {
      if (!response.ok) throw new Error(`Options request failed (${response.status})`);
      return response.json();
    }).catch((error) => {
      nflOptionsCache.delete(team);
      throw error;
    });
    nflOptionsCache.set(team, requestPromise);
  }
  return nflOptionsCache.get(team);
}

const nflForm = document.querySelector('.nfl-selection-form');
if (nflForm) {
  const teamSelect = nflForm.querySelector('.place-select');
  const gameSelect = nflForm.querySelector('.game-select');
  const sectionSelect = nflForm.querySelector('.section-select');
  const submit = nflForm.querySelector('.submit-analysis');
  const mapButton = nflForm.querySelector('[data-map-launch]');
  let currentOptions = { games: [], sections_by_game: {} };

  const updateActions = () => {
    submit.disabled = !(teamSelect.value && gameSelect.value && sectionSelect.value);
    if (mapButton) mapButton.disabled = !(teamSelect.value && gameSelect.value);
  };

  teamSelect.addEventListener('change', async () => {
    const team = teamSelect.value;
    currentOptions = { games: [], sections_by_game: {} };
    replaceNflOptions(gameSelect, [], team ? 'Loading games…' : 'Select a game');
    replaceNflOptions(sectionSelect, [], 'Select a section');
    gameSelect.disabled = true;
    sectionSelect.disabled = true;
    updateActions();
    if (!team) return;

    nflForm.setAttribute('aria-busy', 'true');
    try {
      const options = await loadNflOptions(team);
      if (teamSelect.value !== team) return;
      currentOptions = options;
      replaceNflOptions(gameSelect, options.games || [], 'Select a game');
    } catch (_error) {
      if (teamSelect.value === team) {
        replaceNflOptions(gameSelect, [], 'Unable to load games');
        gameSelect.disabled = true;
      }
    } finally {
      nflForm.removeAttribute('aria-busy');
      updateActions();
    }
  });

  gameSelect.addEventListener('change', () => {
    const sections = (currentOptions.sections_by_game || {})[gameSelect.value] || [];
    replaceNflOptions(sectionSelect, sections, 'Select a section');
    updateActions();
  });

  sectionSelect.addEventListener('change', updateActions);

  if (mapButton) {
    mapButton.addEventListener('click', () => {
      if (mapButton.disabled) return;
      const params = new URLSearchParams({
        team: teamSelect.value,
        game: gameSelect.value,
      });
      if (sectionSelect.value) params.set('section', sectionSelect.value);
      window.location.assign(`${mapButton.dataset.mapBase}?${params.toString()}`);
    });
  }

  nflForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const params = new URLSearchParams({
      team: teamSelect.value,
      game: gameSelect.value,
      section: sectionSelect.value,
    });
    window.location.assign(`/nfl/graph?${params.toString()}`);
  });
}
