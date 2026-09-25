'use strict';
/* Static data adapter for the unchanged production presentation scripts.
 * Only published same-origin files are fetched. No Python/API/database runtime.
 */
(() => {
  const nativeFetch = window.fetch.bind(window);
  const loader = document.currentScript;
  const cache = new Map();
  let catalogPromise;
  async function read(path) {
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin || !(/^\/(?:native\/data-[a-f0-9]{64}\.json|data\/(?:series|game|report|index)-[a-f0-9]{64}\.json|original-manifest\.json)$/.test(url.pathname))) {
      throw new Error('Invalid published file reference.');
    }
    if (cache.has(url.pathname)) return cache.get(url.pathname);
    const response = await nativeFetch(url, {cache: path.includes('manifest') ? 'no-cache' : 'default'});
    if (!response.ok) throw new Error('This published selection is unavailable. Reload the preview.');
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength > 3 * 1024 * 1024) throw new Error('Published response exceeds its size limit.');
    const match = url.pathname.match(/-([a-f0-9]{64})\.json$/);
    if (match) {
      const actual = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(x => x.toString(16).padStart(2,'0')).join('');
      if (actual !== match[1]) throw new Error('Published data failed its integrity check.');
    }
    const data = JSON.parse(new TextDecoder().decode(bytes));
    cache.set(url.pathname, data);
    if (cache.size > 24) cache.delete(cache.keys().next().value);
    return data;
  }
  function catalog(sport) {
    if (!catalogPromise) catalogPromise = read('/original-manifest.json');
    return catalogPromise.then(manifest => {
      if (manifest.presentation !== 'original-templates' || manifest.live_updates_enabled !== false || !manifest.sports[sport]) throw new Error('Unsupported static publication.');
      return read(manifest.sports[sport]);
    });
  }
  // Native homepage scripts retain their URLs and UI logic. The adapter serves
  // their option requests from static JSON, without sending an /api request.
  window.fetch = function(input, init) {
    const raw = input instanceof Request ? input.url : String(input);
    const url = new URL(raw, location.href);
    const api = url.pathname.match(/^\/api\/(baseball|nfl|nhl)\/options$/);
    if (url.origin === location.origin && api) {
      if ((init?.method || (input instanceof Request ? input.method : 'GET')).toUpperCase() !== 'GET') return Promise.reject(new Error('Static options support reads only.'));
      const sport = api[1] === 'baseball' ? 'mlb' : api[1];
      const key = url.searchParams.get(sport === 'mlb' ? 'venue' : 'team') || '';
      return catalog(sport).then(async c => {
        const data = c.options[key] ? await read(c.options[key]) : {games:[], multi_sections:[], sections_by_game:{}};
        return new Response(JSON.stringify(data), {status:200, headers:{'Content-Type':'application/json'}});
      });
    }
    if (url.origin === location.origin && url.pathname.startsWith('/api/')) return Promise.reject(new Error('This preview has no application API.'));
    return nativeFetch(input, init);
  };
  function fail(message) {
    const box = document.createElement('section'); box.className = 'empty-state nfl-empty-state';
    const title = document.createElement('h2'); title.textContent = 'Selection unavailable';
    const text = document.createElement('p'); text.textContent = message;
    const back = document.createElement('a'); back.className = 'button button--primary'; back.href = '/'; back.textContent = 'Back to teams';
    box.append(title, text, back);
    const chart = document.querySelector('.interactive-chart') || document.querySelector('main');
    if (chart) chart.replaceChildren(box);
    document.body.removeAttribute('data-static-pending');
    document.body.dataset.staticError = 'true';
  }
  const roundEven = x => { const a = Math.floor(x); return x-a === .5 ? a+(a%2) : Math.round(x); };
  function substitute(values) {
    const replace = value => value.replace(/TSVALUE_([A-Za-z]+)/g, (_, key) => String(values[key] ?? ''));
    const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_TEXT);
    const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
    nodes.forEach(node => { if (!['SCRIPT','STYLE'].includes(node.parentElement?.tagName)) node.textContent = replace(node.textContent); });
    document.querySelectorAll('*').forEach(node => {
      [...node.attributes].forEach(attr => {
        if (!attr.value.includes('TSVALUE_')) return;
        if (attr.name === 'href') {
          const url = new URL(attr.value, location.origin);
          if (url.origin !== location.origin) throw new Error('Unexpected link in native template.');
          [...url.searchParams].forEach(([key, value]) => url.searchParams.set(key, replace(value)));
          node.setAttribute(attr.name, url.pathname+url.search+url.hash);
        } else node.setAttribute(attr.name, replace(attr.value));
      });
    });
  }
  async function chart(boot, c, params) {
    const sport = boot.sport, multi = sport === 'mlb' && params.get('mode') !== 'single';
    const requested = params.get('display') || params.get('id');
    const display = ['percentage','%'].includes(requested) ? 'percentage' : 'money';
    const section = params.get('section') || '';
    let x, y, total = 0, game, place = params.get('event') || '';
    if (multi) {
      const directory = c.market[place] ? await read(c.market[place]) : {};
      const key = Object.keys(directory).find(name => name === section) || Object.keys(directory).find(name => name.toLowerCase() === section.toLowerCase());
      if (!key) throw new Error('No multi-game price history is published for this selection.');
      const payload = await read(directory[key]);
      ({x,y,total} = payload[display]);
    } else {
      game = c.games[params.get('game')];
      if (!game) throw new Error('That game is not in this saved snapshot.');
      const selection = params.get(sport === 'mlb' ? 'event' : 'team') || params.get('event') || '';
      const allowed = sport === 'mlb' ? [game.place, game.venue] : [game.team, game.venue];
      if (selection && !allowed.includes(selection)) throw new Error('The game does not belong to that team or venue.');
      const record = await read('/'+game.file);
      const entries = record.sections.filter(row => row.name.toLowerCase() === section.toLowerCase());
      if (!entries.length) throw new Error('No stored chart observations for this section.');
      const points = [];
      for (const entry of entries) {
        const shard = await read('/'+entry.file), series = shard.sections[entry.key];
        series.x.forEach((v,i) => points.push([v, series.y[i]]));
      }
      points.sort((a,b) => b[0]-a[0]); x=points.map(p=>p[0]); y=points.map(p=>p[1]);
      if (display === 'percentage' && y[0] !== 0) y=y.map(p=>roundEven((p/y[0])*100));
      total = y.length ? 1 : 0; place = game.place || game.venue;
    }
    if (!x?.length || x.length !== y.length || [...x,...y].some(v=>!Number.isFinite(v))) throw new Error('Not enough comparable data for this selection.');
    substitute({place, section, team:game?.team || '', venue:game?.venue || place,
                game:game?.id || '', gameLabel:game?.label || '', currency:game?.currency || 'USD'});
    const dataNode = document.querySelector('.interactive-chart__data');
    dataNode.textContent = JSON.stringify({x,y});
    const chart = document.querySelector('.interactive-chart'); chart.dataset.displayMode = display;
    chart.dataset.currency = game?.currency || 'USD';
    chart.dataset.yAxisLabel = display === 'percentage' ? 'Relative price movement' : sport === 'mlb' ? 'Average listed price' : 'Lowest observed section price'+(sport==='nhl' ? ' ('+chart.dataset.currency+')' : '');
    const toggle = document.querySelector(sport==='mlb' ? '.view-toggle' : '.nfl-chart-actions a:last-child');
    const target = new URL(location.href); target.searchParams.set('display', display === 'money' ? 'percentage' : 'money');
    toggle.href=target.pathname+target.search; toggle.textContent='Show '+(display==='money' ? '%' : '$')+' view';
    document.querySelector('.chart-card__top strong').textContent=display==='percentage' ? 'Relative movement' : sport==='mlb' ? 'Average listed price' : 'Lowest observed section price';
    if (sport==='mlb') {
      document.querySelector('.result-heading .section-kicker').textContent=multi ? 'Market trend' : 'Single-game analysis';
      document.querySelector('.result-heading h1').textContent=multi ? 'Average price movement' : 'Price history for one game';
      const line = document.querySelector('.result-heading p'); line.textContent=place+' · Section '+section+(multi ? '' : ' · '+game.label);
      document.querySelector('.result-badge strong').textContent=total+' game'+(total===1?'':'s');
      document.querySelector('.insight-card dl div:last-child dt').textContent=String(total);
    }
    window.__staticChart = {x,y,display,sport,total};
  }
  async function predict(c, params) {
    const place=params.get('event')||'', section=params.get('section')||'';
    const directory=c.market[place] ? await read(c.market[place]) : {};
    if (!directory[section]) throw new Error('No historical buying window is published for that selection.');
    const payload=await read(directory[section]);
    if (payload.time === null) throw new Error('Not enough overlapping observations for a historical buying window.');
    substitute({place, section});
    document.querySelector('.time-value strong').textContent=Number(payload.time).toFixed(1);
    document.querySelector('.confidence-row strong').textContent=payload.percentage.total+' game'+(payload.percentage.total===1?'':'s')+' analyzed';
  }
  function selectReport(c, params) {
    const team=params.get('team'), venue=params.get('venue');
    return c.reports.find(r=>(!team || r.team.toLowerCase()===team.toLowerCase()) && (!venue || r.venue.toLowerCase()===venue.toLowerCase()));
  }
  async function bootPage(boot, params) {
    const c=boot.sport ? await catalog(boot.sport) : null;
    if (c) document.querySelectorAll('[data-source-freshness]').forEach(n=>n.textContent='Last capture: '+(c.captured_through ? new Date(c.captured_through).toLocaleString() : 'none')+'.');
    if (boot.kind==='report-router' && (params.get('team') || params.get('venue'))) {
      const report=selectReport(c,params); if (!report) throw new Error('That report is not in the saved snapshot.');
      location.replace('/reports/'+report.id+'.html'); return false;
    }
    if (boot.kind==='section-router') {
      const report=selectReport(c,params); if (!report) throw new Error('Choose a tracked team and section.');
      const rows=await read(c.sections[report.id]);
      const row=rows.find(r=>r.name.toLowerCase()===(params.get('section')||'').toLowerCase());
      if (!row) throw new Error('That section is not published in this report.');
      location.replace(row.url); return false;
    }
    if (boot.kind==='map-router') {
      if (!c.games[params.get('game')]) throw new Error('Choose a game from this snapshot.');
      location.replace('/maps/'+boot.sport+'-'+params.get('game')+'.html'+location.search); return false;
    }
    if (boot.kind==='concerts') {
      const note=document.querySelector('.static-snapshot-note');
      note.textContent='Staging preview · Concert data has not been migrated. The production concert site is unchanged.';
      document.querySelectorAll('.hero-stats dt').forEach(n=>n.textContent='—');
    }
    await Promise.all([...document.querySelectorAll('[data-static-json]')].map(async node=>{
      const value=structuredClone(await read(node.dataset.staticJson));
      if (node.id.includes('map-data')) {
        value.selected_section=node.dataset.selectedSection || '';
        if (boot.kind==='map') {
          const selected=params.get('section')||'';
          value.selected_section=value.sections.some(s=>s.name===selected) ? selected : '';
        }
      }
      node.textContent=JSON.stringify(value);
    }));
    if (boot.kind==='graph') await chart(boot,c,params);
    if (boot.kind==='predict') await predict(c,params);
    return true;
  }
  (async()=>{
    try {
      const boot=await read(loader.dataset.staticBoot);
      if (boot.kind==='home' && location.hash.startsWith('#sport=')) {
        const sport=new URLSearchParams(location.hash.slice(1)).get('sport');
        if (['nfl','nhl'].includes(sport)) {location.replace('/'+sport+'/');return;}
      }
      if (!await bootPage(boot,new URLSearchParams(location.search))) return;
      for (const placeholder of document.querySelectorAll('[data-original-script]')) {
        const path=placeholder.dataset.originalScript;
        if (!/^\/(?:static\/js\/[\w-]+\.js(?:\?v=\d+)?|native\/script-[a-f0-9]{64}\.js)$/.test(path)) throw new Error('Unexpected original script path.');
        await new Promise((resolve,reject)=>{const script=document.createElement('script');script.src=path;script.onload=resolve;script.onerror=()=>reject(new Error('Original interface script could not load.'));document.body.appendChild(script);});
      }
      document.body.removeAttribute('data-static-pending');
      document.body.dataset.staticReady='true';
    } catch(error) {fail(error.message);}
  })();
})();
