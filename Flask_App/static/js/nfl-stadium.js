(() => {
  function normalize(value) {
    return String(value || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, ' ')
      .trim();
  }

  function bindVenueSearch(input) {
    const scope = input.closest('[data-venue-search-scope], .nfl-stadium-picker, .nfl-stadium-directory') || document;
    const cards = Array.from(scope.querySelectorAll('[data-stadium-card]'));
    const count = scope.querySelector('[data-stadium-count]');
    const empty = scope.querySelector('[data-stadium-empty]');
    const noun = input.dataset.venueNoun || scope.dataset.venueNoun || 'stadium';
    if (!cards.length) return;

    const update = () => {
      const query = normalize(input.value);
      let visible = 0;
      cards.forEach((card) => {
        const haystack = normalize(card.dataset.search || card.textContent);
        const matches = !query || haystack.includes(query);
        card.hidden = !matches;
        if (matches) visible += 1;
      });

      if (count) count.textContent = `${visible} ${noun}${visible === 1 ? '' : 's'}`;
      if (empty) empty.hidden = visible !== 0;
    };

    input.addEventListener('input', update);
    input.addEventListener('search', update);
  }

  document.querySelectorAll('[data-stadium-search]').forEach(bindVenueSearch);

  document.querySelectorAll('[data-stadium-switch]').forEach((stadiumSwitch) => {
    const select = stadiumSwitch.querySelector('select[name="team"], select[name="venue"]');
    if (select) {
      select.addEventListener('change', () => {
        if (select.value) stadiumSwitch.submit();
      });
    }
  });

  document.querySelectorAll('[data-section-jump]').forEach((form) => {
    const select = form.querySelector('[data-section-jump-select]');
    const button = form.querySelector('[data-section-jump-button]');
    if (!select || !button) return;

    const update = () => {
      button.disabled = !select.value;
    };
    select.addEventListener('change', update);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      if (select.value) window.location.assign(select.value);
    });
    update();
  });

  document.querySelectorAll('[data-game-section-form]').forEach((picker) => {
    const select = picker.querySelector('[data-game-section]');
    const link = picker.querySelector('[data-game-open]');
    const baseUrl = String(picker.dataset.baseUrl || '');
    if (!select || !link || !baseUrl) return;

    const update = () => {
      const section = select.value;
      if (!section) {
        link.href = '#';
        link.setAttribute('aria-disabled', 'true');
        return;
      }
      const separator = baseUrl.includes('?') ? '&' : '?';
      link.href = `${baseUrl}${separator}section=${encodeURIComponent(section)}`;
      link.setAttribute('aria-disabled', 'false');
    };

    select.addEventListener('change', update);
    link.addEventListener('click', (event) => {
      if (link.getAttribute('aria-disabled') === 'true') event.preventDefault();
    });
    update();
  });

  const sectionTable = document.querySelector('[data-section-table]');
  if (sectionTable) {
    const rows = Array.from(sectionTable.querySelectorAll('[data-section-row]'));
    const filter = document.querySelector('[data-section-filter]');
    const sort = document.querySelector('[data-section-sort]');
    const visibleCount = document.querySelector('[data-section-visible]');
    const empty = document.querySelector('[data-section-table-empty]');

    const number = (row, key, fallback = 0) => {
      const value = Number(row.dataset[key]);
      return Number.isFinite(value) ? value : fallback;
    };

    const compare = (a, b, mode) => {
      if (mode === 'drop-desc') {
        return number(b, 'drop', -999999) - number(a, 'drop', -999999)
          || a.dataset.name.localeCompare(b.dataset.name, undefined, { numeric: true });
      }
      if (mode === 'games-desc') {
        return number(b, 'games') - number(a, 'games')
          || number(a, 'price', Number.POSITIVE_INFINITY)
            - number(b, 'price', Number.POSITIVE_INFINITY);
      }
      if (mode === 'name-asc') {
        return a.dataset.name.localeCompare(b.dataset.name, undefined, { numeric: true });
      }
      return number(a, 'price', Number.POSITIVE_INFINITY)
        - number(b, 'price', Number.POSITIVE_INFINITY)
        || a.dataset.name.localeCompare(b.dataset.name, undefined, { numeric: true });
    };

    const updateTable = () => {
      const query = normalize(filter ? filter.value : '');
      const mode = sort ? sort.value : 'price-asc';
      rows
        .slice()
        .sort((a, b) => compare(a, b, mode))
        .forEach((row) => sectionTable.appendChild(row));

      let visible = 0;
      rows.forEach((row) => {
        const matches = !query || normalize(row.dataset.name).includes(query);
        row.hidden = !matches;
        if (matches) visible += 1;
      });

      if (visibleCount) visibleCount.textContent = String(visible);
      if (empty) empty.hidden = visible !== 0;
    };

    if (filter) {
      filter.addEventListener('input', updateTable);
      filter.addEventListener('search', updateTable);
    }
    if (sort) sort.addEventListener('change', updateTable);
    updateTable();
  }

  const dashboardLinks = Array.from(
    document.querySelectorAll('.nfl-dashboard-nav a[href^="#"]'),
  );
  const dashboardSections = dashboardLinks
    .map((link) => document.querySelector(link.getAttribute('href')))
    .filter(Boolean);

  if (dashboardLinks.length && dashboardSections.length && 'IntersectionObserver' in window) {
    const byId = new Map(
      dashboardLinks.map((link) => [link.getAttribute('href').slice(1), link]),
    );
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
        if (!visible) return;
        dashboardLinks.forEach((link) => link.classList.remove('is-active'));
        const active = byId.get(visible.target.id);
        if (active) active.classList.add('is-active');
      },
      { rootMargin: '-18% 0px -68% 0px', threshold: [0, 0.1, 0.25] },
    );
    dashboardSections.forEach((section) => observer.observe(section));
  }
})();
