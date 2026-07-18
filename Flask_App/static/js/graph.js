const SVG_NAMESPACE = 'http://www.w3.org/2000/svg';

function addSvgElement(parent, name, attributes = {}, text = '') {
  const element = document.createElementNS(SVG_NAMESPACE, name);
  Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
  if (text) element.textContent = text;
  parent.appendChild(element);
  return element;
}

function formatHours(hours) {
  if (hours < 1) return `${Math.round(hours * 60)} min before game`;
  const precision = hours < 10 ? 1 : 0;
  return `${hours.toFixed(precision)} hours before game`;
}

document.querySelectorAll('.interactive-chart').forEach((chart) => {
  const dataElement = chart.querySelector('.interactive-chart__data');
  const svg = chart.querySelector('.interactive-chart__svg');
  const tooltip = chart.querySelector('.interactive-chart__tooltip');
  const mode = chart.dataset.displayMode;
  const rawData = JSON.parse(dataElement.textContent);
  const points = rawData.x
    .map((x, index) => ({ x: Number(x), y: Number(rawData.y[index]) }))
    .filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y))
    .sort((a, b) => b.x - a.x);

  if (!points.length) return;

  const width = 900;
  const height = 520;
  const margin = { top: 28, right: 28, bottom: 70, left: 82 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const xValues = points.map((point) => point.x);
  const yValues = points.map((point) => point.y);
  const xMin = Math.min(...xValues);
  const xMax = Math.max(...xValues);
  const rawYMin = Math.min(...yValues);
  const rawYMax = Math.max(...yValues);
  const yPadding = Math.max((rawYMax - rawYMin) * 0.12, mode === 'money' ? 1 : 2);
  const yMin = Math.max(0, rawYMin - yPadding);
  const yMax = rawYMax + yPadding;
  const scaleX = (value) => margin.left + ((xMax - value) / Math.max(xMax - xMin, 1)) * plotWidth;
  const scaleY = (value) => margin.top + ((yMax - value) / Math.max(yMax - yMin, 1)) * plotHeight;
  const formatValue = (value) => mode === 'money'
    ? `$${value.toLocaleString(undefined, { minimumFractionDigits: 0, maximumFractionDigits: 2 })}`
    : `${Math.round(value)}%`;

  const gridGroup = addSvgElement(svg, 'g');
  for (let index = 0; index <= 4; index += 1) {
    const ratio = index / 4;
    const yPosition = margin.top + ratio * plotHeight;
    const value = yMax - ratio * (yMax - yMin);
    addSvgElement(gridGroup, 'line', { x1: margin.left, y1: yPosition, x2: width - margin.right, y2: yPosition, class: 'interactive-chart__grid' });
    addSvgElement(gridGroup, 'text', { x: margin.left - 13, y: yPosition + 4, 'text-anchor': 'end', class: 'interactive-chart__tick' }, formatValue(value));
  }

  for (let index = 0; index <= 6; index += 1) {
    const ratio = index / 6;
    const xPosition = margin.left + ratio * plotWidth;
    const value = xMax - ratio * (xMax - xMin);
    addSvgElement(gridGroup, 'text', { x: xPosition, y: height - margin.bottom + 25, 'text-anchor': 'middle', class: 'interactive-chart__tick' }, `${Math.round(value)}h`);
  }

  addSvgElement(svg, 'text', { x: margin.left + plotWidth / 2, y: height - 13, 'text-anchor': 'middle', class: 'interactive-chart__axis-label' }, 'Hours until event');
  addSvgElement(svg, 'text', { x: 18, y: margin.top + plotHeight / 2, 'text-anchor': 'middle', transform: `rotate(-90 18 ${margin.top + plotHeight / 2})`, class: 'interactive-chart__axis-label' }, mode === 'money' ? 'Average listed price' : 'Relative price');

  const plottedPoints = points.map((point) => ({ ...point, plotX: scaleX(point.x), plotY: scaleY(point.y) }));
  const linePath = plottedPoints.map((point, index) => `${index ? 'L' : 'M'} ${point.plotX} ${point.plotY}`).join(' ');
  const areaPath = `${linePath} L ${plottedPoints.at(-1).plotX} ${margin.top + plotHeight} L ${plottedPoints[0].plotX} ${margin.top + plotHeight} Z`;
  addSvgElement(svg, 'path', { d: areaPath, class: 'interactive-chart__area' });
  addSvgElement(svg, 'path', { d: linePath, class: 'interactive-chart__line' });
  const guide = addSvgElement(svg, 'line', { y1: margin.top, y2: margin.top + plotHeight, class: 'interactive-chart__guide', visibility: 'hidden' });

  let activeCircle = null;
  const hideTooltip = () => {
    tooltip.hidden = true;
    guide.setAttribute('visibility', 'hidden');
    if (activeCircle) activeCircle.classList.remove('is-active');
    activeCircle = null;
  };

  const showTooltip = (point, circle, clientX, clientY) => {
    if (activeCircle) activeCircle.classList.remove('is-active');
    activeCircle = circle;
    circle.classList.add('is-active');
    guide.setAttribute('x1', point.plotX);
    guide.setAttribute('x2', point.plotX);
    guide.setAttribute('visibility', 'visible');
    tooltip.innerHTML = `<strong>${formatValue(point.y)}</strong><span>${formatHours(point.x)}</span>`;
    tooltip.hidden = false;
    const chartBounds = chart.getBoundingClientRect();
    tooltip.style.left = `${Math.min(Math.max(clientX - chartBounds.left, 74), chartBounds.width - 74)}px`;
    tooltip.style.top = `${Math.max(clientY - chartBounds.top, 62)}px`;
  };

  const circles = plottedPoints.map((point) => {
    const circle = addSvgElement(svg, 'circle', { cx: point.plotX, cy: point.plotY, r: 4.5, tabindex: 0, class: 'interactive-chart__point', 'aria-label': `${formatValue(point.y)}, ${formatHours(point.x)}` });
    circle.addEventListener('focus', () => {
      const bounds = circle.getBoundingClientRect();
      showTooltip(point, circle, bounds.left + bounds.width / 2, bounds.top);
    });
    circle.addEventListener('blur', hideTooltip);
    return circle;
  });

  svg.addEventListener('pointermove', (event) => {
    const svgBounds = svg.getBoundingClientRect();
    const pointerX = ((event.clientX - svgBounds.left) / svgBounds.width) * width;
    const closestIndex = plottedPoints.reduce((bestIndex, point, index) => (
      Math.abs(point.plotX - pointerX) < Math.abs(plottedPoints[bestIndex].plotX - pointerX) ? index : bestIndex
    ), 0);
    showTooltip(plottedPoints[closestIndex], circles[closestIndex], event.clientX, event.clientY);
  });
  svg.addEventListener('pointerleave', hideTooltip);
});
