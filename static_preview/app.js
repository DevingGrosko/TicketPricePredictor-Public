'use strict';
// The browser fetches ONLY static, content-addressed files on this origin.
// There is no API URL, database client, credential, or server computation here.
const $ = id => document.getElementById(id);
const state = {sport: 'mlb', index: null, report: null, game: null, series: null, view: 'report', version: 0};
const cache = new Map();
let manifest;
function status(message = '', error = false) { $('status').textContent = message; $('status').classList.toggle('error', error); }
function textNode(tag, text, cls) { const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n; }
function moment(value) { return value ? new Date(value).toLocaleString(undefined, {dateStyle:'medium', timeStyle:'short'}) : 'No recorded captures'; }
function money(value, currency = 'USD') { return Number.isFinite(value) ? new Intl.NumberFormat(undefined, {style:'currency', currency, maximumFractionDigits:2}).format(value) : 'Not enough data'; }
function roundEven(x) { const floor = Math.floor(x), fraction = x - floor; return fraction === 0.5 ? floor + (floor % 2) : Math.round(x); }
async function data(file) {
  if (!/^data\/(?:series|game|report|index)-[0-9a-f]{64}\.json$/.test(file)) throw new Error('Invalid published data path.');
  if (cache.has(file)) return cache.get(file);
  const response = await fetch(file);
  if (!response.ok) throw new Error('Published data is unavailable. Refresh to load the latest complete build.');
  const bytes = await response.arrayBuffer();
  const expected = file.match(/-([0-9a-f]{64})\.json$/)[1];
  const actual = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2,'0')).join('');
  if (actual !== expected) throw new Error('Data integrity check failed. Refresh the preview.');
  const result = JSON.parse(new TextDecoder().decode(bytes));
  cache.set(file, result); if (cache.size > 24) cache.delete(cache.keys().next().value);
  return result;
}
function options(select, rows, value, label) {
  select.replaceChildren(...rows.map(row => { const option = textNode('option', label(row)); option.value = value(row); return option; }));
  select.disabled = !rows.length;
}
async function loadSport(sport, reportId) {
  const version = ++state.version;
  state.sport = sport; state.index = null; state.report = state.game = state.series = null;
  $('detail').hidden = true; $('directory').hidden = false; $('teams').replaceChildren(); $('team-search').value = '';
  document.body.dataset.sport = sport;
  document.querySelectorAll('[data-sport]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.sport === sport)));
  $('sport-label').textContent = sport.toUpperCase() + ' · PRICE INTELLIGENCE';
  status('Loading the published ' + sport.toUpperCase() + ' directory…');
  try {
    const item = manifest.sports.find(x => x.sport === sport);
    const index = await data(item.file); if (version !== state.version) return;
    state.index = index;
    $('data-time').textContent = 'Latest source capture: ' + moment(item.captured_through);
    $('build-time').textContent = 'Publication built: ' + moment(manifest.generated_at);
    $('totals').textContent = `${index.reports.length} team / venue reports · ${index.games.length} eligible games`;
    directory(); status('');
    if (reportId && index.reports.some(r => r.id === reportId)) await openReport(reportId);
    else history.replaceState(null, '', '#sport=' + sport);
  } catch (error) { if (version === state.version) status(error.message, true); }
}
function directory() {
  if (!state.index) return;
  const query = $('team-search').value.trim().toLowerCase();
  const rows = state.index.reports.filter(r => [r.team,r.venue,r.season].join(' ').toLowerCase().includes(query));
  $('teams').replaceChildren(...rows.map(r => {
    const button = textNode('button', '', 'team-card'); button.type = 'button'; button.dataset.report = r.id;
    button.append(textNode('span', r.venue, 'venue'), textNode('strong', r.team), textNode('small', `${r.game_count} games · ${r.season} · ${r.currency}  →`));
    button.addEventListener('click', () => openReport(r.id)); return button;
  }));
  $('no-teams').hidden = rows.length > 0;
}
async function openReport(id) {
  const version = ++state.version;
  status('Loading the precomputed report…');
  try {
    const entry = state.index.reports.find(r => r.id === id); if (!entry) throw new Error('Unknown report.');
    const report = await data(entry.file); if (version !== state.version) return;
    state.report = report; state.game = state.series = null;
    $('directory').hidden = true; $('detail').hidden = false;
    $('detail-heading').textContent = report.team;
    $('detail-venue').textContent = report.venue;
    $('detail-meta').textContent = `${report.season} · ${report.game_count} eligible games · ${report.currency} · Source through ${moment(report.captured_through)}`;
    function rankings(id, keys, field) {
      const list = $(id); list.replaceChildren();
      if (!keys.length) { list.append(textNode('li','Not enough comparable completed-game evidence.')); return; }
      keys.forEach(key => {
        const row = report.sections.find(s => s.section_key === key); if (!row) return;
        const li = document.createElement('li'), button = textNode('button','');
        button.append(textNode('span',row.name),textNode('span', field === 'price' ? money(row.ranking_price, report.currency) : Number(row.ranking_drop_percent).toFixed(1) + '%'));
        button.addEventListener('click', () => { $('report-section').value = row.section_key; changeView('report'); });
        li.append(button); list.append(li);
      });
    }
    rankings('cheap', report.cheapest, 'price'); rankings('drops', report.drops, 'drop');
    options($('report-section'), report.sections, r => r.section_key, r => r.name);
    options($('game'), report.games, g => g.id, g => g.title + ' · ' + moment(g.event_at));
    $('game-section').replaceChildren(); $('game-section').disabled = true;
    $('display').value = 'money';
    history.replaceState(null, '', '#sport=' + state.sport + '&report=' + id);
    changeView('report'); status('');
    $('detail').scrollIntoView({block:'start',behavior:'instant'});
  } catch (error) { if (version === state.version) status(error.message, true); }
}
function changeView(view) {
  state.view = view;
  document.querySelectorAll('[data-view]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === view)));
  $('report-controls').hidden = view !== 'report'; $('game-controls').hidden = view !== 'game';
  if (view === 'game' && !state.game) loadGame(); else drawCurrent();
}
async function loadGame() {
  const version = ++state.version;
  state.game = state.series = null; $('game-section').disabled = true;
  clearChart('Loading the published game…'); status('Loading the published game…');
  try {
    const entry = state.report.games.find(g => g.id === $('game').value);
    if (!entry) { clearChart('No game data.'); status(''); return; }
    const game = await data(entry.file); if (version !== state.version) return;
    state.game = game;
    options($('game-section'), game.sections, s => s.key, s => s.name + ' · ' + s.points + ' points');
    await loadSeries();
  } catch(error) { if (version === state.version) status(error.message, true); }
}
async function loadSeries() {
  const version = ++state.version; state.series = null;
  clearChart('Loading published chart points…');
  try {
    const entry = state.game.sections.find(s => s.key === $('game-section').value);
    if (!entry) { clearChart('No usable section observations in this game window.'); status(''); return; }
    const shard = await data(entry.file); if (version !== state.version) return;
    state.series = shard.sections[entry.key]; status(''); drawCurrent();
  } catch(error) { if (version === state.version) status(error.message, true); }
}
function clearChart(message) { $('chart').replaceChildren(); $('chart-title').textContent=message; $('chart-tooltip').textContent=''; $('chart-method').textContent=''; $('chart-sample').textContent=''; $('window').textContent=''; }
function drawCurrent() {
  if (!state.report) return;
  const currency = state.report.currency;
  let x, y, name, sample, method, windowText = '';
  if (state.view === 'report') {
    const row = state.report.sections.find(s => s.section_key === $('report-section').value);
    if (!row || !row.timeline.length) { clearChart('No supported historical windows for this selection.'); return; }
    x = row.timeline.map(p => p.lead_time); y = row.timeline.map(p => p.average_price); name = row.name;
    sample = `${row.game_count} games with usable windows · ${row.observation_count.toLocaleString()} observations`;
    method = 'Per-game window medians, averaged with equal game weight. Missing windows are not filled in.';
    const best = row.timeline.reduce((a,b) => a.average_price <= b.average_price ? a : b);
    windowText = `Lowest displayed historical window: ${best.label} (${best.game_count} games). Not a future-price prediction.`;
  } else {
    if (!state.series) return;
    ({x,y} = state.series); name = state.game.sections.find(s => s.key === $('game-section').value).name;
    sample = `${x.length.toLocaleString()} recorded price points · ${state.game.capture_count.toLocaleString()} game captures`;
    method = `Recorded observations for this provider label. Display window: final ${state.sport === 'mlb' ? 96 : 720} hours before the event.`;
  }
  $('chart-title').textContent = name;
  $('chart-method').textContent = method; $('chart-sample').textContent = sample; $('window').textContent = windowText;
  const percentOption = $('display').querySelector('[value=percent]'); percentOption.disabled = !y.length || y[0] === 0;
  if (percentOption.disabled) $('display').value = 'money';
  const relative = $('display').value === 'percent';
  const values = relative ? y.map(p => roundEven(100 * p / y[0])) : y;
  draw(x, values, relative ? v => v.toFixed(0) + '%' : v => money(v,currency));
}
function draw(x, y, format) {
  const svg = $('chart'); svg.replaceChildren();
  if (!x.length || x.length !== y.length || [...x,...y].some(v => !Number.isFinite(v))) { clearChart('Invalid published chart.'); return; }
  const ns='http://www.w3.org/2000/svg';
  function node(tag, attrs, value) { const e=document.createElementNS(ns,tag); Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,String(v))); if(value!==undefined)e.textContent=value; svg.append(e); return e; }
  let xmin=Infinity,xmax=-Infinity,ymin=Infinity,ymax=-Infinity;
  x.forEach(v=>{xmin=Math.min(xmin,v);xmax=Math.max(xmax,v);}); y.forEach(v=>{ymin=Math.min(ymin,v);ymax=Math.max(ymax,v);});
  const pad=Math.max((ymax-ymin)*.12,1); ymin=Math.max(0,ymin-pad); ymax+=pad;
  const sx=v=>86+(xmax-v)/(xmax-xmin||1)*778, sy=v=>340-(v-ymin)/(ymax-ymin||1)*305;
  for(let i=0;i<5;i++){ const v=ymin+(ymax-ymin)*i/4; node('line',{x1:86,x2:864,y1:sy(v),y2:sy(v),class:'grid'}); node('text',{x:76,y:sy(v)+4,'text-anchor':'end'},format(v)); const h=xmax-(xmax-xmin)*i/4; node('text',{x:sx(h),y:372,'text-anchor':'middle'},Math.round(h)+'h'); }
  node('text',{x:480,y:402,'text-anchor':'middle'},'Hours before the event →');
  node('polyline',{points:x.map((v,i)=>sx(v).toFixed(2)+','+sy(y[i]).toFixed(2)).join(' '),class:'curve'});
  const point=node('circle',{cx:sx(x[0]),cy:sy(y[0]),r:5,class:'point'});
  let current=0;
  function inspect(i){current=i;point.setAttribute('cx',sx(x[i]));point.setAttribute('cy',sy(y[i]));$('chart-tooltip').textContent=`${x[i].toFixed(2)} hours before · ${format(y[i])} · point ${i+1} of ${x.length}`;}
  svg.onpointermove=e=>{const box=svg.getBoundingClientRect(), px=(e.clientX-box.left)/box.width*900; let best=0; for(let i=1;i<x.length;i++)if(Math.abs(sx(x[i])-px)<Math.abs(sx(x[best])-px))best=i;inspect(best);};
  svg.onkeydown=e=>{if(e.key==='ArrowRight'||e.key==='ArrowLeft'){e.preventDefault();inspect(Math.max(0,Math.min(x.length-1,current+(e.key==='ArrowRight'?1:-1))));}};
  svg.setAttribute('aria-label',`${$('chart-title').textContent}; ${x.length} points. Use left and right arrow keys to inspect prices.`);
  inspect(0);
}
document.querySelectorAll('[data-sport]').forEach(b=>b.addEventListener('click',()=>loadSport(b.dataset.sport)));
document.querySelectorAll('[data-view]').forEach(b=>b.addEventListener('click',()=>changeView(b.dataset.view)));
$('team-search').addEventListener('input',directory);
$('back').addEventListener('click',()=>{++state.version;$('detail').hidden=true;$('directory').hidden=false;status('');history.replaceState(null,'','#sport='+state.sport);});
$('report-section').addEventListener('change',drawCurrent);$('display').addEventListener('change',drawCurrent);
$('game').addEventListener('change',loadGame);$('game-section').addEventListener('change',loadSeries);
(async()=>{try{const response=await fetch('manifest.json',{cache:'no-cache'});if(!response.ok)throw new Error('No published snapshot is available yet.');manifest=await response.json();if(manifest.version!==1||manifest.mode!=='historical-snapshot-preview'||manifest.live_updates_enabled!==false)throw new Error('Unsupported publication manifest.');const params=new URLSearchParams(location.hash.slice(1));await loadSport(['mlb','nfl','nhl'].includes(params.get('sport'))?params.get('sport'):'mlb',params.get('report'));}catch(error){status(error.message,true);}})();
