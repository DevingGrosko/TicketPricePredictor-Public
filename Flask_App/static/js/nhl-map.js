(() => {
  const dataNode = document.getElementById('nhl-map-data');
  const svg = document.querySelector('[data-stadium-map]');
  if (!dataNode || !svg) return;

  let mapData;
  try {
    mapData = JSON.parse(dataNode.textContent || '{}');
  } catch (error) {
    console.error('Could not read NHL arena map data.', error);
    return;
  }

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const SCHEMATIC_VIEW = { x: 0, y: 0, width: 1000, height: 700 };
  const stage = document.querySelector('[data-map-stage]');
  const tooltip = document.querySelector('[data-map-tooltip]');
  const levelContainer = document.querySelector('[data-map-levels]');
  const searchInput = document.querySelector('[data-map-search]');
  const searchButton = document.querySelector('[data-map-search-button]');
  const nameNode = document.querySelector('[data-section-name]');
  const levelNode = document.querySelector('[data-section-level]');
  const priceNode = document.querySelector('[data-section-price]');
  const listingsNode = document.querySelector('[data-section-listings]');
  const historyLink = document.querySelector('[data-section-history]');
  const zoomInButton = document.querySelector('[data-map-zoom-in]');
  const zoomOutButton = document.querySelector('[data-map-zoom-out]');
  const resetButton = document.querySelector('[data-map-reset]');
  const currency = String(mapData.currency || 'USD').toUpperCase();

  let currencyFormatter = null;
  try {
    currencyFormatter = new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
      minimumFractionDigits: 0,
      maximumFractionDigits: 2,
    });
  } catch (error) {
    currencyFormatter = null;
  }

  function isParkingSectionName(value) {
    const normalized = String(value || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, ' ')
      .trim();
    return /\bparking\b/.test(normalized)
      || /^(lot|garage)\b/.test(normalized)
      || normalized.includes('park and ride');
  }

  const sections = Array.isArray(mapData.sections)
    ? mapData.sections.map((section) => ({
        name: String(section.name || '').trim(),
        price: Number.isFinite(Number(section.price)) ? Number(section.price) : null,
        listing_count: Number.isFinite(Number(section.listing_count))
          ? Number(section.listing_count)
          : null,
      })).filter(
        (section) => section.name && !isParkingSectionName(section.name),
      )
    : [];

  function createSvgElement(name, attributes = {}) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => {
      if (value !== '' && value !== null && value !== undefined) {
        element.setAttribute(key, String(value));
      }
    });
    return element;
  }

  function normalize(value) {
    return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  }

  function sectionNumber(name) {
    const matches = String(name || '').match(/\d+/g);
    return matches ? Number(matches[matches.length - 1]) : null;
  }

  function sectionLevel(section) {
    const normalized = normalize(section.name);
    const number = sectionNumber(section.name);
    if (/\b(glass|ice|rink|floor)\b/.test(normalized) && number === null) {
      return { key: 'ice', label: 'Ice level', order: 50 };
    }
    if (/\bsuite/.test(normalized) && number === null) {
      return { key: 'suites', label: 'Suites', order: 900 };
    }
    if (number !== null) {
      const hundred = number < 100 ? 100 : Math.floor(number / 100) * 100;
      return { key: String(hundred), label: `${hundred} level`, order: hundred };
    }
    if (/\b(lower|club)\b/.test(normalized)) {
      return { key: 'lower', label: 'Lower / club', order: 150 };
    }
    if (/\b(loge|mezzanine)\b/.test(normalized)) {
      return { key: 'middle', label: 'Loge / mezzanine', order: 450 };
    }
    if (/\b(upper|terrace|balcony)\b/.test(normalized)) {
      return { key: 'upper', label: 'Upper bowl', order: 750 };
    }
    return { key: 'other', label: 'Other sections', order: 850 };
  }

  function compareSections(a, b) {
    const aNumber = sectionNumber(a.name);
    const bNumber = sectionNumber(b.name);
    if (aNumber !== null && bNumber !== null && aNumber !== bNumber) return aNumber - bNumber;
    if (aNumber !== null && bNumber === null) return -1;
    if (aNumber === null && bNumber !== null) return 1;
    return a.name.localeCompare(b.name, undefined, { numeric: true });
  }

  const grouped = new Map();
  sections.forEach((section) => {
    const level = sectionLevel(section);
    if (!grouped.has(level.key)) grouped.set(level.key, { ...level, sections: [] });
    grouped.get(level.key).sections.push(section);
  });
  const levels = Array.from(grouped.values())
    .map((level) => ({ ...level, sections: level.sections.sort(compareSections) }))
    .sort((a, b) => a.order - b.order || a.label.localeCompare(b.label));

  function parseProviderGeometry() {
    const raw = mapData.geometry;
    if (!raw || mapData.geometry_mode !== 'provider') return null;
    const box = Array.isArray(raw.view_box) ? raw.view_box.map(Number) : [];
    if (box.length !== 4 || !box.every(Number.isFinite) || box[2] <= 0 || box[3] <= 0) {
      return null;
    }
    const geometryBySection = new Map();
    const knownNames = new Set(sections.map((section) => section.name));
    (Array.isArray(raw.sections) ? raw.sections : []).forEach((row) => {
      const name = String(row && row.name || '').trim();
      if (!knownNames.has(name) || !Array.isArray(row.shapes)) return;
      const shapes = row.shapes.map((shape) => ({
        path: String(shape && shape.path || '').trim(),
        transform: String(shape && shape.transform || '').trim(),
      })).filter((shape) => shape.path);
      if (shapes.length) geometryBySection.set(name, shapes);
    });
    const required = Math.max(4, Math.min(12, Math.ceil(sections.length * 0.6)));
    if (geometryBySection.size < required) return null;
    return {
      view: { x: box[0], y: box[1], width: box[2], height: box[3] },
      sections: geometryBySection,
    };
  }

  const providerGeometry = parseProviderGeometry();
  const sectionElements = new Map();
  let baseView = providerGeometry ? { ...providerGeometry.view } : { ...SCHEMATIC_VIEW };
  let selectedSection = null;
  let activeLevel = 'all';
  let zoom = 1;
  let centerX = baseView.x + baseView.width / 2;
  let centerY = baseView.y + baseView.height / 2;
  let currentView = { ...baseView };
  let dragState = null;

  function ellipsePoint(cx, cy, rx, ry, angle) {
    return { x: cx + rx * Math.cos(angle), y: cy + ry * Math.sin(angle) };
  }

  function annularSegmentPath(cx, cy, innerRx, innerRy, outerRx, outerRy, start, end) {
    const outerStart = ellipsePoint(cx, cy, outerRx, outerRy, start);
    const outerEnd = ellipsePoint(cx, cy, outerRx, outerRy, end);
    const innerEnd = ellipsePoint(cx, cy, innerRx, innerRy, end);
    const innerStart = ellipsePoint(cx, cy, innerRx, innerRy, start);
    const largeArc = end - start > Math.PI ? 1 : 0;
    return [
      `M ${outerStart.x.toFixed(2)} ${outerStart.y.toFixed(2)}`,
      `A ${outerRx.toFixed(2)} ${outerRy.toFixed(2)} 0 ${largeArc} 1 ${outerEnd.x.toFixed(2)} ${outerEnd.y.toFixed(2)}`,
      `L ${innerEnd.x.toFixed(2)} ${innerEnd.y.toFixed(2)}`,
      `A ${innerRx.toFixed(2)} ${innerRy.toFixed(2)} 0 ${largeArc} 0 ${innerStart.x.toFixed(2)} ${innerStart.y.toFixed(2)}`,
      'Z',
    ].join(' ');
  }

  function shortSectionLabel(section) {
    const number = sectionNumber(section.name);
    if (number !== null) return String(number);
    const words = section.name.split(/\s+/).filter(Boolean);
    return words.length > 1
      ? words.map((word) => word[0]).join('').slice(0, 5).toUpperCase()
      : section.name.slice(0, 6).toUpperCase();
  }

  function formatPrice(section) {
    if (section.price === null) return 'No current price';
    return currencyFormatter
      ? currencyFormatter.format(section.price)
      : `${currency} ${Math.round(section.price).toLocaleString('en-US')}`;
  }

  function formatListings(section) {
    if (section.listing_count === null) return 'Not recorded';
    return `${section.listing_count.toLocaleString('en-US')} listing${section.listing_count === 1 ? '' : 's'}`;
  }

  function bindSection(group, section, level) {
    group.addEventListener('pointerenter', (event) => showTooltip(event, section, level));
    group.addEventListener('pointermove', positionTooltip);
    group.addEventListener('pointerleave', hideTooltip);
    group.addEventListener('click', () => selectSection(section, level));
    group.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectSection(section, level);
      }
    });
    sectionElements.set(section.name, { element: group, section, level });
  }

  function sectionGroup(section, level, levelIndex) {
    return createSvgElement('g', {
      class: `nfl-stadium-section level-${Math.min(levelIndex, 7)}`,
      tabindex: 0,
      role: 'button',
      'data-section': section.name,
      'data-level': level.key,
      'aria-label': `${section.name}, ${formatPrice(section)}, ${formatListings(section)}`,
    });
  }

  function addSectionTitle(group, section) {
    const title = createSvgElement('title');
    title.textContent = `${section.name} — ${formatPrice(section)}`;
    group.appendChild(title);
  }

  function drawRink() {
    svg.appendChild(createSvgElement('ellipse', {
      class: 'nfl-map-bowl-shadow',
      cx: 500,
      cy: 356,
      rx: 235,
      ry: 150,
    }));
    const rink = createSvgElement('g', { class: 'nhl-map-rink', 'aria-hidden': 'true' });
    rink.appendChild(createSvgElement('rect', {
      class: 'nhl-map-rink__ice',
      x: 326,
      y: 238,
      width: 348,
      height: 224,
      rx: 67,
    }));
    rink.appendChild(createSvgElement('line', {
      class: 'nhl-map-rink__center',
      x1: 500,
      y1: 240,
      x2: 500,
      y2: 460,
    }));
    [438, 562].forEach((x) => rink.appendChild(createSvgElement('line', {
      class: 'nhl-map-rink__blue',
      x1: x,
      y1: 244,
      x2: x,
      y2: 456,
    })));
    rink.appendChild(createSvgElement('circle', {
      class: 'nhl-map-rink__center-circle',
      cx: 500,
      cy: 350,
      r: 35,
    }));
    [[390, 294], [390, 406], [610, 294], [610, 406]].forEach(([cx, cy]) => {
      rink.appendChild(createSvgElement('circle', {
        class: 'nhl-map-rink__faceoff',
        cx,
        cy,
        r: 26,
      }));
    });
    rink.appendChild(createSvgElement('path', {
      class: 'nhl-map-rink__goal',
      d: 'M 346 322 Q 324 350 346 378',
    }));
    rink.appendChild(createSvgElement('path', {
      class: 'nhl-map-rink__goal',
      d: 'M 654 322 Q 676 350 654 378',
    }));
    const label = createSvgElement('text', {
      class: 'nhl-map-rink__label',
      x: 500,
      y: 356,
    });
    label.textContent = 'ICE';
    rink.appendChild(label);
    svg.appendChild(rink);
  }

  function renderSchematicMap() {
    baseView = { ...SCHEMATIC_VIEW };
    svg.classList.remove('is-provider-map');
    const cx = 500;
    const cy = 350;
    const ringCount = Math.max(levels.length, 1);
    const innerRx = 205;
    const innerRy = 123;
    const outerRx = 475;
    const outerRy = 322;
    const stepRx = (outerRx - innerRx) / ringCount;
    const stepRy = (outerRy - innerRy) / ringCount;

    levels.forEach((level, levelIndex) => {
      const ringInnerRx = innerRx + levelIndex * stepRx + 4;
      const ringInnerRy = innerRy + levelIndex * stepRy + 4;
      const ringOuterRx = innerRx + (levelIndex + 1) * stepRx - 4;
      const ringOuterRy = innerRy + (levelIndex + 1) * stepRy - 4;
      const count = Math.max(level.sections.length, 1);
      const slot = (Math.PI * 2) / count;
      const gap = Math.min(0.035, slot * 0.22);

      level.sections.forEach((section, index) => {
        const start = -Math.PI / 2 + index * slot + gap / 2;
        const end = -Math.PI / 2 + (index + 1) * slot - gap / 2;
        const group = sectionGroup(section, level, levelIndex);
        group.appendChild(createSvgElement('path', {
          d: annularSegmentPath(
            cx,
            cy,
            ringInnerRx,
            ringInnerRy,
            ringOuterRx,
            ringOuterRy,
            start,
            end,
          ),
        }));
        const middle = (start + end) / 2;
        const labelPoint = ellipsePoint(
          cx,
          cy,
          (ringInnerRx + ringOuterRx) / 2,
          (ringInnerRy + ringOuterRy) / 2,
          middle,
        );
        const text = createSvgElement('text', {
          x: labelPoint.x.toFixed(2),
          y: labelPoint.y.toFixed(2),
        });
        text.textContent = shortSectionLabel(section);
        group.appendChild(text);
        addSectionTitle(group, section);
        bindSection(group, section, level);
        svg.appendChild(group);
      });
    });
    drawRink();
  }

  function renderProviderMap() {
    if (!providerGeometry) return false;
    baseView = { ...providerGeometry.view };
    svg.classList.add('is-provider-map');
    let rendered = 0;
    levels.forEach((level, levelIndex) => {
      level.sections.forEach((section) => {
        const shapes = providerGeometry.sections.get(section.name);
        if (!shapes) return;
        const group = sectionGroup(section, level, levelIndex);
        shapes.forEach((shape) => {
          group.appendChild(createSvgElement('path', {
            d: shape.path,
            transform: shape.transform,
            'vector-effect': 'non-scaling-stroke',
          }));
        });
        addSectionTitle(group, section);
        bindSection(group, section, level);
        svg.appendChild(group);
        rendered += 1;
      });
    });
    return rendered > 0;
  }

  function applyLevelFilter() {
    sectionElements.forEach(({ element, level }) => {
      const visible = activeLevel === 'all' || level.key === activeLevel;
      element.classList.toggle('is-dimmed', !visible);
    });
  }

  function renderMap() {
    svg.replaceChildren();
    sectionElements.clear();
    if (!renderProviderMap()) {
      svg.replaceChildren();
      sectionElements.clear();
      renderSchematicMap();
    }
    centerX = baseView.x + baseView.width / 2;
    centerY = baseView.y + baseView.height / 2;
    currentView = { ...baseView };
    applyLevelFilter();
  }

  function renderLevelFilters() {
    if (!levelContainer) return;
    levelContainer.replaceChildren();
    const options = [{ key: 'all', label: 'All levels' }, ...levels];
    options.forEach((option) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = option.label;
      button.dataset.level = option.key;
      button.classList.toggle('is-active', option.key === activeLevel);
      button.addEventListener('click', () => {
        activeLevel = option.key;
        levelContainer.querySelectorAll('button').forEach((candidate) => {
          candidate.classList.toggle('is-active', candidate.dataset.level === activeLevel);
        });
        applyLevelFilter();
      });
      levelContainer.appendChild(button);
    });
  }

  function showTooltip(event, section, level) {
    if (!tooltip) return;
    tooltip.innerHTML = '';
    const title = document.createElement('strong');
    title.textContent = section.name;
    const detail = document.createElement('span');
    detail.textContent = `${level.label} · ${formatPrice(section)} · ${formatListings(section)}`;
    tooltip.append(title, detail);
    tooltip.hidden = false;
    positionTooltip(event);
  }

  function positionTooltip(event) {
    if (!tooltip || tooltip.hidden || !stage) return;
    const bounds = stage.getBoundingClientRect();
    const width = tooltip.offsetWidth || 220;
    const height = tooltip.offsetHeight || 70;
    let left = event.clientX - bounds.left + 12;
    let top = event.clientY - bounds.top + 12;
    if (left + width > bounds.width - 8) left -= width + 24;
    if (top + height > bounds.height - 8) top -= height + 24;
    tooltip.style.left = `${Math.max(8, left)}px`;
    tooltip.style.top = `${Math.max(8, top)}px`;
  }

  function hideTooltip() {
    if (tooltip) tooltip.hidden = true;
  }

  function selectSection(section, level) {
    selectedSection = section.name;
    sectionElements.forEach(({ element }, name) => {
      element.classList.toggle('is-selected', name === selectedSection);
      element.classList.remove('is-match');
    });
    if (nameNode) nameNode.textContent = section.name;
    if (levelNode) levelNode.textContent = level.label;
    if (priceNode) priceNode.textContent = section.price === null ? '—' : formatPrice(section);
    if (listingsNode) {
      listingsNode.textContent = section.listing_count === null
        ? '—'
        : section.listing_count.toLocaleString('en-US');
    }
    if (historyLink) {
      const query = new URLSearchParams({
        team: mapData.team,
        game: String(mapData.game),
        section: section.name,
      });
      historyLink.href = `${mapData.graph_url || '/nhl/graph'}?${query.toString()}`;
      historyLink.setAttribute('aria-disabled', 'false');
    }
    const url = new URL(window.location.href);
    url.searchParams.set('section', section.name);
    window.history.replaceState({}, '', url);
  }

  function clearMatches() {
    sectionElements.forEach(({ element }) => element.classList.remove('is-match'));
  }

  function runSearch() {
    if (!searchInput) return;
    const query = normalize(searchInput.value);
    clearMatches();
    if (!query) return;
    const exactNumber = /^\d+$/.test(query) ? Number(query) : null;
    const matches = sections.filter((section) => {
      const normalizedName = normalize(section.name);
      return (exactNumber !== null && sectionNumber(section.name) === exactNumber)
        || normalizedName.includes(query);
    });
    matches.forEach((section) => {
      const entry = sectionElements.get(section.name);
      if (entry) entry.element.classList.add('is-match');
    });
    if (matches.length) {
      activeLevel = 'all';
      if (levelContainer) {
        levelContainer.querySelectorAll('button').forEach((candidate) => {
          candidate.classList.toggle('is-active', candidate.dataset.level === 'all');
        });
      }
      applyLevelFilter();
      const section = matches[0];
      const entry = sectionElements.get(section.name);
      selectSection(section, entry ? entry.level : sectionLevel(section));
      if (entry) entry.element.focus({ preventScroll: true });
    }
  }

  function clamp(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, value));
  }

  function applyViewBox() {
    const width = baseView.width / zoom;
    const height = baseView.height / zoom;
    centerX = clamp(centerX, baseView.x + width / 2, baseView.x + baseView.width - width / 2);
    centerY = clamp(centerY, baseView.y + height / 2, baseView.y + baseView.height - height / 2);
    currentView = {
      x: centerX - width / 2,
      y: centerY - height / 2,
      width,
      height,
    };
    svg.setAttribute(
      'viewBox',
      `${currentView.x.toFixed(2)} ${currentView.y.toFixed(2)} ${currentView.width.toFixed(2)} ${currentView.height.toFixed(2)}`,
    );
  }

  function setZoom(nextZoom, anchorEvent = null) {
    const bounded = clamp(nextZoom, 1, 4.2);
    if (bounded === zoom) return;
    if (anchorEvent && stage) {
      const bounds = stage.getBoundingClientRect();
      const relativeX = clamp((anchorEvent.clientX - bounds.left) / bounds.width, 0, 1);
      const relativeY = clamp((anchorEvent.clientY - bounds.top) / bounds.height, 0, 1);
      const anchorX = currentView.x + relativeX * currentView.width;
      const anchorY = currentView.y + relativeY * currentView.height;
      const nextWidth = baseView.width / bounded;
      const nextHeight = baseView.height / bounded;
      centerX = anchorX - (relativeX - 0.5) * nextWidth;
      centerY = anchorY - (relativeY - 0.5) * nextHeight;
    }
    zoom = bounded;
    applyViewBox();
  }

  function resetView() {
    zoom = 1;
    centerX = baseView.x + baseView.width / 2;
    centerY = baseView.y + baseView.height / 2;
    applyViewBox();
  }

  if (zoomInButton) zoomInButton.addEventListener('click', () => setZoom(zoom * 1.25));
  if (zoomOutButton) zoomOutButton.addEventListener('click', () => setZoom(zoom / 1.25));
  if (resetButton) resetButton.addEventListener('click', resetView);
  if (searchButton) searchButton.addEventListener('click', runSearch);
  if (searchInput) {
    searchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        runSearch();
      }
    });
  }

  if (stage) {
    stage.addEventListener('wheel', (event) => {
      event.preventDefault();
      setZoom(zoom * (event.deltaY < 0 ? 1.16 : 0.86), event);
    }, { passive: false });
    stage.addEventListener('pointerdown', (event) => {
      if (event.target.closest && event.target.closest('.nfl-stadium-section')) return;
      dragState = {
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        centerX,
        centerY,
      };
      stage.setPointerCapture(event.pointerId);
      stage.classList.add('is-dragging');
    });
    stage.addEventListener('pointermove', (event) => {
      if (!dragState || dragState.pointerId !== event.pointerId) return;
      const bounds = stage.getBoundingClientRect();
      centerX = dragState.centerX
        - (event.clientX - dragState.x) * currentView.width / bounds.width;
      centerY = dragState.centerY
        - (event.clientY - dragState.y) * currentView.height / bounds.height;
      applyViewBox();
    });
    const endDrag = (event) => {
      if (!dragState || dragState.pointerId !== event.pointerId) return;
      dragState = null;
      stage.classList.remove('is-dragging');
      try {
        stage.releasePointerCapture(event.pointerId);
      } catch (error) {
        // Pointer may already be released.
      }
    };
    stage.addEventListener('pointerup', endDrag);
    stage.addEventListener('pointercancel', endDrag);
  }

  renderMap();
  renderLevelFilters();
  resetView();

  const requestedSection = String(mapData.selected_section || '').trim();
  if (requestedSection) {
    const section = sections.find((candidate) => candidate.name === requestedSection);
    const entry = sectionElements.get(requestedSection);
    if (section) selectSection(section, entry ? entry.level : sectionLevel(section));
  }
})();
