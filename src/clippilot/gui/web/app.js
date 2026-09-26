/* ClipPilot desktop UI - talks to the local FastAPI backend over SSE. */
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  settings: null,
  busy: false,
  screen: [0, 0, 1920, 1080],
  elements: [],
  showBoxes: true,
  segments: [],
  inputPath: '',
  mode: 'highlights',
  res: '1080x1920',
  lastShot: 0,
  stopTimer: null,
};

const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
};

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};

const stamp = (t) => new Date((t || Date.now() / 1000) * 1000).toLocaleTimeString([], { hour12: false });

/* ─────────────────────────── tabs ─────────────────────────── */
$$('.tab').forEach((tab) => {
  tab.onclick = () => {
    $$('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    $$('.panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === tab.dataset.tab));
    if (tab.dataset.tab === 'agent') refreshScreen();
  };
});

/* ─────────────────────────── status / log ─────────────────────────── */
function setStatus(text, cls) {
  $('#statusText').textContent = text;
  const dot = $('#statusDot');
  dot.className = 'dot' + (cls ? ' ' + cls : '');
}

function addLog(text, level = 'info') {
  const box = $('#logList');
  const line = el('div', level);
  line.innerHTML = `<span class="t">${stamp()}</span> ${escapeHtml(text)}`;
  box.appendChild(line);
  while (box.childElementCount > 250) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

const escapeHtml = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

/* ─────────────────────────── chat ─────────────────────────── */
function addMsg(role, text, cls = '') {
  $('#chatEmpty')?.remove();
  const chat = $('#chat');
  const wrap = el('div', 'msg ' + role + (cls ? ' ' + cls : ''));
  wrap.appendChild(el('div', 'who', role === 'user' ? 'you' : role === 'assistant' ? 'clippilot' : 'system'));
  wrap.appendChild(el('div', 'bubble', text || '(no output)'));
  chat.appendChild(wrap);
  chat.scrollTop = chat.scrollHeight;
  return wrap;
}

function addStep(payload) {
  const a = payload.action || {};
  const node = el('div', 'step');
  const head = el('div', 'head');
  head.appendChild(el('span', 'n', 'step ' + payload.index));
  head.appendChild(el('span', 'kind', a.type || '?'));
  const res = el('span', 'res', 'running…');
  head.appendChild(res);
  node.appendChild(head);
  if (payload.thought) node.appendChild(el('div', 'thought', payload.thought));
  if (a.plan) {
    const d = el('div', 'detail', `plan: ${(a.plan.segments || []).length} segment(s) -> ${a.plan.output}`);
    node.appendChild(d);
  } else if (Object.keys(a).length > 1) {
    node.appendChild(el('div', 'detail', brief(a)));
  }
  node.dataset.step = payload.index;
  $('#chat').appendChild(node);
  $('#chat').scrollTop = $('#chat').scrollHeight;
  return node;
}

function brief(a) {
  const t = a.type;
  if (t === 'click') return `click ${a.text || a.element_id || `${a.x},${a.y}`}`;
  if (t === 'type') return `type "${String(a.text || '').slice(0, 70)}"`;
  if (t === 'key') return `key ${a.keys}`;
  if (t === 'wait') return `wait ${a.seconds}s`;
  if (t === 'scroll') return `scroll ${a.amount}`;
  if (t === 'drag') return `drag -> ${JSON.stringify(a.to)}`;
  if (t === 'launch_app') return `launch ${a.app}`;
  if (t === 'focus_window') return `focus "${a.title_contains}"`;
  if (t === 'assert') return `assert ${a.text_present || a.statement || ''}`;
  if (t === 'plan_video') return `analyse ${a.mode} ${a.input}`;
  if (t === 'handoff') return `handoff -> ${a.app}`;
  if (t === 'task_done') return 'done';
  if (t === 'give_up') return 'give up';
  if (t === 'ask_human') return 'asks the human';
  return t;
}

function finishStep(payload) {
  const node = $(`.step[data-step="${payload.step}"] .res`);
  if (node) {
    node.textContent = payload.ok ? 'ok' : 'failed';
    node.className = 'res ' + (payload.ok ? 'ok' : 'bad');
  }
  const parent = $(`.step[data-step="${payload.step}"]`);
  if (parent && payload.message) {
    const d = el('div', 'detail', payload.message.slice(0, 400));
    parent.appendChild(d);
  }
  $('#chat').scrollTop = $('#chat').scrollHeight;
}

/* ─────────────────────────── live screen ─────────────────────────── */
async function refreshScreen(force = false) {
  const now = Date.now();
  if (!force && now - state.lastShot < 900) return;
  state.lastShot = now;
  const img = $('#screenImg');
  try {
    const data = await fetch(`/api/screenshot?t=${now}&w=1000&q=62`);
    if (!data.ok) return;
    const err = data.headers.get('X-Error');
    if (err) { $('#screenMeta').textContent = 'capture failed: ' + err; return; }
    const blob = await data.blob();
    const old = img.dataset.url;
    if (old) URL.revokeObjectURL(old);
    const url = URL.createObjectURL(blob);
    img.dataset.url = url;
    img.src = url;
  } catch (e) { /* server restarting */ }
}

async function inspect() {
  try {
    const data = await api('/api/inspect');
    state.elements = data.elements || [];
    state.screen = data.screen || state.screen;
    renderBoxes();
    renderElements();
    $('#screenMeta').textContent = `${data.window || 'unknown window'} · ${data.ocr} · ${state.elements.length} elements`;
  } catch (e) {
    addLog('inspect failed: ' + e.message, 'error');
  }
}

function renderBoxes() {
  const layer = $('#boxLayer');
  layer.innerHTML = '';
  if (!state.showBoxes) return;
  const img = $('#screenImg');
  if (!img.naturalWidth) return;
  const rect = img.getBoundingClientRect();
  const wrapRect = $('#screenWrap').getBoundingClientRect();
  const offX = rect.left - wrapRect.left;
  const offY = rect.top - wrapRect.top;
  const [sx, sy, ex, ey] = state.screen;
  const sw = ex - sx, sh = ey - sy;
  const SVG = 'http://www.w3.org/2000/svg';

  state.elements.slice(0, 90).forEach((e) => {
    if (!e.box) return;
    const x = offX + ((e.box[0] - sx) / sw) * rect.width;
    const y = offY + ((e.box[1] - sy) / sh) * rect.height;
    const w = ((e.box[2] - e.box[0]) / sw) * rect.width;
    const h = ((e.box[3] - e.box[1]) / sh) * rect.height;
    if (w < 6 || h < 6) return;
    const r = document.createElementNS(SVG, 'rect');
    r.setAttribute('x', x); r.setAttribute('y', y);
    r.setAttribute('width', w); r.setAttribute('height', h);
    r.setAttribute('rx', '2');
    layer.appendChild(r);
    const label = (e.id || '') + ' ' + (e.text || '').slice(0, 18);
    if (label.trim() && w > 40) {
      const t = document.createElementNS(SVG, 'text');
      t.setAttribute('x', x + 2); t.setAttribute('y', Math.max(8, y - 2));
      t.setAttribute('fill', '#4cc9f0');
      t.setAttribute('style', 'font:700 8px Segoe UI, sans-serif');
      t.textContent = label;
      layer.appendChild(t);
    }
  });
}

function renderElements() {
  const list = $('#elementList');
  list.innerHTML = '';
  state.elements.slice(0, 120).forEach((e) => {
    const chip = el('div', 'el');
    chip.innerHTML = `<b>${e.id}</b> ${escapeHtml((e.text || e.kind || '').slice(0, 40))}`;
    chip.title = `click to paste "${e.text}" into the task box`;
    chip.onclick = () => {
      const input = $('#taskInput');
      input.value = (input.value ? input.value.replace(/\s*$/, ' ') : '') + `click ${e.text || e.id}`;
      input.focus();
    };
    list.appendChild(chip);
  });
}

$('#screenImg').onload = renderBoxes;
$('#btnSnap').onclick = () => refreshScreen(true);
$('#btnInspect').onclick = inspect;
$('#optBoxes').onchange = (e) => { state.showBoxes = e.target.checked; renderBoxes(); };

/* ─────────────────────────── run a task ─────────────────────────── */
async function runTask() {
  const task = $('#taskInput').value.trim();
  if (!task || state.busy) return;
  setBusy(true);
  addMsg('user', task);
  $('#taskInput').value = '';
  try {
    await api('/api/run', {
      method: 'POST',
      body: {
        task,
        dry_run: $('#optDry').checked,
        require_confirmation: $('#optConfirm').checked,
        vlm_verify: $('#optVerify').checked,
        max_steps: parseInt($('#optSteps').value || '40', 10),
        monitor: $('#optMonitor').value,
        handoff_app: state.settings?.handoff?.default_app || '',
      },
    });
  } catch (e) {
    addMsg('assistant', 'could not start: ' + e.message, 'err');
    setBusy(false);
  }
}

$('#btnRun').onclick = runTask;
$('#taskInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); runTask(); }
});
$$('.qtask').forEach((b) => {
  b.onclick = () => { $('#taskInput').value = b.dataset.q; $('#taskInput').focus(); };
});

function setBusy(v) {
  state.busy = v;
  $('#btnRun').disabled = v;
  $('#btnStop').disabled = !v;
  setStatus(v ? 'working' : 'idle', v ? 'busy' : '');
  if (v) refreshScreen(true);
}

/* ─────────────────────────── modal ─────────────────────────── */
function showModal(title, bodyHtml, actions) {
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = bodyHtml;
  const box = $('#modalActions');
  box.innerHTML = '';
  actions.forEach((a) => {
    const b = el('button', a.primary ? 'primary' : '', a.label);
    b.onclick = async () => {
      if (a.value !== undefined) await a.value();
      if (a.close !== false) $('#modal').classList.add('hidden');
    };
    box.appendChild(b);
  });
  $('#modal').classList.remove('hidden');
}

async function onConfirm(payload) {
  showModal('Approval needed', `
    <p>${escapeHtml(payload.reason || 'the agent wants to do something sensitive')}</p>
    <pre>${escapeHtml(JSON.stringify(payload.action, null, 2))}</pre>`,
  [
    { label: 'Decline', value: () => api('/api/confirm', { method: 'POST', body: { approved: false } }) },
    { label: 'Approve once', primary: true, value: () => api('/api/confirm', { method: 'POST', body: { approved: true } }) },
  ]);
}

async function onAsk(payload) {
  const opts = (payload.options || []).map((o) => `<button class="mini" data-opt="${escapeHtml(o)}">${escapeHtml(o)}</button>`).join(' ');
  showModal('The agent asked a question', `
    ${payload.why ? `<p style="color:var(--dimmer)">${escapeHtml(payload.why)}</p>` : ''}
    <p style="color:var(--text);font-size:14px">${escapeHtml(payload.question)}</p>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0">${opts}</div>
    <input id="askInput" placeholder="type your answer…">`,
  [
    { label: 'Skip', value: () => api('/api/answer', { method: 'POST', body: { answer: '' } }) },
    { label: 'Send', primary: true, value: () => api('/api/answer', { method: 'POST', body: { answer: ($('#askInput') || {}).value || '' } }) },
  ]);
  $$('#modalBody [data-opt]').forEach((b) => {
    b.onclick = () => {
      const input = $('#askInput');
      if (input) input.value = b.dataset.opt;
    };
  });
  setTimeout(() => $('#askInput')?.focus(), 60);
}

/* ─────────────────────────── editor tab ─────────────────────────── */
$$('#edMode .seg').forEach((b) => {
  b.onclick = () => {
    $$('#edMode .seg').forEach((x) => x.classList.toggle('active', x === b));
    state.mode = b.dataset.mode;
  };
});
$$('#edRes .seg').forEach((b) => {
  b.onclick = () => {
    $$('#edRes .seg').forEach((x) => x.classList.toggle('active', x === b));
    state.res = b.dataset.res;
  };
});
$$('[data-browse]').forEach((b) => {
  b.onclick = async () => {
    const kind = b.dataset.browse;
    const target = b.closest('.row').querySelector('input');
    try {
      const r = await api(`/api/browse?kind=${kind}`);
      if (r.ok && r.path) {
        target.value = r.path;
        if (target.id === 'edInput') { state.inputPath = r.path; probeInput(); }
      }
    } catch (e) { addLog('picker failed: ' + e.message, 'error'); }
  };
});

async function probeInput() {
  const path = $('#edInput').value.trim();
  if (!path) return;
  state.inputPath = path;
  const r = await api('/api/probe?path=' + encodeURIComponent(path));
  const info = $('#edInfo');
  if (r.ok) {
    info.textContent = `${r.width}x${r.height} · ${r.duration.toFixed(1)}s · ${r.fps.toFixed(2)}fps · ${r.orientation} · audio:${r.has_audio ? r.audio_codec : 'none'}`;
    if (state.res === 'source') $('#edRes [data-res="source"]').click();
    if (!$('#edOutput').value) {
      const dot = path.match(/\.[^.]+$/)?.[0] || '.mp4';
      $('#edOutput').placeholder = path.replace(dot, '_clippilot' + dot);
    }
  } else {
    info.textContent = 'cannot read: ' + r.error;
    info.style.color = 'var(--bad)';
  }
}
$('#edInput').onchange = probeInput;
$('#edInput').addEventListener('blur', probeInput);

$('#btnAnalyse').onclick = async () => {
  const path = $('#edInput').value.trim();
  if (!path) { addLog('pick an input first', 'warn'); return; }
  $('#edSegments').innerHTML = '<div class="info-line">analysing…</div>';
  try {
    await api('/api/analyse', { method: 'POST', body: { inputs: [path], mode: state.mode === 'whole' ? 'highlights' : state.mode, top_n: parseInt($('#edTopN').value || '5', 10), max_seconds: parseFloat($('#edMax').value || '0') } });
  } catch (e) {
    $('#edSegments').innerHTML = `<div class="info-line" style="color:var(--bad)">${escapeHtml(e.message)}</div>`;
  }
};

function renderSegments(segs, label) {
  const box = $('#edSegments');
  box.innerHTML = '';
  if (!segs.length) { box.innerHTML = '<div class="info-line">no segments yet</div>'; return; }
  const head = el('div', 'info-line', label + ': ' + segs.length + ' range(s)');
  box.appendChild(head);
  segs.forEach((s, i) => {
    const row = el('div', 'seg-row');
    row.innerHTML = `<span>${String(i + 1).padStart(2, '0')}</span><span class="t">${s.start.toFixed(2)}s</span><span>→</span><span>${s.end.toFixed(2)}s</span><span class="f">${(s.end - s.start).toFixed(2)}s</span>`;
    box.appendChild(row);
  });
}

$('#btnRender').onclick = async () => {
  const path = $('#edInput').value.trim();
  if (!path) { addLog('pick an input first', 'warn'); return; }
  const body = {
    inputs: [path],
    mode: state.mode,
    resolution: state.res === 'source' ? (await api('/api/probe?path=' + encodeURIComponent(path))).ok ? `${(await api('/api/probe?path=' + encodeURIComponent(path))).width}x${(await api('/api/probe?path=' + encodeURIComponent(path))).height}` : '1080x1920' : state.res,
    fps: parseInt($('#edFps').value, 10),
    crf: parseInt($('#edCrf').value, 10),
    transitions: $('#edTrans').value,
    music: $('#edMusic').value.trim(),
    music_gain_db: parseFloat($('#edGain').value || '-18'),
    duck: $('#edDuck').checked,
    captions: $('#edCaptions').checked,
    burn_captions: !$('#edSoftCaps').checked,
    normalize: $('#edNorm').value,
    top_n: parseInt($('#edTopN').value || '5', 10),
    max_seconds: parseFloat($('#edMax').value || '0'),
    overwrite: true,
    also_handoff: $('#edHandoff').checked,
    handoff_app: $('#edHandoffApp').value || 'capcut',
  };
  const out = $('#edOutput').value.trim();
  if (out) body.output = out;
  $('#edResult').className = 'result';
  $('#edResult').textContent = 'starting…';
  $('#edProgress').classList.add('on');
  $('.bar', $('#edProgress')).style.width = '2%';
  try {
    await api('/api/edit', { method: 'POST', body });
  } catch (e) {
    $('#edResult').className = 'result err';
    $('#edResult').textContent = e.message;
    $('#edProgress').classList.remove('on');
  }
};

/* ─────────────────────────── handoff tab ─────────────────────────── */
$('#btnHandoff').onclick = async () => {
  const app = $('#hoApp').value;
  const project = $('#hoProject').value.trim();
  if (!project) { addLog('pick a file to import', 'warn'); return; }
  $('#hoResult').className = 'result';
  $('#hoResult').textContent = 'launching ' + app + '…';
  try {
    await api('/api/handoff', { method: 'POST', body: { app, project, notes: $('#hoNotes').value } });
  } catch (e) {
    $('#hoResult').className = 'result err';
    $('#hoResult').textContent = e.message;
  }
};

$('#hoApp').onchange = () => {
  const r = (state.settings?.apps || []).find((a) => a.key === $('#hoApp').value);
  if (!r) return;
  const steps = r.steps.map((s) => {
    const label = Object.entries(s).filter(([k]) => !k.startsWith('_') && k !== 'type')
      .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`).join(' ');
    return `  ${s.type}${s._optional ? ' (optional)' : ''} ${label}`;
  }).join('\n');
  $('#hoRecipe').innerHTML = `<b>${escapeHtml(r.name)}</b> — ${escapeHtml(r.description)}\n${escapeHtml(steps)}` +
    (r.tips?.length ? '\n' + r.tips.map((t) => '  tip: ' + escapeHtml(t)).join('\n') : '');
};

/* ─────────────────────────── settings tab ─────────────────────────── */
$('#btnSaveSettings').onclick = async () => {
  const body = {
    llm_base_url: $('#setBaseUrl').value.trim(),
    llm_model: $('#setModel').value.trim(),
    ocr_backend: $('#setOcr').value,
  };
  if ($('#setApiKey').value.trim()) body.llm_api_key = $('#setApiKey').value.trim();
  try {
    await api('/api/settings', { method: 'POST', body });
    const saved = await api('/api/settings/save', { method: 'POST' });
    addLog('settings saved to ' + saved.path, 'good');
    $('#setApiKey').value = '';
    await loadSettings();
    await doctor();
  } catch (e) { addLog('save failed: ' + e.message, 'error'); }
};

$('#btnTestLlm').onclick = async () => {
  const out = $('#llmTestOut');
  out.textContent = 'testing…';
  await api('/api/settings', { method: 'POST', body: { llm_base_url: $('#setBaseUrl').value.trim(), llm_model: $('#setModel').value.trim(), llm_api_key: $('#setApiKey').value.trim() || undefined } }).catch(() => {});
  const r = await api('/api/test/llm', { method: 'POST' });
  out.textContent = r.ok ? `ok — ${r.model} replied "${r.reply}"` : 'failed: ' + r.error;
  out.style.color = r.ok ? 'var(--good)' : 'var(--bad)';
};

$('#btnDoctor').onclick = doctor;
$('#btnRevealRuns').onclick = async () => {
  const r = await api('/api/paths');
  const runDir = state.runDir || r.cwd + '/runs';
  await api('/api/browse?kind=folder&start=' + encodeURIComponent(runDir));
};

async function doctor() {
  const box = $('#doctorList');
  box.innerHTML = '<div class="info-line">checking…</div>';
  try {
    const r = await api('/api/doctor');
    box.innerHTML = '';
    r.checks.forEach((c) => {
      const row = el('div', 'doc ' + (c.ok ? 'ok' : 'bad'));
      row.innerHTML = `<span class="m">${c.ok ? '✔' : '✖'}</span><span class="n">${escapeHtml(c.name)}</span><span class="d">${escapeHtml(String(c.detail || '').slice(0, 120))}</span>`;
      box.appendChild(row);
    });
  } catch (e) {
    box.innerHTML = `<div class="info-line" style="color:var(--bad)">${escapeHtml(e.message)}</div>`;
  }
}

/* ─────────────────────────── live events ─────────────────────────── */
function connect() {
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    handle(msg);
  };
  es.onerror = () => {
    setStatus('reconnecting…', 'err');
    setTimeout(connect, 2000);
  };
}

function handle(msg) {
  const k = msg.kind;
  if (k === 'hello') { setStatus('connected'); return; }
  if (k === 'log') { addLog(msg.text, msg.level); return; }
  if (k === 'observation') { refreshScreen(); if (msg.elements) { state.elements = msg.elements; renderBoxes(); renderElements(); } return; }
  if (k === 'step') { addStep(msg); return; }
  if (k === 'step_result') { finishStep(msg); return; }
  if (k === 'progress') {
    const pct = Math.round((msg.pct || 0) * 100);
    $('#edProgress').classList.add('on');
    $('.bar', $('#edProgress')).style.width = pct + '%';
    $('span', $('#edProgress')).textContent = `${pct}% ${msg.message || ''}`;
    return;
  }
  if (k === 'render_done') {
    const v = msg.outputs?.video;
    $('#edProgress').classList.remove('on');
    const files = Object.entries(msg.outputs || {}).filter(([k2, v2]) => v2 && typeof v2 === 'string' && k2 !== 'info');
    $('#edResult').className = 'result ok';
    $('#edResult').innerHTML = 'done in ' + (msg.elapsed ?? '?') + 's\n' + files.map(([k2, v2]) => `${k2}: ${escapeHtml(v2)}`).join('\n') +
      (v ? `\n<button class="mini" data-open="${escapeHtml(v)}" style="margin-top:6px">open folder</button>` : '');
    $$('#edResult [data-open]').forEach((b) => { b.onclick = () => api('/api/browse?kind=folder&start=' + encodeURIComponent(b.dataset.open.replace(/[\\\/][^\\\/]*$/, ''))); });
    addLog('render finished: ' + v, 'good');
    return;
  }
  if (k === 'render_error') {
    $('#edProgress').classList.remove('on');
    $('#edResult').className = 'result err';
    $('#edResult').textContent = msg.message;
    return;
  }
  if (k === 'analysis') {
    if (msg.ok && msg.data?.segments) { renderSegments(msg.data.segments, 'found'); }
    else { $('#edSegments').innerHTML = `<div class="info-line" style="color:var(--bad)">${escapeHtml(msg.message || 'failed')}</div>`; }
    return;
  }
  if (k === 'handoff_result') {
    $('#hoResult').className = 'result ' + (msg.ok ? 'ok' : 'err');
    $('#hoResult').textContent = msg.summary || '';
    return;
  }
  if (k === 'confirm') { onConfirm(msg); return; }
  if (k === 'ask') { onAsk(msg); return; }
  if (k === 'run_finished') {
    addMsg('assistant', msg.summary || (msg.success ? 'done' : 'stopped'), msg.success ? 'ok' : 'err');
    setStatus(msg.success ? 'done' : 'stopped', msg.success ? 'ok' : 'err');
    if (msg.run_dir) { state.runDir = msg.run_dir; $('#runDirLabel').textContent = msg.run_dir; }
    setBusy(false);
    return;
  }
  if (k === 'idle') { setBusy(false); return; }
  if (k === 'abort') { addLog('agent aborted', 'warn'); setBusy(false); }
}

$('#btnStop').onclick = async () => {
  await api('/api/stop', { method: 'POST' });
  setStatus('stopping…', 'err');
  setTimeout(() => setBusy(false), 1200);
};

/* ─────────────────────────── boot ─────────────────────────── */
async function loadSettings() {
  const s = await api('/api/settings');
  state.settings = s;
  $('#brandSub').textContent = `v${s.version} · ${(s.llm.model || 'no model')}`;
  $('#setBaseUrl').value = s.llm.base_url || '';
  $('#setModel').value = s.llm.model || '';
  $('#setOcr').value = s.perception.ocr_backend || 'auto';
  $('#setApiKey').placeholder = s.llm.api_key_set ? '•••••• (set)' : 'sk-…';
  $('#optDry').checked = !!s.control.dry_run;
  $('#optConfirm').checked = s.safety.require_confirmation !== false;
  $('#optVerify').checked = !!s.agent.vlm_verify;
  $('#optSteps').value = s.agent.max_steps || 40;

  const mon = $('#optMonitor');
  mon.innerHTML = '';
  (s.monitors || []).forEach((m) => {
    const o = document.createElement('option');
    o.value = m.name;
    o.textContent = `${m.name} (${m.box[2] - m.box[0]}x${m.box[3] - m.box[1]})`;
    mon.appendChild(o);
  });
  if (s.perception.capture) mon.value = s.perception.capture;

  const fill = (sel, apps, selected) => {
    sel.innerHTML = '';
    apps.forEach((a) => {
      const o = document.createElement('option');
      o.value = a.key;
      o.textContent = a.name;
      sel.appendChild(o);
    });
    if (selected && apps.some((a) => a.key === selected)) sel.value = selected;
  };
  fill($('#edHandoffApp'), s.apps || [], s.handoff?.default_app);
  fill($('#hoApp'), s.apps || [], s.handoff?.default_app);
  $('#hoApp').onchange();
}

(async function boot() {
  try {
    await loadSettings();
    await doctor();
    connect();
    refreshScreen(true);
    setInterval(() => { if (state.busy) refreshScreen(); }, 1400);
    const st = await api('/api/state');
    (st.logs || []).slice(-40).forEach((l) => addLog(l.text, l.level));
    if (st.run_dir) { state.runDir = st.run_dir; $('#runDirLabel').textContent = st.run_dir; }
    (st.history || []).forEach((h) => addMsg(h.role, h.text, h.success === false ? 'err' : ''));
  } catch (e) {
    addLog('startup failed: ' + e.message, 'error');
    setStatus('backend error', 'err');
  }
})();
