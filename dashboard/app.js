'use strict';

// SHA-256 of an empty body: required by CloudFront OAC for POST to a Lambda Function URL.
const EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
const REFRESH_MS = 60 * 1000;

const $ = (id) => document.getElementById(id);
const fmtT = (v) => (v === null || v === undefined ? '–' : `${Number(v).toFixed(1)}°`);
const fmtOffset = (v) => (v === null || v === undefined ? '–' : `${v >= 0 ? '+' : ''}${Number(v).toFixed(1)}°`);

const DECISIONS = {
  write: 'Scrive il setpoint compensato',
  release: 'Rilascia la stanza al programma',
  error: 'Errore',
  'skip:offset_negligible': 'Nessuna correzione necessaria',
  'skip:within_hysteresis': 'Setpoint già corretto',
  'skip:user_override': 'Manuale impostato da te: rispettato',
  'skip:module_stale': 'Dato stazione troppo vecchio',
  'skip:module_missing': 'Dato stazione mancante',
  'skip:valve_unreachable': 'Valvola non raggiungibile',
  'skip:no_scheduled_setpoint': 'Nessun setpoint nel programma',
};
const MODES = { schedule: 'programma', manual: 'manuale', max: 'boost', off: 'spento', hg: 'antigelo', away: 'assente', home: 'programma' };

function decisionLabel(d) {
  if (!d) return '–';
  if (DECISIONS[d]) return DECISIONS[d];
  if (d.startsWith('skip:home_mode_')) return `Casa non in programma (${d.slice(15)})`;
  if (d.startsWith('skip:room_mode_')) return `Stanza in modalità ${MODES[d.slice(15)] || d.slice(15)}`;
  return d;
}

function relTime(ts) {
  if (!ts) return '';
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return 'pochi secondi fa';
  if (s < 3600) return `${Math.round(s / 60)} min fa`;
  return `${Math.round(s / 3600)} h fa`;
}

function render(st) {
  const when = st.ts ? new Date(st.ts * 1000) : null;
  $('updated').textContent = when
    ? `Ultima lettura ${when.toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'medium' })} (${relTime(st.ts)}) · ${st.trigger === 'manual' ? 'manuale' : 'automatica'}`
    : 'Nessuna lettura disponibile';

  $('t_valve').textContent = fmtT(st.t_valve);
  $('t_module').textContent = fmtT(st.t_module);
  $('module_age').textContent = st.module_age_min !== undefined ? `dato di ${st.module_age_min} min fa` : '';
  $('offset').textContent = fmtOffset(st.offset);
  $('target').textContent = fmtT(st.target);
  $('current_sp').textContent = fmtT(st.current_sp);
  $('room_mode').textContent = st.room_mode ? MODES[st.room_mode] || st.room_mode : '';
  $('new_sp').textContent = fmtT(st.new_sp);

  const dec = $('decision');
  dec.textContent = decisionLabel(st.decision);
  dec.title = st.decision || '';
  dec.className = 'badge ' + (st.decision || '').split(':')[0];
  $('dry_run').hidden = !st.dry_run;
  $('error').hidden = !st.error;
  $('error').textContent = st.error ? `${st.error_type}: ${st.error}` : '';

  $('boiler').textContent = st.boiler_status === true ? 'accesa' : st.boiler_status === false ? 'spenta' : 'n/d';
  renderRooms(st);
}

function cell(text, cls) {
  const td = document.createElement('td');
  if (cls) td.className = cls;
  if (text instanceof Node) td.append(text); else td.textContent = text;
  return td;
}

const BATTERY = {
  full: ['piena', 'ok'], high: ['alta', 'ok'], medium: ['media', 'ok'],
  low: ['bassa', 'warn'], very_low: ['scarica', 'err'],
};
const DEVICE_TYPES = { NRV: 'Valvola', NATherm1: 'Termostato', OTM: 'Termostato' };

// rf_strength: lower is better. Thresholds as in the Home Assistant Netatmo integration.
function signalBars(rf) {
  if (typeof rf !== 'number') return null;
  if (rf >= 90) return 1;
  if (rf >= 76) return 2;
  return 3;
}

function renderDevices(devices) {
  const ul = document.createElement('ul');
  ul.className = 'devices';
  const sameType = {};
  devices.forEach((d) => {
    sameType[d.type] = (sameType[d.type] || 0) + 1;
  });
  const seen = {};
  devices.forEach((d) => {
    seen[d.type] = (seen[d.type] || 0) + 1;
    const li = document.createElement('li');

    const label = document.createElement('span');
    label.className = 'dev-name';
    const base = DEVICE_TYPES[d.type] || d.type;
    label.textContent = d.name || (sameType[d.type] > 1 ? `${base} ${seen[d.type]}` : base);
    label.title = d.id;
    li.append(label);

    if (d.reachable === false) {
      const off = document.createElement('span');
      off.className = 'dev-off';
      off.textContent = 'non raggiungibile';
      li.append(off);
      ul.append(li);
      return;
    }

    if (d.battery_state) {
      const [txt, level] = BATTERY[d.battery_state] || [d.battery_state, 'ok'];
      const bat = document.createElement('span');
      bat.className = `dev-bat ${level}`;
      bat.textContent = `batteria ${txt}`;
      if (d.battery_level) bat.title = `${d.battery_level} mV`;
      li.append(bat);
    }

    const bars = signalBars(d.rf_strength);
    if (bars) {
      const sig = document.createElement('span');
      sig.className = 'dev-sig';
      sig.title = `rf_strength ${d.rf_strength} (più basso = migliore)`;
      sig.setAttribute('aria-label', `segnale ${bars} su 3`);
      const icon = document.createElement('span');
      icon.className = 'sig-icon';
      for (let i = 1; i <= 3; i++) {
        const b = document.createElement('i');
        if (i <= bars) b.className = 'on';
        icon.append(b);
      }
      sig.append(icon, document.createTextNode(` ${bars}/3`));
      li.append(sig);
    }
    ul.append(li);
  });
  return ul;
}

function renderRooms(st) {
  // Older status files only have rooms_power_request.
  const rooms = st.rooms || Object.entries(st.rooms_power_request || {}).map(([name, power]) => ({ name, power }));
  const tbody = $('rooms');
  tbody.replaceChildren();
  rooms
    .slice()
    .sort((a, b) => (b.compensated === true) - (a.compensated === true) || String(a.name).localeCompare(b.name))
    .forEach((r) => {
      const tr = document.createElement('tr');
      if (r.compensated) tr.className = 'own';

      const name = document.createElement('div');
      const strong = document.createElement('strong');
      strong.textContent = r.name;
      name.append(strong);
      const notes = [];
      if (r.mode) {
        let m = MODES[r.mode] || r.mode;
        if (r.mode === 'manual' && r.end_time) {
          m += ` fino alle ${new Date(r.end_time * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' })}`;
        }
        notes.push(m);
      }
      if (r.compensated) notes.push('compensata');
      if (r.open_window) notes.push('finestra aperta');
      if (r.reachable === false) notes.push('non raggiungibile');
      if (notes.length) {
        const small = document.createElement('div');
        small.className = 'sub';
        small.textContent = notes.join(' · ');
        name.append(small);
      }
      if (r.devices && r.devices.length) name.append(renderDevices(r.devices));

      const power = document.createElement('div');
      power.className = 'power';
      if (typeof r.power === 'number') {
        const bar = document.createElement('div');
        bar.className = 'bar';
        const fill = document.createElement('span');
        fill.style.width = `${Math.max(0, Math.min(100, r.power))}%`;
        bar.append(fill);
        const pct = document.createElement('span');
        pct.className = 'pct';
        pct.textContent = `${r.power}%`;
        power.append(bar, pct);
      } else {
        power.textContent = '–';
      }

      tr.append(cell(name), cell(fmtT(r.t), 'num'), cell(fmtT(r.sp), 'num'), cell(power));
      tbody.append(tr);
    });
}

const SERIES = [
  { key: 't_valve', label: 'Valvola', color: '--s-valve' },
  { key: 't_module', label: 'Stazione', color: '--s-module' },
  { key: 'target', label: 'Target programma', color: '--s-target', dash: '4 4' },
  { key: 'new_sp', label: 'Setpoint calcolato', color: '--s-newsp', dash: '2 3' },
];

let lastHistory = null;
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => lastHistory && renderChart(lastHistory), 200);
});

function renderChart(history) {
  const box = $('chart');
  const pts = (history || []).filter((h) => h && h.ts).sort((a, b) => a.ts - b.ts);
  $('chart-empty').hidden = pts.length > 1;
  box.hidden = pts.length <= 1;
  lastHistory = history;
  if (pts.length <= 1) return;

  const css = getComputedStyle(document.documentElement);
  // Draw at the real pixel width so labels stay readable on phones.
  const W = Math.max(300, Math.round(box.clientWidth || box.parentElement.clientWidth || 900));
  const H = W < 500 ? 220 : 280, L = 36, R = 10, T = 10, B = 24;
  const labelEvery = W < 500 ? 6 : 3; // hours between x-axis labels
  const x0 = pts[0].ts, x1 = pts[pts.length - 1].ts;
  const vals = pts.flatMap((p) => SERIES.map((s) => p[s.key])).filter((v) => typeof v === 'number');
  let y0 = Math.floor(Math.min(...vals) - 0.5), y1 = Math.ceil(Math.max(...vals) + 0.5);
  if (y1 - y0 < 2) y1 = y0 + 2;
  const sx = (t) => L + ((t - x0) / Math.max(1, x1 - x0)) * (W - L - R);
  const sy = (v) => T + (1 - (v - y0) / (y1 - y0)) * (H - T - B);

  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  const el = (tag, attrs, text) => {
    const e = document.createElementNS(ns, tag);
    Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v));
    if (text !== undefined) e.textContent = text;
    svg.append(e);
    return e;
  };

  const step = y1 - y0 > 8 ? 2 : 1;
  for (let v = y0; v <= y1; v += step) {
    el('line', { x1: L, x2: W - R, y1: sy(v), y2: sy(v), class: 'grid-line' });
    el('text', { x: L - 6, y: sy(v) + 4, 'text-anchor': 'end' }, `${v}°`);
  }
  const hourMs = 3600;
  const firstHour = Math.ceil(x0 / (labelEvery * hourMs)) * labelEvery * hourMs;
  for (let t = firstHour; t <= x1; t += labelEvery * hourMs) {
    el('text', { x: sx(t), y: H - 6, 'text-anchor': 'middle' },
      new Date(t * 1000).toLocaleTimeString('it-IT', { hour: '2-digit', minute: '2-digit' }));
  }

  SERIES.forEach((s) => {
    let d = '';
    let pen = false;
    pts.forEach((p) => {
      const v = p[s.key];
      if (typeof v !== 'number') { pen = false; return; }
      d += `${pen ? 'L' : 'M'}${sx(p.ts).toFixed(1)},${sy(v).toFixed(1)}`;
      pen = true;
    });
    if (d) {
      const attrs = { d, fill: 'none', stroke: css.getPropertyValue(s.color).trim(), 'stroke-width': 2, 'stroke-linejoin': 'round' };
      if (s.dash) attrs['stroke-dasharray'] = s.dash;
      el('path', attrs);
    }
  });

  box.replaceChildren(svg);
  $('legend').innerHTML = SERIES.map((s) => `<span><i style="background:var(${s.color})"></i>${s.label}</span>`).join('');
}

async function getJson(path) {
  const r = await fetch(`${path}?t=${Date.now()}`, { cache: 'no-store' });
  if (r.status === 403 || r.status === 404) return null; // not written yet
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

async function refresh() {
  try {
    const [st, hist] = await Promise.all([getJson('data/status.json'), getJson('data/history.json')]);
    if (st) render(st);
    else $('updated').textContent = 'Nessuna lettura ancora: attendi la prossima esecuzione o premi "Leggi ora".';
    renderChart(hist);
  } catch (e) {
    showMessage(`Impossibile caricare i dati: ${e.message}`);
  }
}

function showMessage(text) {
  const m = $('message');
  m.textContent = text;
  m.hidden = !text;
}

async function runNow() {
  const btn = $('run-btn');
  btn.disabled = true;
  btn.textContent = 'Lettura in corso…';
  showMessage('');
  try {
    const r = await fetch('api/run', { method: 'POST', headers: { 'x-amz-content-sha256': EMPTY_SHA256 } });
    if (r.status === 429) {
      showMessage("Un'esecuzione è già in corso: riprova tra qualche secondo.");
    } else {
      const body = await r.json().catch(() => null);
      if (!body) throw new Error(`HTTP ${r.status}`);
      render(body);
      if (!r.ok) showMessage("La lettura è fallita: vedi l'errore sotto.");
    }
    const hist = await getJson('data/history.json');
    renderChart(hist);
  } catch (e) {
    showMessage(`Lettura non riuscita: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Leggi ora';
  }
}

$('run-btn').addEventListener('click', runNow);
refresh();
setInterval(refresh, REFRESH_MS);
