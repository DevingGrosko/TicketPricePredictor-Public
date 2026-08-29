function replaceNflOptions(select, values, placeholder) {
  select.innerHTML = '';
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = placeholder;
  select.appendChild(empty);

  values.forEach((value) => {
    const option = document.createElement('option');
    option.value = typeof value === 'object' ? value.value : value;
    option.textContent = typeof value === 'object' ? value.label : value;
    select.appendChild(option);
  });
  select.disabled = values.length === 0;
}

const nflForm = document.querySelector('.nfl-selection-form');
if (nflForm) {
  const teamSelect = nflForm.querySelector('.place-select');
  const gameSelect = nflForm.querySelector('.game-select');
  const sectionSelect = nflForm.querySelector('.section-select');
  const submit = nflForm.querySelector('.submit-analysis');

  const updateSubmit = () => {
    submit.disabled = !(teamSelect.value && gameSelect.value && sectionSelect.value);
  };

  teamSelect.addEventListener('change', () => {
    replaceNflOptions(gameSelect, nflGamesData[teamSelect.value] || [], 'Select a game');
    replaceNflOptions(sectionSelect, [], 'Select a section');
    updateSubmit();
  });

  gameSelect.addEventListener('change', () => {
    const sections =
      (nflGameSectionsData[teamSelect.value] &&
        nflGameSectionsData[teamSelect.value][gameSelect.value]) || [];
    replaceNflOptions(sectionSelect, sections, 'Select a section');
    updateSubmit();
  });

  sectionSelect.addEventListener('change', updateSubmit);

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
