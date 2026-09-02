(() => {
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function readJson(id) {
    const node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent || '{}');
    } catch (error) {
      console.error(`Could not read ${id}.`, error);
      return null;
    }
  }

  function svgElement(name, attributes = {}) {
    const element = document.createElementNS(SVG_NS, name);
    Object.entries(attributes).forEach(([key, value]) => {
      if (value !== null && value !== undefined && value !== '') {
        element.setAttribute(key, String(value));
      }
    });
    return element;
  }

  function normalize(value) {
    return String(value || '')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, ' ')
      .trim();
  }

  function sectionNumber(name) {
    const matches = String(name || '').match(/\d+/g);
    return matches ? Number(matches[matches.length - 1]) : null;
  }

  function shortSectionLabel(section) {
    const number = sectionNumber(section.name);
    if (number !== null) return String(number);
    const words = String(section.name || '').split(/\s+/).filter(Boolean);
    return words.length > 1
      ? words.map((word) => word[0]).join('').slice(0, 5).toUpperCase()
      : String(section.name || '').slice(0, 6).toUpperCase();
  }

  function sectionLevel(section) {
    const normalized = normalize(section.name);
    const number = sectionNumber(section.name);

    if (/\b(field|floor|pit)\b/.test(normalized) && number === null) {
      return { key: 'field', order: 50 };
    }
    if (number !== null) {
      const hundred = number < 100 ? 100 : Math.floor(number / 100) * 100;
      return { key: String(hundred), order: hundred };
    }
    if (/\b(lower|club|loge)\b/.test(normalized)) {
      return { key: 'lower', order: 180 };
    }
    if (/\b(mezzanine|middle)\b/.test(normalized)) {
      return { key: 'middle', order: 480 };
    }
    if (/\b(upper|terrace|balcony|gridiron)\b/.test(normalized)) {
      return { key: 'upper', order: 760 };
    }
    if (/\bsuite/.test(normalized)) {
      return { key: 'suite', order: 900 };
    }
    return { key: 'other', order: 850 };
  }

  function compareSections(a, b) {
    const aNumber = sectionNumber(a.name);
    const bNumber = sectionNumber(b.name);
    if (aNumber !== null && bNumber !== null && aNumber !== bNumber) {
      return aNumber - bNumber;
    }
    if (aNumber !== null && bNumber === null) return -1;
    if (aNumber === null && bNumber !== null) return 1;
    return String(a.name).localeCompare(String(b.name), undefined, { numeric: true });
  }

  function ellipsePoint(cx, cy, rx, ry, angle) {
    return {
      x: cx + rx * Math.cos(angle),
      y: cy + ry * Math.sin(angle),
    };
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

  function addTitle(group, section, selected) {
    const title = svgElement('title');
    const price = section.average_price_label || 'No average price';
    const games = Number(section.game_count || 0);
    title.textContent = `${section.name} — ${price} across ${games} game${games === 1 ? '' : 's'}${selected ? ' — selected' : ''}`;
    group.appendChild(title);
  }

  function addSurface(svg, sport) {
    const surface = svgElement('g', { 'aria-hidden': 'true' });

    if (sport === 'mlb') {
      surface.appendChild(svgElement('path', {
        class: 'venue-location-surface',
        d: 'M 500 238 L 632 350 L 500 470 L 368 350 Z',
      }));
      surface.appendChild(svgElement('path', {
        class: 'venue-location-surface-line',
        d: 'M 500 238 L 500 350 L 632 350 M 500 350 L 500 470 L 368 350',
      }));
      surface.appendChild(svgElement('circle', {
        class: 'venue-location-surface-line',
        cx: 500,
        cy: 350,
        r: 22,
      }));
    } else if (sport === 'nhl') {
      surface.appendChild(svgElement('rect', {
        class: 'venue-location-surface',
        x: 340,
        y: 245,
        width: 320,
        height: 210,
        rx: 92,
      }));
      surface.appendChild(svgElement('line', {
        class: 'venue-location-surface-line',
        x1: 500,
        y1: 245,
        x2: 500,
        y2: 455,
      }));
      surface.appendChild(svgElement('circle', {
        class: 'venue-location-surface-line',
        cx: 500,
        cy: 350,
        r: 42,
      }));
      surface.appendChild(svgElement('line', {
        class: 'venue-location-surface-line',
        x1: 405,
        y1: 245,
        x2: 405,
        y2: 455,
      }));
      surface.appendChild(svgElement('line', {
        class: 'venue-location-surface-line',
        x1: 595,
        y1: 245,
        x2: 595,
        y2: 455,
      }));
    } else {
      surface.appendChild(svgElement('rect', {
        class: 'venue-location-surface',
        x: 340,
        y: 250,
        width: 320,
        height: 200,
        rx: 15,
      }));
      for (let index = 1; index < 10; index += 1) {
        const x = 340 + (320 / 10) * index;
        surface.appendChild(svgElement('line', {
          class: 'venue-location-surface-line',
          x1: x,
          y1: 250,
          x2: x,
          y2: 450,
        }));
      }
    }

    const label = svgElement('text', {
      class: 'venue-location-surface-text',
      x: 500,
      y: 355,
    });
    label.textContent = sport === 'mlb' ? 'FIELD' : sport === 'nhl' ? 'ICE' : 'FIELD';
    surface.appendChild(label);
    svg.appendChild(surface);
  }

  function renderSchematicMap(svg, data, sections, selectedNormalized) {
    svg.setAttribute('viewBox', '0 0 1000 700');
    const grouped = new Map();
    sections.forEach((section) => {
      const level = sectionLevel(section);
      if (!grouped.has(level.key)) {
        grouped.set(level.key, { ...level, sections: [] });
      }
      grouped.get(level.key).sections.push(section);
    });

    const levels = Array.from(grouped.values())
      .map((level) => ({ ...level, sections: level.sections.sort(compareSections) }))
      .sort((a, b) => a.order - b.order);

    const cx = 500;
    const cy = 350;
    const ringCount = Math.max(levels.length, 1);
    const innerRx = 205;
    const innerRy = 122;
    const outerRx = 468;
    const outerRy = 316;
    const stepRx = (outerRx - innerRx) / ringCount;
    const stepRy = (outerRy - innerRy) / ringCount;

    levels.forEach((level, levelIndex) => {
      const ringInnerRx = innerRx + levelIndex * stepRx + 4;
      const ringInnerRy = innerRy + levelIndex * stepRy + 4;
      const ringOuterRx = innerRx + (levelIndex + 1) * stepRx - 4;
      const ringOuterRy = innerRy + (levelIndex + 1) * stepRy - 4;
      const count = Math.max(level.sections.length, 1);
      const slot = (Math.PI * 2) / count;
      const gap = Math.min(0.035, slot * 0.2);

      level.sections.forEach((section, index) => {
        const start = -Math.PI / 2 + index * slot + gap / 2;
        const end = -Math.PI / 2 + (index + 1) * slot - gap / 2;
        const isSelected = normalize(section.name) === selectedNormalized;
        const group = svgElement('g', {
          class: `venue-location-section${isSelected ? ' is-selected' : ''}`,
          'data-section': section.name,
        });
        group.appendChild(svgElement('path', {
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
        addTitle(group, section, isSelected);

        if (isSelected) {
          const middle = (start + end) / 2;
          const point = ellipsePoint(
            cx,
            cy,
            (ringInnerRx + ringOuterRx) / 2,
            (ringInnerRy + ringOuterRy) / 2,
            middle,
          );
          const label = svgElement('text', {
            class: 'venue-location-label',
            x: point.x.toFixed(2),
            y: point.y.toFixed(2),
          });
          label.textContent = shortSectionLabel(section);
          group.appendChild(label);
        }
        svg.appendChild(group);
      });
    });

    addSurface(svg, data.sport || 'nfl');
  }

  function providerGeometryIsUsable(data, sections, selectedNormalized) {
    const geometry = data.geometry;
    if (!geometry || data.geometry_mode !== 'provider') return false;
    const box = Array.isArray(geometry.view_box) ? geometry.view_box.map(Number) : [];
    if (box.length !== 4 || !box.every(Number.isFinite) || box[2] <= 0 || box[3] <= 0) {
      return false;
    }
    const known = new Set(sections.map((section) => normalize(section.name)));
    return (Array.isArray(geometry.sections) ? geometry.sections : []).some((row) => {
      const name = normalize(row && row.name);
      return name === selectedNormalized
        && known.has(name)
        && Array.isArray(row.shapes)
        && row.shapes.some((shape) => String(shape && shape.path || '').trim());
    });
  }

  function renderProviderMap(svg, data, sections, selectedNormalized) {
    const geometry = data.geometry;
    const box = geometry.view_box.map(Number);
    svg.setAttribute('viewBox', box.join(' '));
    const sectionsByName = new Map(
      sections.map((section) => [normalize(section.name), section]),
    );
    let selectedGroup = null;

    (Array.isArray(geometry.sections) ? geometry.sections : []).forEach((row) => {
      const normalizedName = normalize(row && row.name);
      const section = sectionsByName.get(normalizedName);
      if (!section || !Array.isArray(row.shapes)) return;
      const isSelected = normalizedName === selectedNormalized;
      const group = svgElement('g', {
        class: `venue-location-section${isSelected ? ' is-selected' : ''}`,
        'data-section': section.name,
      });
      row.shapes.forEach((shape) => {
        const path = String(shape && shape.path || '').trim();
        if (!path) return;
        group.appendChild(svgElement('path', {
          d: path,
          transform: String(shape && shape.transform || '').trim(),
        }));
      });
      if (!group.querySelector('path')) return;
      addTitle(group, section, isSelected);
      svg.appendChild(group);
      if (isSelected) selectedGroup = group;
    });

    if (selectedGroup) {
      requestAnimationFrame(() => {
        try {
          const bounds = selectedGroup.getBBox();
          const section = sectionsByName.get(selectedNormalized);
          const label = svgElement('text', {
            class: 'venue-location-label',
            x: bounds.x + bounds.width / 2,
            y: bounds.y + bounds.height / 2,
          });
          label.textContent = shortSectionLabel(section || { name: data.selected_section });
          selectedGroup.appendChild(label);
        } catch (error) {
          // Some browsers may not expose a bounding box before the SVG paints.
        }
      });
    }
  }

  function renderLocationMap() {
    const svg = document.querySelector('[data-section-location-map]');
    const data = readJson('venue-section-map-data');
    if (!svg || !data) return;

    const sections = Array.isArray(data.sections)
      ? data.sections
        .map((section) => ({
          name: String(section && section.name || '').trim(),
          average_price: Number.isFinite(Number(section && section.average_price))
            ? Number(section.average_price)
            : null,
          average_price_label: String(section && section.average_price_label || '—'),
          game_count: Number(section && section.game_count || 0),
        }))
        .filter((section) => section.name)
      : [];
    if (!sections.length) return;

    const selectedNormalized = normalize(data.selected_section);
    svg.replaceChildren();
    if (providerGeometryIsUsable(data, sections, selectedNormalized)) {
      renderProviderMap(svg, data, sections, selectedNormalized);
    } else {
      renderSchematicMap(svg, data, sections, selectedNormalized);
    }
  }

  function niceStep(range, targetTicks = 5) {
    if (!Number.isFinite(range) || range <= 0) return 1;
    const rough = range / targetTicks;
    const magnitude = 10 ** Math.floor(Math.log10(rough));
    const normalized = rough / magnitude;
    const factor = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10;
    return factor * magnitude;
  }

  function currencyFormatter(currency) {
    try {
      return new Intl.NumberFormat(undefined, {
        style: 'currency',
        currency: String(currency || 'USD').toUpperCase(),
        maximumFractionDigits: 0,
      });
    } catch (error) {
      return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
    }
  }

  function showChartTooltip(tooltip, container, circle, point) {
    if (!tooltip || !container || !circle) return;
    tooltip.replaceChildren();
    const price = document.createElement('strong');
    price.textContent = point.average_price_label;
    const label = document.createElement('span');
    label.textContent = point.label;
    const games = document.createElement('small');
    games.textContent = `${point.game_count} game${point.game_count === 1 ? '' : 's'} in this window`;
    tooltip.append(price, label, games);
    tooltip.hidden = false;

    const containerBounds = container.getBoundingClientRect();
    const circleBounds = circle.getBoundingClientRect();
    const width = tooltip.offsetWidth || 180;
    const height = tooltip.offsetHeight || 74;
    let left = circleBounds.left - containerBounds.left + circleBounds.width / 2 - width / 2;
    let top = circleBounds.top - containerBounds.top - height - 12;
    left = Math.min(containerBounds.width - width - 8, Math.max(8, left));
    if (top < 8) top = circleBounds.bottom - containerBounds.top + 10;
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${top}px`;
  }

  function renderTimelineChart() {
    const svg = document.querySelector('[data-section-timeline-svg]');
    const container = document.querySelector('[data-section-timeline-chart]');
    const tooltip = document.querySelector('[data-section-chart-tooltip]');
    const data = readJson('venue-section-timeline-data');
    if (!svg || !container || !data || !Array.isArray(data.points) || !data.points.length) {
      return;
    }

    const points = data.points
      .map((point) => ({
        ...point,
        slot: Number(point.slot),
        average_price: Number(point.average_price),
        game_count: Number(point.game_count || 0),
      }))
      .filter((point) => Number.isFinite(point.slot) && Number.isFinite(point.average_price))
      .sort((a, b) => a.slot - b.slot);
    if (!points.length) return;

    const width = 920;
    const height = 430;
    const margin = { top: 46, right: 34, bottom: 76, left: 86 };
    const chartWidth = width - margin.left - margin.right;
    const chartHeight = height - margin.top - margin.bottom;
    const slotCount = Math.max(Number(data.slot_count) || points.length, 1);
    const formatCurrency = currencyFormatter(data.currency);

    const values = points.map((point) => point.average_price);
    let minValue = Math.min(...values);
    let maxValue = Math.max(...values);
    if (Math.abs(maxValue - minValue) < 0.01) {
      const padding = Math.max(5, maxValue * 0.08);
      minValue = Math.max(0, minValue - padding);
      maxValue += padding;
    } else {
      const padding = (maxValue - minValue) * 0.14;
      minValue = Math.max(0, minValue - padding);
      maxValue += padding;
    }

    const step = niceStep(maxValue - minValue, 5);
    const yMin = Math.max(0, Math.floor(minValue / step) * step);
    const yMax = Math.ceil(maxValue / step) * step || step;
    const yRange = Math.max(yMax - yMin, step);
    const xForSlot = (slot) => margin.left + (
      slotCount <= 1 ? chartWidth / 2 : (slot / (slotCount - 1)) * chartWidth
    );
    const yForValue = (value) => margin.top + ((yMax - value) / yRange) * chartHeight;

    svg.replaceChildren();
    const defs = svgElement('defs');
    const gradient = svgElement('linearGradient', {
      id: 'venue-chart-area-gradient',
      x1: '0',
      y1: '0',
      x2: '0',
      y2: '1',
    });
    gradient.appendChild(svgElement('stop', { class: 'venue-chart-gradient-start', offset: '0%' }));
    gradient.appendChild(svgElement('stop', { class: 'venue-chart-gradient-end', offset: '100%' }));
    defs.appendChild(gradient);
    svg.appendChild(defs);

    const tickCount = Math.round(yRange / step);
    for (let index = 0; index <= tickCount; index += 1) {
      const value = yMin + index * step;
      const y = yForValue(value);
      svg.appendChild(svgElement('line', {
        class: 'venue-chart-grid',
        x1: margin.left,
        y1: y,
        x2: width - margin.right,
        y2: y,
      }));
      const label = svgElement('text', {
        class: 'venue-chart-axis-label venue-chart-axis-label--y',
        x: margin.left - 13,
        y,
      });
      label.textContent = formatCurrency.format(value);
      svg.appendChild(label);
    }

    points.forEach((point) => {
      const x = xForSlot(point.slot);
      svg.appendChild(svgElement('line', {
        class: 'venue-chart-grid',
        x1: x,
        y1: margin.top,
        x2: x,
        y2: height - margin.bottom,
      }));
      const label = svgElement('text', {
        class: 'venue-chart-axis-label venue-chart-axis-label--x',
        x,
        y: height - margin.bottom + 28,
      });
      label.textContent = point.short_label;
      svg.appendChild(label);
    });

    const coordinates = points.map((point) => ({
      point,
      x: xForSlot(point.slot),
      y: yForValue(point.average_price),
    }));
    const baseline = height - margin.bottom;
    const linePath = coordinates
      .map((coordinate, index) => `${index === 0 ? 'M' : 'L'} ${coordinate.x.toFixed(2)} ${coordinate.y.toFixed(2)}`)
      .join(' ');
    const areaPath = coordinates.length === 1
      ? `M ${coordinates[0].x.toFixed(2)} ${baseline} L ${coordinates[0].x.toFixed(2)} ${coordinates[0].y.toFixed(2)} L ${coordinates[0].x.toFixed(2)} ${baseline} Z`
      : `${linePath} L ${coordinates[coordinates.length - 1].x.toFixed(2)} ${baseline} L ${coordinates[0].x.toFixed(2)} ${baseline} Z`;

    svg.appendChild(svgElement('path', { class: 'venue-chart-area', d: areaPath }));
    svg.appendChild(svgElement('path', { class: 'venue-chart-line', d: linePath }));

    coordinates.forEach(({ point, x, y }) => {
      const label = svgElement('text', {
        class: 'venue-chart-point-label',
        x,
        y: Math.max(margin.top + 12, y - 18),
      });
      label.textContent = point.average_price_label;
      svg.appendChild(label);

      const circle = svgElement('circle', {
        class: 'venue-chart-point',
        cx: x,
        cy: y,
        r: 7,
        tabindex: 0,
        role: 'img',
        'aria-label': `${point.average_price_label}, ${point.label}, ${point.game_count} game${point.game_count === 1 ? '' : 's'}`,
      });
      const openTooltip = () => showChartTooltip(tooltip, container, circle, point);
      circle.addEventListener('pointerenter', openTooltip);
      circle.addEventListener('focus', openTooltip);
      circle.addEventListener('pointerleave', () => { if (tooltip) tooltip.hidden = true; });
      circle.addEventListener('blur', () => { if (tooltip) tooltip.hidden = true; });
      svg.appendChild(circle);
    });
  }

  renderLocationMap();
  renderTimelineChart();
})();
