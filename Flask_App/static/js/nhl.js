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

function replaceNhlOptions(select, values, placeholder) {
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

const nhlForm = document.querySelector('.nhl-selection-form');
if (nhlForm) {
  const teamSelect = nhlForm.querySelector('.place-select');
  const gameSelect = nhlForm.querySelector('.game-select');
  const sectionSelect = nhlForm.querySelector('.section-select');
  const submit = nhlForm.querySelector('.submit-analysis');
  const mapButton = nhlForm.querySelector('[data-map-launch]');

  const updateActions = () => {
    submit.disabled = !(teamSelect.value && gameSelect.value && sectionSelect.value);
    if (mapButton) {
      mapButton.disabled = !(teamSelect.value && gameSelect.value);
    }
  };

  teamSelect.addEventListener('change', () => {
    replaceNhlOptions(gameSelect, nhlGamesData[teamSelect.value] || [], 'Select a game');
    replaceNhlOptions(sectionSelect, [], 'Select a section');
    updateActions();
  });

  gameSelect.addEventListener('change', () => {
    const sections =
      (nhlGameSectionsData[teamSelect.value] &&
        nhlGameSectionsData[teamSelect.value][gameSelect.value]) || [];
    replaceNhlOptions(sectionSelect, sections, 'Select a section');
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

  nhlForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const params = new URLSearchParams({
      team: teamSelect.value,
      game: gameSelect.value,
      section: sectionSelect.value,
    });
    window.location.assign(`/nhl/graph?${params.toString()}`);
  });
}
