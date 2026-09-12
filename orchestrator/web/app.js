/* Claude Code Remote - phone client.
   Type a prompt, POST it to the orchestrator, poll the session transcript. */
'use strict';

const $ = (s) => document.querySelector(s);
const S = {
  base: localStorage.getItem('ccr_base') || location.origin,
  token: localStorage.getItem('ccr_token') || '',
  pinned: [], profiles: [], project: null, profile: null,
  models: [], efforts: [], modes: [], model: null, effort: null, mode: null,
  root: '', here: null, entries: [],
  sid: null, lastSeq: 0, session: null,
  busy: false, timer: null, tools: new Map(),
};

/* ---------- one conversation per folder ----------
   Picking a folder should feel like walking back into the room you left, not
   starting over. The map is per folder path, so a folder you return to a week
   later resumes where it stopped. */
const SIDS = 'ccr_sids';
function sidMap() {
  try { return JSON.parse(localStorage.getItem(SIDS) || '{}'); } catch (_) { return {}; }
}
function rememberSid(dir, sid) {
  if (!dir) return;
  const m = sidMap();
  if (sid) m[dir] = sid; else delete m[dir];
  try { localStorage.setItem(SIDS, JSON.stringify(m)); } catch (_) {}
}
function sidFor(dir) { return sidMap()[dir] || null; }

/* ---------- api ---------- */
async function api(path, opts = {}) {
  const res = await fetch(S.base.replace(/\/$/, '') + path, {
    ...opts,
    headers: { 'X-Token': S.token, 'Content-Type': 'application/json', ...(opts.headers || {}) },
  });
  if (res.status === 401) { toast('Bad or missing token'); openSheet('#sheet-settings'); throw new Error('401'); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------- markdown: escape first, then a small safe subset ---------- */
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function inline(s) {
  return esc(s)
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
}

function blocks(text) {
  let html = '', list = null, para = [];
  const flushPara = () => { if (para.length) { html += `<p>${para.join('<br>')}</p>`; para = []; } };
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };

  for (const raw of text.split(/\r?\n/)) {
    const t = raw.trim();
    if (!t) { flushPara(); closeList(); continue; }
    let m;
    if ((m = t.match(/^(#{1,6})\s+(.*)$/))) {
      flushPara(); closeList();
      const h = Math.min(m[1].length + 2, 6);
      html += `<h${h}>${inline(m[2])}</h${h}>`; continue;
    }
    if ((m = t.match(/^[-*+]\s+(.*)$/))) {
      flushPara(); if (list !== 'ul') { closeList(); html += '<ul>'; list = 'ul'; }
      html += `<li>${inline(m[1])}</li>`; continue;
    }
    if ((m = t.match(/^\d+[.)]\s+(.*)$/))) {
      flushPara(); if (list !== 'ol') { closeList(); html += '<ol>'; list = 'ol'; }
      html += `<li>${inline(m[1])}</li>`; continue;
    }
    closeList(); para.push(inline(t));
  }
  flushPara(); closeList();
  return html;
}

function md(src) {
  // Odd-indexed chunks are fenced code; even-indexed chunks are prose.
  return String(src).split('```').map((part, i) => {
    if (i % 2 === 0) return blocks(part);
    const nl = part.indexOf('\n');
    const code = nl >= 0 ? part.slice(nl + 1) : part;
    return `<pre><code>${esc(code.replace(/\n+$/, ''))}</code></pre>`;
  }).join('');
}

/* ---------- thread rendering ---------- */
const thread = $('#thread');

function nearBottom() {
  return thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;
}
function toBottom(force) {
  if (force || nearBottom()) requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
}
function emptyState() {
  const d = document.createElement('div');
  d.className = 'empty';
  d.innerHTML = '<div class="empty-mark">&gt;_</div><h1>Talk to Claude Code</h1>'
    + '<p>Your prompt runs in the folder you pick, on your server, in a real Claude&nbsp;Code session.</p>';
  return d;
}
function clearThread() {
  thread.innerHTML = '';
  thread.appendChild(emptyState());
  S.tools.clear(); S.lastSeq = 0;
}
function dropEmpty() { const e = thread.querySelector('.empty'); if (e) e.remove(); }

function fmtDur(ms) {
  if (!ms) return '';
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

function build(m) {
  let node = null;

  if (m.role === 'user') {
    node = document.createElement('div');
    node.className = 'msg user';
    node.textContent = m.text;
  } else if (m.role === 'assistant') {
    node = document.createElement('div');
    node.className = 'msg assistant';
    node.innerHTML = md(m.text);
  } else if (m.role === 'thinking') {
    node = document.createElement('details');
    node.className = 'row think';
    node.innerHTML = '<summary><span class="tag">think</span><span class="line">reasoning</span></summary>'
      + '<div class="body"></div>';
    node.querySelector('.body').textContent = m.text;
  } else if (m.role === 'tool') {
    node = document.createElement('details');
    node.className = 'row';
    node.innerHTML = '<summary><span class="tag"></span><span class="line"></span></summary>';
    node.querySelector('.tag').textContent = m.meta.name || 'tool';
    node.querySelector('.line').textContent = m.text.replace(/^[^:]+:\s*/, '');
    if (m.meta.tool_use_id) S.tools.set(m.meta.tool_use_id, node);
  } else if (m.role === 'tool_result') {
    const host = m.meta.tool_use_id && S.tools.get(m.meta.tool_use_id);
    const body = document.createElement('div');
    body.className = 'body';
    body.textContent = m.text || '(no output)';
    if (host) {
      host.appendChild(body);
      if (m.meta.is_error) host.classList.add('err');
      return null;
    }
    node = document.createElement('details');
    node.className = 'row' + (m.meta.is_error ? ' err' : '');
    node.innerHTML = '<summary><span class="tag">result</span><span class="line">output</span></summary>';
    node.appendChild(body);
  } else if (m.role === 'result') {
    node = document.createElement('div');
    node.className = 'stamp';
    const bits = [];
    bits.push(m.meta.is_error ? '<span class="bad">failed</span>' : '<span class="ok">done</span>');
    if (m.meta.cost_usd != null) bits.push(`$${Number(m.meta.cost_usd).toFixed(3)}`);
    if (m.meta.duration_ms) bits.push(fmtDur(m.meta.duration_ms));
    if (m.meta.num_turns) bits.push(`${m.meta.num_turns} turns`);
    const denials = (m.meta.permission_denials || []).length;
    if (denials) bits.push(`<span class="bad">${denials} denied</span>`);
    node.innerHTML = bits.join(' <span>&middot;</span> ');
  } else if (m.role === 'error') {
    node = document.createElement('div');
    node.className = 'msg error';
    node.textContent = m.text;
  } else {
    node = document.createElement('div');
    node.className = 'msg system';
    node.textContent = m.text;
  }
  return node;
}

function append(messages) {
  dropEmpty();
  for (const m of messages) {
    const node = build(m);
    if (node) thread.appendChild(node);
  }
}

/* ---------- polling ---------- */
function schedule() {
  clearTimeout(S.timer);
  S.timer = setTimeout(poll, S.busy ? 1000 : 4000);
}

async function poll() {
  if (!S.sid || !S.token) { schedule(); return; }
  try {
    const r = await api(`/api/sessions/${S.sid}/messages?after=${S.lastSeq}`);
    S.session = r.session;
    S.busy = !!r.session.busy || ['running', 'queued'].includes(r.session.status);
    if (r.messages.length) {
      document.querySelectorAll('.pending').forEach((e) => e.remove());
      const stick = nearBottom();
      append(r.messages);
      S.lastSeq = r.messages[r.messages.length - 1].seq;
      toBottom(stick);
    }
    paintStatus();
  } catch (e) { /* keep polling through transient network loss */ }
  schedule();
}

function paintStatus() {
  const bar = $('#statusbar');
  if (S.busy) {
    bar.hidden = false;
    $('#status-text').textContent = S.session && S.session.status === 'queued' ? 'Queued…' : 'Working…';
  } else {
    bar.hidden = true;
  }
  $('#btn-send').disabled = !$('#input').value.trim();
}

/* ---------- actions ---------- */
async function send() {
  const input = $('#input');
  const text = input.value;
  if (!text.trim()) return;
  if (!S.project) { toast('Pick a folder first'); openSheet('#sheet-projects'); return; }
  input.value = ''; autogrow(); paintStatus();

  dropEmpty();
  const pending = document.createElement('div');
  pending.className = 'msg user pending';
  pending.textContent = text;
  thread.appendChild(pending);
  toBottom(true);

  try {
    if (!S.sid) {
      const r = await api('/api/sessions', {
        method: 'POST',
        body: JSON.stringify(Object.assign(
          S.project.pinned
            ? { project: S.project.key, prompt: text, profile: S.profile }
            : { dir: S.project.path, prompt: text, profile: S.profile },
          S.model ? { model: S.model } : {},
          S.effort ? { effort: S.effort } : {},
          S.mode ? { permission_mode: S.mode } : {})),
      });
      S.sid = r.session_id;
      S.lastSeq = 0;
      S.tools.clear();
      localStorage.setItem('ccr_sid', S.sid);
      rememberSid(S.project.path, S.sid);
    } else {
      await api(`/api/sessions/${S.sid}/messages`, {
        method: 'POST', body: JSON.stringify({ prompt: text }),
      });
    }
    S.busy = true; paintStatus();
    poll();
  } catch (e) {
    pending.remove();
    input.value = text; autogrow(); paintStatus();
    toast(e.message || 'Send failed');
  }
}

async function openSession(sid) {
  S.sid = sid; S.lastSeq = 0; S.tools.clear();
  localStorage.setItem('ccr_sid', sid);
  thread.innerHTML = '';
  const r = await api(`/api/sessions/${sid}/messages?after=0`);
  S.session = r.session;
  S.busy = !!r.session.busy || ['running', 'queued'].includes(r.session.status);
  S.project = { name: r.session.project_name, path: r.session.cwd,
                key: r.session.project, pinned: !!S.pinned.find((x) => x.key === r.session.project) };
  S.profile = r.session.profile || S.profile;
  S.model = r.session.model || null;
  S.effort = r.session.effort || null;
  S.mode = r.session.permission_mode || null;
  $('#project-name').textContent = S.project.name;
  rememberSid(S.project.path, sid);
  if (r.messages.length) {
    append(r.messages);
    S.lastSeq = r.messages[r.messages.length - 1].seq;
  } else { thread.appendChild(emptyState()); }
  toBottom(true); paintStatus(); schedule();
}

function newSession() {
  // Deliberately forgets this folder's conversation: "+" means start over here.
  if (S.project) rememberSid(S.project.path, null);
  S.sid = null; S.session = null; S.busy = false;
  localStorage.removeItem('ccr_sid');
  clearThread(); paintStatus();
  $('#input').focus();
}

/* ---------- sheets ---------- */
function openSheet(sel) {
  $('#scrim').hidden = false;
  $(sel).hidden = false;
  if (sel === '#sheet-sessions') loadSessions();
  if (sel === '#sheet-projects') { paintProfiles(); browse(S.project ? S.project.path : ''); }
}
function closeSheets() {
  $('#scrim').hidden = true;
  document.querySelectorAll('.sheet').forEach((s) => { s.hidden = true; });
}

const FOLDER_SVG = '<svg class="folder-ico" viewBox="0 0 24 24">'
  + '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
const UP_SVG = '<svg class="folder-ico" viewBox="0 0 24 24">'
  + '<path d="M19 12H5M11 6l-6 6 6 6"/></svg>';

function paintCrumbs() {
  const box = $('#fb-crumbs');
  box.innerHTML = '';
  if (!S.here) return;
  const rel = S.here.rel === '/' ? '' : S.here.rel;
  const parts = rel ? rel.split('/') : [];
  const add = (label, path) => {
    if (box.children.length) {
      const sep = document.createElement('span');
      sep.className = 'sep'; sep.textContent = '/';
      box.appendChild(sep);
    }
    const b = document.createElement('button');
    b.textContent = label;
    b.onclick = () => browse(path);
    box.appendChild(b);
  };
  add('workspace', S.root);
  let acc = S.root;
  for (const part of parts) { acc += '/' + part; add(part, acc); }
  box.scrollLeft = box.scrollWidth;
}

async function browse(path) {
  const list = $('#project-list');
  list.innerHTML = '<p class="hint" style="padding:12px">Loading…</p>';
  let r;
  try {
    r = await api('/api/folders?path=' + encodeURIComponent(path || ''));
  } catch (e) {
    // A pinned folder that was never cloned, or one deleted since, must not
    // strand the picker with a bare error and no way back. Fall back to the
    // workspace root, which always exists.
    if (path) { toast('That folder is gone \u2014 showing the workspace'); return browse(''); }
    list.innerHTML = `<p class="hint bad" style="padding:12px">${esc(e.message)}</p>`;
    return;
  }
  S.here = r; S.root = r.root; S.entries = r.dirs;
  paintCrumbs();
  list.innerHTML = '';

  // Pinned shortcuts only make sense at the top of the tree.
  if (r.path === r.root) {
    for (const pin of S.pinned) {
      const b = document.createElement('button');
      b.className = 'item';
      b.innerHTML = `<span class="t">${FOLDER_SVG}${esc(pin.name)}`
        + `<span class="pin">${pin.exists ? 'pinned' : ''}</span>`
        + `${pin.exists ? '' : '<span class="missing">missing</span>'}</span>`
        + `<span class="s">${esc(pin.path)}</span>`;
      b.onclick = () => {
        if (!pin.exists) { toast(pin.name + ' is not on the server yet'); return; }
        choose({ name: pin.name, path: pin.path, key: pin.key, pinned: true }, pin.profile);
      };
      list.appendChild(b);
    }
  }

  if (r.parent) {
    const up = document.createElement('button');
    up.className = 'item up';
    up.innerHTML = `<span class="t">${UP_SVG}..</span>`;
    up.onclick = () => browse(r.parent);
    list.appendChild(up);
  }

  for (const d of r.dirs) {
    const b = document.createElement('button');
    b.className = 'item' + (S.project && S.project.path === d.path ? ' sel' : '');
    b.innerHTML = `<span class="t">${FOLDER_SVG}${esc(d.name)}`
      + `${d.git ? '<span class="git">git</span>' : ''}</span>`;
    b.onclick = () => browse(d.path);
    list.appendChild(b);
  }

  if (!r.dirs.length && !r.parent && !S.pinned.length) {
    list.innerHTML = '<p class="hint" style="padding:12px">Workspace is empty. '
      + 'Tap <b>New folder</b> to make one.</p>';
  } else if (!r.dirs.length) {
    const note = document.createElement('p');
    note.className = 'hint';
    note.style.padding = '10px 12px';
    note.textContent = 'No sub-folders here. Open this one, or make a new one.';
    list.appendChild(note);
  }
}

function fill(sel, values, current, blankLabel) {
  sel.innerHTML = '';
  if (blankLabel) {
    const o = document.createElement('option');
    o.value = ''; o.textContent = blankLabel;
    sel.appendChild(o);
  }
  for (const v of values) {
    const o = document.createElement('option');
    // Accepts plain strings or {id,label} so the server can name things
    // properly ("Opus 5") instead of the phone showing a bare alias.
    o.value = (v && v.id !== undefined) ? v.id : v;
    o.textContent = (v && v.label !== undefined) ? v.label : v;
    if (v && v.note) o.title = v.note;
    sel.appendChild(o);
  }
  sel.value = current || '';
}

function paintProfiles() {
  fill($('#fb-mode'), S.modes, S.mode, 'tools default');
  fill($('#fb-profile'), S.profiles.map((p) => p.name), S.profile || 'default');
  // Blank means "whatever the mode says", which is the honest default: the
  // profile already picks a model, and effort is unset unless asked for.
  fill($('#fb-model'), S.models, S.model, 'mode default');
  fill($('#fb-effort'), S.efforts, S.effort, 'mode default');
  explainRun();
}

function explainRun() {
  const el = $('#fb-explain');
  if (!el) return;
  const pr = S.profiles.find((p) => p.name === (S.profile || 'default'));
  const model = S.model || (pr && pr.model) || 'sonnet';
  const mode = S.mode || (pr && pr.permission_mode) || 'dontAsk';
  const bits = [`--model ${model}`, `--permission-mode ${mode}`];
  const effort = S.effort || (pr && pr.effort);
  if (effort) bits.push(`--effort ${effort}`);
  const budget = pr && pr.max_budget_usd != null ? `$${pr.max_budget_usd} cap` : 'no cap';
  el.textContent = `claude ${bits.join(' ')} · ${budget}`;
}

function choose(project, profile) {
  S.project = project;
  if (profile) { S.profile = profile; $('#fb-profile').value = profile; }
  localStorage.setItem('ccr_dir', project.path);
  localStorage.setItem('ccr_pinned', project.pinned ? project.key : '');
  localStorage.setItem('ccr_profile', S.profile || '');
  $('#project-name').textContent = project.name;
  closeSheets();

  const prior = sidFor(project.path);
  if (prior && prior !== S.sid) {
    // A session that was deleted server-side must not strand the folder.
    openSession(prior).catch(() => { rememberSid(project.path, null); newSession(); });
  } else if (!prior) {
    newSession();
  }
}

function useHere() {
  if (!S.here) return;
  const pin = S.pinned.find((x) => x.path === S.here.path);
  choose({ name: pin ? pin.name : (S.here.name || 'workspace'),
           path: S.here.path, key: pin ? pin.key : null, pinned: !!pin });
}

async function deleteFolder() {
  if (!S.here) return;
  if (S.here.path === S.root) { toast('Cannot delete the workspace itself'); return; }
  const name = S.here.name || S.here.rel;
  if (!confirm(`Delete "${name}" and everything inside it, on the server?\n\n`
      + 'This removes files permanently. Conversations that ran here are kept.')) return;
  const url = '/api/folders?path=' + encodeURIComponent(S.here.path);
  try {
    await api(url, { method: 'DELETE' });
  } catch (e) {
    // 409 means "not empty": say how much is at stake before forcing.
    if (/not empty/i.test(e.message || '')) {
      if (!confirm(e.message + '\n\nDelete it anyway?')) return;
      try { await api(url + '&force=true', { method: 'DELETE' }); }
      catch (e2) { toast(e2.message || 'Delete failed'); return; }
    } else { toast(e.message || 'Delete failed'); return; }
  }
  rememberSid(S.here.path, null);
  const parent = S.here.parent || '';
  toast('Deleted ' + name);
  // The open folder just ceased to exist; move up before anything reads it.
  if (S.project && S.project.path === S.here.path) {
    S.project = { name: 'workspace', path: S.root, key: null, pinned: false };
    localStorage.setItem('ccr_dir', S.root);
    $('#project-name').textContent = S.project.name;
    newSession();
  }
  browse(parent);
}

async function newFolder() {
  if (!S.here) return;
  const name = prompt('New folder inside ' + (S.here.rel === '/' ? 'workspace' : S.here.rel));
  if (!name || !name.trim()) return;
  try {
    const made = await api('/api/folders', {
      method: 'POST',
      body: JSON.stringify({ parent: S.here.path, name: name.trim() }),
    });
    await browse(made.path);
    toast('Created ' + made.name);
  } catch (e) { toast(e.message || 'Could not create folder'); }
}

/* ---------- conversation drawer ---------- */
S.scope = localStorage.getItem('ccr_scope') || 'folder';
S.openMenu = null;

// Same buckets the Claude interface uses, so the list reads by recency rather
// than as one undifferentiated wall of rows.
function bucket(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  if (t >= midnight) return 'Today';
  if (t >= midnight - 864e5) return 'Yesterday';
  if (t >= midnight - 7 * 864e5) return 'Previous 7 days';
  if (t >= midnight - 30 * 864e5) return 'Previous 30 days';
  return d.toLocaleDateString([], { month: 'long', year: 'numeric' });
}

function shortTime(ts) {
  const d = new Date(ts * 1000);
  const now = Date.now();
  const age = now - d.getTime();
  if (age < 864e5) return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (age < 7 * 864e5) return d.toLocaleDateString([], { weekday: 'short' });
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function paintScope() {
  $('#seg-folder').classList.toggle('sel', S.scope === 'folder');
  $('#seg-all').classList.toggle('sel', S.scope === 'all');
  $('#seg-folder').textContent = S.project ? S.project.name : 'This folder';
}

async function loadSessions() {
  const list = $('#session-list');
  paintScope();
  list.innerHTML = '<p class="hint" style="padding:12px">Loading…</p>';
  let sessions;
  try {
    ({ sessions } = await api('/api/sessions?limit=100'));
  } catch (e) {
    list.innerHTML = `<p class="hint bad" style="padding:12px">${esc(e.message)}</p>`;
    return;
  }

  if (S.scope === 'folder' && S.project) {
    sessions = sessions.filter((x) => x.cwd === S.project.path);
  }

  list.innerHTML = '';
  if (!sessions.length) {
    list.innerHTML = '<p class="hint" style="padding:14px">'
      + (S.scope === 'folder'
        ? 'No conversations in this folder yet.'
        : 'No conversations yet.') + '</p>';
    return;
  }

  let group = null;
  for (const c of sessions) {
    const b = bucket(c.updated_at);
    if (b !== group) {
      group = b;
      const h = document.createElement('div');
      h.className = 'grp'; h.textContent = b;
      list.appendChild(h);
    }
    list.appendChild(convRow(c));
  }
}

function convRow(c) {
  const row = document.createElement('div');
  row.className = 'conv' + (c.id === S.sid ? ' sel' : '');

  const dot = document.createElement('span');
  dot.className = 'dot ' + c.status;
  row.appendChild(dot);

  const open = document.createElement('button');
  open.className = 'open';
  const sub = [];
  if (S.scope === 'all') sub.push(c.project_name);
  sub.push(shortTime(c.updated_at));
  if (c.cost_usd) sub.push('$' + Number(c.cost_usd).toFixed(2));
  open.innerHTML = `<span class="ttl">${esc(c.title || 'New conversation')}</span>`
    + `<span class="sub">${esc(sub.join(' · '))}</span>`;
  open.onclick = () => { closeSheets(); openSession(c.id); };
  row.appendChild(open);

  const more = document.createElement('button');
  more.className = 'more'; more.textContent = '⋯';
  more.setAttribute('aria-label', 'Actions');
  more.onclick = (e) => { e.stopPropagation(); toggleActions(row, c); };
  row.appendChild(more);
  return row;
}

function toggleActions(row, c) {
  if (S.openMenu && S.openMenu.parentNode) S.openMenu.remove();
  if (S.openMenu && S.openMenu.dataset.for === c.id) { S.openMenu = null; return; }

  const acts = document.createElement('div');
  acts.className = 'acts';
  acts.dataset.for = c.id;

  const rename = document.createElement('button');
  rename.textContent = 'Rename';
  rename.onclick = async () => {
    const name = prompt('Name this conversation', c.title || '');
    if (name === null || !name.trim()) return;
    try {
      await api(`/api/sessions/${c.id}`, {
        method: 'PATCH', body: JSON.stringify({ title: name.trim() }),
      });
      acts.remove(); S.openMenu = null; loadSessions();
    } catch (e) { toast(e.message || 'Rename failed'); }
  };

  const del = document.createElement('button');
  del.className = 'danger';
  del.textContent = 'Delete';
  del.onclick = async () => {
    if (!confirm('Delete this conversation? The files it changed stay on the server.')) return;
    try {
      await api(`/api/sessions/${c.id}`, { method: 'DELETE' });
      if (c.id === S.sid) { rememberSid(c.cwd, null); newSession(); }
      else { rememberSid(c.cwd, null); }
      acts.remove(); S.openMenu = null; loadSessions();
    } catch (e) { toast(e.message || 'Delete failed'); }
  };

  acts.append(rename, del);
  row.after(acts);
  S.openMenu = acts;
}

/* ---------- boot ---------- */
function toast(msg) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(t._t);
  t._t = setTimeout(() => { t.hidden = true; }, 2600);
}

function autogrow() {
  const i = $('#input');
  i.style.height = 'auto';
  i.style.height = Math.min(i.scrollHeight, window.innerHeight * 0.38) + 'px';
}

async function loadConfig() {
  const cfg = await api('/api/config');
  S.pinned = cfg.projects || [];
  S.profiles = cfg.profiles || [];
  S.models = cfg.models || [];
  S.efforts = cfg.efforts || [];
  S.modes = cfg.permission_modes || [];
  S.model = localStorage.getItem('ccr_model') || null;
  S.effort = localStorage.getItem('ccr_effort') || null;
  S.mode = localStorage.getItem('ccr_mode') || null;
  S.root = (cfg.workspace && cfg.workspace.root) || '';
  S.profile = localStorage.getItem('ccr_profile')
    || (cfg.workspace && cfg.workspace.default_profile) || 'default';
  paintProfiles();

  // Remember the folder itself, not an index into a list that no longer exists.
  const dir = localStorage.getItem('ccr_dir');
  const pinnedKey = localStorage.getItem('ccr_pinned');
  if (dir) {
    const pin = S.pinned.find((p) => p.key === pinnedKey && p.path === dir);
    S.project = { name: pin ? pin.name : (dir.split('/').pop() || 'workspace'),
                  path: dir, key: pin ? pin.key : null, pinned: !!pin };
  } else if (S.pinned.length) {
    const p = S.pinned[0];
    S.project = { name: p.name, path: p.path, key: p.key, pinned: true };
  } else {
    S.project = { name: 'workspace', path: S.root, key: null, pinned: false };
  }
  $('#project-name').textContent = S.project.name;
}

async function connect() {
  try {
    await loadConfig();
    // Reopen on the conversation belonging to the folder we left off in.
    const sid = (S.project && sidFor(S.project.path)) || localStorage.getItem('ccr_sid');
    if (sid) {
      try { await openSession(sid); }
      catch (_) { if (S.project) rememberSid(S.project.path, null); newSession(); }
    } else { schedule(); }
    return true;
  } catch (e) {
    if (e.message !== '401') toast(e.message || 'Cannot reach server');
    return false;
  }
}

/* ---------- dictation ----------
   Claude Code's mic has one button with two gestures, split at 200ms: a quick
   tap toggles recording and leaves it running, a press held past the threshold
   records only while it is held. Same split here, same Cmd/Ctrl+D shortcut.

   One deliberate difference from Claude Code's own handler, forced by Safari:
   it opens the recogniser from a timer once the press passes 200ms. Safari
   only hands the microphone to a recogniser started *synchronously* inside a
   user event, so a timer start is refused and the hold silently does nothing.
   Recording therefore begins on pointerdown here and the gesture is classified
   on release. The behaviour you see is the same -- a tap leaves it running, a
   hold ends when you lift -- and a held sentence loses no leading words.

   The engine sits behind start/stop on purpose. Claude Code's webview does not
   transcribe anything itself; it calls the host, and the VS Code host answers
   "not supported in this environment". Swapping this for a POST to a
   /transcribe endpoint means replacing Speech.start/stop and nothing else. */
const HOLD_MS = 200;
const IS_WEBKIT = /iP(hone|ad|od)/.test(navigator.userAgent)
  || (/Safari/.test(navigator.userAgent) && !/Chrome|CriOS|Android/.test(navigator.userAgent));

const Speech = {
  rec: null, live: false, want: false, denied: false, err: null, base: '', said: '',
  get on() { return this.want || this.live; },
  get supported() { return !!(window.SpeechRecognition || window.webkitSpeechRecognition); },

  /* Must be called synchronously from a user gesture, or Safari refuses. */
  start() {
    if (this.on || this.denied) return;
    this.err = null;
    const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Rec) { this.fail('unavailable in this browser'); return; }
    this.base = $('#input').value;
    this.said = '';

    let r;
    try { r = new Rec(); } catch (err) { this.fail(err && err.message); return; }
    r.lang = navigator.language || 'en-US';
    r.interimResults = true;
    // WebKit does not implement continuous recognition; asking for it makes
    // start() throw outright. Elsewhere it is what keeps a long prompt intact.
    r.continuous = !IS_WEBKIT;

    r.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) this.said += t; else interim += t;
      }
      this.paint(interim);
    };
    r.onerror = (e) => {
      if (e.error === 'no-speech' || e.error === 'aborted') return;
      this.want = false;
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        this.denied = true;
        this.err = 'Microphone blocked — allow it for this site, and check Settings › General › Keyboard › Enable Dictation';
      } else {
        this.err = `Dictation error: ${e.error}`;
      }
      paintMic();
    };
    // WebKit ends the stream at every pause, which would cut a held sentence
    // short. Restarting is only allowed to fail quietly: by here the mic has
    // already been granted once, so a refusal means the gesture is over.
    r.onend = () => {
      this.live = false;
      if (this.want && !this.denied) {
        try { r.start(); this.live = true; return; } catch (_) { this.want = false; }
      }
      this.paint('');
      paintMic();
    };

    this.want = true;
    try {
      r.start();
      this.live = true;
    } catch (err) {
      // A silent catch here is what made this look like a dead button.
      this.want = false;
      this.fail(err && err.message);
      return;
    }
    this.rec = r;
    paintMic();
  },

  fail(msg) {
    this.err = `Dictation error: ${msg || 'could not start'}`;
    paintMic();
  },

  stop() {
    this.want = false;
    if (this.rec) { try { this.rec.stop(); } catch (_) {} }
    this.live = false;
    paintMic();
  },

  /* Dictation adds to what is already typed, so you can start a prompt with
     your thumbs and finish it out loud. */
  paint(interim) {
    const input = $('#input');
    const heard = (this.said + interim).trim();
    const base = this.base.replace(/\s+$/, '');
    input.value = base && heard ? `${base} ${heard}` : (heard || base);
    autogrow(); paintStatus();
  },
};

function micTip(text, isErr) {
  const t = $('#mic-tip');
  if (!t) return;
  t.textContent = text;
  t.classList.toggle('err', !!isErr);
}

function paintMic() {
  const b = $('#btn-mic');
  if (!b) return;
  b.classList.toggle('recording', Speech.on);
  $('#mic-wrap').classList.toggle('rec', Speech.on);
  b.disabled = Speech.denied;
  // An error outranks the label; otherwise repainting would erase the reason
  // the button just failed, which is what made this look like a dead button.
  if (Speech.err) { micTip(Speech.err, true); return; }
  const label = Speech.on ? 'Stop recording' : 'Tap or hold to record';
  micTip(label, false);
  b.setAttribute('aria-label', label);
}

function wireMic() {
  const btn = $('#btn-mic');
  if (!btn || !Speech.supported) return;   // no engine, no button
  $('#mic-wrap').hidden = false;

  let pressAt = null;

  const down = (e) => {
    if (e.button != null && e.button > 0) return;
    e.preventDefault();                     // a long press must not select text
    if (Speech.on) { pressAt = null; Speech.stop(); return; }  // tap while live = stop
    pressAt = Date.now();
    Speech.start();                         // synchronous: keeps Safari's grant
  };
  const up = () => {
    if (pressAt === null) return;
    const held = Date.now() - pressAt;
    pressAt = null;
    if (held >= HOLD_MS && Speech.on) Speech.stop();  // hold released
    // Under the threshold it was a tap, so leave it running.
  };

  btn.addEventListener('pointerdown', down);
  // On the document: a finger that slides off the button still ends the hold,
  // and a pointercancel (scroll, incoming call) must not strand the mic open.
  document.addEventListener('pointerup', up);
  document.addEventListener('pointercancel', up);
  btn.addEventListener('contextmenu', (e) => e.preventDefault());

  // Cmd+D / Ctrl+D, same two gestures, for a paired keyboard.
  const isMac = /Mac|iP(hone|ad|od)/.test(navigator.platform || navigator.userAgent);
  const combo = (e) => (isMac ? e.metaKey : e.ctrlKey)
    && e.key.toLowerCase() === 'd' && !e.shiftKey && !e.altKey;
  let keyAt = null, keyOn = false;

  document.addEventListener('keydown', (e) => {
    if (e.repeat || !combo(e)) return;
    e.preventDefault();
    if (Speech.on) { Speech.stop(); keyAt = null; keyOn = false; return; }
    keyAt = Date.now(); keyOn = true; Speech.start();
  }, true);
  document.addEventListener('keyup', (e) => {
    if (!keyOn) return;
    if (e.key.toLowerCase() !== 'd' && e.key !== (isMac ? 'Meta' : 'Control')) return;
    keyOn = false;
    const at = keyAt; keyAt = null;
    if (at !== null && Date.now() - at >= HOLD_MS && Speech.on) Speech.stop();
  }, true);

  // Backgrounding the app must not leave the microphone live. This listens for
  // a real background, not window blur: iOS blurs the window to show the
  // microphone permission prompt, and stopping there killed the session that
  // the prompt was granting.
  document.addEventListener('visibilitychange', () => {
    if (document.hidden && Speech.on) { pressAt = null; keyOn = false; Speech.stop(); }
  });

  paintMic();
}

function wire() {
  $('#btn-send').onclick = send;
  wireMic();
  $('#btn-new').onclick = newSession;
  $('#btn-sessions').onclick = () => openSheet('#sheet-sessions');
  $('#project-pill').onclick = () => openSheet('#sheet-projects');
  $('#btn-settings').onclick = () => openSheet('#sheet-settings');
  $('#scrim').onclick = closeSheets;
  $('#fb-use').onclick = useHere;
  $('#fb-new').onclick = newFolder;
  $('#fb-del').onclick = deleteFolder;
  $('#seg-folder').onclick = () => {
    S.scope = 'folder'; localStorage.setItem('ccr_scope', 'folder'); loadSessions();
  };
  $('#seg-all').onclick = () => {
    S.scope = 'all'; localStorage.setItem('ccr_scope', 'all'); loadSessions();
  };
  $('#sess-new').onclick = () => { closeSheets(); newSession(); };
  const remember = (key, field) => async (e) => {
    const v = e.target.value || null;
    S[field === 'permission_mode' ? 'mode' : field] = v;
    if (v) localStorage.setItem(key, v); else localStorage.removeItem(key);
    explainRun();
    // An open conversation keeps its own setting, applied from the next turn.
    if (S.sid && field !== 'profile') {
      try {
        await api(`/api/sessions/${S.sid}`, {
          method: 'PATCH', body: JSON.stringify({ [field]: v || '' }),
        });
        toast(`${field} \u2192 ${v || 'mode default'} from the next turn`);
      } catch (err) { toast(err.message || 'Could not change ' + field); }
    }
  };
  $('#fb-profile').onchange = remember('ccr_profile', 'profile');
  $('#fb-model').onchange = remember('ccr_model', 'model');
  $('#fb-mode').onchange = remember('ccr_mode', 'permission_mode');
  $('#fb-effort').onchange = remember('ccr_effort', 'effort');
  document.querySelectorAll('[data-close]').forEach((b) => { b.onclick = closeSheets; });

  const input = $('#input');
  input.addEventListener('input', () => { autogrow(); paintStatus(); });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); send(); }
  });

  $('#btn-stop').onclick = async () => {
    if (!S.sid) return;
    try { await api(`/api/sessions/${S.sid}/stop`, { method: 'POST' }); toast('Stopping…'); poll(); }
    catch (e) { toast(e.message); }
  };

  $('#cfg-save').onclick = async () => {
    S.base = ($('#cfg-base').value || location.origin).replace(/\/$/, '');
    S.token = $('#cfg-token').value.trim();
    localStorage.setItem('ccr_base', S.base);
    localStorage.setItem('ccr_token', S.token);
    const st = $('#cfg-status');
    st.className = 'hint'; st.textContent = 'Connecting…';
    const ok = await connect();
    st.className = 'hint ' + (ok ? 'ok' : 'bad');
    st.textContent = ok ? 'Connected.' : 'Could not connect.';
    if (ok) setTimeout(closeSheets, 500);
  };

  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
}

// Says plainly whether this is the installed app or a browser tab, so
// "is it actually installed?" is answerable without guessing at chrome.
function paintMode() {
  const standalone = window.navigator.standalone === true
    || window.matchMedia('(display-mode: standalone)').matches
    || window.matchMedia('(display-mode: fullscreen)').matches;
  const el = $('#cfg-mode');
  el.className = 'hint mode ' + (standalone ? 'ok' : '');
  el.textContent = standalone
    ? 'Running as an installed app.'
    : 'Running in the browser \u2014 Share \u2192 Add to Home Screen to install.';
}

(async function boot() {
  wire();
  paintMode();
  $('#cfg-base').value = S.base;
  $('#cfg-token').value = S.token;
  paintStatus();
  if (!S.token) {
    openSheet('#sheet-settings');
    $('#cfg-status').textContent = 'Paste the server token to start.';
  } else {
    await connect();
  }
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
})();
