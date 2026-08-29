function replaceConcertOptions(select, values, placeholder) {
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

const concertForm = document.querySelector('.concert-selection-form');
if (concertForm) {
  const venueSelect = concertForm.querySelector('.place-select');
  const concertSelect = concertForm.querySelector('.concert-select');
  const sectionSelect = concertForm.querySelector('.section-select');
  const submit = concertForm.querySelector('.submit-analysis');

  const updateSubmit = () => {
    submit.disabled = !(venueSelect.value && concertSelect.value && sectionSelect.value);
  };

  venueSelect.addEventListener('change', () => {
    replaceConcertOptions(
      concertSelect,
      concertsData[venueSelect.value] || [],
      'Select a concert'
    );
    replaceConcertOptions(sectionSelect, [], 'Select a section');
    updateSubmit();
  });

  concertSelect.addEventListener('change', () => {
    const sections =
      (concertSectionsData[venueSelect.value] &&
        concertSectionsData[venueSelect.value][concertSelect.value]) || [];
    replaceConcertOptions(sectionSelect, sections, 'Select a section');
    updateSubmit();
  });

  sectionSelect.addEventListener('change', updateSubmit);

  concertForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const params = new URLSearchParams({
      event: venueSelect.value,
      concert: concertSelect.value,
      section: sectionSelect.value,
    });
    window.location.assign(`/concerts/graph?${params.toString()}`);
  });
}
