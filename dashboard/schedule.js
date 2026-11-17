'use strict';

const DAYS = ['Lunedì', 'Martedì', 'Mercoledì', 'Giovedì', 'Venerdì', 'Sabato', 'Domenica'];
const DAY_MIN = 1440;
const ZONE_TYPE_NAMES = { 0: 'Comfort', 1: 'Notte', 5: 'Eco', 8: 'Comfort+' };
const ZONE_COLORS = 6; // --z0 .. --z5 in style.css
const ROOM_KEY = 'trv-dashboard-room';

const $ = (id) => document.getElementById(id);
const fmtT = (v) => (v === null || v === undefined ? '–' : `${Number(v).toFixed(1)}°`);
const hhmm = (m) => `${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`;

let sched = null;
let zoneById = new Map();

function zoneName(z) {
  return z.name || ZONE_TYPE_NAMES[z.type] || `Fascia ${z.id}`;
}

function zoneColor(z) {
  return `var(--z${z.colorIndex % ZONE_COLORS})`;
}

/** Segments [{start, end, zone}] (minutes within the day) for day 0..6. */
function daySegments(day) {
  const tt = sched.timetable;
  const dayStart = day * DAY_MIN;
  const dayEnd = dayStart + DAY_MIN;
  // Zone active at the start of the day: last entry <= dayStart, else wrap to Sunday's last one.
  let current = tt[tt.length - 1];
  for (const e of tt) if (e.m_offset <= dayStart) current = e;
  const segs = [];
  let start = dayStart;
  for (const e of tt) {
    if (e.m_offset <= dayStart || e.m_offset >= dayEnd) continue;
    if (e.zone_id !== current.zone_id) {
      segs.push({ start: start - dayStart, end: e.m_offset - dayStart, zone: zoneById.get(current.zone_id) });
      start = e.m_offset;
    }
    current = e;
  }
  segs.push({ start: start - dayStart, end: DAY_MIN, zone: zoneById.get(current.zone_id) });
  return segs.filter((s) => s.end > s.start);
}

/** Minutes since Monday 00:00 in the home's timezone. */
function nowWeekMinutes(tz) {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat('en-GB', { timeZone: tz, weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
      .formatToParts(new Date())
      .map((p) => [p.type, p.value]),
  );
  const day = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].indexOf(parts.weekday);
  return day * DAY_MIN + Number(parts.hour) * 60 + Number(parts.minute);
}

function selectedRoom() {
  return $('room-select').value;
}

function tempFor(zone, room) {
  return zone && zone.rooms ? zone.rooms[room] : undefined;
}

function renderNow() {
  const now = nowWeekMinutes(sched.timezone);
  const day = Math.floor(now / DAY_MIN);
  const minute = now % DAY_MIN;
  const seg = daySegments(day).find((s) => minute >= s.start && minute < s.end);
  const box = $('now');
  box.replaceChildren();
  if (!seg || !seg.zone) {
    box.textContent = 'Fascia attuale non determinabile.';
    return;
  }
  const head = document.createElement('div');
  head.className = 'now-head';
  const chip = document.createElement('span');
  chip.className = 'chip';
  chip.style.background = zoneColor(seg.zone);
  const title = document.createElement('strong');
  title.textContent = zoneName(seg.zone);
  const when = document.createElement('span');
  when.className = 'muted';
  when.textContent = ` ${DAYS[day]} ${hhmm(seg.start)}–${hhmm(seg.end)}`;
  head.append(chip, title, when);

  const room = selectedRoom();
  const big = document.createElement('div');
  big.className = 'value';
  big.textContent = fmtT(tempFor(seg.zone, room));
  const sub = document.createElement('div');
  sub.className = 'sub';
  sub.textContent = `Temperatura programmata · ${room}`;
  box.append(head, big, sub);
}

function renderZones() {
  const rooms = [...new Set(sched.zones.flatMap((z) => Object.keys(z.rooms || {})))].sort((a, b) => a.localeCompare(b));
  const room = selectedRoom();
  const headRow = document.createElement('tr');
  ['Fascia', ...rooms].forEach((label, i) => {
    const th = document.createElement('th');
    th.textContent = label;
    if (i > 0) th.className = 'num' + (label === room ? ' sel' : '');
    headRow.append(th);
  });
  $('zones-head').replaceChildren(headRow);

  const body = $('zones-body');
  body.replaceChildren();
  sched.zones.forEach((z) => {
    const tr = document.createElement('tr');
    const name = document.createElement('td');
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.style.background = zoneColor(z);
    name.append(chip, document.createTextNode(zoneName(z)));
    tr.append(name);
    rooms.forEach((r) => {
      const td = document.createElement('td');
      td.className = 'num' + (r === room ? ' sel' : '');
      td.textContent = fmtT(z.rooms[r]);
      tr.append(td);
    });
    body.append(tr);
  });

  const extra = [];
  if (sched.away_temp != null) extra.push(`Assenza: ${fmtT(sched.away_temp)}`);
  if (sched.hg_temp != null) extra.push(`Antigelo: ${fmtT(sched.hg_temp)}`);
  $('extra-temps').textContent = extra.join(' · ');
}

function renderWeek() {
  const now = nowWeekMinutes(sched.timezone);
  const today = Math.floor(now / DAY_MIN);
  const room = selectedRoom();
  const week = $('week');
  week.replaceChildren();

  DAYS.forEach((label, day) => {
    const segs = daySegments(day);
    const row = document.createElement('div');
    row.className = 'day' + (day === today ? ' today' : '');

    const name = document.createElement('div');
    name.className = 'day-name';
    name.textContent = label + (day === today ? ' · oggi' : '');

    const bar = document.createElement('div');
    bar.className = 'day-bar';
    segs.forEach((s) => {
      const part = document.createElement('span');
      part.style.left = `${(s.start / DAY_MIN) * 100}%`;
      part.style.width = `${((s.end - s.start) / DAY_MIN) * 100}%`;
      part.style.background = s.zone ? zoneColor(s.zone) : 'var(--border)';
      part.title = `${hhmm(s.start)}–${hhmm(s.end)} ${s.zone ? zoneName(s.zone) : '?'} ${fmtT(tempFor(s.zone, room))}`;
      bar.append(part);
    });
    if (day === today) {
      const mark = document.createElement('i');
      mark.className = 'now-mark';
      mark.style.left = `${((now % DAY_MIN) / DAY_MIN) * 100}%`;
      bar.append(mark);
    }

    const list = document.createElement('ul');
    list.className = 'bands';
    segs.forEach((s) => {
      const li = document.createElement('li');
      const chip = document.createElement('span');
      chip.className = 'chip';
      chip.style.background = s.zone ? zoneColor(s.zone) : 'var(--border)';
      const time = document.createElement('span');
      time.className = 'band-time';
      time.textContent = `${hhmm(s.start)}–${hhmm(s.end)}`;
      const z = document.createElement('span');
      z.textContent = s.zone ? zoneName(s.zone) : '?';
      const t = document.createElement('span');
      t.className = 'band-temp';
      t.textContent = fmtT(tempFor(s.zone, room));
      li.append(chip, time, z, t);
      list.append(li);
    });

    row.append(name, bar, list);
    week.append(row);
  });
}

function renderAll() {
  renderNow();
  renderZones();
  renderWeek();
}

function fillRoomSelect(compensatedRoom) {
  const rooms = [...new Set(sched.zones.flatMap((z) => Object.keys(z.rooms || {})))].sort((a, b) => a.localeCompare(b));
  const sel = $('room-select');
  sel.replaceChildren(...rooms.map((r) => new Option(r, r)));
  let preferred = null;
  try { preferred = localStorage.getItem(ROOM_KEY); } catch (e) { /* storage unavailable */ }
  sel.value = rooms.includes(preferred) ? preferred : rooms.includes(compensatedRoom) ? compensatedRoom : rooms[0];
  sel.addEventListener('change', () => {
    try { localStorage.setItem(ROOM_KEY, sel.value); } catch (e) { /* storage unavailable */ }
    renderAll();
  });
}

async function getJson(path) {
  const r = await fetch(`${path}?t=${Date.now()}`, { cache: 'no-store' });
  if (r.status === 403 || r.status === 404) return null;
  if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
  return r.json();
}

async function init() {
  try {
    const [s, status] = await Promise.all([getJson('data/schedule.json'), getJson('data/status.json')]);
    if (!s || !Array.isArray(s.timetable) || !s.timetable.length) {
      $('sched-updated').textContent = 'Programma non ancora disponibile: attendi la prossima esecuzione o premi "Leggi ora" nella pagina Stato.';
      return;
    }
    sched = s;
    sched.timetable.sort((a, b) => a.m_offset - b.m_offset);
    sched.zones.forEach((z, i) => { z.colorIndex = i; z.rooms = z.rooms || {}; });
    zoneById = new Map(sched.zones.map((z) => [z.id, z]));

    $('sched-name').textContent = sched.name || 'Programma';
    $('sched-updated').textContent = sched.updated_ts
      ? `Programma attivo · letto il ${new Date(sched.updated_ts * 1000).toLocaleString('it-IT', { dateStyle: 'short', timeStyle: 'short' })} · orari ${sched.timezone}`
      : `Programma attivo · orari ${sched.timezone}`;

    const own = ((status && status.rooms) || []).find((r) => r.compensated);
    fillRoomSelect(own && own.name);
    renderAll();
    setInterval(renderAll, 60 * 1000);
  } catch (e) {
    const m = $('message');
    m.textContent = `Impossibile caricare il programma: ${e.message}`;
    m.hidden = false;
  }
}

init();
