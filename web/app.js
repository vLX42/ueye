// uEye web UI. Talks to the FastAPI backend (/api/*) and embeds go2rtc
// live streams via its <video-stream> web component (/go2rtc/).
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = (p, opt) => fetch(`/api${p}`, opt).then(async r => {
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
});

let CHANNELS = [];

// --- view switching ---------------------------------------------------------
$$('nav button').forEach(b => b.onclick = () => {
  $$('nav button').forEach(x => x.classList.toggle('active', x === b));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === b.dataset.view));
  // Only run the live streams while the Live tab is showing: leaving it tears the
  // <video-stream> elements down (closing their go2rtc WebSockets) so we stop
  // decoding in the background and free the NVR's limited concurrent sub-streams.
  if (b.dataset.view === 'live') renderGrid(); else stopGrid();
  if (b.dataset.view === 'settings') loadSettingsNav();
  if (b.dataset.view === 'recordings') onRecordingsShown();
  if (b.dataset.view === 'people') onPeopleShown();
});
// Also drop the streams when the tab/window is hidden, and bring them back on return.
document.addEventListener('visibilitychange', () => {
  const liveActive = document.querySelector('nav button.active')?.dataset.view === 'live';
  if (!liveActive) return;
  if (document.hidden) stopGrid(); else renderGrid();
});

const el = (tag, cls, txt) => { const e = document.createElement(tag); if (cls) e.className = cls; if (txt != null) e.textContent = txt; return e; };
const chName = id => (CHANNELS.find(c => c.id === id) || {}).name || `Camera ${id}`;

// ============================================================================
//  FRIENDLY-LABEL LAYER
//  Labels / option names harvested from the XMEye app's own language pack
//  (assets/language/en.txt). Keyed by the config leaf field name. Anything not
//  listed falls back to prettify() which spaces out CamelCase. This is what
//  turns raw Xiongmai JSON ("OverWrite", "PacketLength") into readable forms.
// ============================================================================
const LABELS = {
  Enable: 'Enabled', Level: 'Sensitivity', Region: 'Detection area',
  DeviceName: 'Device name', OverWrite: 'Overwrite oldest when disk full',
  MachineName: 'Device name', PreRecord: 'Pre-record (seconds)',
  PacketLength: 'Clip length (minutes)', RecordLen: 'Clip length (minutes)',
  Resolution: 'Resolution', FPS: 'Frame rate', Quality: 'Quality',
  BitRate: 'Bit rate (kbps)', BitRateControl: 'Rate mode', GOP: 'I-frame interval',
  Compression: 'Codec', AudioEnable: 'Record audio', VideoEnable: 'Enabled',
  Brightness: 'Brightness', Contrast: 'Contrast', Saturation: 'Saturation',
  Hue: 'Hue', Sharpness: 'Sharpness', Gain: 'Gain', Exposure: 'Exposure',
  DayNightColor: 'Day / night mode', BLCMode: 'Backlight compensation',
  Mirror: 'Mirror', Flip: 'Flip', WhiteBalance: 'White balance',
  HostName: 'Host name', HostIP: 'IP address', GateWay: 'Gateway',
  Submask: 'Subnet mask', HttpPort: 'HTTP port', TCPPort: 'TCP port',
  DNSServer1: 'Primary DNS', DNSServer2: 'Secondary DNS', MAC: 'MAC address',
  EnableDHCP: 'Automatic IP (DHCP)', TimeZone: 'Time zone', DST: 'Daylight saving',
  EventHandler: 'When triggered', RecordEnable: 'Record this camera',
  AlarmEnable: 'Trigger alarm', SnapEnable: 'Take snapshot',
  MailEnable: 'Send e-mail', FTPEnable: 'Upload to FTP', Dimension: 'Resolution',
};
// Coded enum values -> option names (app's own wording where it has one).
const ENUMS = {
  Level: { 1: '1 - Lowest', 2: '2 - Lower', 3: '3 - Middle', 4: '4 - Higher', 5: '5 - High', 6: '6 - Highest' },
  BitRateControl: { CBR: 'Constant (CBR)', VBR: 'Variable (VBR)' },
  Compression: { 'H.264': 'H.264', 'H.265': 'H.265 (HEVC)', H264: 'H.264', H265: 'H.265 (HEVC)' },
  DayNightColor: { 0: 'Auto', 1: 'Color (day)', 2: 'Black & white (night)' },
};
// Numeric validation bounds, keyed by leaf name.
const BOUNDS = {
  Level: { min: 1, max: 6 }, FPS: { min: 1, max: 30 }, PreRecord: { min: 0, max: 30 },
  Brightness: { min: 0, max: 100 }, Contrast: { min: 0, max: 100 },
  Saturation: { min: 0, max: 100 }, Hue: { min: 0, max: 100 }, Sharpness: { min: 0, max: 100 },
  HttpPort: { min: 1, max: 65535 }, TCPPort: { min: 1, max: 65535 },
};
const prettify = k => String(k)
  .replace(/_/g, ' ')
  .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
  .replace(/\b([A-Z]{2,})\b/g, m => m) // keep acronyms
  .replace(/^./, c => c.toUpperCase());
const labelFor = k => LABELS[k] || prettify(k);

// ============================================================================
//  SETTINGS  (single consolidated menu: Cameras + System + Advanced)
// ============================================================================
let SECTIONS = null;          // {groups:[{group,sections:[]}]} from /config-sections
let settingsNavLoaded = false;

async function loadSettingsNav() {
  if (settingsNavLoaded) return;
  const nav = $('#set-nav');
  nav.innerHTML = '<p class="empty">Probing device…</p>';
  try {
    if (!SECTIONS) SECTIONS = await api('/config-sections');
    nav.innerHTML = '';

    // --- Cameras: one friendly page per camera ---
    nav.appendChild(el('h4', 'cfg-group', 'Cameras'));
    CHANNELS.forEach(c => {
      const b = el('button', 'cfg-sec', c.name);
      b.onclick = () => selectNav(b, () => renderCameraPage(c.id));
      nav.appendChild(b);
    });

    // --- System: hand-picked friendly sections ---
    const have = name => SECTIONS.groups.some(g => g.sections.includes(name));
    const sys = [
      ['General.General', 'General'],
      ['Record', 'Recording'],
      ['NetWork.NetCommon', 'Network'],
      ['System.TimeZone', 'Time & date'],
    ].filter(([n]) => have(n));
    if (sys.length) {
      nav.appendChild(el('h4', 'cfg-group', 'System'));
      sys.forEach(([name, label]) => {
        const b = el('button', 'cfg-sec', label); b.title = name;
        b.onclick = () => selectNav(b, () => renderSection(name));
        nav.appendChild(b);
      });
    }

    // --- Advanced: every probed section, raw but friendly-rendered ---
    nav.appendChild(el('h4', 'cfg-group', 'Advanced (all sections)'));
    for (const g of SECTIONS.groups) {
      for (const name of g.sections) {
        const b = el('button', 'cfg-sec cfg-adv', name.replace(/^[A-Za-z]+\./, ''));
        b.title = name;
        b.onclick = () => selectNav(b, () => renderSection(name, true));
        nav.appendChild(b);
      }
    }
    settingsNavLoaded = true;
  } catch (e) { nav.innerHTML = `<p class="empty">Error: ${e.message}</p>`; }
}
function selectNav(btn, fn) {
  $$('.cfg-sec').forEach(x => x.classList.remove('active'));
  btn.classList.add('active');
  fn();
}

// --- per-camera friendly page ----------------------------------------------
// Motion detection (enable + sensitivity + visual zone painter) and, when the
// NVR supports it, face detection. This replaces the old separate "Motion
// zones" tab and the redundant standalone motion on/off list.
const ZCOLS = 32, ZROWS = 18, ZTOTAL = 32;
let zoneGrid = [], zonePainting = false, zonePaintVal = true;

function regionToGrid(region) {
  const g = [];
  for (let r = 0; r < ZROWS; r++) {
    const mask = (parseInt(region[r] || '0x0', 16) >>> 0);
    const row = [];
    for (let c = 0; c < ZCOLS; c++) row.push(((mask >>> (31 - c)) & 1) === 1);
    g.push(row);
  }
  return g;
}
function gridToRegion(g) {
  const region = [];
  for (let r = 0; r < ZTOTAL; r++) {
    if (r < ZROWS) {
      let mask = 0;
      for (let c = 0; c < ZCOLS; c++) if (g[r][c]) mask = (mask | (1 << (31 - c))) >>> 0;
      region.push('0x' + mask.toString(16).toUpperCase().padStart(8, '0'));
    } else region.push('0x00000000');
  }
  return region;
}
function paintCells() {
  const cells = $('#zone-grid')?.children; if (!cells) return;
  for (let r = 0; r < ZROWS; r++)
    for (let c = 0; c < ZCOLS; c++)
      cells[r * ZCOLS + c].classList.toggle('on', zoneGrid[r][c]);
}
function buildGrid() {
  const grid = $('#zone-grid');
  grid.style.gridTemplateColumns = `repeat(${ZCOLS}, 1fr)`;
  grid.style.gridTemplateRows = `repeat(${ZROWS}, 1fr)`;
  grid.innerHTML = '';
  for (let r = 0; r < ZROWS; r++) for (let c = 0; c < ZCOLS; c++) {
    const cell = el('div', 'zcell');
    const set = () => { zoneGrid[r][c] = zonePaintVal; cell.classList.toggle('on', zonePaintVal); };
    cell.addEventListener('mousedown', e => { e.preventDefault(); zonePainting = true; zonePaintVal = !zoneGrid[r][c]; set(); });
    cell.addEventListener('mouseenter', () => { if (zonePainting) set(); });
    grid.appendChild(cell);
  }
}
document.addEventListener('mouseup', () => { zonePainting = false; });

async function renderCameraPage(ch) {
  const box = $('#set-body');
  box.innerHTML = '<p class="empty">Loading…</p>';
  try {
    const md = (await api('/config/Detect.MotionDetect')).value;
    let fd = null;
    if (SECTIONS.groups.some(g => g.sections.includes('Detect.FaceDetection'))) {
      try { fd = (await api('/config/Detect.FaceDetection')).value; } catch (_) { fd = null; }
    }
    const m = md[ch] || {};
    zoneGrid = regionToGrid(m.Region || []);

    box.innerHTML = '';
    const head = el('div', 'cfg-head');
    head.appendChild(el('h3', null, `${chName(ch)} — detection`));
    const save = el('button', 'cfg-save', 'Save to device');
    head.appendChild(save);
    box.appendChild(head);
    const status = el('div', 'status'); status.id = 'set-status'; box.appendChild(status);

    // Motion detection card
    const card = el('div', 'set-card');
    card.appendChild(el('h4', 'set-card-title', 'Motion detection'));
    const rowEn = el('div', 'cfg-field');
    rowEn.appendChild(el('label', 'cfg-label', LABELS.Enable));
    const enWrap = el('label', 'switch'); const enIn = el('input'); enIn.type = 'checkbox'; enIn.checked = !!m.Enable;
    enWrap.appendChild(enIn); enWrap.appendChild(el('span', 'slider')); rowEn.appendChild(enWrap);
    card.appendChild(rowEn);

    // everything below the toggle lives in a block we show/hide with it
    const detail = el('div', 'set-detail');
    const rowLv = el('div', 'cfg-field');
    rowLv.appendChild(el('label', 'cfg-label', LABELS.Level));
    const lvSel = el('select', 'cfg-input');
    Object.entries(ENUMS.Level).forEach(([v, t]) => { const o = el('option', null, t); o.value = v; lvSel.appendChild(o); });
    lvSel.value = String(m.Level || 3);
    rowLv.appendChild(lvSel); detail.appendChild(rowLv);

    detail.appendChild(el('p', 'hint', 'Drag across the image to paint the motion-detection area. Highlighted cells are watched.'));
    const tools = el('div', 'toolbar');
    const bAll = el('button', 'ghost', 'Select all'), bClr = el('button', 'ghost', 'Clear'), bInv = el('button', 'ghost', 'Invert');
    bAll.onclick = () => { zoneGrid.forEach(r => r.fill(true)); paintCells(); };
    bClr.onclick = () => { zoneGrid.forEach(r => r.fill(false)); paintCells(); };
    bInv.onclick = () => { zoneGrid.forEach(r => r.forEach((v, i) => r[i] = !v)); paintCells(); };
    tools.append(bAll, bClr, bInv); detail.appendChild(tools);

    const stage = el('div', 'zone-stage');
    const img = el('img'); img.id = 'zone-img'; img.alt = 'camera snapshot';
    img.src = `/go2rtc/api/frame.jpeg?src=cam${ch}&t=${Date.now()}`;
    const grid = el('div', 'zone-grid'); grid.id = 'zone-grid';
    stage.append(img, grid); detail.appendChild(stage);
    card.appendChild(detail);
    box.appendChild(card);
    buildGrid(); paintCells();

    const syncMotion = () => { detail.style.display = enIn.checked ? '' : 'none'; };
    enIn.onchange = syncMotion; syncMotion();

    // Face detection card (only if supported)
    let faceIn = null;
    if (fd) {
      const f = fd[ch] || {};
      const fcard = el('div', 'set-card');
      fcard.appendChild(el('h4', 'set-card-title', 'Face detection'));
      fcard.appendChild(el('p', 'hint', 'The NVR flags frames that contain a face. Used later for browsing recordings by person.'));
      const frow = el('div', 'cfg-field');
      frow.appendChild(el('label', 'cfg-label', LABELS.Enable));
      const fw = el('label', 'switch'); faceIn = el('input'); faceIn.type = 'checkbox'; faceIn.checked = !!f.Enable;
      fw.appendChild(faceIn); fw.appendChild(el('span', 'slider')); frow.appendChild(fw);
      fcard.appendChild(frow); box.appendChild(fcard);
    }

    save.onclick = async () => {
      if (!confirm(`Save detection settings for ${chName(ch)} to the NVR?`)) return;
      status.textContent = 'Saving…';
      try {
        md[ch].Enable = enIn.checked;
        md[ch].Level = Number(lvSel.value);
        md[ch].Region = gridToRegion(zoneGrid);
        await api('/config/Detect.MotionDetect', {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ value: md }),
        });
        if (fd && faceIn) {
          fd[ch].Enable = faceIn.checked;
          await api('/config/Detect.FaceDetection', {
            method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ value: fd }),
          });
        }
        status.textContent = '✓ Saved to device';
      } catch (e) { status.textContent = 'Error: ' + e.message; }
    };
  } catch (e) { box.innerHTML = `<p class="empty">Error: ${e.message}</p>`; }
}

// --- generic friendly section editor (System + Advanced) -------------------
let cfgData = null, cfgName = null;

async function renderSection(name, advanced = false) {
  const box = $('#set-body');
  box.innerHTML = '<p class="empty">Loading…</p>';
  try {
    cfgData = (await api(`/config/${encodeURIComponent(name)}`)).value;
    cfgName = name;
    box.innerHTML = '';
    const head = el('div', 'cfg-head');
    head.appendChild(el('h3', null, labelFor(name.replace(/^[A-Za-z]+\./, ''))));
    const save = el('button', 'cfg-save', 'Save to device');
    save.onclick = saveSection;
    head.appendChild(save);
    box.appendChild(head);
    if (advanced) box.appendChild(el('p', 'hint', `Raw section: ${name}. Edit with care.`));
    const status = el('div', 'status'); status.id = 'set-status'; box.appendChild(status);
    const form = el('div', 'cfg-form');
    form.appendChild(renderValue({ root: cfgData }, 'root', null));
    box.appendChild(form);
  } catch (e) { box.innerHTML = `<p class="empty">Error: ${e.message}</p>`; }
}

// render parent[key]; mutates parent[key] in place on edit
function renderValue(parent, key, label) {
  const v = parent[key];
  if (typeof v === 'boolean') return field(label, boolInput(parent, key));
  if (typeof v === 'number') return field(label, selectOrNum(parent, key));
  if (typeof v === 'string') return field(label, selectOrText(parent, key));
  if (Array.isArray(v)) return arrayNode(parent, key, label);
  if (v && typeof v === 'object') return objectNode(parent, key, label);
  return field(label, el('span', 'cfg-ro', String(v)));
}
function field(label, control) {
  const row = el('div', 'cfg-field');
  if (label != null) row.appendChild(el('label', 'cfg-label', label));
  row.appendChild(control);
  return row;
}
function boolInput(p, k) {
  const l = el('label', 'switch'); const i = el('input'); i.type = 'checkbox'; i.checked = !!p[k];
  i.onchange = () => { p[k] = i.checked; }; l.appendChild(i); l.appendChild(el('span', 'slider')); return l;
}
// number: dropdown if we have a named enum for it, else validated number input
function selectOrNum(p, k) {
  if (ENUMS[k]) return enumSelect(p, k, true);
  const i = el('input', 'cfg-input'); i.type = 'number';
  const b = BOUNDS[k]; if (b) { i.min = b.min; i.max = b.max; }
  i.value = p[k];
  i.onchange = () => {
    let n = i.value === '' ? 0 : Number(i.value);
    if (b) { if (n < b.min) n = b.min; if (n > b.max) n = b.max; i.value = n; }
    i.classList.toggle('bad', Number.isNaN(n));
    p[k] = n;
  };
  return i;
}
function selectOrText(p, k) {
  if (ENUMS[k]) return enumSelect(p, k, false);
  const i = el('input', 'cfg-input'); i.type = 'text'; i.value = p[k];
  i.onchange = () => {
    if (k === 'DeviceName' || k === 'MachineName') i.classList.toggle('bad', !i.value.trim());
    p[k] = i.value;
  };
  return i;
}
function enumSelect(p, k, numeric) {
  const sel = el('select', 'cfg-input');
  const opts = ENUMS[k];
  let present = false;
  for (const [val, text] of Object.entries(opts)) {
    const o = el('option', null, text); o.value = val;
    if (String(p[k]) === String(val)) { o.selected = true; present = true; }
    sel.appendChild(o);
  }
  if (!present) { const o = el('option', null, `(current: ${p[k]})`); o.value = String(p[k]); o.selected = true; sel.appendChild(o); }
  sel.onchange = () => { p[k] = numeric ? Number(sel.value) : sel.value; };
  return sel;
}
// objects with an Enable flag collapse their other fields when disabled
function objectNode(parent, key, label) {
  const obj = parent[key];
  const fs = el('fieldset', 'cfg-obj');
  if (label != null) fs.appendChild(el('legend', null, label));
  const keys = Object.keys(obj);
  const gate = keys.includes('Enable') ? 'Enable' : null;
  if (gate) fs.appendChild(renderValue(obj, gate, labelFor(gate)));
  const rest = el('div', 'set-detail');
  for (const k of keys) { if (k === gate) continue; rest.appendChild(renderValue(obj, k, labelFor(k))); }
  fs.appendChild(rest);
  if (gate) {
    const sync = () => { rest.style.display = obj[gate] ? '' : 'none'; };
    const chk = fs.querySelector('input[type=checkbox]');
    if (chk) chk.addEventListener('change', sync);
    sync();
  }
  return fs;
}
function arrayNode(parent, key, label) {
  const arr = parent[key];
  const fs = el('fieldset', 'cfg-arr');
  const perChannel = arr.length === CHANNELS.length || arr.length === 8;
  if (label != null) fs.appendChild(el('legend', null, `${label} [${arr.length}]`));
  arr.forEach((item, i) => {
    const d = el('details', 'cfg-item'); if (i === 0) d.open = true;
    const sum = perChannel ? (chName(i) || `Channel ${i}`) : `#${i}`;
    d.appendChild(el('summary', null, sum));
    d.appendChild(renderValue(arr, i, null));
    fs.appendChild(d);
  });
  return fs;
}
async function saveSection() {
  if (!confirm(`Write ${cfgName} back to the NVR?`)) return;
  const st = $('#set-status'); if (st) st.textContent = 'Saving…';
  try {
    await api(`/config/${encodeURIComponent(cfgName)}`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ value: cfgData }),
    });
    if (st) st.textContent = '✓ Saved to device';
  } catch (e) { if (st) st.textContent = 'Error: ' + e.message; }
}

// ============================================================================
//  PEOPLE — face sorting (CPU, hybrid: motion events -> faces -> clusters)
// ============================================================================
let peopleInit = false;
function onPeopleShown() {
  if (!peopleInit) {
    peopleInit = true;
    $('#fx-date').value = REC.date || todayISO();
    $('#fx-index').onclick = indexFaces;
  }
  loadPeople();
}
async function loadPeople() {
  const box = $('#fx-people');
  try {
    const { people } = await api('/faces/people');
    if (!people.length) { box.innerHTML = '<p class="empty">No people yet. Scan a day to start.</p>'; return; }
    box.innerHTML = '';
    for (const p of people) {
      const card = el('div', 'fx-person');
      const img = el('img', 'fx-avatar'); img.src = `/api/faces/crop/${p.rep_face_id}`; img.alt = '';
      const name = el('div', 'fx-name', p.label || `Person ${p.id}`);
      const sub = el('div', 'fx-sub', `${p.count} sighting${p.count === 1 ? '' : 's'}`);
      card.append(img, name, sub);
      card.onclick = () => openPerson(p.id, p.label);
      box.appendChild(card);
    }
  } catch (e) {
    box.innerHTML = `<p class="empty">${e.message.includes('503') ? 'Face sorting needs the updated container (CV libraries). Rebuild to enable.' : 'Error: ' + e.message}</p>`;
  }
}
async function openPerson(pid, label) {
  const box = $('#fx-detail');
  box.classList.remove('hidden');
  box.innerHTML = '<p class="empty">Loading…</p>';
  const { faces } = await api(`/faces/person/${pid}`);
  box.innerHTML = '';
  const head = el('div', 'cfg-head');
  const nameIn = el('input', 'cfg-input'); nameIn.value = label || ''; nameIn.placeholder = `Person ${pid}`;
  const saveName = el('button', 'pb-btn', 'Save name');
  saveName.onclick = async () => { await api(`/faces/person/${pid}/label`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ label: nameIn.value }) }); setStatus('Name saved'); loadPeople(); };
  const close = el('button', 'pb-btn', '✕'); close.onclick = () => box.classList.add('hidden');
  head.append(el('h3', null, `${faces.length} sightings`), nameIn, saveName, close);
  box.appendChild(head);
  const grid = el('div', 'fx-grid');
  for (const f of faces) {
    const card = el('div', 'fx-person');
    const img = el('img', 'fx-avatar'); img.src = `/api/faces/crop/${f.id}`; img.alt = '';
    const t = f.ts.slice(11, 19);
    card.append(img, el('div', 'fx-name', t), el('div', 'fx-sub', chName(f.channel)));
    card.onclick = () => playAt(f.channel, f.ts);
    grid.appendChild(card);
  }
  box.appendChild(grid);
}
// jump from a face sighting to that moment in the Recordings timeline
async function playAt(channel, ts) {
  $$('nav button').forEach(x => x.classList.toggle('active', x.dataset.view === 'recordings'));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'recordings'));
  const day = ts.slice(0, 10);
  if (REC.date !== day || !REC.loaded) await loadDay(day); else renderTimeline();
  seekTo(channel, secOfDay(ts));
}
async function indexFaces() {
  const date = $('#fx-date').value;
  const st = $('#fx-status');
  try {
    const r = await api(`/faces/index?date=${date}`, { method: 'POST' });
    if (r.started === false && r.running) st.textContent = 'Already scanning…';
  } catch (e) {
    st.textContent = e.message.includes('503') ? 'Face sorting unavailable (rebuild container).' : 'Error: ' + e.message;
    return;
  }
  const poll = setInterval(async () => {
    try {
      const s = await api('/faces/status');
      st.textContent = `Scanning ${s.done}/${s.total} events · ${s.faces} faces found`;
      if (!s.running) {
        clearInterval(poll);
        st.textContent = s.error ? `Error: ${s.error}` : `Done · ${s.faces} faces · ${s.people} people total`;
        loadPeople();
      }
    } catch (e) { clearInterval(poll); st.textContent = 'Error: ' + e.message; }
  }, 1500);
}

// --- live grid --------------------------------------------------------------
let gridGen = 0;
async function renderGrid() {
  const grid = $('#grid');
  grid.innerHTML = '';
  const gen = ++gridGen;
  // video-stream.js is a module (deferred), so on first load app.js can get here
  // before the element is defined. Setting .src on a not-yet-upgraded element
  // creates an own property that shadows the component's setter: no WebSocket
  // ever opens and the tile stays black. Wait for the definition first.
  await customElements.whenDefined('video-stream');
  if (gen !== gridGen) return;  // stopGrid/renderGrid ran while we waited
  for (const ch of CHANNELS) {
    const tile = document.createElement('div');
    tile.className = 'tile';
    // video-stream picks MSE or WebRTC automatically; src is the go2rtc stream name
    const vs = document.createElement('video-stream');
    // MSE first: streams reliably over the single proxied TCP port. WebRTC as fallback.
    vs.setAttribute('mode', 'mse,webrtc,mp4');
    // the component expects a WebSocket URL to go2rtc's api
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    // Grid uses the sub (low-res) stream: the NVR allows many more concurrent
    // sub connections than main. Main stream is for single/fullscreen view.
    vs.src = new URL(`/go2rtc/api/ws?src=cam${ch.id}_sub`, `${proto}://${location.host}`);
    tile.appendChild(vs);
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = ch.name;
    tile.appendChild(label);
    tile.appendChild(buildTalkBar(ch.id));
    grid.appendChild(tile);
  }
}

// --- talk controls: Halloween sound + hold-to-talk --------------------------
// One talk session at a time (the NVR routes talk through a single intercom
// path), so a global flag greys out every other talk control while one runs.
let TALK_BUSY = false;
function setTalkBusy(on) {
  TALK_BUSY = on;
  $$('.talk-bar button').forEach(b => {
    if (!b.classList.contains('talk-active')) b.disabled = on;
  });
}

function buildTalkBar(channel) {
  const bar = el('div', 'talk-bar');

  const pumpkin = el('button', 'talk-btn talk-pumpkin', '🎃');
  pumpkin.title = 'Play a Halloween sound on this camera';
  pumpkin.onclick = async () => {
    if (TALK_BUSY) return;
    setTalkBusy(true);
    pumpkin.classList.add('talk-active');
    pumpkin.textContent = '🔊';
    try {
      await api('/talk/sound', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel, sound: 'halloween' }),
      });
    } catch (e) {
      setStatus(`Halloween sound failed: ${e.message}`, true);
    } finally {
      pumpkin.classList.remove('talk-active');
      pumpkin.textContent = '🎃';
      setTalkBusy(false);
    }
  };

  const ptt = el('button', 'talk-btn talk-ptt', '🎤');
  ptt.title = 'Hold to talk to this camera';
  wirePushToTalk(ptt, channel);

  bar.append(pumpkin, ptt);
  return bar;
}

// Push-to-talk: hold the mic button, capture the browser mic, downsample to
// 8 kHz 16-bit PCM and stream it over a WebSocket; the backend a-law encodes
// and pushes it to the camera. Needs a secure context (https or localhost) for
// getUserMedia — over plain http on a LAN IP the browser blocks the mic.
let stopActiveTalk = null;   // set while a push-to-talk session runs

// Mute every live tile while talking, else the camera mic picks up its own
// speaker and it loops back through ours (echo). On release the camera we
// talked to is unmuted (to hear the reply); the others get their state back.
let talkMuted = null;
function muteLive() {
  if (talkMuted) return;
  talkMuted = $$('#grid video').map(v => [v, v.muted]);
  talkMuted.forEach(([v]) => { v.muted = true; });
}
function unmuteLive(replyVideo) {
  if (!talkMuted) return;
  talkMuted.forEach(([v, was]) => { if (v.isConnected) v.muted = v === replyVideo ? false : was; });
  talkMuted = null;
}

function wirePushToTalk(btn, channel) {
  // One object per press. Every await re-checks `s.live`, so letting go while
  // the mic prompt / WebSocket is still starting tears down whatever came up
  // afterwards instead of leaving the mic streaming.
  let s = null;

  function start(ev) {
    ev.preventDefault();
    if (s || TALK_BUSY) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus('Mic needs https (or localhost). Open the UI over https to talk.', true);
      return;
    }
    // keep receiving pointerup even if the finger/cursor drifts off the button
    try { btn.setPointerCapture(ev.pointerId); } catch {}
    s = { live: true, ws: null, ctx: null, stream: null, node: null, src: null, queue: [] };
    stopActiveTalk = stop;
    muteLive();
    setTalkBusy(true);
    btn.classList.add('talk-active');
    setStatus(`Talking to ${chName(channel)}…`);
    // Create the AudioContext synchronously inside the gesture: iOS Safari
    // leaves one created after an await suspended (silent).
    s.ctx = new (window.AudioContext || window.webkitAudioContext)();
    s.ctx.resume?.();
    run(s).catch(e => {
      if (s?.live) setStatus(`Talk failed: ${e.message || e}`, true);
      stop();
    });
  }

  async function run(cur) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    // open the socket and the mic in parallel; audio is queued until the socket is up
    cur.ws = new WebSocket(`${proto}://${location.host}/api/talk/ws/${channel}`);
    cur.ws.binaryType = 'arraybuffer';
    cur.ws.onclose = e => {
      if (cur.live && e.code === 1013) setStatus('Talk is busy on another camera', true);
      if (cur === s) stop();
    };
    cur.ws.onopen = () => { for (const b of cur.queue) cur.ws.send(b); cur.queue = []; };

    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    });
    if (!cur.live) { stream.getTracks().forEach(t => t.stop()); return; }
    cur.stream = stream;

    const ctx = cur.ctx;
    cur.src = ctx.createMediaStreamSource(stream);
    cur.node = ctx.createScriptProcessor(2048, 1, 1);
    const ratio = ctx.sampleRate / 8000;
    cur.node.onaudioprocess = (e) => {
      if (!cur.live) return;
      const inp = e.inputBuffer.getChannelData(0);
      // decimate input rate -> 8 kHz with a simple box average
      const outLen = Math.floor(inp.length / ratio);
      const pcm = new Int16Array(outLen);
      for (let i = 0; i < outLen; i++) {
        const from = Math.floor(i * ratio), to = Math.floor((i + 1) * ratio);
        let acc = 0, n = 0;
        for (let j = from; j < to && j < inp.length; j++) { acc += inp[j]; n++; }
        let v = n ? acc / n : 0;
        v = Math.max(-1, Math.min(1, v));
        pcm[i] = v < 0 ? v * 32768 : v * 32767;
      }
      if (cur.ws.readyState === 1) cur.ws.send(pcm.buffer);
      else if (cur.ws.readyState === 0) cur.queue.push(pcm.buffer);
    };
    cur.src.connect(cur.node);
    cur.node.connect(ctx.destination);
  }

  function stop() {
    const cur = s;
    if (!cur) return;
    s = null;
    cur.live = false;
    if (stopActiveTalk === stop) stopActiveTalk = null;
    try { cur.node && cur.node.disconnect(); } catch {}
    try { cur.src && cur.src.disconnect(); } catch {}
    try { cur.ctx && cur.ctx.close(); } catch {}
    try { cur.stream && cur.stream.getTracks().forEach(t => t.stop()); } catch {}
    try { cur.ws && cur.ws.readyState <= 1 && cur.ws.close(); } catch {}
    unmuteLive(btn.closest('.tile')?.querySelector('video'));
    btn.classList.remove('talk-active');
    setTalkBusy(false);
    if ($('#status').textContent.startsWith('Talking to')) setStatus('');  // keep error messages
  }

  // Hold to talk: talks only while pressed (mouse or finger). With pointer
  // capture, release anywhere ends it; cancel covers the OS stealing the touch.
  btn.addEventListener('pointerdown', start);
  btn.addEventListener('pointerup', stop);
  btn.addEventListener('pointercancel', stop);
  btn.addEventListener('lostpointercapture', stop);
  btn.addEventListener('contextmenu', e => e.preventDefault());  // long-press menu
}
// Never keep the mic open when the page is hidden or loses focus mid-press.
window.addEventListener('blur', () => stopActiveTalk?.());
document.addEventListener('visibilitychange', () => { if (document.hidden) stopActiveTalk?.(); });

// Remove the live tiles -> each <video-stream>'s disconnectedCallback closes its
// go2rtc WebSocket, stopping the decode and releasing the NVR stream slot.
function stopGrid() {
  stopActiveTalk?.();
  gridGen++;
  const grid = $('#grid');
  if (grid) grid.innerHTML = '';
}

// ============================================================================
//  RECORDINGS — visual timeline (UniFi / NX-Witness style)
//  A 24h track per camera with colored activity, a draggable playhead, hover
//  thumbnails, an event rail, and a chunked player that streams hour-long
//  recordings by seeking the NVR with ByTime (/api/clip) so any moment plays
//  without downloading a whole 4K hour.
// ============================================================================
const dur = s => s >= 3600 ? `${(s/3600).toFixed(1)}h` : s >= 60 ? `${Math.round(s/60)}m` : `${s}s`;
const size = kb => kb >= 1048576 ? `${(kb/1048576).toFixed(1)} GB` : kb >= 1024 ? `${(kb/1024).toFixed(0)} MB` : `${kb} KB`;
const TYPE_COLOR = { continuous: '#2a3a4a', motion: '#d29922', alarm: '#f87171', manual: '#2dd4bf' };
const DAY = 86400, CHUNK = 20, PREFETCH = 10;

const pad = n => String(n).padStart(2, '0');
const secOfDay = ts => { const t = (ts || '').slice(11, 19).split(':').map(Number); return (t[0] || 0) * 3600 + (t[1] || 0) * 60 + (t[2] || 0); };
const fmtClock = s => { s = Math.max(0, Math.round(s)); return `${pad(Math.floor(s / 3600))}:${pad(Math.floor(s % 3600 / 60))}:${pad(s % 60)}`; };
const todayISO = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; };

const REC = {
  date: null, loaded: false,
  seg: {},                 // channel -> [{beginSec,endSec,...}]
  events: [],              // motion/alarm segments, time-sorted
  filter: new Set(['continuous', 'motion', 'alarm', 'manual']),
  zoom: 1, fitWidth: 900,
  quality: (() => { try { return localStorage.getItem('rec-quality') || 'sd'; } catch { return 'sd'; } })(),
  play: { ch: null, chunkStart: 0, prefetched: null },
};

const pps = () => (REC.fitWidth * REC.zoom) / DAY;      // pixels per second
const xOf = sec => sec * pps();
const secAt = x => x / pps();

// --- data load --------------------------------------------------------------
async function loadDay(date) {
  REC.date = date; REC.loaded = true;
  $('#rec-date').value = date;
  $('#rec-summary').textContent = 'Loading…';
  try {
    const r = await api(`/recordings?channel=all&date=${date}&event=all`);
    REC.seg = {}; REC.events = [];
    const files = (r.days[0] && r.days[0].files) || [];
    for (const f of files) {
      const s = { ...f, beginSec: secOfDay(f.begin), endSec: Math.max(secOfDay(f.begin) + 1, secOfDay(f.end) || DAY) };
      (REC.seg[f.channel] = REC.seg[f.channel] || []).push(s);
      if (f.type === 'motion' || f.type === 'alarm') REC.events.push(s);
    }
    REC.events.sort((a, b) => a.beginSec - b.beginSec || a.channel - b.channel);
    $('#rec-summary').textContent = `${r.count} clips · ${size(r.total_size_kb)} · ${REC.events.length} events`;
  } catch (e) {
    $('#rec-summary').textContent = 'Error: ' + e.message;
    REC.seg = {}; REC.events = [];
  }
  renderFilters(); renderTimeline(); renderEvents();
}
function onRecordingsShown() {
  if (!REC.loaded) { loadDay(REC.date || todayISO()); return; }
  renderTimeline();   // widths are only correct once the view is visible
}

// --- filters ----------------------------------------------------------------
function renderFilters() {
  const box = $('#rec-filters'); box.innerHTML = '';
  [['continuous', 'Continuous'], ['motion', 'Motion'], ['alarm', 'Alarm']].forEach(([k, lbl]) => {
    const chip = el('button', 'rec-chip' + (REC.filter.has(k) ? ' on' : ''));
    chip.innerHTML = `<i style="background:${TYPE_COLOR[k]}"></i>${lbl}`;
    chip.onclick = () => { REC.filter.has(k) ? REC.filter.delete(k) : REC.filter.add(k); renderFilters(); renderTimeline(); renderEvents(); };
    box.appendChild(chip);
  });
}

// --- timeline render --------------------------------------------------------
function renderTimeline() {
  const scroll = $('#tl-scroll');
  REC.fitWidth = Math.max(640, scroll.clientWidth - 2);
  const width = REC.fitWidth * REC.zoom;
  $('#tl-inner').style.width = width + 'px';

  // hour gridlines on the tracks container
  const hourW = 3600 * pps();
  const tracks = $('#tl-tracks');
  tracks.style.backgroundImage =
    `repeating-linear-gradient(to right, var(--line) 0 1px, transparent 1px ${hourW}px)`;

  // ruler
  const ruler = $('#tl-ruler'); ruler.innerHTML = '';
  const step = hourW < 34 ? 6 : hourW < 60 ? 3 : hourW < 90 ? 2 : 1;
  for (let h = 0; h <= 24; h += step) {
    const t = el('span', 'tl-tick', pad(h) + ':00');
    t.style.left = xOf(h * 3600) + 'px';
    ruler.appendChild(t);
  }

  // one track per camera
  tracks.innerHTML = '';
  const labels = $('#tl-labels'); labels.innerHTML = '<div class="tl-lab-spacer"></div>';
  for (const ch of CHANNELS) {
    labels.appendChild(el('div', 'tl-lab', ch.name));
    const row = el('div', 'tl-row'); row.dataset.ch = ch.id;
    for (const s of (REC.seg[ch.id] || [])) {
      if (!REC.filter.has(s.type)) continue;
      const bar = el('div', 'tl-seg tl-' + s.type);
      bar.style.left = xOf(s.beginSec) + 'px';
      bar.style.width = Math.max(s.type === 'continuous' ? 1 : 3, xOf(s.endSec - s.beginSec)) + 'px';
      bar.style.background = TYPE_COLOR[s.type] || '#555';
      bar.title = `${chName(ch.id)} · ${fmtClock(s.beginSec)}–${fmtClock(s.endSec)} · ${s.type} · ${dur(s.duration_s)}`;
      row.appendChild(bar);
    }
    row.onclick = e => { const r = row.getBoundingClientRect(); seekTo(ch.id, secAt(e.clientX - r.left)); };
    row.onmousemove = e => { const r = row.getBoundingClientRect(); showHover(e.clientX - r.left, ch.id); };
    row.onmouseleave = hideHover;
    tracks.appendChild(row);
  }
  positionPlayhead();
}

// --- hover thumbnail --------------------------------------------------------
let hoverTimer = null;
function showHover(x, ch) {
  const line = $('#tl-hover'); line.classList.remove('hidden'); line.style.left = x + 'px';
  const sec = secAt(x);
  clearTimeout(hoverTimer);
  hoverTimer = setTimeout(() => {
    const thumb = $('#tl-thumb'), img = $('#tl-thumb-img');
    $('#tl-thumb-time').textContent = `${chName(ch)} · ${fmtClock(sec)}`;
    thumb.classList.remove('hidden');
    const scroll = $('#tl-scroll');
    const vx = x - scroll.scrollLeft;
    thumb.style.left = Math.max(4, Math.min(scroll.clientWidth - 180, vx - 85)) + 'px';
    if (hasFootage(ch, sec)) {
      img.style.display = ''; img.src = `/api/thumb?channel=${ch}&time=${encodeURIComponent(REC.date + ' ' + fmtClock(sec))}`;
    } else { img.style.display = 'none'; }
  }, 180);
}
function hideHover() { clearTimeout(hoverTimer); $('#tl-hover').classList.add('hidden'); $('#tl-thumb').classList.add('hidden'); }

// --- events rail (lazy thumbnails) ------------------------------------------
let thumbObserver = null;
function renderEvents() {
  const box = $('#rec-events'); box.innerHTML = '';
  const evs = REC.events.filter(e => REC.filter.has(e.type));
  if (!evs.length) { box.innerHTML = '<p class="empty">No motion or alarm events on this day.</p>'; return; }
  if (thumbObserver) thumbObserver.disconnect();
  thumbObserver = new IntersectionObserver(ents => {
    for (const en of ents) if (en.isIntersecting) {
      const img = en.target; img.src = img.dataset.src; thumbObserver.unobserve(img);
    }
  }, { root: box, rootMargin: '200px' });
  for (const ev of evs) {
    const card = el('div', 'ev-card');
    const img = el('img', 'ev-thumb');
    img.dataset.src = `/api/thumb?channel=${ev.channel}&time=${encodeURIComponent(ev.begin)}`;
    img.alt = '';
    const meta = el('div', 'ev-meta');
    meta.innerHTML = `<span class="ev-badge" style="background:${TYPE_COLOR[ev.type]}">${ev.type}</span>`
      + `<strong>${fmtClock(ev.beginSec)}</strong><span class="ev-sub">${chName(ev.channel)} · ${dur(ev.duration_s)}</span>`;
    card.append(img, meta);
    card.onclick = () => seekTo(ev.channel, ev.beginSec);
    box.appendChild(card);
    thumbObserver.observe(img);
  }
}

// --- playback (chunked, seekable) -------------------------------------------
const hasFootage = (ch, sec) => (REC.seg[ch] || []).some(s => sec >= s.beginSec && sec < s.endSec);
const segAt = (ch, sec) => (REC.seg[ch] || []).find(s => sec >= s.beginSec && sec < s.endSec);
const clipUrl = (ch, sec) => `/api/clip?channel=${ch}&start=${encodeURIComponent(REC.date + ' ' + fmtClock(Math.max(0, Math.min(DAY - 1, Math.floor(sec)))))}&duration=${CHUNK}&q=${REC.quality}`;

function seekTo(ch, sec) {
  sec = Math.max(0, Math.min(DAY - 1, Math.floor(sec)));
  if (!hasFootage(ch, sec)) { const n = nextFootage(ch, sec); if (n == null) { setStatus('No footage there'); return; } sec = n; }
  REC.play.ch = ch; REC.play.chunkStart = sec; REC.play.prefetched = null;
  $('#rec-player').classList.remove('hidden');
  spinner(true);
  const v = $('#rec-video');
  // show the moment's thumbnail instantly (often already cached from hover) as a
  // poster so the player isn't blank while the chunk downloads
  v.poster = `/api/thumb?channel=${ch}&time=${encodeURIComponent(REC.date + ' ' + fmtClock(sec))}`;
  v.src = clipUrl(ch, sec);
  v.playbackRate = Number($('#pb-rate').value);
  v.play().catch(() => {});
  $('#pb-play').textContent = '❚❚';
  updateDownloadLink(ch, sec);
  positionPlayhead();
}
function nextFootage(ch, sec) {
  const segs = (REC.seg[ch] || []).filter(s => s.beginSec >= sec).sort((a, b) => a.beginSec - b.beginSec);
  return segs.length ? segs[0].beginSec : null;
}
function updateDownloadLink(ch, sec) {
  const s = segAt(ch, sec), a = $('#pb-dl');
  if (!s) { a.style.visibility = 'hidden'; return; }
  a.style.visibility = '';
  a.href = `/api/playback?file=${encodeURIComponent(s.fileName)}&begin=${encodeURIComponent(s.begin)}`
    + `&end=${encodeURIComponent(s.end)}&size_kb=${s.size_kb}&transcode=0`;
}
function spinner(on) { $('#rec-spinner').classList.toggle('hidden', !on); }
function positionPlayhead(curSec) {
  const ph = $('#tl-playhead');
  if (REC.play.ch == null) { ph.classList.add('hidden'); return; }
  const sec = curSec == null ? REC.play.chunkStart : curSec;
  ph.classList.remove('hidden'); ph.style.left = xOf(sec) + 'px';
}

function bindPlayer() {
  const v = $('#rec-video');
  v.ontimeupdate = () => {
    const cur = REC.play.chunkStart + v.currentTime;
    positionPlayhead(cur);
    $('#pb-time').textContent = `${chName(REC.play.ch)} · ${fmtClock(cur)}`;
    if (v.duration && (v.duration - v.currentTime) < PREFETCH) {
      const next = REC.play.chunkStart + v.duration;
      if (REC.play.prefetched !== next && hasFootage(REC.play.ch, next)) {
        REC.play.prefetched = next;
        // Drain the body: the server only finalises its on-disk clip cache once the
        // response is fully read, so an unconsumed fetch leaves the next chunk cold.
        fetch(clipUrl(REC.play.ch, next)).then(r => r.arrayBuffer()).catch(() => {});
      }
    }
    // keep playhead in view
    const sc = $('#tl-scroll'), x = xOf(cur);
    if (x < sc.scrollLeft || x > sc.scrollLeft + sc.clientWidth) sc.scrollLeft = x - sc.clientWidth / 2;
  };
  v.onended = () => {
    const played = v.duration || 0;
    const next = REC.play.chunkStart + played;
    // Stop at the live edge / a gap instead of rapidly re-requesting: if this
    // chunk was tiny (ran out of footage) or the next moment has no recording,
    // halt rather than loop.
    if (played >= 2 && hasFootage(REC.play.ch, next + 0.5)) seekTo(REC.play.ch, next);
    else { $('#pb-play').textContent = '▶'; setStatus('End of footage'); }
  };
  v.onplay = () => $('#pb-play').textContent = '❚❚';
  v.onpause = () => $('#pb-play').textContent = '▶';
  v.onplaying = () => spinner(false);
  v.onloadeddata = () => spinner(false);
  v.onwaiting = () => spinner(true);

  $('#pb-play').onclick = () => { v.paused ? v.play() : v.pause(); };
  $('#pb-rate').onchange = () => v.playbackRate = Number($('#pb-rate').value);
  $('#pb-back').onclick = () => jump(-10);
  $('#pb-fwd').onclick = () => jump(10);
  $('#pb-prevev').onclick = () => jumpEvent(-1);
  $('#pb-nextev').onclick = () => jumpEvent(1);
  const setQuality = qv => {
    REC.quality = qv;
    try { localStorage.setItem('rec-quality', qv); } catch {}
    $('#pb-hd').classList.toggle('on', qv === 'hd');
    $('#pb-sd').classList.toggle('on', qv === 'sd');
    if (REC.play.ch != null) {   // reload current moment at the new quality
      const at = REC.play.chunkStart + v.currentTime;
      seekTo(REC.play.ch, at);
    }
  };
  $('#pb-hd').classList.toggle('on', REC.quality === 'hd');
  $('#pb-sd').classList.toggle('on', REC.quality === 'sd');
  $('#pb-hd').onclick = () => setQuality('hd');
  $('#pb-sd').onclick = () => setQuality('sd');
  $('#pb-full').onclick = () => { const b = $('.rec-video-box'); (b.requestFullscreen || b.webkitRequestFullscreen).call(b); };
  $('#pb-close').onclick = () => {
    v.pause(); v.removeAttribute('src'); v.load();
    $('#rec-player').classList.add('hidden'); REC.play.ch = null; positionPlayhead();
  };
}
function jump(delta) {
  const v = $('#rec-video'), cur = REC.play.chunkStart + v.currentTime, target = cur + delta;
  if (target >= REC.play.chunkStart && v.duration && target < REC.play.chunkStart + v.duration) v.currentTime += delta;
  else seekTo(REC.play.ch, target);
}
function jumpEvent(dir) {
  if (REC.play.ch == null) return;
  const cur = REC.play.chunkStart + ($('#rec-video').currentTime || 0);
  const evs = REC.events.filter(e => e.channel === REC.play.ch);
  const next = dir > 0
    ? evs.find(e => e.beginSec > cur + 1)
    : [...evs].reverse().find(e => e.beginSec < cur - 1);
  if (next) seekTo(next.channel, next.beginSec); else setStatus('No more events');
}

// --- day nav + zoom ---------------------------------------------------------
function shiftDay(n) { const d = new Date(REC.date + 'T00:00:00'); d.setDate(d.getDate() + n); loadDay(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`); }
function setZoom(z) {
  const sc = $('#tl-scroll'), mid = (sc.scrollLeft + sc.clientWidth / 2) / (REC.fitWidth * REC.zoom);
  REC.zoom = Math.max(1, Math.min(48, z));
  renderTimeline();
  sc.scrollLeft = mid * REC.fitWidth * REC.zoom - sc.clientWidth / 2;
}
function bindRecordingControls() {
  $('#rec-prev').onclick = () => shiftDay(-1);
  $('#rec-next').onclick = () => shiftDay(1);
  $('#rec-today').onclick = () => loadDay(todayISO());
  $('#rec-date').onchange = e => loadDay(e.target.value);
  $('#rec-zoomin').onclick = () => setZoom(REC.zoom * 1.8);
  $('#rec-zoomout').onclick = () => setZoom(REC.zoom / 1.8);
  $('#rec-fit').onclick = () => setZoom(1);
  $('#tl-scroll').addEventListener('wheel', e => {
    if (!e.ctrlKey && !e.shiftKey) return;        // ctrl/shift+wheel = zoom
    e.preventDefault(); setZoom(REC.zoom * (e.deltaY < 0 ? 1.2 : 1 / 1.2));
  }, { passive: false });
  window.addEventListener('resize', () => { if ($('#recordings').classList.contains('active')) renderTimeline(); });
}

// --- boot -------------------------------------------------------------------
let statusTimer;
function setStatus(msg) {
  const e = $('#status'); e.textContent = msg;
  clearTimeout(statusTimer); statusTimer = setTimeout(() => (e.textContent = ''), 4000);
}

async function boot() {
  try {
    const h = await api('/health');
    CHANNELS = h.channels;
    setStatus(`NVR ${h.nvr}`);
  } catch (e) {
    setStatus('Cannot reach backend: ' + e.message);
    return;
  }
  renderGrid();
  // recordings timeline: wire controls and default to today (rendered on show)
  REC.date = todayISO();
  $('#rec-date').value = REC.date;
  bindRecordingControls();
  bindPlayer();
}
boot();
