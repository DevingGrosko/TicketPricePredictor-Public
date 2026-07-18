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

function replaceOptions(select, values, placeholder) {
  select.innerHTML = '';
  const empty = document.createElement('option');
  empty.value = '';
  empty.textContent = placeholder;
  select.appendChild(empty);

  values.forEach((value) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  select.disabled = values.length === 0;
}

function updateSubmitState(form) {
  const requiredSelects = [...form.querySelectorAll('select')];
  form.querySelector('.submit-analysis').disabled = !requiredSelects.every((select) => select.value);
}

document.querySelectorAll('.selection-form').forEach((form) => {
  const placeSelect = form.querySelector('.place-select');
  const gameSelect = form.querySelector('.game-select');
  const sectionSelect = form.querySelector('.section-select');

  placeSelect.addEventListener('change', () => {
    const place = placeSelect.value;
    if (gameSelect) {
      replaceOptions(gameSelect, gamesData[place] || [], 'Select a game');
      replaceOptions(sectionSelect, [], 'Select a section');
    } else {
      replaceOptions(sectionSelect, placesData[place] || [], 'Select a section');
    }
    updateSubmitState(form);
  });

  if (gameSelect) {
    gameSelect.addEventListener('change', () => {
      const place = placeSelect.value;
      const game = gameSelect.value;
      const sections = (gameSectionsData[place] && gameSectionsData[place][game]) || placesData[place] || [];
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
