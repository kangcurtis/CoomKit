// CoomKit — front end. Vanilla, no build step.
// Layout: topbar (status/model/preset) · left roster · center chat · right rail
// · settings modal. Everything talks to the stdlib server over /api.

const $ = (id) => document.getElementById(id);

async function api(path, opts) {
  const r = await fetch(path, opts);
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('json')) return { error: `non-json response (${r.status})` };
  return r.json();
}
const post = (path, body) => api(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body || {}),
});
const del = (path) => fetch(path, { method: 'DELETE' });

let toastTimer = null;
function toast(msg) {
  const t = $('toast');
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('on'), 2600);
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// The server strips ```tool and ```director blocks once the reply is
// complete, but mid-stream they arrive character by character — without
// this they flash in the bubble and then vanish. Cut from the opening fence
// onward, including a fence still being typed out.
const BLOCK_RE = /```(?:tool|director)\b[\s\S]*?```/g;
// an opened tool/director fence with no closing fence yet…
const UNCLOSED_RE = /```(?:tool|director)\b[\s\S]*$/;
// …or a bare fence at the very end whose tag is still being typed. Anything
// else (a real ```python block) is left alone.
const TYPING_RE = /```(?:t(?:o(?:o(?:l)?)?)?|d(?:i(?:r(?:e(?:c(?:t(?:o(?:r)?)?)?)?)?)?)?)?$/;
function stripBlocks(s) {
  return String(s || '')
    .replace(BLOCK_RE, '').replace(UNCLOSED_RE, '').replace(TYPING_RE, '').trim();
}

// Text a display-scope regex rule produced, already run through the server's
// allowlist. This is the ONE path where markup reaches a bubble as markup —
// and it is markup the *user* installed a rule to produce, never anything the
// model wrote. `fmt` still escapes everything else, unchanged.
// Deliberately does NOT parse fences: re-parsing already-sanitised markup is
// how an allowlist gets walked around.
function fmtHtml(s) {
  return String(s == null ? '' : s).replace(/\r?\n/g, '<br>');
}

// bubble formatting: *action* → em, (ooc) → dim, `code` → code,
// ```fenced``` → a real code block.
//
// Everything here runs on untrusted model text, so esc() comes first and the
// only markup that exists afterwards is markup this function put there.
// esc() does not escape quotes, so nothing below may interpolate model text
// into an attribute — the language label is filtered to [A-Za-z0-9_+.-] and
// carried as element content, not as a class.

// Prose only. Never sees the inside of a code span or a fence, so a `*ptr`
// in C stops turning into italics and `if (cond)` stops becoming an ooc note.
function fmtProse(s) {
  let out = esc(s);
  out = out.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
  out = out.replace(/(^|\s)\(([^)\n]{3,})\)/g, '$1<span class="ooc">($2)</span>');
  return out.replace(/\r?\n/g, '<br>');
}

// `inline code` inside a prose run.
function fmtInline(s) {
  let out = '';
  let last = 0;
  const re = /`([^`\n]+)`/g;
  let m;
  while ((m = re.exec(s)) !== null) {
    out += fmtProse(s.slice(last, m.index));
    out += `<code class="inline">${esc(m[1])}</code>`;
    last = re.lastIndex;
  }
  return out + fmtProse(s.slice(last));
}

// A fence that is opened and not yet closed still renders as a code block.
// That matters because fmt() is re-run on every streamed chunk: falling back
// to raw text until the closing fence arrives makes the bubble flicker
// between the two forms as she types.
const FENCE_RE = /```([A-Za-z0-9_+.-]{0,24})[ \t]*\r?\n?([\s\S]*?)(?:```|$)/g;

function fmt(s) {
  const src = String(s == null ? '' : s);
  let out = '';
  let last = 0;
  let m;
  FENCE_RE.lastIndex = 0;
  while ((m = FENCE_RE.exec(src)) !== null) {
    out += fmtInline(src.slice(last, m.index));
    const lang = m[1] || '';
    out += '<div class="code-block"><div class="code-head">'
      + `<span class="code-lang">${esc(lang || 'text')}</span>`
      + '<button class="mini-btn code-copy" type="button">copy</button></div>'
      + `<pre><code>${esc(m[2])}</code></pre></div>`;
    last = FENCE_RE.lastIndex;
    if (FENCE_RE.lastIndex === m.index) FENCE_RE.lastIndex++;  // zero-width guard
  }
  return out + fmtInline(src.slice(last));
}

// Delegated because fmt() output is assigned as innerHTML in five places and
// re-rendered on every streamed chunk — per-element handlers would not survive.
document.addEventListener('click', (e) => {
  const btn = e.target.closest && e.target.closest('.code-copy');
  if (!btn) return;
  const code = btn.closest('.code-block').querySelector('code');
  navigator.clipboard.writeText(code.textContent).then(() => {
    btn.textContent = 'copied';
    setTimeout(() => { btn.textContent = 'copy'; }, 1200);
  }, () => toast('clipboard said no'));
});

// ── state ────────────────────────────────────────────────────────
const S = {
  chars: [], personas: [], presets: [], jailbreaks: [], workflows: [],
  chat: null,          // {id, mode, charId, name, avatar}
  llm: { backend: '', model: '' },
  presetId: '',
  attachments: [],     // [{name, b64, dataUrl}]
  director: '',        // the OPEN chat's stage direction (mirror of the map)
  directorByChat: {},  // chat_id -> direction text. Stage direction is scene
                       // furniture: one global string followed the user into
                       // every chat they opened, forever, silently.
  directorOn: false,       // bar open for the OPEN chat (mirror of the map)
  directorOnByChat: {},    // chat_id -> bar open. Also scene furniture: a
                           // globally-open bar injected the director channel
                           // into every chat, not the one being directed.
  directorNotes: true, // …and she answers in the same channel
  sendAs: 'auto',      // the OPEN chat's forced speaker (mirror of the map)
  sendAsByChat: {},    // chat_id -> pick. A global pick forced the same
                       // character in every multi chat, labelled "you".
  tools: true,
  busy: false,
  chatsByChar: {},     // "charId:mode" -> chat_id (so switching back resumes)
};

// ── session persistence ──────────────────────────────────────────
// Everything the user picked lives in one localStorage blob. A reload used
// to drop the open chat, the model, the preset and every slider back to
// defaults, which read as "it lost my messages" — the messages were in
// sqlite the whole time, the UI just had no idea which chat it was in.
const STORE_KEY = 'coomkit.session.v1';

function loadUI() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY)) || {}; }
  catch { return {}; }
}
let saveTimer = null;
function saveUI() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const ui = {
      chatsByChar: S.chatsByChar,
      chat: S.chat && { id: S.chat.id, charId: S.chat.charId, mode: S.chat.mode },
      presetId: S.presetId,
      llm: S.llm,
      persona: $('personaSel').value,
      tools: S.tools,
      rail: (document.querySelector('.rail-tab.on') || {}).dataset?.rail || 'scene',
      directorOnByChat: S.directorOnByChat,
      directorNotes: S.directorNotes,
      directorByChat: S.directorByChat,
      sendAsByChat: S.sendAsByChat,
      thinkMode: $('thinkMode').value,
      rosterSort: $('rosterSort').value,
      thinkPrefill: $('thinkPrefill').value,
      replyPrefill: $('replyPrefill').value,
      samplers: samplersFromInputs(),
      samplerOpen: $('samplerBlock').open,
      // Who the user chose to speak. renderCast() rebuilds the option list on
      // every loadChat, so without this the pick did not survive one turn.
      sendAs: S.sendAs || 'auto',
    };
    try { localStorage.setItem(STORE_KEY, JSON.stringify(ui)); } catch { /* full/private mode */ }
  }, 150);
}

// The commentary bubble under the portrait is gone — it ate the space the
// picture wanted and said nothing the status pill and the bubbles don't
// already say. Anything that genuinely needed surfacing became a toast.
function setStatus(kind, text) {
  $('statusDot').className = 'dot' + (kind ? ' ' + kind : '');
  $('statusText').textContent = text;
}

// ── boot ─────────────────────────────────────────────────────────
async function boot() {
  setStatus('busy', 'connecting…');
  const h = await api('/api/health');
  if (!h.ok) { setStatus('bad', 'server down'); return; }
  setStatus('ok', `v${h.version}`);
  const ui = loadUI();
  S.chatsByChar = ui.chatsByChar || {};
  S.tools = ui.tools !== false;
  await Promise.all([
    loadBackends(ui), loadChars(), loadPersonas(),
    loadPresets(ui), loadJailbreaks(), loadWorkflows(), loadConfig(),
    loadLibrary(), loadPrompts(), loadStudio(), loadRegex(), loadLore(),
  ]);
  syncSceneFromPreset();
  loadPromptRail();          // the prompt rail is the default tab
  restoreUI(ui);
  // The <head> script already set the attribute; this syncs S.theme and the
  // button's tooltip without repainting.
  applyTheme(localStorage.getItem('coomkit.theme.v1') || 'rose');
  await restoreChat(ui);
  // First run. This used to also require `!S.presets.length`, which
  // server.seed_first_run() makes permanently false — it installs the shipped
  // library into an empty database BEFORE the first request is ever served,
  // so the wizard could not fire on a genuinely fresh install and the `else`
  // branch below was dead for the same reason. The gate landed hours before
  // the seeding did; seeding silently invalidated its premise.
  //
  // The flag lives in data/config.json rather than localStorage so that
  // `rm -rf data/` means what everyone assumes it means. localStorage is
  // still honoured so an existing user is not walked through setup again.
  const done = (S.cfg && S.cfg.setup) || localStorage.getItem('coomkit.setup.v1');
  if (!done) {
    openWizard();
  } else if (!(S.cfg && S.cfg.setup) && localStorage.getItem('coomkit.setup.v1')) {
    markSetupDone();          // migrate the old marker forward, once
  } else if (!S.tourDone && !localStorage.getItem('coomkit.tour.v1')
             && S.chars.length) {
    // The tour used to chain ONLY from finishWizard, so suppressing the
    // wizard suppressed the walkthrough with it.
    setTimeout(startTour, 600);
  }
}

async function markSetupDone() {
  localStorage.setItem('coomkit.setup.v1', '1');
  const cfg = (S.cfg = S.cfg || {});
  cfg.setup = { done: true, version: 1 };
  await post('/api/config', { setup: cfg.setup });
}

// Re-apply saved widget values. Runs AFTER syncSceneFromPreset so a preset
// provides the baseline and the user's own tweaks win over it.
function restoreUI(ui) {
  if (ui.persona) $('personaSel').value = ui.persona;
  if (ui.thinkMode) $('thinkMode').value = ui.thinkMode;
  if (ui.rosterSort) $('rosterSort').value = ui.rosterSort;
  if (ui.thinkPrefill !== undefined) $('thinkPrefill').value = ui.thinkPrefill;
  if (ui.replyPrefill !== undefined) $('replyPrefill').value = ui.replyPrefill;
  if (ui.samplers) setSamplerInputs(ui.samplers);
  $('samplerBlock').open = !!ui.samplerOpen;
  $('toolsToggle').checked = S.tools;
  $('thinkBadge').textContent = $('thinkMode').value;
  if (ui.rail && ui.rail !== 'scene') {
    const tab = document.querySelector(`.rail-tab[data-rail="${ui.rail}"]`);
    if (tab) tab.click();
  }
  // Per chat, not one global string. The old `ui.director` / `ui.directorOn`
  // / `ui.sendAs` are deliberately NOT migrated: they cannot be attributed to
  // a chat, and one global value silently following the user into every
  // scene was the bug being fixed. The bar's visibility, its text and the
  // forced speaker are all restored when a chat opens, from its own entry.
  S.directorByChat = ui.directorByChat || {};
  S.directorOnByChat = ui.directorOnByChat || {};
  S.sendAsByChat = ui.sendAsByChat || {};
  S.directorNotes = ui.directorNotes !== false;
  $('directorNotes').checked = S.directorNotes;
  updateSamplerSummary();
}

// Reopen whatever was on screen last. The chat may be gone (db wiped), in
// which case fall back to the empty state rather than a broken half-view.
function showEmpty() {
  $('emptyState').hidden = false;
  $('chatHead').hidden = true;
  $('stream').hidden = true;
  $('composer').hidden = true;
  $('herName').textContent = 'nobody yet';
  $('herRole').textContent = 'idle';
  $('herImg').hidden = true;
  $('herNoAva').hidden = false;
  $('chatList').innerHTML = '';
  $('chatsCount').textContent = '—';
}

async function restoreChat(ui) {
  const want = ui.chat;
  if (!want || !want.id) return;
  const c = S.chars.find((x) => x.id === want.charId);
  if (!c) return;
  const mode = want.mode || 'rp';
  const d = await api('/api/chats/' + want.id);
  if (!d || d.error || !Array.isArray(d.messages)) {
    // The pointer is stale, not the history. Ask the server what she has
    // before falling back to the empty state, or a transient failure looks
    // exactly like "it lost all my chats".
    delete S.chatsByChar[want.charId + ':' + mode];
    saveUI();
    const rows = await chatsFor(want.charId, mode);
    if (rows.length) await openChatById(c, rows[0].id, mode);
    return;
  }
  await openChatById(c, want.id, mode);
}

async function loadConfig() {
  const cfg = await api('/api/config');
  // Read at two call sites and assigned at none, so the block editor silently
  // fell back to 8192 context and the wizard always showed a hardcoded
  // ComfyUI URL instead of the configured one.
  S.cfg = cfg;
  $('comfyUrl').value = cfg.comfyui_url || '';
  $('comfyBadge').textContent = cfg.comfyui_url ? 'configured' : 'offline';
  const d = cfg.defaults || {};
  setSamplerInputs(d);
  renderBackendList(cfg.remote_backends || []);
}

// ── backends / models ────────────────────────────────────────────
// The picker is a button + popover rather than a native <select> because a
// llama-server started on a whole model folder serves dozens of models, and
// a native dropdown cannot be filtered. Same rule as the wizard: past a
// dozen, scrolling to find one is worse than typing three letters.
let BACKENDS = [];
let MODEL_OPTS = [];   // flat [{backend, model, label, remote, hay}]

async function loadBackends(ui) {
  const { backends } = await api('/api/backends');
  BACKENDS = backends || [];
  MODEL_OPTS = [];
  let firstLocal = null;
  for (const b of BACKENDS) {
    for (const m of b.models || []) {
      const o = { backend: b.url, model: m, remote: !!b.remote,
                  label: b.label + (b.remote ? ' (remote)' : ''),
                  hay: (m + ' ' + (b.label || b.url)).toLowerCase() };
      MODEL_OPTS.push(o);
      if (!firstLocal && !b.remote) firstLocal = o;
    }
  }
  if (!MODEL_OPTS.length) {
    $('modelBtn').textContent = 'no models found';
    setStatus('bad', 'no LLM backend');
    renderBackendList();
    return;
  }
  // prefer the model the user was last on, if it is still being served
  const want = (ui && ui.llm && ui.llm.model) ? ui.llm : S.llm;
  const match = want && want.model && MODEL_OPTS.find(
    (o) => o.backend === want.backend && o.model === want.model);
  setModel(match || firstLocal || MODEL_OPTS[0]);
  renderBackendList();
}

function setModel(o, save) {
  S.llm = { backend: o.backend, model: o.model };
  const b = BACKENDS.find((x) => x.url === o.backend);
  const remote = !!(b && b.remote);
  const btn = $('modelBtn');
  btn.textContent = o.model.split('/').pop();
  btn.title = o.model + ' — ' + (b ? b.label : o.backend);
  setStatus('ok', (b ? b.label : 'backend') + ' · ' + o.model.split('/').pop());
  $('visionBadge').hidden = remote;
  // prefill semantics differ: local backends genuinely continue an assistant
  // turn; hosted APIs drop it, so we emulate via an instruction instead.
  $('prefillBadge').hidden = !remote;
  $('prefillHint').textContent = remote
    ? 'Hosted APIs drop real prefills, so this is emulated as an instruction, softer, and she may drift from it.'
    : 'Put words in her mouth. She literally continues from here.';
  $('thinkPrefill').parentElement.style.opacity = remote ? .55 : 1;
  if (save) saveUI();
}

// Restore the topbar status pill and badges for the current model — what
// send() and rerollMsg() need after a stream replaces the pill with
// "generating…". The old applyModelSel() did this by re-reading the select;
// setModel with the current pick is the same refresh without the select.
function refreshModelStatus() {
  if (!S.llm || !S.llm.model) return;
  setModel(MODEL_OPTS.find((o) => o.backend === S.llm.backend
                                && o.model === S.llm.model)
           || { backend: S.llm.backend, model: S.llm.model });
}

function renderModelPop() {
  const q = ($('modelFind').value || '').trim().toLowerCase();
  const list = $('modelList');
  list.innerHTML = '';
  let lastLabel = null;
  let shown = 0;
  for (const o of MODEL_OPTS) {
    if (q && !o.hay.includes(q)) continue;
    if (o.label !== lastLabel) {
      const h = document.createElement('div');
      h.className = 'model-pop-group';
      h.textContent = o.label;
      list.appendChild(h);
      lastLabel = o.label;
    }
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'model-pop-row'
      + (S.llm && S.llm.backend === o.backend && S.llm.model === o.model
         ? ' on' : '');
    row.textContent = o.model;
    row.title = o.model;
    row.onclick = () => { setModel(o, true); closeModelPop(); };
    list.appendChild(row);
    shown++;
  }
  if (!shown) {
    list.innerHTML = '<div class="model-pop-none">nothing matches</div>';
  }
}

async function openModelPop() {
  const pop = $('modelPop');
  if (!pop.hidden) { closeModelPop(); return; }
  // Re-probe when empty: the page may have loaded before the user's LLM
  // server was up, and opening the picker is exactly when they expect a
  // fresh look. Same behaviour pickModel() has always had.
  if (!MODEL_OPTS.length) await loadBackends();
  pop.hidden = false;
  const find = $('modelFind');
  find.value = '';
  find.hidden = MODEL_OPTS.length <= 12;
  renderModelPop();
  if (!find.hidden) find.focus();
}
function closeModelPop() { $('modelPop').hidden = true; }
$('modelBtn').onclick = (e) => { e.stopPropagation(); openModelPop(); };
$('modelFind').oninput = renderModelPop;
$('modelFind').onkeydown = (e) => {
  if (e.key === 'Escape') { closeModelPop(); e.stopPropagation(); }
  if (e.key === 'Enter') {
    const r = $('modelList').querySelector('.model-pop-row');
    if (r) r.click();
  }
};
document.addEventListener('click', (e) => {
  const pop = $('modelPop');
  if (!pop.hidden && !pop.contains(e.target)) closeModelPop();
});
// Escape must close the popover even when the filter input is hidden (a
// dozen models or fewer) or not focused — the input's own handler only
// covers keystrokes landing in it.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('modelPop').hidden) closeModelPop();
});
function renderBackendList(remotes) {
  const ul = $('backendList');
  if (!ul) return;
  ul.innerHTML = '';
  if (!BACKENDS.length) {
    ul.innerHTML = '<li class="mem-empty">Nothing answered. Start LM Studio / llama-server, or add a remote below.</li>';
    return;
  }
  for (const b of BACKENDS) {
    const li = document.createElement('li');
    li.innerHTML = `<b>${esc(b.label)}</b> ${b.remote ? '<span class="badge alt">remote</span>' : ''}
      <div class="note">${esc(b.url)} · ${b.models.length} model${b.models.length === 1 ? '' : 's'}</div>`;
    ul.appendChild(li);
  }
}

// ── characters / roster ──────────────────────────────────────────
async function loadChars() {
  const { rows } = await api('/api/characters');
  S.chars = rows || [];
  renderRoster();
  renderCardsList();
}

// Free text from the card that is worth matching on. Kept out of the render
// loop so typing in the box does not re-flatten every description.
function charHaystack(c) {
  if (c._hay) return c._hay;
  const f = (c.data && c.data.fields) || {};
  c._hay = [c.name, f.description, f.personality, f.scenario,
            (f.tags || []).join(' '), (c.data && c.data.creator) || '']
    .join(' ').replace(/<[^>]*>/g, ' ').toLowerCase();
  return c._hay;
}

function rosterView() {
  const q = ($('rosterSearch').value || '').trim().toLowerCase();
  const sort = $('rosterSort').value || 'recent';
  let list = S.chars.slice();
  if (q) list = list.filter((c) => charHaystack(c).includes(q));
  if (sort === 'fav') list = list.filter((c) => c.fav);
  const by = {
    // last_seen is chats.updated — the clock that moves when you actually
    // talk to her. characters.updated is card mtime and never does.
    recent: (a, b) => (b.last_seen || 0) - (a.last_seen || 0)
                   || (b.updated || 0) - (a.updated || 0),
    name: (a, b) => a.name.localeCompare(b.name),
    added: (a, b) => (b.created || 0) - (a.created || 0),
    fav: (a, b) => (b.last_seen || 0) - (a.last_seen || 0),
  }[sort];
  list.sort(by);
  // Pinned always float, whatever the sort — that is what pinning is for.
  if (sort !== 'fav') list.sort((a, b) => (b.fav ? 1 : 0) - (a.fav ? 1 : 0));
  return list;
}

function renderRoster() {
  const wrap = $('roster');
  wrap.innerHTML = '';
  if (!S.chars.length) {
    wrap.innerHTML = '<div style="padding:14px" class="mem-empty">No cards yet. Hit <b>+ card</b>.</div>';
    return;
  }
  const list = rosterView();
  if (!list.length) {
    wrap.innerHTML = '<div style="padding:14px" class="mem-empty">Nobody matches that. Picky.</div>';
    return;
  }
  for (const c of list) {
    const row = document.createElement('div');
    row.className = 'roster-item' + (S.chat && S.chat.charId === c.id ? ' on' : '');
    const desc = (c.data.fields && c.data.fields.description || '').replace(/<[^>]*>/g, '').trim();
    row.innerHTML = `
      ${c.avatar ? `<img class="roster-ava" src="/api/avatars/${c.avatar}" alt="">`
                 : '<div class="roster-ava ph">♡</div>'}
      <div class="roster-meta"><b></b><small></small></div>
      <div class="roster-go">
        <button class="mini-btn go-fav" title="Pin her to the top">${c.fav ? '★' : '☆'}</button>
        <button class="mini-btn go-rp" title="Roleplay">chat</button>
        <button class="mini-btn go-sms" title="Text her">💬</button>
        <button class="mini-btn go-edit" title="View / edit her card">✎</button>
      </div>`;
    // card text is someone else's content — never innerHTML
    row.querySelector('.roster-meta b').textContent = c.name;
    row.querySelector('.roster-meta small').textContent =
      c.chat_count ? `${c.chat_count} chat${c.chat_count > 1 ? 's' : ''} · ${desc.slice(0, 36)}`
                   : desc.slice(0, 48);
    const fav = row.querySelector('.go-fav');
    if (c.fav) fav.classList.add('on');
    fav.onclick = async (e) => {
      e.stopPropagation();
      c.fav = c.fav ? 0 : 1;
      await post(`/api/characters/${c.id}/fav`, { on: !!c.fav });
      renderRoster();
    };
    row.querySelector('.go-rp').onclick = (e) => { e.stopPropagation(); openChat(c, 'rp'); };
    row.querySelector('.go-sms').onclick = (e) => { e.stopPropagation(); openPhone(c.id); };
    row.querySelector('.go-edit').onclick = (e) => {
      e.stopPropagation(); openForge('cards'); openCardEditor(c.id);
    };
    row.onclick = () => openChat(c, 'rp');
    wrap.appendChild(row);
  }
}
$('rosterSearch').addEventListener('input', renderRoster);
$('rosterSort').onchange = () => { renderRoster(); saveUI(); };

function renderCardsList() {
  const ul = $('cardsList');
  if (!ul) return;
  ul.innerHTML = '';
  for (const c of S.chars) {
    const li = document.createElement('li');
    li.innerHTML = `<span><b>${esc(c.name)}</b> <span class="note">${esc(c.data.spec || 'v2')}</span></span>`;
    const ed = document.createElement('button');
    ed.className = 'mini-btn'; ed.textContent = 'edit';
    ed.onclick = () => openCardEditor(c.id);
    const exp = document.createElement('button');
    exp.className = 'mini-btn'; exp.textContent = 'export png';
    exp.onclick = async () => {
      const r = await fetch(`/api/characters/${c.id}/export`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ format: 'png' }),
      });
      if (!r.ok) { toast('export failed'); return; }
      const blob = await r.blob();
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = c.name.replace(/[^\w-]+/g, '_') + '.png';
      a.click();
    };
    const rm = document.createElement('button');
    rm.className = 'mini-btn danger-btn'; rm.textContent = '✕';
    rm.onclick = async () => { await del('/api/characters/' + c.id); loadChars(); };
    li.append(ed, exp, rm);
    ul.appendChild(li);
  }
}

// ── card editor ──────────────────────────────────────────────────
// The card is the single biggest chunk of every prompt and was the one
// thing you could not look at. Edits go through /fields so they land in the
// embedded card as well and survive an export back to SillyTavern.
const CARD_FIELDS = {
  cfName: 'name', cfDescription: 'description', cfPersonality: 'personality',
  cfScenario: 'scenario', cfFirstMes: 'first_mes', cfMesExample: 'mes_example',
  cfSystemPrompt: 'system_prompt', cfPostHistory: 'post_history_instructions',
  cfCreatorNotes: 'creator_notes',
};

function openCardEditor(charId) {
  const c = S.chars.find((x) => x.id === charId);
  if (!c) return;
  const f = (c.data && c.data.fields) || {};
  $('cardEditId').value = c.id;
  $('cardEditWho').textContent = c.name;
  $('cardEditSpec').textContent = c.data.spec || 'v2';
  for (const [id, key] of Object.entries(CARD_FIELDS)) $(id).value = f[key] || '';
  $('cfAltGreetings').value = (f.alternate_greetings || []).join('\n');
  $('cardNote').textContent = '';
  $('cardNote').className = 'note';
  fillLooksAndVoice(c);
  showPortrait(c);
  fillPortraitPickers();
  $('cardEditor').hidden = false;
  $('cardEditor').scrollIntoView({ behavior: 'smooth', block: 'start' });
}
$('cardEditClose').onclick = () => { $('cardEditor').hidden = true; };

$('cardSave').onclick = async () => {
  const id = $('cardEditId').value;
  if (!id) return;
  const fields = {};
  for (const [elId, key] of Object.entries(CARD_FIELDS)) fields[key] = $(elId).value.trim();
  if (!fields.name) {
    $('cardNote').textContent = 'she needs a name';
    $('cardNote').className = 'note bad';
    return;
  }
  fields.alternate_greetings = $('cfAltGreetings').value
    .split('\n').map((s) => s.trim()).filter(Boolean);
  const r = await post(`/api/characters/${id}/fields`, { fields });
  if (r.error) {
    $('cardNote').textContent = 'failed: ' + r.error;
    $('cardNote').className = 'note bad';
    return;
  }
  $('cardNote').textContent = `saved "${r.name}", exports will carry this too`;
  $('cardNote').className = 'note ok';
  await loadChars();
  // a card edit changes what she is; refresh the open scene's header/greetings
  if (S.chat && String(S.chat.charId) === String(id)) {
    const c = S.chars.find((x) => x.id === +id);
    if (c) { $('herName').textContent = c.name; $('chatWho').textContent = c.name; S.chat.name = c.name; }
  }
};

$('importBtn').onclick = () => $('cardFile').click();
$('cardsImport').onclick = () => $('cardFile').click();
$('cardFile').onchange = async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  const b64 = await fileToB64(f);
  const r = await post('/api/cards/import', { filename: f.name, b64 });
  ev.target.value = '';
  if (r.error) { toast('import failed: ' + r.error); return; }
  toast(`imported ${r.name} (${r.spec})`);
  await loadChars();
};

function fileToB64(file) {
  return new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result.split(',')[1]);
    fr.onerror = rej;
    fr.readAsDataURL(file);
  });
}

// drag a card onto the roster
const rosterEl = $('roster');
['dragover', 'dragenter'].forEach((e) => rosterEl.addEventListener(e, (ev) => {
  ev.preventDefault(); rosterEl.classList.add('drop-hot');
}));
['dragleave', 'drop'].forEach((e) => rosterEl.addEventListener(e, () => rosterEl.classList.remove('drop-hot')));
rosterEl.addEventListener('drop', async (ev) => {
  ev.preventDefault();
  const f = ev.dataTransfer.files[0];
  if (!f) return;
  const b64 = await fileToB64(f);
  const r = await post('/api/cards/import', { filename: f.name, b64 });
  if (r.error) { toast('import failed: ' + r.error); return; }
  toast(`imported ${r.name}`);
  loadChars();
});

// ── personas ─────────────────────────────────────────────────────
async function loadPersonas() {
  const { rows } = await api('/api/personas');
  S.personas = rows || [];
  for (const [selId, blank] of [['personaSel', '(just me)'], ['personaList', '— new persona —'],
    ['cgPersonaSel', '(nobody in particular)'], ['cfPersonaSel', '(nobody in particular)']]) {
    const sel = $(selId);
    if (!sel) continue;
    const keep = sel.value;
    sel.innerHTML = `<option value="">${blank}</option>`;
    for (const p of S.personas) {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.name;
      sel.appendChild(o);
    }
    if (keep) sel.value = keep;
  }
  $('personaList').onchange = () => {
    const id = $('personaList').value;
    const p = S.personas.find((x) => String(x.id) === String(id));
    $('personaId').value = p ? p.id : '';
    $('personaName').value = p ? p.name : '';
    $('personaDesc').value = p ? (p.data.description || '') : '';
    $('personaInto').value = p ? (p.data.into || '') : '';
    renderRefs();
  };
}
$('personaSave').onclick = async () => {
  const id = $('personaId').value;
  const r = await post('/api/personas' + (id ? '/' + id : ''), {
    name: $('personaName').value.trim(),
    data: { description: $('personaDesc').value, into: $('personaInto').value },
  });
  $('personaNote').textContent = r.error ? 'error: ' + r.error : `saved "${r.name}"`;
  $('personaNote').className = 'note ' + (r.error ? 'bad' : 'ok');
  loadPersonas();
};
$('personaDelete').onclick = async () => {
  const id = $('personaId').value;
  if (!id) return;
  await del('/api/personas/' + id);
  $('personaId').value = ''; $('personaName').value = ''; $('personaDesc').value = '';
  $('personaInto').value = '';
  loadPersonas();
};
$('managePersonas').onclick = () => openForge('you');

// ── presets ──────────────────────────────────────────────────────
async function loadPresets(ui) {
  const { rows } = await api('/api/presets');
  S.presets = rows || [];
  const top = $('presetSel');
  const keepTop = top.value || (ui && ui.presetId) || '';
  top.innerHTML = '<option value="">no preset</option>';
  const list = $('presetList');
  list.innerHTML = '<option value="">— new preset —</option>';
  for (const p of S.presets) {
    for (const sel of [top, list]) {
      const o = document.createElement('option');
      o.value = p.id; o.textContent = p.name;
      sel.appendChild(o);
    }
  }
  if (keepTop && [...top.options].some((o) => o.value === String(keepTop))) {
    top.value = keepTop;
  }
  S.presetId = top.value;
  top.onchange = () => {
    S.presetId = top.value;
    syncSceneFromPreset();
    loadPromptRail();      // blocks belong to the preset
    saveUI();
  };
  list.onchange = () => fillPresetForm(S.presets.find((p) => String(p.id) === list.value));
}

function activePreset() {
  return S.presets.find((p) => String(p.id) === String(S.presetId));
}

function syncSceneFromPreset() {
  const p = activePreset();
  const d = (p && p.data) || {};
  if (d.thinking_mode) $('thinkMode').value = d.thinking_mode;
  if (d.thinking_prefill !== undefined) $('thinkPrefill').value = d.thinking_prefill || '';
  if (d.prefill !== undefined) $('replyPrefill').value = d.prefill || '';
  if (d.samplers) setSamplerInputs(d.samplers);
  $('thinkBadge').textContent = $('thinkMode').value;
}

function setSamplerInputs(s) {
  const set = (id, v, out) => {
    if (v === undefined || v === null) return;
    $(id).value = v;
    if (out) $(out).textContent = Number(v).toFixed(id === 'sTopP' || id === 'sMinP' ? 2 : 2);
  };
  set('sTemp', s.temperature ?? 0.9, 'sTempOut');
  set('sTopP', s.top_p ?? 0.95, 'sTopPOut');
  set('sMinP', s.min_p ?? 0.05, 'sMinPOut');
  if (s.top_k !== undefined) $('sTopK').value = s.top_k;
  if (s.repetition_penalty !== undefined) $('sRep').value = s.repetition_penalty;
  if (s.max_tokens !== undefined) $('sMaxTok').value = s.max_tokens;
}
function samplersFromInputs() {
  return {
    temperature: +$('sTemp').value,
    top_p: +$('sTopP').value,
    min_p: +$('sMinP').value,
    top_k: +$('sTopK').value || 0,
    repetition_penalty: +$('sRep').value || 1,
    max_tokens: +$('sMaxTok').value || 1024,
  };
}
// A one-line digest so the collapsed block still tells you where you are.
function updateSamplerSummary() {
  const s = samplersFromInputs();
  $('samplerSummary').textContent =
    `${s.temperature.toFixed(2)} · p${s.top_p} · ${s.max_tokens}t`;
}

// every sampler control: live readout, remembered, no save button needed
for (const [r, o] of [['sTemp', 'sTempOut'], ['sTopP', 'sTopPOut'], ['sMinP', 'sMinPOut']]) {
  $(r).oninput = () => { $(o).textContent = Number($(r).value).toFixed(2); };
}
for (const id of ['sTemp', 'sTopP', 'sMinP', 'sTopK', 'sRep', 'sMaxTok']) {
  $(id).addEventListener('input', () => { updateSamplerSummary(); saveUI(); });
}
$('samplerBlock').addEventListener('toggle', saveUI);
for (const id of ['thinkPrefill', 'replyPrefill']) {
  $(id).addEventListener('input', saveUI);
}
$('personaSel').onchange = saveUI;
$('thinkMode').onchange = () => {
  $('thinkBadge').textContent = $('thinkMode').value;
  saveUI();
};

// One write-through for everything the scene rail owns. Both buttons call it
// so the sampler block and the thinking block cannot save different subsets.
async function saveRailIntoPreset(noteId) {
  const p = activePreset();
  if (!p) { toast('pick a preset in the topbar first'); return; }
  const tmode = $('thinkMode').value;
  const data = { ...p.data, samplers: samplersFromInputs(),
                 thinking: tmode !== 'off',
                 thinking_mode: tmode,
                 thinking_prefill: $('thinkPrefill').value,
                 prefill: $('replyPrefill').value };
  const r = await post('/api/presets/' + p.id, { name: p.name, data });
  $(noteId).textContent = r.error ? 'error' : `saved into "${p.name}"`;
  $(noteId).className = 'note ' + (r.error ? 'bad' : 'ok');
  loadPresets();
}
$('samplerSave').onclick = () => saveRailIntoPreset('samplerNote');
$('thinkSave').onclick = () => saveRailIntoPreset('thinkNote');
$('samplerReset').onclick = () => {
  const p = activePreset();
  if (!p || !p.data.samplers) { toast('no preset to revert to'); return; }
  setSamplerInputs(p.data.samplers);
  updateSamplerSummary();
  saveUI();
  $('samplerNote').textContent = `back to "${p.name}"`;
  $('samplerNote').className = 'note ok';
};

function fillPresetForm(p) {
  const d = (p && p.data) || {};
  $('presetId').value = p ? p.id : '';
  $('presetName').value = p ? p.name : '';
  $('presetMode').value = d.mode || 'chat';
  $('presetTemplate').value = d.template || 'gemma4';
  $('presetJailbreak').value = d.jailbreak_id || '';
  // Thinking and prefills follow the sampler rule: one editor, in the scene
  // rail, writing through on demand. A second copy here is how the two ended
  // up disagreeing about the same fact.
  $('presetThinkSummary').textContent = p
    ? `thinking: ${d.thinking_mode || 'normal'}`
      + `${d.thinking_prefill ? ' · seeded' : ''}`
      + `${d.prefill ? ' · reply prefill set' : ''}, edit in the scene rail`
    : 'Thinking and prefills live in the scene rail. Set them there, then "save into preset".';
  const s = d.samplers || {};
  $('presetSamplers').textContent = p
    ? `sampling: temp ${s.temperature ?? '—'} · top_p ${s.top_p ?? '—'} · top_k `
      + `${s.top_k ?? '—'} · min_p ${s.min_p ?? '—'} · rep ${s.repetition_penalty ?? '—'}`
      + ` · max ${s.max_tokens ?? '—'}, edit in the scene rail`
    : 'Sampling lives in the scene rail. Tune it there, then "save into preset".';
  toggleCompletionFields();
}
function toggleCompletionFields() {
  $('completionFields').style.display = $('presetMode').value === 'completion' ? '' : 'none';
}
$('presetMode').onchange = toggleCompletionFields;

$('presetSave').onclick = async () => {
  const id = $('presetId').value;
  const existing = S.presets.find((p) => String(p.id) === String(id));
  const body = {
    name: $('presetName').value.trim(),
    data: {
      mode: $('presetMode').value,
      template: $('presetTemplate').value,
      jailbreak_id: +$('presetJailbreak').value || null,
      // owned by the scene rail, same as samplers — keep what the preset had
      thinking: (existing && existing.data.thinking) ?? true,
      thinking_mode: (existing && existing.data.thinking_mode) || 'normal',
      thinking_prefill: (existing && existing.data.thinking_prefill) || '',
      prefill: (existing && existing.data.prefill) || '',
      // samplers are owned by the scene rail; keep whatever this preset
      // already had rather than stamping the rail's values onto it
      samplers: (existing && existing.data.samplers) || samplersFromInputs(),
    },
  };
  const r = await post('/api/presets' + (id ? '/' + id : ''), body);
  $('presetNote').textContent = r.error ? 'error: ' + r.error : `saved "${r.name}"`;
  $('presetNote').className = 'note ' + (r.error ? 'bad' : 'ok');
  await loadPresets();
};
$('presetDelete').onclick = async () => {
  const id = $('presetId').value;
  if (!id) return;
  await del('/api/presets/' + id);
  fillPresetForm(null); loadPresets();
};

// ── jailbreaks ───────────────────────────────────────────────────
async function loadJailbreaks() {
  const { rows } = await api('/api/jailbreaks');
  S.jailbreaks = rows || [];
  const list = $('jbList'), pick = $('presetJailbreak');
  list.innerHTML = '<option value="">— new jailbreak —</option>';
  pick.innerHTML = '<option value="">(none)</option>';
  for (const r of S.jailbreaks) {
    for (const sel of [list, pick]) {
      const o = document.createElement('option');
      o.value = r.id; o.textContent = r.name;
      sel.appendChild(o);
    }
  }
  list.onchange = () => {
    const r = S.jailbreaks.find((x) => String(x.id) === list.value);
    $('jbId').value = r ? r.id : '';
    $('jbName').value = r ? r.name : '';
    $('jbText').value = r ? (r.data.text || '') : '';
    $('jbNotes').value = r ? (r.data.notes || '') : '';
  };
}
$('jbSave').onclick = async () => {
  const id = $('jbId').value;
  const r = await post('/api/jailbreaks' + (id ? '/' + id : ''), {
    name: $('jbName').value.trim(),
    data: { text: $('jbText').value, notes: $('jbNotes').value },
  });
  $('jbNote').textContent = r.error ? 'error: ' + r.error : `saved "${r.name}"`;
  $('jbNote').className = 'note ' + (r.error ? 'bad' : 'ok');
  loadJailbreaks();
};
$('jbDelete').onclick = async () => {
  const id = $('jbId').value;
  if (!id) return;
  await del('/api/jailbreaks/' + id);
  $('jbId').value = ''; $('jbName').value = ''; $('jbText').value = ''; $('jbNotes').value = '';
  loadJailbreaks();
};
$('jbDraft').onclick = () => {
  $('jbNote').textContent = 'not wired yet, coming with the jailbreak library';
  $('jbNote').className = 'note';
};

// ── regex rules ──────────────────────────────────────────────────
// The chain runs top to bottom. Everything here is deliberately visible: a
// rule that cannot compile is listed with the reason rather than dropped,
// because a silent no-op is the single most confusing thing find/replace can
// do to you.

S.regex = [];

async function loadRegex() {
  const d = await api('/api/regex');
  S.regex = d.rows || [];
  const ul = $('rxList');
  ul.innerHTML = '';
  if (!S.regex.length) {
    ul.innerHTML = '<li class="mem-empty">No rules. Import a SillyTavern preset '
      + "or write one, she doesn't need any to work.</li>";
    return;
  }
  for (const r of S.regex) {
    const li = document.createElement('li');
    const where = [r.on_prompt ? 'prompt' : '', r.on_display ? 'display' : '']
      .filter(Boolean).join(' + ') || 'nowhere';
    li.innerHTML = `<span><b>${esc(r.name)}</b>
      <span class="hint">${esc(where)}</span></span>
      ${r.problem ? `<span class="badge warm" title="${esc(r.problem)}">broken</span>` : ''}
      <span class="badge${r.enabled ? '' : ' alt'}">${r.enabled ? 'on' : 'off'}</span>`;
    const tools = document.createElement('span');
    const toggle = document.createElement('button');
    toggle.className = 'mini-btn';
    toggle.textContent = r.enabled ? 'disable' : 'enable';
    toggle.onclick = async () => {
      await post('/api/regex', { ...r, enabled: !r.enabled });
      loadRegex();
    };
    const edit = document.createElement('button');
    edit.className = 'mini-btn';
    edit.textContent = 'edit';
    edit.onclick = () => rxEdit(r);
    const gone = document.createElement('button');
    gone.className = 'mini-btn';
    gone.textContent = '✕';
    gone.onclick = async () => { await del('/api/regex/' + r.id); loadRegex(); };
    tools.append(toggle, edit, gone);
    li.appendChild(tools);
    ul.appendChild(li);
  }
}

function rxEdit(r) {
  r = r || { name: '', pattern: '', replace: '', on_prompt: 0,
             on_display: 1, enabled: 1, ord: 0 };
  $('rxId').value = r.id || '';
  $('rxName').value = r.name || '';
  $('rxPattern').value = r.pattern || '';
  $('rxReplace').value = r.replace || '';
  $('rxOrd').value = r.ord || 0;
  $('rxOnPrompt').checked = !!r.on_prompt;
  $('rxOnDisplay').checked = !!r.on_display;
  $('rxEnabled').checked = !!r.enabled;
  $('rxEditor').hidden = false;
}

$('rxNew').onclick = () => rxEdit(null);
$('rxCancel').onclick = () => { $('rxEditor').hidden = true; };
$('rxSave').onclick = async () => {
  const r = await post('/api/regex', {
    id: $('rxId').value || undefined,
    name: $('rxName').value.trim() || 'rule',
    pattern: $('rxPattern').value,
    replace: $('rxReplace').value,
    ord: Number($('rxOrd').value) || 0,
    on_prompt: $('rxOnPrompt').checked,
    on_display: $('rxOnDisplay').checked,
    enabled: $('rxEnabled').checked,
  });
  $('rxNote').textContent = r.error ? r.error : `saved "${r.rule.name}"`;
  $('rxNote').className = 'note ' + (r.error ? 'bad' : 'ok');
  if (!r.error) { $('rxEditor').hidden = true; loadRegex(); }
};
$('rxImport').onclick = () => $('rxFile').click();
$('rxFile').onchange = async (ev) => {
  const f = (ev.target.files || [])[0];
  ev.target.value = '';
  if (!f) return;
  const b64 = await fileToB64(f);
  const dry = await post('/api/regex/import', { b64, dry_run: true });
  if (dry.error) {
    $('rxNote').textContent = dry.error;
    $('rxNote').className = 'note bad';
    return;
  }
  const sm = dry.summary;
  const bits = [`${sm.total} scripts`, `${sm.enabled} enabled`,
                `${sm.display} display`, `${sm.prompt} prompt`];
  if (sm.problems.length) bits.push(`${sm.problems.length} won't convert`);
  if (!confirm(`Import ${bits.join(' · ')}?`)) return;
  const r = await post('/api/regex/import', { b64 });
  $('rxNote').textContent = r.error ? r.error : `imported ${r.imported} rules`;
  $('rxNote').className = 'note ' + (r.error ? 'bad' : 'ok');
  loadRegex();
};

// ── lorebooks ────────────────────────────────────────────────────
// Invisible until one exists. `grep -rn "lore" web/` returned nothing before
// this, so hiding the whole surface is preserving the status quo rather than
// promising something — and it rests on [hidden]{display:none!important} in
// style.css, which CLAUDE.md flags as load-bearing for exactly this.
async function loadLore() {
  const { rows } = await api('/api/lorebooks'
    + (S.chat ? `?character_id=${S.chat.charId}&chat_id=${S.chat.id}` : ''));
  S.lore = rows || [];
  $('mtabLore').hidden = !S.lore.length;
  const ul = $('loreList');
  ul.innerHTML = '';
  if (!S.lore.length) {
    ul.innerHTML = '<li class="mem-empty">No lorebooks yet.</li>';
    renderLoreAttach();
    return;
  }
  for (const b of S.lore) {
    const li = document.createElement('li');
    li.className = 'mem-item';
    const main = document.createElement('div');
    main.className = 'chat-main';
    const t = document.createElement('b');
    t.textContent = b.name;               // model/file text: never innerHTML
    const meta = document.createElement('small');
    meta.className = 'chat-meta';
    const bits = [`${b.entries} entries`];
    if (b.always_on) bits.push(`${b.always_on} always-on`);
    if (b.links) bits.push(`${b.links} attachment${b.links > 1 ? 's' : ''}`);
    bits.push(b.source);
    meta.textContent = bits.join(' · ');
    main.appendChild(t); main.appendChild(meta);
    li.appendChild(main);
    if ((b.notes || []).length) {
      const warn = document.createElement('span');
      warn.className = 'cast-chip away';
      warn.textContent = 'partial';
      warn.title = b.notes.map((n) => `${n.n} × ${n.what}`).join('\n')
        + '\n\nImported and working; these parts I cannot honour yet.';
      li.appendChild(warn);
    }
    const on = document.createElement('button');
    on.className = 'mini-btn';
    on.textContent = b.enabled ? 'on' : 'off';
    on.title = b.enabled ? 'switch this book off' : 'switch it back on';
    on.onclick = async () => {
      await post('/api/lorebooks', { id: b.id, enabled: b.enabled ? 0 : 1 });
      loadLore();
    };
    const del = document.createElement('button');
    del.className = 'mini-btn danger-btn';
    del.textContent = '✕';
    del.title = 'delete this book and every attachment';
    del.onclick = async () => {
      if (!confirm(`Delete "${b.name}" and its ${b.entries} entries?`)) return;
      await del(`/api/lorebooks/${b.id}`);
      loadLore();
    };
    li.appendChild(on); li.appendChild(del);
    ul.appendChild(li);
  }
  renderLoreAttach();
}

// Attaching lives in the scene rail because that is where "what is in play
// right now" already lives. Four values mapping exactly onto lore_links rows:
// no link / (NULL, chat) / (character, NULL) / (NULL, NULL).
function renderLoreAttach() {
  const wrap = $('loreAttach');
  const books = S.lore || [];
  $('loreBlock').hidden = !books.length || !S.chat;
  $('loreCount').textContent = books.filter((b) => b.scope !== 'off').length;
  wrap.innerHTML = '';
  if (!books.length || !S.chat) return;
  for (const b of books) {
    const row = document.createElement('label');
    row.className = 'lore-row';
    const nm = document.createElement('span');
    nm.textContent = b.name;              // file text: never innerHTML
    nm.title = `${b.entries} entries · ${b.source}`;
    const sel = document.createElement('select');
    for (const [v, t] of [['off', 'off'], ['chat', 'this chat'],
                          ['character', 'her'], ['always', 'always']]) {
      const o = document.createElement('option');
      o.value = v; o.textContent = t;
      sel.appendChild(o);
    }
    sel.value = b.scope;
    sel.disabled = !b.enabled;
    sel.onchange = async () => {
      const r = await post('/api/lorebooks/link', {
        id: b.id, scope: sel.value,
        character_id: S.chat.charId, chat_id: S.chat.id });
      if (r.error) { toast(r.error); return; }
      toast(sel.value === 'off' ? `${b.name} detached`
        : `${b.name} → ${sel.options[sel.selectedIndex].textContent}`);
      loadLore();
    };
    row.appendChild(nm); row.appendChild(sel);
    wrap.appendChild(row);
  }
}

$('loreImport').onclick = () => $('loreFile').click();
$('loreFile').onchange = async (ev) => {
  const f = (ev.target.files || [])[0];
  ev.target.value = '';
  if (!f) return;
  const b64 = await fileToB64(f);
  const name = f.name.replace(/\.(json|png)$/i, '').slice(0, 80);
  const dry = await post('/api/lorebooks/import', { b64, name, dry_run: true });
  if (dry.error) {
    $('loreNote').textContent = dry.error;
    $('loreNote').className = 'note bad';
    return;
  }
  if (!confirm(loreConfirm(dry.summary))) return;
  const r = await post('/api/lorebooks/import', { b64, name });
  $('loreNote').textContent = r.error ? r.error
    : `imported ${r.summary.entries} entries — attach it in the scene rail`;
  $('loreNote').className = 'note ' + (r.error ? 'bad' : 'ok');
  loadLore();
};

// The copy IS the feature. The at-depth refusal leads because it is the
// largest fidelity gap and it is aimed at the only person who wants this.
function loreConfirm(sm) {
  const L = [`${sm.name} — ${sm.entries} entries.`];
  const depth = (sm.not_honoured || []).find((n) => n.what.startsWith('at-depth'));
  if (depth) {
    L.push(`\n${depth.n} of them ask to sit a few messages deep in the`
      + ` conversation and I can't do that yet — they'll land with the rest of`
      + ` the lore, which is weaker than SillyTavern.`);
  }
  L.push(`\nThis book is about ${sm.tokens.toLocaleString()} tokens.`
    + ` I'll show roughly 1,200 of it per turn, best matches first.`);
  if (sm.cjk_keys) {
    L.push(`(${sm.cjk_keys} of its keys aren't Latin script, so that token`
      + ` count is an undercount — my estimate assumes 4 characters a token.)`);
  }
  const bits = [];
  if (sm.always_on) bits.push(`${sm.always_on} always-on`);
  if (sm.disabled) bits.push(`${sm.disabled} switched off (staying off)`);
  for (const n of sm.not_honoured || []) {
    if (!n.what.startsWith('at-depth')) bits.push(`${n.n} use ${n.what}, which I don't do`);
  }
  if (bits.length) L.push('\n' + bits.join(' · '));
  for (const why of (sm.refused_by_us || []).slice(0, 3)) {
    L.push(`\nOne entry ${why} — imported switched off.`);
  }
  if (sm.whole_words_added) {
    L.push(`\nI match whole words, which SillyTavern leaves off. It stops a`
      + ` key like "age" firing on "message". If something stops firing, that's`
      + ` why.`);
  }
  L.push('\nEverything else comes across. Import?');
  return L.join('\n');
}

// ── workflows ────────────────────────────────────────────────────
async function loadWorkflows() {
  const { rows } = await api('/api/workflows');
  S.workflows = rows || [];
  const sel = $('wfList');
  sel.innerHTML = '<option value="">— new workflow —</option>';
  for (const w of S.workflows) {
    const o = document.createElement('option');
    o.value = w.id; o.textContent = `${w.name} · ${w.kind}`;
    sel.appendChild(o);
  }
  sel.onchange = () => {
    const w = S.workflows.find((x) => String(x.id) === sel.value);
    $('wfId').value = w ? w.id : '';
    $('wfName').value = w ? w.name : '';
    $('wfKind').value = w ? w.kind : 'image';
    $('wfJson').value = w ? JSON.stringify(w.data.workflow, null, 1) : '';
  };
  renderWfSummary();
}

// The shipped graphs and the user's own, in one list.
//
// This pane used to render only the sqlite `workflows` table, which nothing
// seeds — so a fresh install showed "None yet — she can't generate until you
// add one" while fourteen working graphs sat in the repo driving the entire
// studio. The message was not just unhelpful, it was false.
//
// Shipped entries are read-only on purpose: they are files on disk, spliced
// per run by wfpack, and there is no meaningful "edit" that could be saved
// back here. What this tab is actually for is bringing your own.
function renderWfSummary() {
  const ul = $('wfSummary');
  if (!ul) return;
  ul.innerHTML = '';
  const shipped = ((S.studio && S.studio.workflows) || []);
  for (const w of shipped) {
    const li = document.createElement('li');
    const short = (w.packs || []).length
      ? `<span class="badge warm" title="missing: ${esc((w.missing || []).join(', '))}">needs ${esc(w.packs.join(', '))}</span>`
      : '';
    li.innerHTML = `<span><b>${esc(w.label)}</b> <span class="hint">${esc(w.kind)}</span></span>`
      + `${short}<span class="badge alt">shipped</span>`;
    ul.appendChild(li);
  }
  for (const w of S.workflows) {
    const li = document.createElement('li');
    li.innerHTML = `<span><b>${esc(w.name)}</b></span><span class="badge">${esc(w.kind)}</span>`;
    ul.appendChild(li);
  }
  if (!shipped.length && !S.workflows.length) {
    ul.innerHTML = '<li class="mem-empty">Nothing loaded, check the server log.</li>';
  }
}
$('openWorkflows').onclick = () => openSettings('workflows');
$('wfSave').onclick = async () => {
  let wf;
  try { wf = JSON.parse($('wfJson').value); }
  catch (e) { $('wfNote').textContent = 'bad JSON: ' + e.message; $('wfNote').className = 'note bad'; return; }
  const id = $('wfId').value;
  const r = await post('/api/workflows' + (id ? '/' + id : ''), {
    name: $('wfName').value.trim(), kind: $('wfKind').value, data: { workflow: wf },
  });
  $('wfNote').textContent = r.error ? 'error: ' + r.error : `saved "${r.name}"`;
  $('wfNote').className = 'note ' + (r.error ? 'bad' : 'ok');
  loadWorkflows();
};
$('wfScan').onclick = async () => {
  let wf;
  try { wf = JSON.parse($('wfJson').value); }
  catch (e) { $('wfNote').textContent = 'bad JSON: ' + e.message; $('wfNote').className = 'note bad'; return; }
  const r = await post('/api/comfy/slots', { workflow: wf });
  const slots = Object.keys(r.slots || {});
  $('wfNote').textContent = slots.length ? 'slots: ' + slots.join(', ') : 'no {{slots}} found. add some';
  $('wfNote').className = 'note ' + (slots.length ? 'ok' : 'bad');
};
$('wfDelete').onclick = async () => {
  const id = $('wfId').value;
  if (!id) return;
  await del('/api/workflows/' + id);
  $('wfId').value = ''; $('wfJson').value = ''; $('wfName').value = '';
  loadWorkflows();
};

// ── comfy config ─────────────────────────────────────────────────
$('comfySave').onclick = async () => {
  await post('/api/config', { comfyui_url: $('comfyUrl').value.trim() });
  $('comfyBadge').textContent = $('comfyUrl').value.trim() ? 'configured' : 'offline';
  toast('comfy url saved');
};
$('comfyPing').onclick = async () => {
  $('comfyNote').textContent = 'poking…';
  $('comfyNote').className = 'note';
  const r = await post('/api/comfy/ping', { url: $('comfyUrl').value.trim() });
  if (r.ok) {
    $('comfyNote').textContent = 'alive: ' + (r.devices || []).join(', ');
    $('comfyNote').className = 'note ok';
    $('comfyBadge').textContent = 'online';
  } else {
    $('comfyNote').textContent = r.error || 'unreachable';
    $('comfyNote').className = 'note bad';
    $('comfyBadge').textContent = 'offline';
  }
};
$('toolsToggle').onchange = (e) => { S.tools = e.target.checked; saveUI(); };

// ── chat ─────────────────────────────────────────────────────────
async function openChat(charId, mode = 'rp') {
  if (!LLM_READY() && !(await pickModel())) {
    alert('no model available. check the topbar');
    return;
  }
  const c = typeof charId === 'object' ? charId
                                       : S.chars.find((x) => x.id === charId);
  if (!c) return;
  // She is already in the open scene. Navigating to her own solo chat here is
  // how group conversations got spread across chat instances — the user
  // clicks the character they want to speak next, the UI silently swaps to a
  // different chat that looks the same, and the reply lands there. Clicking
  // someone who is present hands her the turn instead.
  if (mode === 'rp' && S.chat && S.chat.mode === 'rp'
      && (S.cast || []).some((x) => x.present && !x.lead
                                    && String(x.character_id) === String(c.id))
      && S.sendAs !== String(c.id)) {
    S.sendAs = String(c.id);
    S.sendAsByChat[S.chat.id] = S.sendAs;
    $('sendAs').value = S.sendAs;
    saveUI();
    toast(`${c.name} is already in this scene — she answers next. `
          + `click her again if you really want her own separate chat`);
    return;
  }
  const key = c.id + ':' + mode;
  let chatId = S.chatsByChar[key];
  // chatsByChar is a last-opened CACHE, not the truth. It used to be the only
  // record that a chat existed, so clearing site data (or opening a second
  // browser) made openChat create another row and strand the old adventure in
  // sqlite forever — indistinguishable, from the outside, from deletion.
  if (!chatId) {
    const rows = await chatsFor(c.id, mode);
    if (rows.length) chatId = rows[0].id;
  }
  if (!chatId) chatId = await newChatFor(c, mode);
  if (!chatId) return;
  await openChatById(c, chatId, mode);
}

async function chatsFor(charId, mode) {
  const d = await api(`/api/chats?character_id=${charId}&mode=${mode}`);
  return (d && d.chats) || [];
}

async function newChatFor(c, mode, extra) {
  const r = await post('/api/chats/new', {
    character_id: c.id,
    persona_id: +$('personaSel').value || null,
    mode,
    greeting_index: +($('greetingSel').value || 0),
    ...(extra || {}),
  });
  if (r.error) { toast(r.error); return null; }
  S.chatsByChar[c.id + ':' + mode] = r.chat_id;
  return r.chat_id;
}

// ── her chats: every adventure, re-openable, deleted only on purpose ──
function ago(ts) {
  if (!ts) return '';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 129600) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

async function loadChatList() {
  if (!S.chat) return;
  const rows = await chatsFor(S.chat.charId, S.chat.mode);
  $('chatsCount').textContent = rows.length;
  const ul = $('chatList');
  ul.innerHTML = '';
  if (!rows.length) { ul.innerHTML = '<li class="mem-empty">Nothing yet.</li>'; return; }
  for (const row of rows) {
    const li = document.createElement('li');
    li.className = 'chat-item' + (row.id === S.chat.id ? ' on' : '');
    li.innerHTML = `<div class="chat-main"><b class="chat-title"></b>
        <small class="chat-meta"></small></div>
      <button class="mini-btn chat-ren" title="Rename">✎</button>
      <button class="mini-btn danger-btn chat-del" title="Delete this chat">✕</button>`;
    // title and snippet are model output — textContent, never innerHTML
    const tEl = li.querySelector('.chat-title');
    tEl.textContent = row.title;
    li.querySelector('.chat-meta').textContent =
      `${row.messages} msg · ${ago(row.updated)}${row.has_scenario ? ' · forged' : ''}`
      + (row.as_cast ? ` · in ${row.with}'s scene` : '');
    li.querySelector('.chat-main').onclick = () => {
      if (row.id === S.chat.id) return;
      const c = S.chars.find((x) => x.id === S.chat.charId);
      if (c) openChatById(c, row.id, S.chat.mode);
    };
    li.querySelector('.chat-ren').onclick = (e) => {
      e.stopPropagation();
      // built as an element, not by id — dynamic ids are invisible to
      // tests/test_frontend.py and it cannot check what it cannot see
      const inp = document.createElement('input');
      inp.className = 'chat-rename';
      inp.value = row.named ? row.title : '';
      inp.placeholder = row.title;
      tEl.replaceWith(inp);
      inp.focus();
      let done = false;
      const commit = async () => {
        if (done) return;
        done = true;
        await post(`/api/chats/${row.id}/title`, { title: inp.value });
        loadChatList();
      };
      inp.onblur = commit;
      inp.onkeydown = (ev) => {
        if (ev.key === 'Enter') { ev.preventDefault(); commit(); }
        if (ev.key === 'Escape') { done = true; loadChatList(); }
      };
    };
    li.querySelector('.chat-del').onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${row.title}" and its ${row.messages} messages? `
                   + 'That one really is gone for good.')) return;
      await del('/api/chats/' + row.id);
      if (row.id !== S.chat.id) { loadChatList(); return; }
      const c = S.chars.find((x) => x.id === S.chat.charId);
      const left = (await chatsFor(S.chat.charId, S.chat.mode))
        .filter((r) => r.id !== row.id);
      delete S.chatsByChar[S.chat.charId + ':' + S.chat.mode];
      if (left.length && c) { await openChatById(c, left[0].id, S.chat.mode); }
      else { S.chat = null; saveUI(); showEmpty(); }
      loadChars();
    };
    ul.appendChild(li);
  }
}

function LLM_READY() { return !!(S.llm && S.llm.model); }

// Ensure a model is selected, re-probing backends if the page loaded before
// the user's LLM server was up. Returns true when S.llm is usable.
async function pickModel() {
  if (LLM_READY()) return true;
  await loadBackends();
  return LLM_READY();
}

// Bind the UI to an existing chat row. Used both by the roster and by the
// forge, which creates the chat itself so it can pass a scenario.
// A PLAIN chat has no character at all: preset, jailbreak, samplers and your
// persona, talking to the model as itself. `chats.character_id` was already
// nullable and assemble_blocks already degrades to persona + history when the
// card fields are empty, so this is a shell rather than a second prompt path.
const PLAIN = { id: null, name: 'the model', avatar: '',
                data: { fields: {} }, plain: true };

async function openPlainChat() {
  const r = await post('/api/chats/new', { mode: 'rp',
                                           persona_id: +$('personaSel').value || null });
  if (r.error) { toast(r.error); return; }
  await openChatById(PLAIN, r.chat_id, 'rp');
}

async function openChatById(c, chatId, mode) {
  const plain = !c || c.id == null;
  if (plain) c = PLAIN;
  S.chat = { id: chatId, mode, charId: c.id, name: c.name, avatar: c.avatar,
             plain };
  if (c.avatar) {
    $('herImg').src = '/api/avatars/' + c.avatar;
    $('herImg').hidden = false;
    $('herNoAva').hidden = true;
  } else {
    $('herImg').hidden = true;
    $('herNoAva').hidden = false;
  }
  $('herName').textContent = c.name;
  $('herRole').textContent = plain ? 'no card'
    : (mode === 'sms' ? 'texting' : 'roleplay');
  const alts = ((c.data || {}).fields
                && c.data.fields.alternate_greetings) || [];
  const gsel = $('greetingSel');
  gsel.innerHTML = '<option value="0">first_mes (default)</option>';
  alts.forEach((_, i) => {
    const o = document.createElement('option');
    o.value = i + 1; o.textContent = `alternate #${i + 1}`;
    gsel.appendChild(o);
  });
  $('emptyState').hidden = true;
  $('chatHead').hidden = false;
  $('stream').hidden = false;
  $('composer').hidden = false;
  $('chatWho').textContent = c.name;
  $('chatSub').textContent = plain ? 'plain chat — no character loaded'
    : (mode === 'sms' ? 'sms sidechat' : 'roleplay');
  $('chatAva').innerHTML = c.avatar
    ? `<img src="/api/avatars/${c.avatar}" style="width:100%;height:100%;object-fit:cover;border-radius:9px">`
    : (plain ? '◇' : '♡');
  $('modeBadge').hidden = mode !== 'sms';
  $('stream').className = 'stream' + (mode === 'sms' ? ' sms' : '');
  // Keyed on the character, so a plain chat has nothing to remember it by —
  // it is reopened from the chat list like any other.
  if (!plain) S.chatsByChar[c.id + ':' + mode] = chatId;
  // The director channel belongs to the scene it was opened over. Switching
  // chats swaps the bar's text, its open/closed state AND the forced-speaker
  // pick for this chat's own (usually: closed, empty, auto) — so direction
  // typed in one adventure cannot silently steer another, and an open bar in
  // one scene does not inject the note channel everywhere.
  S.director = S.directorByChat[chatId] || '';
  $('directorInput').value = S.director;
  S.directorOn = !!S.directorOnByChat[chatId];
  $('directorBar').hidden = !S.directorOn;
  $('btnDirector').classList.toggle('on', S.directorOn);
  S.sendAs = S.sendAsByChat[chatId] || 'auto';
  saveUI();
  loadChars();
  await loadChat();
  loadChatList();
  loadMemories();
  loadGallery();
}

async function loadChat() {
  if (!S.chat) return;
  const d = await api('/api/chats/' + S.chat.id);
  if (!d || d.error || !Array.isArray(d.messages)) {
    $('chatSub').textContent = 'chat missing (db reset?). start a new one';
    return;
  }
  // A chat can now be reached from a GUEST's chat list, so the identity the
  // caller passed may be the guest's. The chat's lead is what the header,
  // the unstamped-message fallback, the gallery and the memory panel key
  // off — realign to the server's answer rather than trusting the door the
  // user came in through.
  if (!S.chat.plain && d.chat && d.chat.character_id
      && d.chat.character_id !== S.chat.charId) {
    S.chat.charId = d.chat.character_id;
    S.chat.name = d.character || S.chat.name;
    S.chat.avatar = d.avatar || '';
    $('chatWho').textContent = S.chat.name;
    $('herName').textContent = S.chat.name;
    if (S.chat.avatar) {
      $('herImg').src = '/api/avatars/' + S.chat.avatar;
      $('herImg').hidden = false;
      $('herNoAva').hidden = true;
      $('chatAva').innerHTML = `<img src="/api/avatars/${S.chat.avatar}" `
        + 'style="width:100%;height:100%;object-fit:cover;border-radius:9px">';
    }
    saveUI();
    loadChatList();
    loadMemories();
    loadGallery();
  }
  // BEFORE the render loop, not after. buildMsg resolves each reply's face,
  // name and reason out of S.cast, so assigning it afterwards meant the first
  // load of a chat drew every bubble against the PREVIOUS chat's cast (or
  // none at all) and only came right on the next loadChat. That was already
  // true of the per-speaker avatar; the reason chip just made it visible.
  S.cast = d.cast || [];
  S.castActive = !!d.cast_active;
  S.speakerNext = d.next_speaker || '';
  // The attach select shows the state FOR THIS CONTEXT, so it has to be
  // re-read when the context changes.
  if ((S.lore || []).length) loadLore();
  const frag = document.createDocumentFragment();
  for (const m of d.messages) {
    try { frag.appendChild(buildMsg(m.role, m.content, m)); }
    catch (e) {
      console.error('render failed', m, e);
      const div = document.createElement('div');
      div.className = 'msg ' + (m.role || 'assistant');
      div.innerHTML = `<div class="msg-body"><div class="bubble">${esc(m.content)}</div></div>`;
      frag.appendChild(div);
    }
  }
  const box = $('stream');
  // Re-rendering must not yank a reader back to the bottom. send() calls this
  // the moment a reply finishes, so someone who scrolled up mid-stream to
  // reread something got thrown to the end the instant she stopped writing.
  const wasNear = nearBottom(box);
  const keep = box.scrollTop;
  box.innerHTML = '';
  box.appendChild(frag);
  if (wasNear) box.scrollTop = box.scrollHeight;
  else box.scrollTop = keep;
  // her most recent note back, so reopening a chat doesn't lose the thread
  // of what you two were planning
  renderCast();
  const notes = d.messages.filter((m) => m.director);
  showDirectorNote(notes.length ? notes[notes.length - 1].director : '');
  syncExamples(d);
}

function buildMsg(role, content, meta = {}) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  // The "say it out loud" recipe speaks her *last* message, so the id has to
  // be reachable from the DOM rather than only from the last fetch.
  if (meta.id) div.dataset.msgId = String(meta.id);
  const isHer = role === 'assistant';
  // In a cast scene the reply belongs to whoever spoke, not to the lead.
  // meta.speaker is stamped on the TAKE, so a re-roll as someone else carries
  // the right face; without a cast it is absent and this is the old path.
  const spk = isHer && meta.speaker
    ? (S.cast || []).find((c) => String(c.character_id) === String(meta.speaker))
    : null;
  const face = spk ? spk.avatar : (S.chat && S.chat.avatar);
  const ava = isHer && face
    ? `<img class="msg-ava" src="/api/avatars/${face}" alt="">`
    : `<div class="msg-ava">${isHer ? '♡' : '☻'}</div>`;
  const who = isHer ? (spk ? spk.name : (S.chat ? S.chat.name : 'her')) : 'you';
  // Why she got the turn. Only in a cast scene, and only when the reply was
  // actually routed — this is what stops `auto` reading as a coin flip. The
  // reason is a fixed vocabulary from the server, but esc() it anyway: this
  // is the same div the model-supplied name goes into.
  const chip = (isHer && meta.reason && (S.cast || []).length > 1)
    ? `<span class="msg-why">${esc(meta.reason)}</span>` : '';
  let inner = `<div class="msg-who">${esc(who)}${chip}</div>`;
  if (meta.think) {
    inner += `<details class="think"><summary>her thoughts</summary>
      <div class="think-body">${esc(meta.think)}</div></details>`;
  }
  inner += `<div class="bubble">${meta.html ? fmtHtml(content) : fmt(content)}</div>`;
  if (meta.id) {
    // meta.swipes is the total number of takes (0 on a message that has only
    // ever had one), meta.swipe_index points into that list.
    const tot = Math.max(1, meta.swipes || 0);
    const cur = (meta.swipes ? (meta.swipe_index ?? 0) : 0) + 1;
    inner += `<div class="msg-tools">
      ${isHer ? `<button class="mini-btn sw-l" title="Previous take">◀</button>
      <span class="swipe-info">${cur}/${tot}</span>
      <button class="mini-btn sw-r" title="Next take, or a new one at the end">▶</button>
      <button class="mini-btn rr" title="Another take">↻</button>
      ${(S.cast || []).filter((c) => c.present).length > 1
        ? `<button class="mini-btn rras" title="Re-roll as someone else here">↻ as…</button>` : ''}
      <button class="mini-btn say" title="Say it out loud, in her voice">🔊</button>` : ''}
      <button class="mini-btn ed" title="Edit this message">✎</button>
      <button class="mini-btn danger-btn rm" title="Delete this message">✕</button>
    </div>`;
  }
  div.innerHTML = `${ava}<div class="msg-body">${inner}</div>`;
  // Anything generated FOR this message travels with it. _chat_detail has
  // always returned these; nothing ever rendered them, so a finished clip
  // only existed inside the throwaway approval card.
  //
  // This strip lives in the `assets` table keyed on message_id and is NEVER
  // part of the message — engine.assemble reduces every history turn to
  // {role, content}, so a chat full of renders costs the model nothing.
  if ((meta.assets || []).length) {
    const strip = document.createElement('div');
    strip.className = 'msg-gallery';
    for (const asset of meta.assets) strip.appendChild(assetCell(asset, strip));
    div.querySelector('.msg-body').appendChild(strip);
  }
  if (!meta.id) return div;

  // `current` tracks the live text so repeated edits start from what is on
  // screen, not from whatever this message said when the page loaded.
  let current = content;
  const bubble = div.querySelector('.bubble');
  if (isHer && div.querySelector('.sw-l')) {
    let swIdx = meta.swipes ? (meta.swipe_index ?? 0) : 0;
    let swTot = Math.max(1, meta.swipes || 0);
    div.querySelector('.sw-l').onclick = async () => {
      if (swIdx <= 0) { toast("that's the first take"); return; }
      const r = await swipe(meta.id, swIdx - 1, div);
      if (r && r.ok) { swIdx = r.index; swTot = r.total; }
    };
    div.querySelector('.sw-r').onclick = async () => {
      if (swIdx < swTot - 1) {
        const r = await swipe(meta.id, swIdx + 1, div);
        if (r && r.ok) { swIdx = r.index; swTot = r.total; }
        return;
      }
      await rerollMsg(meta.id, div);   // at the end: make a new one
    };
    div.querySelector('.rr').onclick = () => rerollMsg(meta.id, div);
    const rras = div.querySelector('.rras');
    if (rras) rras.onclick = async () => {
      // Everyone present EXCEPT whoever holds this take already.
      const others = (S.cast || []).filter(
        (c) => c.present && String(c.character_id) !== String(meta.speaker));
      if (!others.length) { toast('nobody else is in the room'); return; }
      // Same idiom as `+ someone` on the cast strip: name it, loosely.
      const name = prompt('re-roll this as…\n\n'
        + others.map((c) => c.name).join('\n'));
      if (!name) return;
      const want = name.trim().toLowerCase();
      const pick = others.find((c) => c.name.toLowerCase() === want)
        || others.find((c) => c.name.toLowerCase().includes(want));
      if (!pick) { toast('nobody here by that name'); return; }
      await rerollMsg(meta.id, div, pick.character_id);
    };
    div.querySelector('.say').onclick = () => speakMsg(meta.id);
  }
  div.querySelector('.ed').onclick = () => {
    if (div.querySelector('.edit-box')) return;
    const box = document.createElement('div');
    box.className = 'edit-box';
    box.innerHTML = `<textarea class="edit-text" rows="6"></textarea>
      <div class="edit-btns">
        <button class="mini-btn primary-btn save">save</button>
        <button class="mini-btn cancel">cancel</button>
        <span class="tc-status st"></span>
      </div>`;
    box.querySelector('.edit-text').value = current;
    bubble.hidden = true;
    bubble.after(box);
    const ta = box.querySelector('.edit-text');
    ta.focus();
    const close = () => { box.remove(); bubble.hidden = false; };
    box.querySelector('.cancel').onclick = close;
    box.querySelector('.save').onclick = async () => {
      const text = ta.value.trim();
      if (!text) { box.querySelector('.st').textContent = 'say something'; return; }
      box.querySelector('.st').textContent = 'saving…';
      const r = await post('/api/messages/' + meta.id, { content: text });
      if (r.error) { box.querySelector('.st').textContent = 'failed: ' + r.error; return; }
      current = text;
      bubble.innerHTML = fmt(text);
      close();
    };
    // ctrl/cmd+enter saves, escape cancels
    ta.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); box.querySelector('.save').click(); }
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
  };
  div.querySelector('.rm').onclick = async () => {
    const r = await del('/api/messages/' + meta.id);
    if (!r.ok) { toast('could not delete'); return; }
    div.remove();
  };
  return div;
}

function appendMsg(role, content, meta) {
  const box = $('stream');
  const el = buildMsg(role, content, meta);
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
  return el;
}

// `render` lets the phone reuse this instead of growing a second swipe
// caller — shipping swipe arrows in a second UI on top of an unreachable
// take 1 would have shipped the same lie twice.
async function swipe(msgId, index, div, render) {
  const r = await post(`/api/messages/${msgId}/swipe`, { index });
  if (!r.ok) {
    toast('only one take of this one. hit ↻ to make another');
    return r;
  }
  // Render exactly as loadChat would: the server already applied macros and
  // the display regex rules, and told us whether the result is markup.
  if (render) render(r.content);
  else div.querySelector('.bubble').innerHTML = r.html ? fmtHtml(r.content) : fmt(r.content);
  // the counter reports where the server SETTLED, not what we asked for
  div.querySelector('.swipe-info').textContent = `${r.index + 1}/${r.total}`;
  // thinking travels with the take
  const old = render ? null : div.querySelector('details.think');
  if (r.think) {
    if (old) { old.querySelector('.think-body').textContent = r.think; }
    else {
      const det = document.createElement('details');
      det.className = 'think';
      det.innerHTML = '<summary>her thoughts</summary><div class="think-body"></div>';
      det.querySelector('.think-body').textContent = r.think;
      div.querySelector('.msg-who').after(det);
    }
  } else if (old) { old.remove(); }
  return r;
}

// ── avatar hover preview ─────────────────────────────────────────
// The chat avatars are 32px. Delegated from document so it keeps working
// across every re-render of the stream.
const avaPop = document.createElement('div');
avaPop.className = 'ava-pop';
avaPop.hidden = true;
avaPop.innerHTML = '<img alt="">';
document.body.appendChild(avaPop);

const AVA_SEL = 'img.msg-ava, #chatAva img, #herImg';
document.addEventListener('mouseover', (e) => {
  const img = e.target.closest && e.target.closest(AVA_SEL);
  if (!img || !img.getAttribute('src')) return;
  avaPop.querySelector('img').src = img.src;
  avaPop.hidden = false;
  // Sized off the viewport and letterboxed, not a fixed 280x420 centre-crop:
  // a 896x1184 portrait was losing its top and bottom to `object-fit: cover`.
  const r = img.getBoundingClientRect();
  const w = Math.min(360, Math.round(window.innerWidth * 0.3));
  const h = Math.min(520, window.innerHeight - 24);
  avaPop.style.setProperty('--pop-w', w + 'px');
  avaPop.style.setProperty('--pop-h', h + 'px');
  let left = r.right + 12;
  if (left + w > window.innerWidth) left = r.left - w - 12;
  avaPop.style.left = Math.max(8, Math.min(left, window.innerWidth - w - 8)) + 'px';
  avaPop.style.top = Math.max(8, Math.min(r.top - 30, window.innerHeight - h - 8)) + 'px';
});
document.addEventListener('mouseout', (e) => {
  if (e.target.closest && e.target.closest(AVA_SEL)) avaPop.hidden = true;
});
window.addEventListener('scroll', () => { avaPop.hidden = true; }, true);

// ── the viewer: any picture, at the size the window can actually give it ──
// The hover pop is a peek. This is the look. Everything that used to be a
// crop, a 280px box, or window.open() into a bare tab comes here instead.
const VIEW_SEL = 'img.msg-ava, #chatAva img, #herImg, img.chat-media,'
               + ' img.gal-thumb, video.chat-media, #cvRefPreview, img.ref-thumb';
let viewerEl = null;

function openViewer(src, kind) {
  if (!viewerEl) {
    viewerEl = document.createElement('div');
    viewerEl.className = 'viewer';
    viewerEl.innerHTML = '<button class="viewer-x" type="button" title="Close">✕</button>'
      + '<div class="viewer-stage"></div>'
      + '<div class="viewer-bar"><a class="mini-btn viewer-open" target="_blank" rel="noopener">open original</a></div>';
    viewerEl.onclick = (ev) => {
      if (ev.target === viewerEl || ev.target.closest('.viewer-x')) closeViewer();
    };
    document.body.appendChild(viewerEl);
  }
  const stage = viewerEl.querySelector('.viewer-stage');
  stage.innerHTML = '';
  const el = document.createElement(kind === 'video' ? 'video' : 'img');
  el.src = src;
  if (kind === 'video') { el.controls = true; el.autoplay = true; el.loop = true; }
  stage.appendChild(el);
  viewerEl.querySelector('.viewer-open').href = src;
  viewerEl.classList.add('on');
  avaPop.hidden = true;
}

function closeViewer() {
  if (!viewerEl) return;
  viewerEl.classList.remove('on');
  viewerEl.querySelector('.viewer-stage').innerHTML = '';   // stop any video
}

document.addEventListener('click', (e) => {
  const el = e.target.closest && e.target.closest(VIEW_SEL);
  if (!el || !el.getAttribute('src')) return;
  // Don't hijack a click meant for the control underneath it.
  if (e.target.closest('button, a, .msg-tools')) return;
  e.preventDefault();
  openViewer(el.src, el.tagName === 'VIDEO' ? 'video' : 'image');
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeViewer();
});

// Read one of her messages out loud, in her voice. Builds its own body on
// purpose — requestBody() is for the chat routes and its `text` field is
// exactly what used to hijack this.
async function speakMsg(msgId) {
  if (!S.chat) return;
  toast('working out what to say…');
  const d = await post('/api/studio/draft', {
    recipe: 'speak',
    chat_id: S.chat.id,
    character_id: S.chat.charId,
    message_id: msgId,
    preset_id: S.presetId || undefined,
  });
  if (d.error) { toast('cannot speak that: ' + d.error); return; }
  showStudioDraft(d);
}

// ── attachments (local vision) ───────────────────────────────────
$('btnAttach').onclick = () => $('imgFile').click();
$('imgFile').onchange = async (ev) => {
  for (const f of ev.target.files) {
    const b64 = await fileToB64(f);
    S.attachments.push({ name: f.name, b64, dataUrl: `data:${f.type};base64,${b64}` });
  }
  ev.target.value = '';
  renderAttachments();
};
document.addEventListener('paste', async (ev) => {
  if ($('composer').hidden) return;
  for (const item of ev.clipboardData.items) {
    if (item.type.startsWith('image/')) {
      const f = item.getAsFile();
      const b64 = await fileToB64(f);
      S.attachments.push({ name: 'pasted.png', b64, dataUrl: `data:${item.type};base64,${b64}` });
      renderAttachments();
    }
  }
});
function renderAttachments() {
  const strip = $('attachStrip');
  strip.innerHTML = '';
  S.attachments.forEach((a, i) => {
    const d = document.createElement('div');
    d.className = 'thumb';
    d.innerHTML = `<img src="${a.dataUrl}" alt=""><button class="x">✕</button>`;
    d.querySelector('.x').onclick = () => { S.attachments.splice(i, 1); renderAttachments(); };
    strip.appendChild(d);
  });
}

// ── director ─────────────────────────────────────────────────────
// Two-way: what you type is invisible stage direction she obeys, and while
// the bar is open she answers in the same channel — a fenced block the
// server cuts out of her reply so it never lands in the prose.
$('btnDirector').onclick = () => {
  const bar = $('directorBar');
  S.directorOn = bar.hidden;          // about to become visible?
  bar.hidden = !S.directorOn;
  $('btnDirector').classList.toggle('on', S.directorOn);
  if (S.chat) {
    if (S.directorOn) S.directorOnByChat[S.chat.id] = true;
    else delete S.directorOnByChat[S.chat.id];
  }
  if (S.directorOn) $('directorInput').focus();
  saveUI();
};
$('directorInput').oninput = (e) => {
  S.director = e.target.value.trim();
  if (S.chat) {
    if (S.director) S.directorByChat[S.chat.id] = S.director;
    else delete S.directorByChat[S.chat.id];
  }
  saveUI();
};
$('directorNotes').onchange = (e) => {
  S.directorNotes = e.target.checked;
  if (!S.directorNotes) showDirectorNote('');
  saveUI();
};
$('directorClear').onclick = () => {
  $('directorInput').value = '';
  S.director = '';
  if (S.chat) delete S.directorByChat[S.chat.id];
  showDirectorNote('');
  saveUI();
};

function showDirectorNote(note) {
  $('directorNote').hidden = !note;
  $('directorNoteBody').textContent = note || '';   // never trust as markup
}

// Stick to the bottom only when you are already there. Forcing the view down
// on every streamed chunk makes it impossible to scroll up and reread
// anything while she is still writing — which is exactly when you want to.
function nearBottom(el, slack = 120) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= slack;
}
function stick(el, wasNear) {
  if (wasNear) el.scrollTop = el.scrollHeight;
}

// ── send ─────────────────────────────────────────────────────────
async function send(regen = false) {
  if (!S.chat || S.busy) return;
  const input = $('input');
  const text = regen ? '' : input.value.trim();
  if (!regen && !text && !S.attachments.length) return;

  S.busy = true;
  $('btnSend').disabled = true;
  setStatus('busy', 'generating…');

  if (!regen) {
    appendMsg('user', text || '(sent an image)');
    input.value = '';
  }
  const replyEl = appendMsg('assistant', '');
  const bubble = replyEl.querySelector('.bubble');
  bubble.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';

  const body = requestBody(regen);
  if (!regen) body.text = text || '(sent an image)';

  const thinkWasOpen = await streamInto(body, replyEl, bubble);
  S.attachments = []; renderAttachments();
  S.busy = false;
  $('btnSend').disabled = false;
  refreshModelStatus();
  // The user may have opened another chat while she was writing — the reply
  // is stored under the chat it was sent from either way, but repainting
  // HERE would redraw whatever chat is now open and re-open ITS thought
  // block. Only refresh the view that this send still owns.
  if (!S.chat || S.chat.id !== body.chat_id) return;
  await loadChat();
  if (thinkWasOpen) {
    const last = $('stream').querySelector('.msg:last-child details.think');
    if (last) last.open = true;
  }
  loadMemories();
}

// Re-dress a live reply as whoever is actually writing it. The server
// announces {speaker: {id, name, avatar, reason}} before the first token;
// without this the streaming bubble wears the LEAD's name and face until the
// post-stream reload, and in a cast scene that is a visible misattribution
// for the whole time she is typing.
function applySpeaker(replyEl, spk) {
  const who = replyEl.querySelector('.msg-who');
  if (who) {
    who.textContent = spk.name || 'her';           // never trust as markup
    if (spk.reason && (S.cast || []).length > 1) {
      const chip = document.createElement('span');
      chip.className = 'msg-why';
      chip.textContent = spk.reason;
      who.appendChild(chip);
    }
  }
  const old = replyEl.querySelector('.msg-ava');
  if (old && spk.avatar) {
    const im = document.createElement('img');
    im.className = 'msg-ava';
    im.src = '/api/avatars/' + spk.avatar;
    im.alt = '';
    old.replaceWith(im);
  }
}

// The one SSE reader for the main chat. send() and rerollMsg() both use it,
// because two divergent consumers is how the phone ended up silently
// discarding every think frame. Returns whether the thought block was open,
// so the caller can restore it across the reload that follows.
async function streamInto(body, replyEl, bubble) {
  let thinkEl = null, rawText = '', rawThink = '';
  try {
    const resp = await fetch('/api/chats/send', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      const wasNear = nearBottom($('stream'));
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        let chunk;
        try { chunk = JSON.parse(payload); } catch { continue; }
        if (chunk.error) { bubble.innerHTML = `<span class="ooc">error: ${esc(chunk.error)}</span>`; toast('error: ' + chunk.error); continue; }
        if (chunk.done) continue;
        if (chunk.notice) { toast(chunk.notice); continue; }
        // Who is actually writing, announced BEFORE the first word. The live
        // bubble used to be built with no speaker at all, so in a cast scene
        // the whole reply streamed in under the lead's name and face and only
        // snapped to the right person on the reload afterwards — which read
        // as the wrong character answering.
        if (chunk.speaker) { applySpeaker(replyEl, chunk.speaker); continue; }
        if (chunk.director_note) { showDirectorNote(chunk.director_note); continue; }
        if (chunk.tool_pending) { showTool(chunk.tool_pending); continue; }
        // she asked for a shot by name — same approval card as the buttons
        if (chunk.studio_pending) { showStudioDraft(chunk.studio_pending); continue; }
        if (chunk.think !== undefined) {
          rawThink += chunk.think;
          if (!thinkEl) {
            thinkEl = document.createElement('details');
            thinkEl.className = 'think';
            // Plenty of models cannot be told to stop reasoning, and an
            // open block means the actual reply is pushed off-screen behind
            // a wall of it. Folded by default; the summary still shows it is
            // happening.
            thinkEl.open = false;
            thinkEl.innerHTML = '<summary>her thoughts <i class="think-live">'
              + 'thinking…</i></summary><div class="think-body"></div>';
            replyEl.querySelector('.msg-body').insertBefore(thinkEl, bubble);
          }
          thinkEl.querySelector('.think-body').textContent = rawThink;
          const live = thinkEl.querySelector('.think-live');
          // A collapsed block gives no sign it is filling up, so say so on
          // the summary rather than forcing it open.
          if (live) live.textContent = `thinking… ${rawThink.length} chars`;
        } else if (chunk.text) {
          rawText += chunk.text;
          bubble.innerHTML = fmt(stripBlocks(rawText));
        }
        stick($('stream'), wasNear);
      }
    }
  } catch (e) {
    bubble.innerHTML = `<span class="ooc">connection died: ${esc(e.message)}</span>`;
  }
  const live = thinkEl && thinkEl.querySelector('.think-live');
  if (live) live.remove();
  // loadChat() rebuilds every bubble from the server, which throws away a
  // thought block the user had opened mid-stream. Carry the one state that
  // was theirs to set.
  return !!(thinkEl && thinkEl.open);
}

// Another take of any of her replies, not just the last. The server rewinds
// the context to just before that message, so she answers the same moment
// again instead of the end of the scene, and stores the result as a swipe
// beside the take that is already there.
async function rerollMsg(msgId, div, asId) {
  if (!S.chat || S.busy) return;
  S.busy = true;
  $('btnSend').disabled = true;
  setStatus('busy', 'another take…');
  const bubble = div.querySelector('.bubble');
  const was = bubble.innerHTML;
  bubble.innerHTML = '<span class="typing"><i></i><i></i><i></i></span>';
  const body = Object.assign(requestBody(true), { swipe_message_id: msgId });
  // An explicit re-roll-as. Without it a re-roll keeps the take's own speaker
  // (the server's `same again` rule), which is what you want by default.
  if (asId) body.speaker_id = +asId;
  try {
    await streamInto(body, div, bubble);
  } catch (e) {
    bubble.innerHTML = was;
    toast('could not re-roll: ' + e.message);
  }
  S.busy = false;
  $('btnSend').disabled = false;
  refreshModelStatus();
  // Same guard as send(): don't repaint a chat this re-roll doesn't belong to.
  if (!S.chat || S.chat.id !== body.chat_id) return;
  await loadChat();
}

$('btnSend').onclick = () => send(false);
$('btnRegen').onclick = () => send(true);
$('input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(false); }
});
$('btnNewChat').onclick = async () => {
  if (!S.chat) return;
  const c = S.chars.find((x) => x.id === S.chat.charId);
  if (!c) return;
  const id = await newChatFor(c, S.chat.mode);
  if (!id) return;
  await openChatById(c, id, S.chat.mode);
  toast('new one started, the old adventure is right there in the list, relax');
};

// ── tool approval ────────────────────────────────────────────────
function showTool(tp) {
  const box = $('stream');
  const div = document.createElement('div');
  div.className = 'msg assistant';
  div.innerHTML = `
    <div class="msg-ava">✨</div>
    <div class="msg-body">
      <div class="tool-card">
        <div class="tc-head">
          <b>she wants to make something</b>
          <span class="badge">${esc((tp.call.action || '').replace('generate_', ''))}</span>
          ${tp.call.workflow ? `<span class="badge alt">${esc(tp.call.workflow)}</span>` : ''}
        </div>
        <textarea class="tc-prompt" rows="5">${esc(tp.prompt || '')}</textarea>
        <div class="tc-btns">
          <button class="primary-btn tc-go">make it ★</button>
          <button class="ghost-btn danger-btn tc-no">nah</button>
          <span class="tc-status"></span>
        </div>
      </div>
    </div>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
  const status = div.querySelector('.tc-status');
  const go = div.querySelector('.tc-go');
  go.onclick = async () => {
    go.disabled = true;
    status.textContent = 'generating on your box…';
    const r = await post('/api/tools/approve', {
      id: tp.id, prompt: div.querySelector('.tc-prompt').value,
    });
    if (r.error) { status.textContent = 'failed: ' + r.error; go.disabled = false; return; }
    status.textContent = `done · ${r.workflow}`;
    const body = div.querySelector('.tool-card');
    for (const a of r.assets) {
      let el;
      if (a.kind === 'audio') { el = document.createElement('audio'); el.controls = true; }
      else if (a.kind === 'videos' || a.kind === 'gifs') { el = document.createElement('video'); el.controls = true; el.loop = true; }
      else el = document.createElement('img');
      el.src = a.url; el.className = 'chat-media';
      body.appendChild(el);
    }
    box.scrollTop = box.scrollHeight;
  };
  div.querySelector('.tc-no').onclick = async () => {
    await post('/api/tools/reject', { id: tp.id });
    div.querySelector('.tool-card').innerHTML = '<span class="tc-status">rejected. she\'s pouting.</span>';
  };
}

// ── memory ───────────────────────────────────────────────────────
async function loadMemories() {
  if (!S.chat) return;
  const d = await api(`/api/chats/${S.chat.id}/memories`);
  const ul = $('memList');
  ul.innerHTML = '';
  const mems = (d && d.memories) || [];
  if (!mems.length) {
    ul.innerHTML = '<li class="mem-empty">Nothing yet. Give her something worth remembering.</li>';
    return;
  }
  const SCOPE_WHY = {
    user: 'about you, follows you to every character',
    character: 'between you and her, survives every chat with her',
    chat: 'this scene only, never leaks elsewhere',
  };
  for (const m of mems) {
    const li = document.createElement('li');
    li.className = 'mem-item';
    li.innerHTML = `<span class="mem-scope ${esc(m.kind)}" title="${esc(SCOPE_WHY[m.kind] || '')}">${esc(m.kind)}</span>`
      + `<span class="mem-text">${esc(m.content)}</span>`;
    const edit = document.createElement('button');
    edit.className = 'mini-btn'; edit.textContent = '✎';
    edit.title = 'edit';
    edit.onclick = () => editMemory(m, li);
    const x = document.createElement('button');
    x.className = 'mini-btn'; x.textContent = '✕';
    x.title = 'forget this one';
    x.onclick = async () => { await del('/api/memories/' + m.id); li.remove(); };
    li.append(edit, x);
    ul.appendChild(li);
  }
}

// Memories are the user's own profile — they get to rewrite what she knows,
// including which scope it lives at, which is what controls how far it
// follows them.
async function editMemory(m, li) {
  // Built as an element and queried through it. A dynamically-created id
  // would be invisible to test_frontend, which cross-checks every id the JS
  // looks up against the ones the HTML actually declares.
  const wrap = document.createElement('div');
  wrap.innerHTML = `
    <label>scope <select class="mem-scope-pick">
      <option value="user"${m.kind === 'user' ? ' selected' : ''}>about you, every character</option>
      <option value="character"${m.kind === 'character' ? ' selected' : ''}>you and her, every chat with her</option>
      <option value="chat"${m.kind === 'chat' ? ' selected' : ''}>this scene only</option>
    </select></label>`;
  const text = await dialog({
    title: m.id ? 'Edit memory' : 'New memory',
    body: wrap, input: 'she remembers that…', value: m.content || '',
    ok: 'save',
  });
  if (!text) return;
  const scope = (wrap.querySelector('.mem-scope-pick') || {}).value || m.kind;
  const r = await post('/api/memories', {
    id: m.id || null, scope, content: text,
    chat_id: S.chat.id, character_id: S.chat.charId,
  });
  if (r.error) { toast('failed: ' + r.error); return; }
  loadMemories();
}

$('memAdd').onclick = () => {
  if (!S.chat) { toast('open a chat first'); return; }
  editMemory({ id: null, kind: 'character', content: '' });
};

$('memTidy').onclick = async () => {
  const r = await post('/api/memories/tidy');
  if (r.error) { toast('failed: ' + r.error); return; }
  toast(r.removed ? `merged ${r.removed} duplicate(s)` : 'nothing to tidy');
  loadMemories();
};

$('memRemember').onclick = async () => {
  if (!S.chat) { toast('open a chat first'); return; }
  if (!LLM_READY()) { pickModel(); return; }
  const btn = $('memRemember');
  btn.disabled = true;
  const was = btn.textContent;
  btn.textContent = 'reading the whole scene…';
  const r = await post('/api/chats/remember',
    Object.assign(requestBody(), { chat_id: S.chat.id }));
  btn.disabled = false;
  btn.textContent = was;
  if (r.error) { toast('failed: ' + r.error); return; }
  toast(r.added
    ? `kept ${r.added} thing(s) from tonight`
    : 'nothing new, she already remembers this');
  loadMemories();
};
$('memRefresh').onclick = loadMemories;
$('memToggle').onchange = async (e) => {
  if (!S.chat) return;
  await post(`/api/chats/${S.chat.id}/memory`, { enabled: e.target.checked });
  toast(e.target.checked ? 'remembering' : 'memory off');
};
$('memWipe').onclick = async () => {
  if (!S.chat) return;
  const d = await api(`/api/chats/${S.chat.id}/memories`);
  for (const m of (d.memories || [])) await del('/api/memories/' + m.id);
  loadMemories();
  toast('she forgot everything');
};

// ── prompt inspector ─────────────────────────────────────────────
// Builds the request body exactly as send() would, then asks the server to
// assemble it through the SAME code path without sending or persisting.
function requestBody(regen = false) {
  const body = {
    chat_id: S.chat ? S.chat.id : 0,
    backend: S.llm.backend, model: S.llm.model,
    regenerate: regen, tools: S.tools,
    samplers: samplersFromInputs(),
    thinking_mode: $('thinkMode').value,
    thinking_prefill: $('thinkPrefill').value,
    reply_prefill: $('replyPrefill').value,
  };
  if (S.presetId) body.preset_id = +S.presetId;
  // Who replies. Sent from the ONE place the request body is built, so the
  // inspector previews the same speaker the send will actually use.
  // `auto` means "you decide" — omitting speaker_id is what asks the server
  // to route. Any other value is the human overriding, and the human wins.
  const as = $('sendAs').value;
  if (S.castActive && as && as !== 'auto') body.speaker_id = +as;
  // BOTH halves are gated on the bar being open. Closing the bar takes the
  // whole director channel out of the context on the very next turn — the
  // text is kept (per chat) for when the bar reopens, but a collapsed bar
  // sends nothing. It used to send S.director whenever the string was
  // non-empty, which persisted in localStorage: stage direction typed once
  // steered every later chat, invisibly, until the user found the ✕.
  if (S.directorOn && S.director) body.director = S.director;
  if (S.directorOn && S.directorNotes) body.director_notes = true;
  if (!regen) body.text = $('input').value.trim() || '(preview)';
  if (S.attachments.length) {
    body.images = S.attachments.map((a) => ({ name: a.name, b64: a.b64 }));
  }
  return body;
}

$('btnInspect').onclick = async () => {
  if (!S.chat) { toast('open a chat first'); return; }
  const r = await post('/api/chats/preview', requestBody(false));
  // Show failures inside the modal. A toast that vanishes in 2.6s while
  // nothing opens is indistinguishable from a dead button — and the most
  // common cause (no model selected) is one the user can act on.
  if (r.error) {
    $('inspMode').textContent = 'failed';
    $('inspModel').textContent = S.llm.model || 'no model';
    $('inspRendered').textContent = r.error
      + (S.llm.model ? '' : '\n\nNo model is selected. Pick one in the topbar.'
        + ' if the list is empty, start your backend and hit ⚙ → backends →'
        + ' probe again.');
    $('inspWire').textContent = '';
    $('inspStats').textContent = '';
    $('inspWarn').textContent = '⚠ nothing was sent';
    $('inspWarn').className = 'note bad';
    $('inspectBack').classList.add('on');
    return;
  }
  $('inspMode').textContent = r.mode + (r.template ? ' · ' + r.template : '');
  $('inspModel').textContent = r.model;
  $('inspRendered').textContent = r.rendered;
  $('inspWire').textContent = JSON.stringify(r.wire, null, 2);
  renderSegments(r.segments || []);
  const s = r.stats;
  $('inspStats').textContent =
    `${s.messages} message${s.messages === 1 ? '' : 's'} · ${s.chars.toLocaleString()} chars · ~${s.approx_tokens.toLocaleString()} tokens · system block ${s.system_chars.toLocaleString()} chars`;
  const warns = [];
  if (r.is_remote) warns.push('remote provider: reasoning prefill is stripped upstream, images are not sent');
  if (r.vision_fallback) warns.push('image attached: this turn uses the chat endpoint instead of raw completion, because /completions cannot carry pictures');
  if (r.prefill && r.is_remote) warns.push('reply prefill is emulated as an instruction here, not a true continuation');
  $('inspWarn').textContent = warns.length ? '⚠ ' + warns.join(' · ') : '';
  $('inspWarn').className = 'note' + (warns.length ? ' bad' : '');
  $('inspectBack').classList.add('on');
};
// Which block, jailbreak or card field produced each stretch of the prompt,
// with the two things you actually want next to it: switch it off, or edit
// the text. Naming the source without letting you act on it is half an answer.
function renderSegments(segments) {
  const box = $('inspSegments');
  box.innerHTML = '';
  if (!segments.length) {
    box.innerHTML = '<p class="note">nothing assembled</p>';
    return;
  }
  for (const seg of segments) {
    const head = document.createElement('div');
    head.className = 'seg-role';
    head.textContent = seg.role;
    box.appendChild(head);
    for (const part of seg.parts) {
      const el = document.createElement('div');
      el.className = 'seg';
      const blk = S.blk.list.find((b) => b.id === part.id);
      const kind = part.marker ? part.marker
        : (part.builtin ? 'built-in' : 'yours');
      el.innerHTML = `<div class="seg-head">
          <b></b>
          <span class="blk-tag ${part.builtin ? 'built' : 'role'}"></span>
          <span class="seg-tok"></span>
          <div class="spacer"></div>
        </div>
        <pre class="seg-body"></pre>`;
      el.querySelector('b').textContent = part.name || '(unnamed)';
      el.querySelector('.blk-tag').textContent = kind;
      el.querySelector('.seg-tok').textContent = '~' + part.tokens + 't';
      el.querySelector('.seg-body').textContent = part.content || '';
      const bar = el.querySelector('.seg-head');

      if (blk) {
        const off = document.createElement('button');
        off.className = 'mini-btn';
        off.textContent = blk.enabled ? 'turn off' : 'turn on';
        off.disabled = !S.blk.presetId;
        off.title = S.blk.presetId ? ''
          : 'no preset selected, so there is nowhere to save this';
        off.onclick = async () => {
          blk.enabled = !blk.enabled;
          S.blk.dirty = true;
          renderBlocks();
          if (!await saveBlocks(true)) {
            toast('pick a preset first, nowhere to save that');
            return;
          }
          $('btnInspect').click();      // re-preview, so this view is truth
        };
        bar.appendChild(off);
      }
      if (part.layer && !part.layer.startsWith('__')) {
        const ed = document.createElement('button');
        ed.className = 'mini-btn';
        ed.textContent = 'edit text';
        ed.onclick = () => {
          $('inspectBack').classList.remove('on');
          openSettings('prompts');
          const d = document.querySelector(`#mtab-prompts [data-layer="${part.layer}"]`);
          if (d) { d.open = true; d.scrollIntoView({ block: 'center' }); }
        };
        bar.appendChild(ed);
      }
      if (part.layer === '__jailbreak__') {
        const ed = document.createElement('button');
        ed.className = 'mini-btn';
        ed.textContent = 'edit jailbreak';
        ed.onclick = () => {
          $('inspectBack').classList.remove('on');
          openSettings('jailbreaks');
        };
        bar.appendChild(ed);
      }
      box.appendChild(el);
    }
  }
}

$('closeInspect').onclick = () => $('inspectBack').classList.remove('on');
$('inspectBack').onclick = (e) => { if (e.target === $('inspectBack')) $('inspectBack').classList.remove('on'); };
document.querySelectorAll('[data-itab]').forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll('[data-itab]').forEach((x) => x.classList.remove('on'));
    document.querySelectorAll('#inspectBack .modal-pane').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    $('itab-' + b.dataset.itab).classList.add('on');
  };
});
$('inspCopy').onclick = async () => {
  // the "by block" pane has one <pre> per segment, so join them
  const pane = document.querySelector('#inspectBack .modal-pane.on');
  const pres = [...pane.querySelectorAll('pre')];
  const text = pres.map((x) => x.textContent).join('\n\n');
  try { await navigator.clipboard.writeText(text); toast('copied'); }
  catch { toast('clipboard blocked, select and copy manually'); }
};

// ── starter library ──────────────────────────────────────────────
async function loadLibrary() {
  const lib = await api('/api/library');
  if (!lib || lib.error) return;
  const jb = $('libJb');
  jb.innerHTML = '';
  for (const j of lib.jailbreaks) {
    const d = document.createElement('details');
    d.className = 'lib-item';
    d.innerHTML = `<summary><b>${esc(j.name)}</b></summary>
      <p class="hint">${esc(j.notes)}</p>
      <pre class="prompt-dump small">${esc(j.text)}</pre>`;
    jb.appendChild(d);
  }
  const pr = $('libPresets');
  pr.innerHTML = '';
  for (const p of lib.presets) {
    const s = p.samplers;
    const d = document.createElement('details');
    d.className = 'lib-item';
    d.innerHTML = `<summary><b>${esc(p.name)}</b>
        <span class="badge">${esc(p.mode)}</span>
        ${p.mode === 'completion' ? `<span class="badge alt">${esc(p.template)}</span>` : ''}
        <span class="badge warm">think: ${esc(p.thinking_mode)}</span></summary>
      <p class="hint">${esc(p.note)}</p>
      <p class="note">temp ${s.temperature} · top_p ${s.top_p} · top_k ${s.top_k} · min_p ${s.min_p} · rep ${s.repetition_penalty} · max ${s.max_tokens}</p>`;
    pr.appendChild(d);
  }
}
$('libInstall').onclick = async () => {
  $('libNote').textContent = 'installing…';
  $('libNote').className = 'note';
  const r = await post('/api/library/install');
  if (r.error) { $('libNote').textContent = 'failed: ' + r.error; $('libNote').className = 'note bad'; return; }
  $('libNote').textContent = `installed ${r.presets.length} presets, ${r.jailbreaks.length} jailbreaks, pick one in the topbar`;
  $('libNote').className = 'note ok';
  await Promise.all([loadPresets(), loadJailbreaks()]);
  syncSceneFromPreset();
};

// ── scenario forge ───────────────────────────────────────────────
// Brainstorm a fresh situation, revise it conversationally, then launch a
// chat that starts already in motion instead of at a canned greeting.
let FORGE = [];   // current pitches, each {scenario, el}

function forgeFill() {
  const cs = $('forgeCharSel');
  cs.innerHTML = '';
  for (const c of S.chars) {
    const o = document.createElement('option');
    o.value = c.id; o.textContent = c.name;
    cs.appendChild(o);
  }
  if (S.chat) cs.value = S.chat.charId;
  const ps = $('forgePersonaSel');
  ps.innerHTML = '<option value="">(just me)</option>';
  for (const p of S.personas) {
    const o = document.createElement('option');
    o.value = p.id; o.textContent = p.name;
    ps.appendChild(o);
  }
  ps.value = $('personaSel').value || '';
  const c = S.chars.find((x) => String(x.id) === cs.value);
  $('forgeChar').textContent = c ? c.name : '—';
}

// The Forge is the whole character workshop: bring a card in, invent one,
// situate her in a scene, and set up who you are. Settings is for how the
// machine runs; none of this belongs there.
const FORGE_TABS = {
  cards:     { title: 'Cards',            char: true  },
  character: { title: 'Character forge',  char: false },
  feel:      { title: 'Card for that feel', char: false },
  scene:     { title: 'Scenario forge',   char: true  },
  you:       { title: 'You',              char: false },
};
function openForge(tab) {
  $('forgeBack').classList.add('on');
  if (tab) forgeTab(tab);
}
function forgeTab(tab) {
  const spec = FORGE_TABS[tab] || FORGE_TABS.cards;
  document.querySelectorAll('#forgeTabs .modal-tab').forEach((x) => x.classList.toggle('on', x.dataset.ftab === tab));
  document.querySelectorAll('#forgeBack .modal-pane').forEach((x) => x.classList.toggle('on', x.id === 'ftab-' + tab));
  $('forgeTitle').textContent = spec.title;
  $('forgeChar').hidden = !spec.char;
  $('forgeMem').hidden = true;
  if (tab === 'scene') {
    $('forgeResults').innerHTML = '';
    $('forgeNote').textContent = S.chars.length ? '' : 'Import or forge a character first.';
    FORGE = [];
    forgeFill();
  }
}
$('forgeBtn').onclick = () => openForge('scene');
$('closeForge').onclick = () => $('forgeBack').classList.remove('on');
$('forgeBack').onclick = (e) => { if (e.target === $('forgeBack')) $('forgeBack').classList.remove('on'); };
$('forgeCharSel').onchange = () => {
  const c = S.chars.find((x) => String(x.id) === $('forgeCharSel').value);
  $('forgeChar').textContent = c ? c.name : '—';
};

function forgeBody(extra) {
  return Object.assign({
    character_id: +$('forgeCharSel').value,
    persona_id: +$('forgePersonaSel').value || null,
    use_memory: $('forgeUseMem').checked,
    backend: S.llm.backend, model: S.llm.model,
  }, extra || {});
}

$('forgeGo').onclick = async () => {
  if (!S.chars.length) { toast('import or forge a character first'); return; }
  if (!LLM_READY()) { pickModel(); return; }
  const btn = $('forgeGo');
  btn.disabled = true;
  $('forgeNote').textContent = 'she\'s thinking up scenarios…';
  $('forgeNote').className = 'note';
  $('forgeResults').innerHTML = '';
  const r = await post('/api/scenarios/suggest', forgeBody({
    brief: $('forgeBrief').value, count: +$('forgeCount').value,
  }));
  btn.disabled = false;
  if (r.error) {
    $('forgeNote').textContent = 'failed: ' + r.error;
    $('forgeNote').className = 'note bad';
    return;
  }
  $('forgeNote').textContent = `${r.scenarios.length} pitched`;
  $('forgeNote').className = 'note ok';
  $('forgeMem').hidden = false;
  $('forgeMem').textContent = r.memory_count
    ? `${r.memory_count} memories used` : 'clean slate';
  FORGE = [];
  for (const s of r.scenarios) renderPitch(s);
};

function renderPitch(scenario) {
  const box = $('forgeResults');
  const card = document.createElement('div');
  card.className = 'pitch';
  const entry = { scenario, el: card };
  FORGE.push(entry);

  const draw = () => {
    const s = entry.scenario;
    card.innerHTML = `
      <div class="pitch-head">
        <b class="pitch-title"></b>
        <div class="spacer"></div>
        ${(s.tags || []).map((t) => `<span class="badge alt"></span>`).join('')}
      </div>
      <p class="pitch-setting"></p>
      <p class="pitch-premise"></p>
      <p class="pitch-hook"></p>
      <div class="pitch-open"><span class="pitch-open-label">opens with</span>
        <div class="pitch-open-text"></div></div>
      <div class="pitch-revise">
        <input class="pitch-input" placeholder="change something: rainier, she initiates, move it to the car…">
        <button class="mini-btn pitch-rev">revise</button>
      </div>
      <div class="pitch-btns">
        <button class="primary-btn pitch-go">start this scene ★</button>
        <button class="ghost-btn pitch-edit">edit by hand</button>
        <span class="tc-status pitch-status"></span>
      </div>`;
    // textContent everywhere: model output is never trusted as markup
    card.querySelector('.pitch-title').textContent = s.title;
    const tagEls = card.querySelectorAll('.pitch-head .badge');
    (s.tags || []).forEach((t, i) => { if (tagEls[i]) tagEls[i].textContent = t; });
    card.querySelector('.pitch-setting').textContent = s.setting || '';
    card.querySelector('.pitch-premise').textContent = s.premise || '';
    card.querySelector('.pitch-hook').textContent = s.hook ? '⟶ ' + s.hook : '';
    card.querySelector('.pitch-open-text').textContent = s.opening || '';

    card.querySelector('.pitch-go').onclick = () => launchScene(entry);
    card.querySelector('.pitch-edit').onclick = () => editPitch(entry, draw);
    const revise = async () => {
      const inp = card.querySelector('.pitch-input');
      const instruction = inp.value.trim();
      if (!instruction) return;
      const st = card.querySelector('.pitch-status');
      st.textContent = 'revising…';
      const r = await post('/api/scenarios/refine',
        forgeBody({ scenario: entry.scenario, instruction }));
      if (r.error) { st.textContent = 'failed: ' + r.error; return; }
      entry.scenario = r.scenario;
      draw();
      card.querySelector('.pitch-status').textContent = 'revised ✓';
    };
    card.querySelector('.pitch-rev').onclick = revise;
    card.querySelector('.pitch-input').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); revise(); }
    });
  };
  draw();
  box.appendChild(card);
}

function editPitch(entry, redraw) {
  const s = entry.scenario;
  const card = entry.el;
  card.innerHTML = `
    <div class="pitch-head"><b>editing by hand</b></div>
    <label>title <input class="e-title"></label>
    <label>setting <input class="e-setting"></label>
    <label>premise <textarea class="e-premise" rows="3"></textarea></label>
    <label>tension <input class="e-hook"></label>
    <label>her opening message <textarea class="e-open" rows="4"></textarea></label>
    <div class="pitch-btns">
      <button class="primary-btn e-save">keep changes</button>
      <button class="ghost-btn e-cancel">cancel</button>
    </div>`;
  card.querySelector('.e-title').value = s.title || '';
  card.querySelector('.e-setting').value = s.setting || '';
  card.querySelector('.e-premise').value = s.premise || '';
  card.querySelector('.e-hook').value = s.hook || '';
  card.querySelector('.e-open').value = s.opening || '';
  card.querySelector('.e-save').onclick = () => {
    entry.scenario = {
      ...s,
      title: card.querySelector('.e-title').value.trim(),
      setting: card.querySelector('.e-setting').value.trim(),
      premise: card.querySelector('.e-premise').value.trim(),
      hook: card.querySelector('.e-hook').value.trim(),
      opening: card.querySelector('.e-open').value.trim(),
    };
    redraw();
  };
  card.querySelector('.e-cancel').onclick = redraw;
}

async function launchScene(entry) {
  const charId = +$('forgeCharSel').value;
  const c = S.chars.find((x) => x.id === charId);
  const r = await post('/api/chats/new', {
    character_id: charId,
    persona_id: +$('forgePersonaSel').value || null,
    mode: 'rp',
    scenario: entry.scenario,
  });
  if (r.error) { toast('could not start: ' + r.error); return; }
  S.chatsByChar[charId + ':rp'] = r.chat_id;
  $('forgeBack').classList.remove('on');
  await openChatById(c, r.chat_id, 'rp');
  toast('scene started: ' + entry.scenario.title);
}

// ── editable prompt layers ───────────────────────────────────────
const GROUP_BLURB = {
  scene: 'Injected into the chat while you play.',
  cast: 'Only when more than one character is in the room. A solo chat never sees any of it.',
  forge: 'Used by the scenario forge when brainstorming.',
  system: 'Background machinery. Keep the output formats intact or the '
        + 'features that parse them will stop working.',
};

// key -> the text actually being sent, so the block list can price a built-in
// layer instead of reporting zero for it.
let PROMPT_TEXT = {};

async function loadPrompts() {
  const r = await api('/api/prompts');
  if (!r || !r.prompts) return;
  PROMPT_TEXT = {};
  for (const p of r.prompts) PROMPT_TEXT[p.key] = p.text ?? p.default ?? '';
  if (S.blk.list.length) renderBlocks();
  const wrap = $('promptGroups');
  wrap.innerHTML = '';
  const byGroup = {};
  for (const p of r.prompts) (byGroup[p.group] ||= []).push(p);
  for (const group of ['scene', 'cast', 'forge', 'system']) {
    const items = byGroup[group] || [];
    if (!items.length) continue;
    const block = document.createElement('div');
    block.className = 'block';
    block.innerHTML = `<div class="block-head"><h3>${esc(group)}</h3></div>
      <p class="hint">${esc(GROUP_BLURB[group] || '')}</p>`;
    for (const p of items) block.appendChild(promptEditor(p));
    wrap.appendChild(block);
  }
}

function promptEditor(p) {
  const d = document.createElement('details');
  d.className = 'lib-item';
  d.dataset.layer = p.key;      // so the inspector can jump straight to it
  d.innerHTML = `
    <summary>
      <b></b>
      ${p.customised ? '<span class="badge warm">edited</span>' : ''}
      ${p.placeholders.map(() => '<span class="badge alt"></span>').join('')}
    </summary>
    <p class="hint"></p>
    <textarea class="p-text" rows="8"></textarea>
    <div class="pitch-btns">
      <button class="mini-btn p-save">save</button>
      <button class="mini-btn p-reset">reset to default</button>
      <span class="tc-status p-status"></span>
    </div>`;
  d.querySelector('summary b').textContent = p.label;
  const phEls = d.querySelectorAll('summary .badge.alt');
  p.placeholders.forEach((ph, i) => {
    if (phEls[i]) phEls[i].textContent = '{' + ph + '}';
  });
  d.querySelector('.hint').textContent = p.desc;
  const ta = d.querySelector('.p-text');
  ta.value = p.text;
  const st = d.querySelector('.p-status');
  d.querySelector('.p-save').onclick = async () => {
    st.textContent = 'saving…';
    const r = await post('/api/prompts', { key: p.key, text: ta.value });
    if (r.error) { st.textContent = 'failed: ' + r.error; return; }
    st.textContent = 'saved ✓';
    loadPrompts();
  };
  d.querySelector('.p-reset').onclick = async () => {
    await post('/api/prompts/reset', { key: p.key });
    loadPrompts();
    toast('reset to default');
  };
  return d;
}

$('promptResetAll').onclick = async () => {
  await post('/api/prompts/reset', {});
  $('promptNote').textContent = 'everything back to shipped defaults';
  $('promptNote').className = 'note ok';
  loadPrompts();
};

// ── rail tabs / settings modal ───────────────────────────────────
document.querySelectorAll('.rail-tab').forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll('.rail-tab').forEach((x) => x.classList.remove('on'));
    document.querySelectorAll('.rail-pane').forEach((x) => x.classList.remove('on'));
    b.classList.add('on');
    $('rail-' + b.dataset.rail).classList.add('on');
    saveUI();
  };
});
// SCOPE THESE TO #settingsBack. The inspector's tabs and panes carry the same
// .modal-tab / .modal-pane classes, so the unscoped version bound the settings
// handler over the inspector's own tab handler (clicking "raw json" opened
// Settings on top of it) and blanked both inspector panes on the way past —
// after which the inspector reopened empty and looked broken.
function openSettings(tab) {
  $('settingsBack').classList.add('on');
  if (tab) {
    document.querySelectorAll('#settingsBack .modal-tab').forEach((x) => x.classList.toggle('on', x.dataset.mtab === tab));
    document.querySelectorAll('#settingsBack .modal-pane').forEach((x) => x.classList.toggle('on', x.id === 'mtab-' + tab));
  }
}
$('openSettings').onclick = () => openSettings();
$('closeSettings').onclick = () => $('settingsBack').classList.remove('on');
$('settingsBack').onclick = (e) => { if (e.target === $('settingsBack')) $('settingsBack').classList.remove('on'); };
document.querySelectorAll('#settingsBack .modal-tab').forEach((b) => {
  b.onclick = () => {
    openSettings(b.dataset.mtab);
    if (b.dataset.mtab === 'blocks') openBlocks();
  };
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  $('settingsBack').classList.remove('on');
  $('forgeBack').classList.remove('on');
});
$('reprobe').onclick = async () => { setStatus('busy', 'probing…'); await loadBackends(); toast('probed'); };
$('rbAdd').onclick = async () => {
  const cfg = await api('/api/config');
  const list = (cfg.remote_backends || []).filter((b) => b.label !== $('rbLabel').value.trim());
  list.push({ label: $('rbLabel').value.trim(), url: $('rbUrl').value.trim(), key: $('rbKey').value.trim() });
  await post('/api/config', { remote_backends: list });
  $('rbKey').value = '';
  toast('backend added');
  loadBackends();
};

// ── studio: recipes, approval, gallery, GPU ──────────────────────
// A recipe is a one-click shot. Draft first, always: the user sees and edits
// the prompt before anything is queued, because a bad draft costs a render.

S.studio = null;      // catalogue from /api/studio
S.recipe = null;      // the recipe whose options are open

async function loadStudio() {
  const d = await api('/api/studio');
  if (d.error) { $('studioBadge').textContent = 'unavailable'; return; }
  S.studio = d;
  renderWfSummary();   // boot loads this and loadWorkflows in parallel
  $('studioBadge').textContent = d.comfy ? 'ready' : 'no comfyui';
  $('studioBadge').className = 'badge' + (d.comfy ? '' : ' alt');

  const grid = $('recipeGrid');
  grid.innerHTML = '';
  for (const r of d.recipes) {
    const b = document.createElement('button');
    b.className = 'recipe-btn';
    b.innerHTML = `<span class="ri">${esc(r.icon)}</span><span class="rl">${esc(r.label)}</span>`;
    b.title = r.blurb;
    b.onclick = () => openRecipe(r);
    grid.appendChild(b);
  }

  const st = $('stageToggles');
  st.innerHTML = '';
  const on = (d.defaults.stages) || {};
  for (const [key, stage] of Object.entries(d.stages || {})) {
    const lab = document.createElement('label');
    lab.className = 'check';
    lab.title = stage.why;
    lab.innerHTML = `<input type="checkbox" data-stage="${esc(key)}"${on[key] ? ' checked' : ''}> ${esc(stage.label)}`;
    lab.querySelector('input').onchange = saveStages;
    st.appendChild(lab);
  }
  renderVram(d.vram);

  // Anything missing its node pack is worth saying out loud once, rather
  // than letting the user find out when a generation 400s.
  const short = d.workflows.filter((w) => w.packs && w.packs.length);
  if (short.length) {
    $('recipeNote').textContent =
      `${short.length} workflow(s) need node packs you don't have: ` +
      [...new Set(short.flatMap((w) => w.packs))].join(', ');
  }
}

async function saveStages() {
  const stages = {};
  document.querySelectorAll('#stageToggles input[data-stage]').forEach((i) => {
    if (i.checked) stages[i.dataset.stage] = true;
  });
  const studio = Object.assign({}, (S.studio && S.studio.defaults) || {}, { stages });
  delete studio.stages_labels;
  await post('/api/config', { studio });
  toast('quality saved');
}

function openRecipe(r) {
  S.recipe = r;
  $('recipeOptsBlock').hidden = false;
  $('recipeOptsTitle').textContent = `${r.icon} ${r.label}`;
  const box = $('recipeOpts');
  box.innerHTML = `<p class="hint">${esc(r.blurb)}</p>`;
  for (const [key, o] of Object.entries(r.options || {})) {
    const wrap = document.createElement('div');
    if (o.type === 'bool') {
      wrap.innerHTML = `<label class="check"><input type="checkbox" data-opt="${esc(key)}"${o.default ? ' checked' : ''}> ${esc(o.label)}</label>`;
    } else if (o.type === 'choice') {
      wrap.innerHTML = `<label>${esc(o.label)}<select data-opt="${esc(key)}">`
        + (o.values || []).map((v) => `<option${v === o.default ? ' selected' : ''}>${esc(v)}</option>`).join('')
        + '</select></label>';
    } else if (o.type === 'number') {
      wrap.innerHTML = `<label>${esc(o.label)}<input type="number" data-opt="${esc(key)}" value="${esc(o.default)}"></label>`;
    } else if (o.type === 'textarea') {
      wrap.innerHTML = `<label>${esc(o.label)}<textarea data-opt="${esc(key)}" rows="4" placeholder="${esc(o.desc || '')}"></textarea></label>`;
    } else {
      wrap.innerHTML = `<label>${esc(o.label)}<input type="text" data-opt="${esc(key)}" placeholder="${esc(o.desc || '')}"></label>`;
    }
    if (o.desc && o.type !== 'text' && o.type !== 'textarea') {
      const h = document.createElement('p'); h.className = 'hint'; h.textContent = o.desc;
      wrap.appendChild(h);
    }
    box.appendChild(wrap);
  }
  if (!Object.keys(r.options || {}).length) {
    box.innerHTML += '<p class="hint">Nothing to configure, just draft it.</p>';
  }
}

function recipeOpts() {
  const out = {};
  document.querySelectorAll('#recipeOpts [data-opt]').forEach((el) => {
    out[el.dataset.opt] = el.type === 'checkbox' ? el.checked
      : (el.type === 'number' ? Number(el.value) : el.value);
  });
  return out;
}

$('recipeClose').onclick = () => { $('recipeOptsBlock').hidden = true; S.recipe = null; };

$('recipeDraft').onclick = async () => {
  if (!S.recipe) return;
  if (!LLM_READY()) { pickModel(); return; }
  if (!S.chat) { toast('open a chat first, she needs a scene to work from'); return; }
  const btn = $('recipeDraft');
  btn.disabled = true;
  $('recipeNote').textContent = 'drafting…';
  const body = Object.assign(requestBody(), {
    recipe: S.recipe.id, opts: recipeOpts(),
    chat_id: S.chat.id, character_id: S.chat.charId,
  });
  // requestBody() builds a /api/chats/send body and ALWAYS sets `text` — to
  // the composer contents, or the literal "(preview)". The studio route has
  // no use for it and the speak recipe used to read it as "say this instead",
  // so every attempt to speak a message spoke the placeholder.
  delete body.text; delete body.images;
  // A recipe may declare fields it cannot work without. Generic, so no
  // recipe id is hardcoded here.
  for (const need of S.recipe.requires || []) {
    if (!String(body.opts[need] || '').trim()) {
      $('recipeNote').textContent = 'type what you want first, genius';
      btn.disabled = false;
      return;
    }
  }
  // "say it out loud" works on her last message, not on a fresh idea
  if (S.recipe.id === 'speak') {
    // [data-msg-id] matters: the studio card, the tool card and the live
    // streaming placeholder are all .msg.assistant with no id, so whenever
    // one was on screen the target was silently dropped.
    const last = [...document.querySelectorAll('#stream .msg.assistant[data-msg-id]')].pop();
    if (last) body.message_id = Number(last.dataset.msgId);
  }
  const d = await post('/api/studio/draft', body);
  btn.disabled = false;
  if (d.error) { $('recipeNote').textContent = 'failed: ' + d.error; return; }
  $('recipeNote').textContent = '';
  $('recipeOptsBlock').hidden = true;
  showStudioDraft(d);
};

// The approval card. Same shape as her own tool calls, plus the pre-flight
// warnings — the ones that cost a whole render to discover otherwise.
function showStudioDraft(d) {
  const box = $('stream');
  const div = document.createElement('div');
  div.className = 'msg assistant';
  const fields = Object.entries(d.values).filter(([k]) => k !== 'emotions');
  div.innerHTML = `
    <div class="msg-ava">✨</div>
    <div class="msg-body">
      <div class="tool-card">
        <div class="tc-head">
          <b>${esc(d.recipe.replace(/-/g, ' '))}</b>
          <span class="badge">${esc(d.kind)}</span>
          <span class="badge alt">${esc(d.label)}</span>
          ${d.refs && d.refs.length ? `<span class="badge alt">refs: ${esc(d.refs.join(', '))}</span>` : ''}
        </div>
        ${(d.review || []).map((w) => `<p class="tc-warn">⚠ ${esc(w)}</p>`).join('')}
        ${fields.map(([k, v]) => `
          <label class="tc-field">${esc(k.replace(/_/g, ' '))}
            <textarea data-val="${esc(k)}" rows="${String(v).length > 120 ? 6 : 2}">${esc(v)}</textarea>
          </label>`).join('')}
        <div class="tc-btns">
          <button class="primary-btn tc-go">make it ★</button>
          <button class="ghost-btn danger-btn tc-no">nah</button>
          <span class="tc-status"></span>
        </div>
      </div>
    </div>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;

  const status = div.querySelector('.tc-status');
  const go = div.querySelector('.tc-go');
  go.onclick = async () => {
    go.disabled = true;
    const values = {};
    div.querySelectorAll('[data-val]').forEach((t) => { values[t.dataset.val] = t.value; });
    status.textContent = d.vram_gb >= 20
      ? 'making room on the GPU…' : 'rendering on your box…';
    const r = await post('/api/studio/approve', { id: d.id, values });
    if (r.error) {
      status.textContent = 'failed: ' + r.error;
      go.disabled = false;
      return;
    }
    status.textContent = `done · ${r.workflow}`;
    const card = div.querySelector('.tool-card');
    card.querySelectorAll('.tc-field').forEach((f) => f.remove());
    for (const a of r.assets) card.appendChild(mediaEl(a));
    box.scrollTop = box.scrollHeight;
    loadGallery();
    renderVram(null);
    // The card is scratch — loadChat clears it. Re-render so the clip lands
    // on the message it was made for and survives a reload.
    await loadChat();
  };
  div.querySelector('.tc-no').onclick = async () => {
    await post('/api/studio/reject', { id: d.id });
    div.querySelector('.tool-card').innerHTML =
      '<span class="tc-status">dropped. nothing ran.</span>';
  };
}

// One place that decides img vs video vs audio, so a clip never lands in an
// <img> tag again.
// One cell of a message's inline gallery: the media, plus make-it-again and
// delete. The prompt and the seed ride along on the asset row, which is what
// makes an exact re-run possible at all — tools.pending is in memory and is
// popped on approve, so before the receipt nothing on disk could reconstruct
// a render.
function assetCell(a, strip) {
  const cell = document.createElement('div');
  cell.className = 'ac-cell';
  cell.appendChild(mediaEl(a));
  if (a.prompt) cell.title = `${a.recipe || 'generated'}\n${a.prompt.slice(0, 300)}`;
  if (a.can_remake) {
    const again = document.createElement('button');
    again.className = 'ac-btn ac-again';
    again.textContent = '⟳';
    again.title = 'Make it again';
    again.onclick = (e) => { e.stopPropagation(); remakeAsset(a, strip); };
    cell.appendChild(again);
  }
  const x = document.createElement('button');
  x.className = 'ac-btn ac-del';
  x.textContent = '✕';
  x.title = 'Delete this one';
  x.onclick = async (e) => {
    e.stopPropagation();
    await del('/api/assets/' + a.id);
    cell.remove();
    loadGallery();
  };
  cell.appendChild(x);
  return cell;
}

async function remakeAsset(a, strip) {
  const body = document.createElement('div');
  body.innerHTML = `<p class="hint">Same shot, your way. Edit the words or leave
      them; a new seed gives you a different roll of the same idea.</p>
    <label>prompt <textarea class="rm-prompt" rows="6"></textarea></label>
    <label class="check"><input type="radio" name="rmseed" value="new" checked> new seed, a different take</label>
    <label class="check"><input type="radio" name="rmseed" value="same"> same seed, same roll, changed words</label>
    <p class="note rm-note"></p>`;
  body.querySelector('.rm-prompt').value = a.prompt || '';   // never innerHTML
  if (a.seed != null) {
    body.querySelector('.rm-note').textContent = `stored seed: ${a.seed}`;
  }
  const go = await dialog({ title: 'Make it again', body, ok: 'make it' });
  if (!go) return;
  const prompt = body.querySelector('.rm-prompt').value;
  const seed = (body.querySelector('input[name=rmseed]:checked') || {}).value || 'new';
  toast('queued on your box…');
  const r = await post('/api/studio/remake', { asset_id: a.id, prompt, seed });
  if (r.error) { toast('failed: ' + r.error); return; }
  for (const n of r.assets) {
    strip.appendChild(assetCell(
      { ...n, prompt, seed: r.seed, recipe: a.recipe, can_remake: true }, strip));
  }
  (r.notes || []).forEach(toast);
  loadGallery();
  renderVram(null);
}

function mediaEl(a) {
  let el;
  if (a.kind === 'audio') { el = document.createElement('audio'); el.controls = true; }
  else if (a.kind === 'video' || a.kind === 'videos' || a.kind === 'gifs') {
    el = document.createElement('video'); el.controls = true; el.loop = true; el.playsInline = true;
  } else el = document.createElement('img');
  el.src = a.url;
  el.className = 'chat-media';
  return el;
}

// ── gallery ──────────────────────────────────────────────────────
async function loadGallery() {
  const grid = $('galleryGrid');
  if (!S.chat || !S.chat.charId) {
    grid.innerHTML = '';
    $('galleryNote').textContent = 'Open a chat and her gallery shows up here.';
    $('galleryCount').textContent = '0';
    return;
  }
  const d = await api(`/api/gallery/${S.chat.charId}`);
  const assets = d.assets || [];
  $('galleryCount').textContent = String(assets.length);
  $('galleryNote').textContent = assets.length ? ''
    : 'Nothing yet. Make her something in the studio tab.';
  grid.innerHTML = '';
  for (const a of assets) {
    const cell = document.createElement('div');
    cell.className = 'gal-cell';
    const el = mediaEl(a);
    el.className = 'gal-thumb';
    cell.appendChild(el);
    const meta = (a.data && a.data.prompt) || '';
    cell.title = `${a.recipe || 'generated'}\n${meta.slice(0, 300)}`;
    const x = document.createElement('button');
    x.className = 'gal-del';
    x.textContent = '✕';
    x.title = 'delete';
    x.onclick = async (e) => {
      e.stopPropagation();
      await del(`/api/assets/${a.id}`);
      loadGallery();
    };
    cell.appendChild(x);
    cell.onclick = () => openViewer(a.url, a.kind === 'video' ? 'video' : 'image');
    grid.appendChild(cell);
  }
}
$('galleryRefresh').onclick = loadGallery;

// ── the cast ─────────────────────────────────────────────────────
// A scene with one person in it shows nothing at all: the strip is hidden,
// no speaker is sent, and the prompt takes the path it always took.
function renderCast() {
  const strip = $('castStrip');
  const cast = S.cast || [];
  // Tombstones don't count: they exist so old messages keep their author,
  // not to make a solo chat look like a scene.
  strip.hidden = cast.filter((c) => !c.tombstone).length < 2;
  const chips = $('castChips');
  chips.innerHTML = '';
  for (const c of cast) {
    // A tombstone exists so old messages keep their author's name and face —
    // she is not in the scene and gets no chip and no speaker option.
    if (c.tombstone) continue;
    const chip = document.createElement('span');
    chip.className = 'cast-chip' + (c.present ? '' : ' away');
    chip.title = c.note || (c.present ? 'in the room' : 'off-stage');
    const face = document.createElement('span');
    face.className = 'cast-face';
    if (c.avatar) {
      const im = document.createElement('img');
      im.src = '/api/avatars/' + c.avatar;
      face.appendChild(im);
    } else { face.textContent = '♡'; }
    const nm = document.createElement('b');
    nm.textContent = c.name;
    chip.appendChild(face); chip.appendChild(nm);
    if (!c.lead) {
      // Presence is the interesting control: off-stage means her card leaves
      // the prompt entirely, which is the whole point of the feature.
      const t = document.createElement('button');
      t.className = 'cast-x';
      t.textContent = c.present ? '↩' : '↪';
      t.title = c.present ? 'send her out of the scene' : 'bring her back in';
      t.onclick = () => castEdit({ op: 'present', character_id: c.character_id,
                                   present: !c.present });
      chip.appendChild(t);
    }
    chips.appendChild(chip);
  }
  const sel = $('sendAs');
  // S.sendAs is the source of truth: onchange mirrors every user pick into
  // it (and into the per-chat map), and openChatById resets it when the chat
  // changes. Preferring the live DOM value here carried the PREVIOUS chat's
  // pick across a chat switch — the one window where the two diverge.
  const keep = S.sendAs || 'auto';
  sel.innerHTML = '';
  const auto = document.createElement('option');
  auto.value = 'auto';
  auto.textContent = S.speakerNext ? `auto (${S.speakerNext})` : 'auto';
  sel.appendChild(auto);
  for (const c of cast.filter((x) => x.present)) {
    const o = document.createElement('option');
    o.value = c.character_id; o.textContent = c.name;
    sel.appendChild(o);
  }
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : 'auto';
}

async function castEdit(body) {
  if (!S.chat) return;
  const r = await post(`/api/chats/${S.chat.id}/cast`, body);
  if (r.error) { toast(r.error); return; }
  S.cast = r.cast; S.castActive = r.active;
  renderCast();
  await loadChat();
}

$('btnPlainChat').onclick = () => openPlainChat();

$('btnCast').onclick = () => {
  if (!S.chat) { toast('open a chat first'); return; }
  $('castStrip').hidden = !$('castStrip').hidden;
};
// Without this the pick lives only in the DOM, and renderCast() rebuilds the
// option list after every send.
$('sendAs').onchange = () => {
  S.sendAs = $('sendAs').value;
  if (S.chat) {
    if (S.sendAs !== 'auto') S.sendAsByChat[S.chat.id] = S.sendAs;
    else delete S.sendAsByChat[S.chat.id];
  }
  saveUI();
};
// A clickable, searchable roster instead of typing a name into prompt() —
// which was a placeholder wearing a feature's clothes. Built as elements,
// not by id: dynamic ids are invisible to tests/test_frontend.py.
$('castAdd').onclick = () => {
  if (!S.chat) return;
  const open = document.querySelector('.cast-pick');
  if (open) { open.remove(); return; }
  const inChat = new Set((S.cast || []).map((c) => String(c.character_id)));
  inChat.add(String(S.chat.charId));
  const options = S.chars.filter((c) => !inChat.has(String(c.id)));
  if (!options.length) { toast('everyone you have is already in here'); return; }

  const pop = document.createElement('div');
  pop.className = 'cast-pick';
  const inp = document.createElement('input');
  inp.type = 'search';
  inp.placeholder = 'who walks in?';
  const list = document.createElement('div');
  list.className = 'cast-pick-list';
  const close = () => {
    pop.remove();
    document.removeEventListener('mousedown', away);
  };
  const away = (e) => {
    if (!pop.contains(e.target) && e.target !== $('castAdd')) close();
  };
  const paint = (q) => {
    list.innerHTML = '';
    const needle = (q || '').trim().toLowerCase();
    const hits = options.filter(
      (c) => !needle || (c.name || '').toLowerCase().includes(needle));
    if (!hits.length) {
      const none = document.createElement('div');
      none.className = 'cast-pick-none';
      none.textContent = 'nobody by that name';
      list.appendChild(none);
      return;
    }
    for (const c of hits.slice(0, 40)) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'cast-pick-row';
      const face = document.createElement('span');
      face.className = 'cast-face';
      if (c.avatar) {
        const im = document.createElement('img');
        im.src = '/api/avatars/' + c.avatar;
        face.appendChild(im);
      } else { face.textContent = '♡'; }
      const nm = document.createElement('b');
      nm.textContent = c.name;                 // never trust as markup
      row.append(face, nm);
      row.onclick = async () => {
        close();
        await castEdit({ op: 'add', character_id: c.id });
      };
      list.appendChild(row);
    }
  };
  inp.oninput = () => paint(inp.value);
  inp.onkeydown = (e) => {
    if (e.key === 'Escape') close();
    if (e.key === 'Enter') {
      const first = list.querySelector('.cast-pick-row');
      if (first) first.click();
    }
  };
  pop.append(inp, list);
  $('castStrip').appendChild(pop);
  paint('');
  inp.focus();
  // Deferred so the opening click doesn't immediately close it.
  setTimeout(() => document.addEventListener('mousedown', away), 0);
};

// ── panels ───────────────────────────────────────────────────────
// Both side columns collapse and zen hides the lot. The toggles live in the
// topbar rather than on the panels themselves, because a control that hides
// its own panel cannot bring it back.
const PANES = { left: false, right: false, zen: false };

function applyPanes() {
  const L = $('layout');
  L.classList.toggle('no-left', PANES.left || PANES.zen);
  L.classList.toggle('no-right', PANES.right || PANES.zen);
  document.body.classList.toggle('zen', PANES.zen);
  $('zenOut').hidden = !PANES.zen;
  $('toggleLeft').classList.toggle('on', !PANES.left);
  $('toggleRight').classList.toggle('on', !PANES.right);
  savePanes();
}
function savePanes() {
  try { localStorage.setItem('coomkit.panes.v1', JSON.stringify(PANES)); }
  catch (e) { /* private mode */ }
}
(() => {
  try { Object.assign(PANES, JSON.parse(localStorage.getItem('coomkit.panes.v1')) || {}); }
  catch (e) { /* nothing stored */ }
})();

$('toggleLeft').onclick = () => { PANES.left = !PANES.left; applyPanes(); };
$('toggleRight').onclick = () => { PANES.right = !PANES.right; applyPanes(); };
$('toggleZen').onclick = () => { PANES.zen = !PANES.zen; applyPanes(); };
$('zenOut').onclick = () => { PANES.zen = false; applyPanes(); };

document.addEventListener('keydown', (e) => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  // never while someone is typing, and never over an open modal
  const t = e.target;
  if (t && (t.matches('input, textarea, select') || t.isContentEditable)) return;
  if (document.querySelector('.modal-back.on')) {
    if (e.key === 'Escape' && PANES.zen) { PANES.zen = false; applyPanes(); }
    return;
  }
  if (e.key === '[') { PANES.left = !PANES.left; applyPanes(); }
  else if (e.key === ']') { PANES.right = !PANES.right; applyPanes(); }
  else if (e.key === '\\') { PANES.zen = !PANES.zen; applyPanes(); }
  else if (e.key === 'Escape' && PANES.zen) { PANES.zen = false; applyPanes(); }
});
applyPanes();

// ── GPU widget ───────────────────────────────────────────────────
async function renderVram(known) {
  const v = known || await api('/api/vram');
  if (!v || v.error) { $('vramBadge').textContent = '—'; return; }
  const gpu = v.gpu || {};
  $('vramBadge').textContent = gpu.vram_total_gb
    ? `${gpu.vram_free_gb} / ${gpu.vram_total_gb} GB free` : 'no gpu';
  $('vramPolicy').value = v.policy || 'off';
  $('vramDriver').value = v.driver || 'none';
  $('vramKcpp').hidden = (v.driver !== 'koboldcpp');
  if (v.kcpp_url && !$('vramKcppUrl').value) $('vramKcppUrl').value = v.kcpp_url;
  const loaded = (v.loaded || []).map((m) =>
    m.context ? `${m.model} @ ${m.context}` : m.model);
  const bits = [];
  if (loaded.length) bits.push('loaded: ' + loaded.join(', '));
  if (v.driver === 'lmstudio' && !v.lms_available) {
    bits.push('`lms` is not on PATH, CoomKit cannot park your model');
  }
  // KoboldCpp answers these endpoints even with admin off; it just refuses to
  // act. Say which of the two is wrong rather than "it didn't work".
  if (v.driver === 'koboldcpp' && !v.kcpp_up) {
    bits.push(`nothing answering at ${v.kcpp_url || 'the KoboldCpp address'}`);
  }
  if (v.problem) bits.push(v.problem);
  if ((v.parked || []).length) {
    bits.push(`${v.parked.length} model(s) still parked, hit "reload my model"`);
  }
  $('vramNote').textContent = bits.join(' · ');
}
$('vramRefresh').onclick = () => renderVram(null);

async function saveVram(patch, said) {
  const cfg = await api('/api/config');
  const vram = Object.assign({}, cfg.vram || {}, patch);
  // Parking only works if something knows how to park. Guess from what is
  // actually running rather than assuming LM Studio, which used to be written
  // in unconditionally the moment anyone turned the policy on.
  if (vram.policy !== 'off' && (!vram.driver || vram.driver === 'none')) {
    const probe = await api('/api/vram');
    vram.driver = probe && probe.lms_available === false && probe.kcpp_up
      ? 'koboldcpp' : 'lmstudio';
  }
  await post('/api/config', { vram });
  toast(said);
  renderVram(null);
}

$('vramPolicy').onchange = () =>
  saveVram({ policy: $('vramPolicy').value }, 'GPU policy: ' + $('vramPolicy').value);
$('vramDriver').onchange = () => {
  $('vramKcpp').hidden = $('vramDriver').value !== 'koboldcpp';
  saveVram({ driver: $('vramDriver').value }, 'driver: ' + $('vramDriver').value);
};
for (const id of ['vramKcppUrl', 'vramKcppKey']) {
  $(id).onchange = () => saveVram(
    { kcpp_url: $('vramKcppUrl').value.trim(),
      kcpp_key: $('vramKcppKey').value }, 'KoboldCpp settings saved');
}
$('vramRestore').onclick = async () => {
  toast('reloading…');
  const r = await post('/api/vram/restore');
  toast((r.steps || []).join(' · ') || 'nothing was parked');
  renderVram(null);
};

// ── character looks & voice ──────────────────────────────────────
// Both feed the studio. The voice half matters more than it looks: a clone
// sounds exactly like whatever clip it was given, and there is no way to tell
// from a filename — hence the player sitting right next to the picker.

function charOf(id) { return S.chars.find((x) => String(x.id) === String(id)); }

// Served by /api/studio from voices.DEFAULT. Never hardcode a preset id here:
// the last one that was hardcoded outlived a rename, matched no <option> (so
// the select rendered blank) and resolved server-side to a different
// archetype entirely, with nothing in the suite to notice.
function voiceDefault() {
  return (S.studio && S.studio.voice_default)
      || ((S.studio && S.studio.voices || [])[0] || {}).name || '';
}

// An embedded character_book fires on every turn and is completely invisible
// in the nine-field editor. Say so plainly, and offer the one useful action.
function fillLoreLine(c) {
  const fields = ((c.data || {}).fields) || {};
  const embedded = (fields.character_book || {}).entries || [];
  const mine = (S.lore || []).filter((b) => b.scope === 'character'
                                         || b.scope === 'always');
  const lifted = (S.lore || []).some((b) => b.from_card_id === c.id);
  const bits = [];
  if (mine.length) bits.push(`Attached: ${mine.map((b) => b.name).join(', ')}.`);
  if (embedded.length && !lifted) {
    bits.push(`This card has its own lorebook (${embedded.length} entries).`
      + " It's already working.");
  }
  $('cvLoreBlock').hidden = !bits.length;
  $('cvLoreLine').textContent = bits.join(' ');
  const lift = $('cvLoreLift');
  lift.hidden = !embedded.length || lifted;
  lift.onclick = async () => {
    // Lifting COPIES; it never edits the card, which keeps round-tripping
    // through cards.CARD_KEYS unchanged. It does switch on full semantics,
    // so MORE entries will fire than before — say so or it reads as a bug.
    if (!confirm(`Copy this card's ${embedded.length}-entry lorebook out into`
      + ' a book you can edit, share and attach elsewhere?\n\nHer card is not'
      + ' touched. Full SillyTavern rules switch on, so "always-on" entries'
      + ' start firing and switched-off ones stop — more will fire than'
      + ' before.')) return;
    const r = await post('/api/lorebooks/import',
                         { from_character_id: c.id, attach: 'character' });
    if (r.error) { toast(r.error); return; }
    toast(`lifted ${r.summary.entries} entries out of her card`);
    await loadLore();
    fillLoreLine(c);
  };
}


function fillLooksAndVoice(c) {
  const data = c.data || {};
  const visual = data.visual || {};
  const voice = data.voice || {};
  fillLoreLine(c);

  const models = ((S.studio && S.studio.workflows) || [])
    .filter((w) => w.kind === 'image');
  $('cvModel').innerHTML = '<option value="">— use the global default —</option>'
    + models.map((w) => `<option value="${esc(w.name)}"${visual.model === w.name ? ' selected' : ''}>${esc(w.label)}</option>`).join('');
  $('cvAppearance').value = visual.appearance || '';
  $('cvSeed').value = visual.seed || '';
  $('cvRefPreview').hidden = !visual.ref;
  if (visual.ref) $('cvRefPreview').src = `/api/avatars/${visual.ref}`;

  loadLoras().then(() => {
    $('loraRows').innerHTML = '';
    for (const l of visual.loras || []) $('loraRows').appendChild(loraRow(l));
    syncLoraCount();
  });
  $('artistMode').value = visual.artist_mode || 'off';
  $('artistCount').value = visual.artist_count || 2;
  $('artistMinPosts').value = visual.artist_min_posts || 500;
  S.artists = visual.artists || [];
  renderArtists();
  syncArtistMode();

  const shipped = (S.studio && S.studio.voices) || [];
  const opts = shipped.map((v) =>
    `<option value="${esc(v.name)}">${esc(v.label)} · ${v.f0_hz} Hz</option>`);
  if (voice.sample) opts.unshift('<option value="__own">her own clip</option>');
  // OmniVoice and IndexTTS-2 can both synthesise a voice from a description,
  // so this is a real third option rather than a fallback for failure.
  opts.push('<option value="none">describe it instead, no clip</option>');
  $('cvVoicePreset').innerHTML = opts.join('');
  $('cvVoicePreset').value = voice.sample ? '__own' : (voice.preset || voiceDefault());
  $('cvVoiceEngine').value = voice.engine === 'emotion' ? 'emotion' : 'clone';
  const speeds = (S.studio && S.studio.speeds) || [];
  $('cvVoiceSpeed').innerHTML = '<option value="">— as the voice was recorded —</option>'
    + speeds.map(([v, lab]) => `<option value="${v}">${v.toFixed(2)} · ${esc(lab)}</option>`).join('');
  $('cvVoiceSpeed').value = voice.speed ? String(voice.speed) : '';
  $('cvVoiceInstruct').value = voice.instruct || '';
  $('cvVoiceNote').textContent = '';
  syncVoicePreview();
}

function syncVoicePreview() {
  const id = $('cardEditId').value;
  const c = charOf(id);
  const voice = ((c && c.data) || {}).voice || {};
  const pick = $('cvVoicePreset').value;
  const player = $('cvVoicePreview');
  const shipped = ((S.studio && S.studio.voices) || []).find((v) => v.name === pick);
  const describing = pick === 'none';
  $('cvVoiceEngine').disabled = describing;
  $('cvVoiceInstruct').disabled = !describing && pick !== '__own';

  if (pick === '__own' && voice.sample) {
    player.src = `/api/avatars/${voice.sample}`;
    player.hidden = false;
    $('cvVoiceCredit').textContent = 'your own clip';
  } else if (shipped) {
    player.src = shipped.url;
    player.hidden = false;
    $('cvVoiceCredit').textContent = `${shipped.blurb}, ${shipped.credit}`;
  } else {
    player.hidden = true;
    $('cvVoiceCredit').textContent = describing
      ? 'No clip. OmniVoice designs the voice from the words below, and only '
        + 'accepts its own fixed vocabulary (gender, age, accent, pitch, whisper).'
      : '';
  }
}
$('cvVoicePreset').onchange = syncVoicePreview;

async function saveCharData(id, patch, noteEl) {
  const c = charOf(id);
  if (!c) return null;
  const data = Object.assign({}, c.data || {});
  for (const [k, v] of Object.entries(patch)) data[k] = Object.assign({}, data[k] || {}, v);
  const r = await post(`/api/characters/${id}`, {
    name: c.name, avatar: c.avatar || '', data,
  });
  if (noteEl) {
    noteEl.textContent = r.error ? 'failed: ' + r.error : 'saved';
    noteEl.className = 'note ' + (r.error ? 'bad' : 'ok');
  }
  await loadChars();
  return r;
}

$('cvSave').onclick = async () => {
  const id = $('cardEditId').value;
  await saveCharData(id, { visual: {
    model: $('cvModel').value,
    appearance: $('cvAppearance').value.trim(),
    seed: $('cvSeed').value ? Number($('cvSeed').value) : '',
    loras: loraValues(),
    artist_mode: $('artistMode').value,
    artist_count: Number($('artistCount').value) || 2,
    artist_min_posts: Number($('artistMinPosts').value) || 500,
    artists: S.artists,
  } }, $('cardNote'));
  toast('looks saved');
};

// ── her picture ──────────────────────────────────────────────────
// A forged character gets one portrait and a pinned seed, so she looks like
// herself from her first picture. That is right up until the first picture is
// ugly, which is what people actually hit — and the fix is usually not another
// render, it is one of the photos she already has.
function showPortrait(c) {
  const img = $('cvPortrait');
  if (c && c.avatar) { img.src = '/api/avatars/' + c.avatar; img.hidden = false; }
  else { img.removeAttribute('src'); img.hidden = true; }
  $('cvPickGrid').hidden = true;
  $('cvPortraitNote').textContent = '';
  $('cvPortraitNote').className = 'note';
}

function ptNote(msg, cls) {
  $('cvPortraitNote').textContent = msg;
  $('cvPortraitNote').className = 'note' + (cls ? ' ' + cls : '');
}

// The shot and the image model are per-render, not saved to her looks: you are
// hunting for a portrait you like, and committing to a model you were only
// trying out is the opposite of that.
function fillPortraitPickers() {
  const shot = $('cvShot');
  if (!shot.options.length && S.studio && S.studio.recipes) {
    for (const r of S.studio.recipes.filter((x) => PORTRAIT_SHOTS.includes(x.id))) {
      const o = document.createElement('option');
      o.value = r.id; o.textContent = r.label || r.id;
      shot.appendChild(o);
    }
  }
  const wf = $('cvWf');
  if (wf.options.length <= 1 && S.studio && S.studio.workflows) {
    for (const w of S.studio.workflows.filter((x) => x.kind === 'image')) {
      const o = document.createElement('option');
      o.value = w.name; o.textContent = w.label || w.name;
      wf.appendChild(o);
    }
  }
  $('cvPrompt').hidden = shot.value !== 'describe';
}
const PORTRAIT_SHOTS = ['solo-model', 'selfie', 'solo-lewd', 'scene', 'describe'];
$('cvShot').onchange = () => { $('cvPrompt').hidden = $('cvShot').value !== 'describe'; };

async function regenPortrait(newSeed) {
  const id = $('cardEditId').value;
  if (!id) return;
  if (!LLM_READY()) { ptNote('pick a model in the top bar first', 'bad'); return; }
  ptNote(newSeed ? 'rolling a new face…' : 'rendering… this takes as long as any shot');
  const r = await post(`/api/characters/${id}/portrait`, {
    backend: S.llm.backend, model: S.llm.model, new_seed: !!newSeed,
    persona_id: $('personaSel').value || undefined,
    recipe: $('cvShot').value || undefined,
    workflow: $('cvWf').value || undefined,
    prompt: $('cvPrompt').value.trim() || undefined,
  });
  if (!r.ok) { ptNote('failed: ' + (r.error || 'unknown'), 'bad'); return; }
  await loadChars();
  const c = S.chars.find((x) => String(x.id) === String(id));
  showPortrait(c);
  ptNote('done', 'ok');
  toast('new portrait');
}

$('cvRegen').onclick = () => regenPortrait(false);
$('cvReroll').onclick = () => regenPortrait(true);

$('cvPickBtn').onclick = async () => {
  const id = $('cardEditId').value;
  const grid = $('cvPickGrid');
  if (!grid.hidden) { grid.hidden = true; return; }
  grid.innerHTML = '';
  const d = await api('/api/gallery/' + id);
  const shots = (d.assets || []).filter((a) => a.kind === 'image');
  if (!shots.length) {
    const p = document.createElement('p');
    p.className = 'pt-empty';
    p.textContent = 'nothing in her gallery yet. Render something first.';
    grid.appendChild(p);
  }
  for (const a of shots) {
    const im = document.createElement('img');
    im.src = a.url;
    im.title = a.prompt || '';
    im.onclick = async () => {
      const r = await post(`/api/characters/${id}/avatar`, { asset_id: a.id });
      if (r.error) { ptNote('failed: ' + r.error, 'bad'); return; }
      await loadChars();
      showPortrait(S.chars.find((x) => String(x.id) === String(id)));
      toast('that one, then');
    };
    grid.appendChild(im);
  }
  grid.hidden = false;
};

$('cvUploadBtn').onclick = () => $('cvUpload').click();
$('cvUpload').onchange = async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  ev.target.value = '';
  const id = $('cardEditId').value;
  ptNote('uploading…');
  const b64 = await fileToB64(f);
  const r = await post('/api/assets/upload', {
    filename: f.name, b64, kind: 'avatar', owner_id: Number(id),
  });
  if (r.error) { ptNote('failed: ' + r.error, 'bad'); return; }
  await loadChars();
  showPortrait(S.chars.find((x) => String(x.id) === String(id)));
  ptNote('uploaded', 'ok');
};

$('cvVoiceSave').onclick = async () => {
  const id = $('cardEditId').value;
  const pick = $('cvVoicePreset').value;
  const patch = {
    engine: $('cvVoiceEngine').value,
    instruct: $('cvVoiceInstruct').value.trim(),
    speed: $('cvVoiceSpeed').value ? Number($('cvVoiceSpeed').value) : '',
  };
  // "__own" means leave the uploaded sample in place; anything else clears it
  // so the chosen shipped voice (or the description) actually takes effect.
  if (pick !== '__own') { patch.sample = ''; patch.preset = pick; }
  else patch.preset = '';
  await saveCharData(id, { voice: patch }, $('cvVoiceNote'));
  toast('voice saved');
};

$('cvVoicePick').onclick = () => $('cvVoiceFile').click();
$('cvVoiceFile').onchange = async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  const id = $('cardEditId').value;
  $('cvVoiceNote').textContent = 'uploading…';
  const r = await post('/api/assets/upload', {
    filename: f.name, kind: 'voice', owner_id: Number(id),
    b64: await fileToB64(f),
  });
  ev.target.value = '';
  if (r.error) {
    $('cvVoiceNote').textContent = 'failed: ' + r.error;
    $('cvVoiceNote').className = 'note bad';
    return;
  }
  await loadChars();
  fillLooksAndVoice(charOf(id));
  $('cvVoicePreset').value = '__own';
  syncVoicePreview();
  $('cvVoiceNote').textContent = 'uploaded, play it back before you rely on it';
  $('cvVoiceNote').className = 'note ok';
};
$('cvVoiceClear').onclick = async () => {
  const id = $('cardEditId').value;
  await saveCharData(id, { voice: { sample: '', preset: voiceDefault() } }, $('cvVoiceNote'));
  fillLooksAndVoice(charOf(id));
};

$('cvRefPick').onclick = () => $('cvRefFile').click();
$('cvRefFile').onchange = async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  const id = $('cardEditId').value;
  const r = await post('/api/assets/upload', {
    filename: f.name, kind: 'character_ref', owner_id: Number(id),
    b64: await fileToB64(f),
  });
  ev.target.value = '';
  if (r.error) { toast('failed: ' + r.error); return; }
  await loadChars();
  $('cvRefPreview').src = r.url;
  $('cvRefPreview').hidden = false;
  toast('reference photo saved, she is <Picture 2> when you are in shot');
};

// ── persona reference photos ─────────────────────────────────────
// Only ever sent for a recipe that declares it wants one (the act shots), and
// only to the local ComfyUI. It used to also require the POV option, which
// read as a stronger promise but quietly meant the shot people actually asked
// for — her looking at the camera — never got the reference at all.
// Everything else in CoomKit already works that way; this is the piece people
// will be most careful about, so it is worth being obvious.

function renderRefs() {
  const p = S.personas.find((x) => String(x.id) === String($('personaId').value));
  const box = $('refList');
  box.innerHTML = '';
  const refs = ((p && p.data) || {}).refs || [];
  if (!p) { $('refNote').textContent = 'pick or save a persona first.'; return; }
  $('refNote').textContent = refs.length ? '' : 'none yet.';
  for (const r of refs) {
    const cell = document.createElement('div');
    cell.className = 'ref-cell';
    cell.innerHTML = `<img src="/api/avatars/${esc(r.file)}" alt="">`
      + `<span class="ref-tag">${esc(r.kind)}</span>`;
    const x = document.createElement('button');
    x.className = 'gal-del';
    x.textContent = '✕';
    x.onclick = async () => {
      const data = Object.assign({}, p.data || {});
      data.refs = (data.refs || []).filter((q) => q.file !== r.file);
      await post('/api/personas/' + p.id, { name: p.name, data });
      await loadPersonas();
      renderRefs();
    };
    cell.appendChild(x);
    box.appendChild(cell);
  }
}

$('refPick').onclick = () => {
  if (!$('personaId').value) { toast('save the persona first'); return; }
  $('refFile').click();
};
$('refFile').onchange = async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  $('refNote').textContent = 'uploading…';
  const r = await post('/api/assets/upload', {
    filename: f.name, kind: 'persona_ref', owner_id: Number($('personaId').value),
    ref_kind: $('refKind').value, b64: await fileToB64(f),
  });
  ev.target.value = '';
  if (r.error) {
    $('refNote').textContent = 'failed: ' + r.error;
    $('refNote').className = 'note bad';
    return;
  }
  await loadPersonas();
  renderRefs();
  toast('reference saved, used on the act recipes, as <Picture 1>');
};

// ── loras & artist blending ──────────────────────────────────────
// LoRA options come from the user's ComfyUI, not from a guessed path — the
// GPU box is very often not this box.

S.loras = null;

async function loadLoras(force) {
  // `if (S.loras)` cached a FAILED probe forever, because [] is truthy: one
  // request while ComfyUI was down and the picker stayed empty until reload.
  if (S.loras && S.loras.length && !force) return S.loras;
  const d = await api('/api/loras');
  S.loras = d.loras || [];
  $('loraNote').textContent = d.error
    ? 'could not read loras from ComfyUI: ' + d.error + ', anything already on her card is kept'
    : (S.loras.length ? '' : 'ComfyUI reported no loras installed');
  return S.loras;
}

function loraRow(entry = {}) {
  const row = document.createElement('div');
  row.className = 'lora-row';
  const known = S.loras || [];
  let opts = known.map((n) =>
    `<option value="${esc(n)}"${n === entry.name ? ' selected' : ''}>${esc(n)}</option>`).join('');
  // A name the live probe did not return is kept and badged, never dropped.
  // studio.review warns at approval and studio.run strips it at render, so
  // holding on to it is safe — silently deleting a lora because ComfyUI
  // happened to be down is not. The server side already promises exactly
  // this ("a flaky probe must never silently strip a LoRA the user does
  // have"); the card editor was doing the opposite.
  if (entry.name && !known.includes(entry.name)) {
    opts += `<option value="${esc(entry.name)}" selected>${esc(entry.name)}, not on this ComfyUI</option>`;
  }
  row.innerHTML = `<select class="lora-name"><option value="">— pick a lora —</option>${opts}</select>`
    + `<input class="lora-strength" type="number" step="0.05" min="-2" max="3" value="${entry.strength ?? 1}">`
    + `<button class="mini-btn lora-del">✕</button>`;
  row.querySelector('.lora-del').onclick = () => { row.remove(); syncLoraCount(); };
  return row;
}

function syncLoraCount() {
  $('loraCount').textContent = String(
    [...document.querySelectorAll('#loraRows .lora-name')].filter((s) => s.value).length);
}

function loraValues() {
  return [...document.querySelectorAll('#loraRows .lora-row')]
    .map((r) => ({
      name: r.querySelector('.lora-name').value,
      strength: Number(r.querySelector('.lora-strength').value),
    }))
    .filter((l) => l.name);
}

$('loraAdd').onclick = async () => {
  await loadLoras(true);
  $('loraRows').appendChild(loraRow());
  syncLoraCount();
};
$('loraRefresh').onclick = async () => {
  const kept = loraValues();
  S.loras = null;
  await loadLoras(true);
  $('loraRows').innerHTML = '';
  kept.forEach((e) => $('loraRows').appendChild(loraRow(e)));
  syncLoraCount();
  toast(`${(S.loras || []).length} loras on this ComfyUI`);
};

// ── artists ──────────────────────────────────────────────────────
S.artists = [];

function tagChip(a, onRemove) {
  const el = document.createElement('span');
  el.className = 'tag-chip';
  el.textContent = a.prompt + (a.count ? ` · ${a.count}` : '');
  if (onRemove) {
    const x = document.createElement('button');
    x.className = 'chip-x';
    x.textContent = '✕';
    x.onclick = () => onRemove(a);
    el.appendChild(x);
  }
  return el;
}

function renderArtists() {
  const box = $('artistChosen');
  box.innerHTML = '';
  for (const a of S.artists) {
    box.appendChild(tagChip(a, (gone) => {
      S.artists = S.artists.filter((x) => x.tag !== gone.tag);
      renderArtists();
    }));
  }
  $('artistNote').textContent = S.artists.length
    ? 'clause: ' + 'by ' + S.artists.map((a) => a.prompt).join(', ')
    : '';
}

function syncArtistMode() {
  const mode = $('artistMode').value;
  $('artistRandomOpts').hidden = mode !== 'random';
  $('artistChosen').hidden = mode === 'off';
  // Only Anima reads booru artist tags. Say so on the character whose model
  // is natural-language rather than letting the setting look effective.
  // An empty box means "use the global default", and that default ships as
  // krea2 — so treating empty as Anima showed a reassuring badge to exactly
  // the people whose artists were about to be discarded.
  const model = $('cvModel').value
    || ((S.studio && S.studio.defaults) || {}).image || 'krea2';
  const tagModel = model === 'anima';
  $('artistBadge').textContent = tagModel ? 'anime models only' : `ignored by ${model}`;
  $('artistBadge').className = 'badge' + (tagModel ? ' alt' : ' warm');
}
$('artistMode').onchange = syncArtistMode;
$('cvModel').onchange = syncArtistMode;

$('artistRoll').onclick = async () => {
  const n = Number($('artistCount').value) || 2;
  const min = Number($('artistMinPosts').value) || 500;
  const d = await api(`/api/tags/artists?n=${n}&min_posts=${min}`);
  if (!d.artists || !d.artists.length) {
    // The corpus ships now, so "no database" is no longer the likely cause —
    // an empty roll almost always means the min-posts floor is above the
    // whole pool. At 500 there are ~1,800 artists; at 500000 there are none.
    $('artistNote').textContent =
      `no artist has ${min}+ posts, lower the floor`;
    return;
  }
  S.artists = d.artists;
  renderArtists();
};

$('artistSearchBtn').onclick = () => {
  const box = $('artistSearch');
  box.hidden = !box.hidden;
  if (!box.hidden) box.focus();
};
let artistSearchTimer = null;
$('artistSearch').oninput = () => {
  clearTimeout(artistSearchTimer);
  artistSearchTimer = setTimeout(async () => {
    const q = $('artistSearch').value.trim();
    if (!q) { $('artistResults').innerHTML = ''; return; }
    const d = await api(`/api/tags/search?cat=artist&limit=12&q=${encodeURIComponent(q)}`);
    const box = $('artistResults');
    box.innerHTML = '';
    for (const a of d.results || []) {
      const chip = tagChip(a);
      chip.classList.add('pick');
      chip.onclick = () => {
        if (!S.artists.some((x) => x.tag === a.tag)) S.artists.push(a);
        renderArtists();
      };
      box.appendChild(chip);
    }
  }, 220);
};

// ── character forge: invent her with whatever model is connected ─────
// Same interaction as the scenario forge — pitch, argue, commit — because it
// is the same interaction. Commit writes a real card, pins her a seed so she
// looks like herself forever, and renders her portrait through the ordinary
// studio path.

S.pitches = [];       // ☆ a whole character
S.feelPitches = [];   // 📷 that feel

// Scoped to #forgeTabs. The settings modal and the inspector both use
// .modal-tab, and an unscoped selector here would bind over theirs.
document.querySelectorAll('#forgeTabs .modal-tab').forEach((b) => {
  b.onclick = () => forgeTab(b.dataset.ftab);
});

// The two forge modes differ in exactly four things: where the cards go, whose
// persona is asked for, whether a portrait is rendered, and whether there is a
// picture to hand over on commit. Everything after the pitch — the card, the
// revise box, the create button — is identical, so it is ONE renderer taking a
// mode rather than two copies that drift.
const CG_MODES = {
  invent: {
    results: 'cgResults', persona: 'cgPersonaSel', list: 'pitches',
    portrait: () => $('cgPortrait').checked,
    picture: () => null,
  },
  feel: {
    results: 'cfResults', persona: 'cfPersonaSel', list: 'feelPitches',
    portrait: () => $('cfPortrait').checked,
    picture: () => S.feel.images[0] || null,
  },
};

function cgBody(extra, mode) {
  const b = requestBody();
  // requestBody() carries whatever is pinned to the CHAT composer. The forge
  // is not the chat: an image stuck to the message box has nothing to do with
  // the card being built, and on the from-image route it would be read as the
  // reference picture. Drop it — the caller passes its own.
  delete b.images;
  b.persona_id = $((mode || CG_MODES.invent).persona).value || null;
  return Object.assign(b, extra || {});
}

$('cgGo').onclick = async () => {
  if (!LLM_READY()) { pickModel(); return; }
  const btn = $('cgGo');
  btn.disabled = true;
  $('cgNote').textContent = 'inventing…';
  $('cgResults').innerHTML = '';
  const d = await post('/api/forge/characters', cgBody({
    brief: $('cgBrief').value.trim(),
    count: Number($('cgCount').value) || 3,
  }));
  btn.disabled = false;
  if (d.error) { $('cgNote').textContent = 'failed: ' + d.error; return; }
  $('cgNote').textContent = '';
  S.pitches = d.characters;
  S.pitches.forEach((c) => renderPitchCard(c, null, CG_MODES.invent));
};

function renderPitchCard(c, replaceEl, mode) {
  mode = mode || CG_MODES.invent;
  const card = document.createElement('div');
  card.className = 'pitch';
  card.innerHTML = `
    <div class="pitch-head">
      <b>${esc(c.name)}</b>
      <span class="badge alt">${esc(c.voice)}</span>
      <span class="badge alt">${esc(c.model)}</span>
    </div>
    <p class="pitch-tag">${esc(c.tagline)}</p>
    ${c.for_you ? `<p class="pitch-for">✦ ${esc(c.for_you)}</p>` : ''}
    <details class="pitch-more"><summary>the whole card</summary>
      <dl class="pitch-dl">
        <dt>personality</dt><dd>${esc(c.personality)}</dd>
        <dt>scenario</dt><dd>${esc(c.scenario)}</dd>
        <dt>appearance</dt><dd>${esc(c.appearance)}</dd>
        <dt>opens with</dt><dd class="pre">${esc(c.first_mes)}</dd>
        <dt>talks like</dt><dd class="pre">${esc(c.mes_example)}</dd>
      </dl>
    </details>
    <div class="pitch-revise">
      <input class="pitch-instr" placeholder="older / meaner / make her my neighbour / less horny up front…">
      <button class="ghost-btn pitch-rev">revise</button>
    </div>
    <div class="row-btns">
      <button class="primary-btn pitch-make">☆ create her</button>
      <span class="note pitch-status"></span>
    </div>`;
  if (replaceEl) replaceEl.replaceWith(card);
  else $(mode.results).appendChild(card);

  const status = card.querySelector('.pitch-status');
  card.querySelector('.pitch-rev').onclick = async () => {
    const instruction = card.querySelector('.pitch-instr').value.trim();
    if (!instruction) return;
    status.textContent = 'revising…';
    const d = await post('/api/forge/characters/refine',
      cgBody({ character: c, instruction }, mode));
    if (d.error) { status.textContent = 'failed: ' + d.error; return; }
    const i = S[mode.list].indexOf(c);
    if (i >= 0) S[mode.list][i] = d.character;
    renderPitchCard(d.character, card, mode);
  };
  card.querySelector('.pitch-make').onclick = async (ev) => {
    ev.target.disabled = true;
    const wants = mode.portrait();
    const pic = mode.picture();
    status.textContent = wants
      ? 'writing her card and rendering her portrait…'
      : (pic ? 'writing her card and giving her that face…'
             : 'writing her card…');
    const extra = { character: c, portrait: wants };
    // The picture she was forged from rides along to the create call rather
    // than being uploaded separately: the server stores it once and makes it
    // both her face and her generation reference, so there is no window where
    // she exists with neither.
    if (pic) { extra.image_b64 = pic.b64; extra.image_name = pic.name; }
    const d = await post('/api/forge/characters/create', cgBody(extra, mode));
    if (d.error) {
      status.textContent = 'failed: ' + d.error;
      ev.target.disabled = false;
      return;
    }
    status.textContent = d.portrait_error
      ? `created, portrait failed: ${d.portrait_error}`
      : 'created ★';
    const shot = d.portrait ? d.portrait.url
      : (d.character && d.character.avatar
         ? '/api/avatars/' + d.character.avatar : '');
    if (shot) {
      const img = document.createElement('img');
      img.src = shot;
      img.className = 'pitch-portrait';
      card.insertBefore(img, card.firstChild);
    }
    await loadChars();
    toast(`${d.character.name} joined the roster`);
  };
}

// ── CFTF: card for that feel ─────────────────────────────────────
// You already found the picture. A LOCAL vision model reads her off it and
// pitches several women who all look like that and are otherwise completely
// different people: the picture fixes how she looks, the pitches decide who
// she is. Everything downstream of the pitch is the ordinary character forge.
//
// Nothing is written to disk until she is committed — the pictures live here
// and go straight into the request — so pitching from a photo and thinking
// better of it leaves no trace on the machine.
S.feel = { images: [], busy: false };

function cfThumbs() {
  const box = $('cfThumbs');
  box.innerHTML = '';
  S.feel.images.forEach((im, i) => {
    const d = document.createElement('div');
    d.className = 'cf-thumb';
    const img = document.createElement('img');
    img.src = im.dataUrl;
    img.alt = im.name;
    const x = document.createElement('button');
    x.className = 'chip-x';
    x.textContent = '✕';
    x.title = 'drop this one';
    x.onclick = () => { S.feel.images.splice(i, 1); cfThumbs(); };
    d.appendChild(img);
    d.appendChild(x);
    box.appendChild(d);
  });
  // ONE place derives the button state. It used to be `!images.length` alone,
  // and every ✕ calls this — so pruning a thumbnail while the vision model was
  // still reading re-enabled the button mid-request, and a second click put two
  // interleaved sets of pitch cards on screen.
  $('cfGo').disabled = S.feel.busy || !S.feel.images.length;
}

async function cfAdd(files) {
  for (const f of Array.from(files || [])) {
    if (S.feel.images.length >= 4) break;
    if (!(f.type || '').startsWith('image/')) continue;
    const b64 = await fileToB64(f);
    S.feel.images.push({
      name: f.name, b64,
      dataUrl: `data:${f.type || 'image/png'};base64,${b64}`,
    });
  }
  cfThumbs();
}

$('cfDrop').onclick = () => $('cfFile').click();
$('cfFile').onchange = () => { cfAdd($('cfFile').files); $('cfFile').value = ''; };
['dragover', 'dragenter'].forEach((e) => $('cfDrop').addEventListener(e, (ev) => {
  ev.preventDefault();
  $('cfDrop').classList.add('drop-hot');
}));
['dragleave', 'drop'].forEach((e) => $('cfDrop').addEventListener(e, () => {
  $('cfDrop').classList.remove('drop-hot');
}));
$('cfDrop').addEventListener('drop', (ev) => {
  ev.preventDefault();
  cfAdd(ev.dataTransfer.files);
});
cfThumbs();

$('cfGo').onclick = async () => {
  if (!S.feel.images.length) { toast('drop a picture in first'); return; }
  if (!LLM_READY()) { pickModel(); return; }
  S.feel.busy = true;
  cfThumbs();
  $('cfNote').className = 'note';
  $('cfNote').textContent = "she's looking at it…";
  $('cfResults').innerHTML = '';
  const d = await post('/api/forge/characters/from-image', cgBody({
    images: S.feel.images.map((im) => ({ name: im.name, b64: im.b64 })),
    brief: $('cfBrief').value.trim(),
    count: Number($('cfCount').value) || 3,
  }, CG_MODES.feel));
  S.feel.busy = false;
  cfThumbs();
  if (d.error) {
    $('cfNote').className = 'note bad';
    // `raw` is only ever set when nothing parsed, and it is the single most
    // useful thing on screen then: a text-only model answers this route with
    // prose about not being able to see, which reads as CoomKit being broken
    // until you can see what it actually said.
    $('cfNote').textContent = d.error
      + (d.raw ? ` — it replied: ${d.raw.slice(0, 220)}` : '');
    return;
  }
  // A picture the server could not use is NAMED here. Dropping one quietly
  // meant the model was told it had fewer references than the user chose,
  // with nothing on screen to explain the difference.
  $('cfNote').className = d.notice ? 'note warn' : 'note ok';
  $('cfNote').textContent =
    `${d.characters.length} pitched — same face, different women`
    + (d.notice ? ` · ${d.notice}` : '');
  S.feelPitches = d.characters;
  S.feelPitches.forEach((c) => renderPitchCard(c, null, CG_MODES.feel));
};

// ── example dialogue toggle ──────────────────────────────────────
function syncExamples(detail) {
  const box = $('examplesToggle');
  const note = $('examplesNote');
  if (!detail) { note.textContent = ''; return; }
  box.checked = detail.examples !== false;
  box.disabled = !detail.has_examples;
  note.textContent = detail.has_examples
    ? (box.checked ? 'on, showing her how she talks' : 'off')
    : "this card has no example dialogue, so there's nothing to inject";
}
$('examplesToggle').onchange = async () => {
  if (!S.chat) return;
  const r = await post(`/api/chats/${S.chat.id}/examples`,
    { enabled: $('examplesToggle').checked });
  if (r.error) { toast('failed: ' + r.error); return; }
  $('examplesNote').textContent = r.examples
    ? 'on, showing her how she talks' : 'off';
  toast(r.examples ? 'example dialogue on' : 'example dialogue off');
};

// ── prompt blocks ────────────────────────────────────────────────
// One ordered list containing both CoomKit's own layers and the user's, with
// the running token cost visible. Shared presets routinely spend 24,000
// tokens before the character is described and no other tool says so.

S.blk = { cat: null, list: [], presetId: null, dirty: false };

const TOK = (t) => Math.ceil((t || '').length / 4);   // matches engine.rough_tokens

async function loadBlockCat() {
  if (S.blk.cat) return S.blk.cat;
  S.blk.cat = await api('/api/blocks');
  return S.blk.cat;
}

async function openBlocks() {
  await loadBlockCat();
  const sel = $('blkPreset');
  sel.innerHTML = S.presets.map((p) =>
    `<option value="${p.id}">${esc(p.name)}</option>`).join('');
  if (S.presetId) sel.value = S.presetId;
  await loadBlocksFor(sel.value);
}

async function loadBlocksFor(presetId) {
  const p = S.presets.find((x) => String(x.id) === String(presetId));
  if (!p) { $('blkList').innerHTML = '<p class="note">no preset selected</p>'; return; }
  S.blk.presetId = p.id;
  // Mirror the server's blocks.merge(): built-ins the stored preset has
  // never heard of are appended, disabled or not as they ship. The server
  // already assembles that way (server.py, _prepare_request), so a panel
  // painting the stored list verbatim was hiding blocks that were really in
  // the prompt — a preset saved before the POV group shipped would simply
  // never show it.
  S.blk.list = (p.data.blocks && p.data.blocks.length)
    ? mergeBlocks(p.data.blocks, S.blk.cat.default)
    : JSON.parse(JSON.stringify(S.blk.cat.default));
  S.blk.dirty = false;
  $('blkContext').value = p.data.context
    || (S.cfg && S.cfg.defaults && S.cfg.defaults.context_tokens) || 8192;
  renderBlocks();
}
function mergeBlocks(stored, defaults) {
  const out = JSON.parse(JSON.stringify(stored));
  const seen = new Set(out.map((b) => b.id));
  for (const d of defaults) {
    if (!seen.has(d.id)) out.push(JSON.parse(JSON.stringify(d)));
  }
  return out;
}
$('blkContext').oninput = renderBlocks;
$('blkCtxDetect').onclick = async () => {
  const r = await post('/api/context/probe',
    { backend: S.llm.backend, model: S.llm.model });
  if (!r.ok) { toast(r.error || 'could not detect'); return; }
  // context 0 means the probe found a capability and no measured load — an
  // unloaded local model. Overwriting the box with what the weights *allow*
  // is how a preset ends up budgeted at 262,144 against a model LM Studio
  // will JIT-load at its default. Say so and leave the number alone.
  if (!r.context) { toast(r.note || 'that model is not loaded'); return; }
  $('blkContext').value = r.context;
  S.blk.dirty = true;
  renderBlocks();
  toast(r.note ? r.note
    : (r.max && r.max !== r.context
       ? `loaded at ${r.context.toLocaleString()} (supports ${r.max.toLocaleString()})`
       : `context: ${r.context.toLocaleString()}`));
};
$('blkPreset').onchange = () => loadBlocksFor($('blkPreset').value);

// The rail owns the prompt now, so the blocks have to be loaded whether or not
// the settings modal was ever opened. Keyed on the active preset, and re-run
// when that changes, because the blocks belong to the preset.
async function loadPromptRail() {
  await loadBlockCat();
  // MUST follow the ACTIVE preset, never "whichever preset is first". Blocks
  // live on the preset, so falling back to presets[0] showed one prompt while
  // the chat sent another: the rail listed preset 33's blocks, the topbar said
  // "no preset", and turning a block off in the inspector changed nothing and
  // explained nothing. A prompt panel that is not the prompt is worse than no
  // panel.
  if (!S.presetId) {
    S.blk.presetId = null;
    S.blk.list = JSON.parse(JSON.stringify(S.blk.cat.default));
    S.blk.dirty = false;
    renderBlocks();
    return;
  }
  const want = S.presetId;
  if (String(S.blk.presetId) !== String(want) || !S.blk.list.length) {
    if ($('blkPreset').options.length) $('blkPreset').value = want;
    await loadBlocksFor(want);
  } else {
    renderBlocks();
  }
}
$('blkRailFull').onclick = () => { openSettings('blocks'); openBlocks(); };
$('blkRailInspect').onclick = () => $('btnInspect').click();
$('blkRailSave').onclick = () => $('blkSave').click();

function blockTokens(b) {
  if (!b.enabled) return 0;
  if (b.kind === 'marker') return 0;      // engine-filled; counted at send time
  // A built-in text block carries no content of its own: its text lives in
  // prompts.py under `layer`. Counting b.content alone reported ZERO for
  // every shipped block, so a fresh install opened the prompt panel and was
  // told its whole prompt cost 0% of context. The two __-prefixed layers come
  // from the preset's jailbreak and the card, not from prompts.py, and are
  // still unknown until send time.
  if (!b.content && b.layer && !b.layer.startsWith('__')) {
    return TOK(PROMPT_TEXT[b.layer] || '');
  }
  return TOK(b.content);
}

function blkContext() {
  return Math.max(512, Number($('blkContext').value) || 8192);
}

function renderBlocks() {
  const box = $('blkList');
  box.innerHTML = '';
  const groups = S.blk.cat.groups;
  let total = 0;

  // One exclusive name = ONE radio set, membered in whole-list order — the
  // same order resolve_exclusive uses to pick which enabled member is
  // actually sent. Clustered per display group instead, an imported preset
  // whose "(Choose One)" members landed in different display groups painted
  // two half-sets that could both show a checked radio — or an "off" saying
  // nothing is sent while the other half was sending.
  const exGroups = {};
  for (const b of S.blk.list) {
    if (b.exclusive) {
      (exGroups[b.exclusive] = exGroups[b.exclusive] || []).push(b);
    }
  }

  // The rail and the settings tab are the same list, painted twice from the
  // same row builder. A DOM node lives in one place, so the rows are built
  // per container; what must not fork is the code that decides what a row
  // says. The rail is the permanent home: the prompt is the thing this app
  // claims to be about, so it does not live behind a modal.
  const rail = $('blkRailList');
  paint(box, false);
  if (rail) paint(rail, true);

  function paint(target, compact) {
    target.innerHTML = '';
    for (const g of groups) {
      const mine = S.blk.list.filter((b) => (b.group || 'style') === g.id);
      if (!mine.length) continue;
      const head = document.createElement('div');
      head.className = 'blk-group';
      head.innerHTML = compact
        ? `<b>${esc(g.label)}</b>`
        : `<b>${esc(g.label)}</b><span>${esc(g.why)}</span>`;
      target.appendChild(head);
      // Blocks sharing an `exclusive` name are one choice, so they are
      // painted as one radio set where the group's FIRST member (whole-list
      // order) lives — checkboxes that secretly shadow each other is the
      // exact ST failure blocks.py exists to not have.
      for (const b of mine) {
        if (b.exclusive) {
          const members = exGroups[b.exclusive];
          if (members[0] !== b) continue;
          target.appendChild(exclusiveSet(b.exclusive, members, compact));
          continue;
        }
        target.appendChild(blockRow(b, compact));
      }
    }
  }

  for (const b of S.blk.list) total += blockTokens(b);

  const ctx = blkContext();
  const pct = Math.min(100, Math.round((total / ctx) * 100));
  const k = (ctx / 1000).toFixed(ctx >= 10000 ? 0 : 1).replace(/\.0$/, '');
  const note = pct >= 40
    ? `${pct}% of your ${k}k context is spent before she says anything. `
      + `Every token here is history she can't have.`
    : `${pct}% of your ${k}k context. Markers (card, history, memory) are counted at send time.`;
  // With no preset selected the server assembles from the built-in defaults,
  // and there is nowhere to save an edit to. Say that rather than let someone
  // rearrange a list that is not going to be kept.
  const loose = !S.blk.presetId;
  for (const [t, m, n] of [['blkTotal', 'blkMeter', 'blkNote'],
                           ['blkRailTotal', 'blkRailMeter', 'blkRailNote']]) {
    if (!$(t)) continue;
    $(t).textContent = `~${total.toLocaleString()} tokens`;
    $(m).style.width = pct + '%';
    $(m).className = 'blk-meter-fill' + (pct > 60 ? ' bad' : pct > 30 ? ' warm' : '');
    $(n).textContent = loose
      ? 'No preset selected, so this is the built-in default and there is '
        + 'nowhere to save a change. Pick a preset in the top bar to edit it.'
      : note;
    $(n).className = 'note' + (loose ? ' bad' : '');
  }
  if ($('blkRailSave')) $('blkRailSave').disabled = loose;
  if ($('blkSave')) $('blkSave').disabled = loose;
}

// Friendly names for the exclusive groups we ship. An imported preset can
// carry any group name at all ("(Choose One)" headers become groups in
// stimport), so anything unknown falls back to the raw name.
const EX_LABELS = { pov: 'Point of view', length: 'Reply length' };
let exSeq = 0;   // radio `name`s must be unique per painted set

function exclusiveSet(exName, members, compact) {
  const wrap = document.createElement('div');
  wrap.className = 'blk-exset' + (compact ? ' tight' : '');
  // The counter alone names the radio group: exName is user/import-supplied
  // text and must never reach markup unescaped (uniqueness is all that
  // matters here — membership is the closure's, not the attribute's).
  const radio = `blk-ex-${++exSeq}`;
  // Same rule as the server: the FIRST enabled member is the one sent.
  const on = members.find((m) => m.enabled) || null;
  const label = EX_LABELS[exName]
    || exName.charAt(0).toUpperCase() + exName.slice(1);
  const head = document.createElement('div');
  head.className = 'blk-exhead';
  head.innerHTML = `<b>${esc(label)}</b><span class="blk-tag ex">select one</span>
    <label class="blk-exoff" title="none of these is sent — the card decides">
      <input type="radio" name="${radio}"${on ? '' : ' checked'}> off</label>`;
  head.querySelector('input').onchange = () => {
    for (const x of S.blk.list) if (x.exclusive === exName) x.enabled = false;
    S.blk.dirty = true;
    renderBlocks();
  };
  wrap.appendChild(head);
  for (const m of members) {
    wrap.appendChild(blockRow(m, compact, { radio, checked: m === on }));
  }
  return wrap;
}

function blockRow(b, compact, ex) {
  const row = document.createElement('div');
  row.className = 'blk-row' + (b.enabled ? '' : ' off')
    + (compact ? ' tight' : '');
  row.draggable = !compact;
  row.dataset.id = b.id;
  const tok = blockTokens(b);
  const tags = [];
  if (b.kind === 'marker') tags.push(`<span class="blk-tag mk">${esc(b.marker)}</span>`);
  if (b.role && b.role !== 'system' && b.kind !== 'marker')
    tags.push(`<span class="blk-tag role">${esc(b.role)}</span>`);
  if (b.place === 'depth') tags.push(`<span class="blk-tag depth">depth ${b.depth}</span>`);
  // Inside a radio set the set's own header already says "select one".
  if (b.exclusive && !ex) tags.push(`<span class="blk-tag ex">${esc(b.exclusive)}</span>`);
  if (b.builtin) tags.push('<span class="blk-tag built">built-in</span>');
  if ((b.models || []).length) tags.push(`<span class="blk-tag mod">${esc(b.models.join('/'))}</span>`);

  row.innerHTML = `
    ${compact ? '' : '<span class="blk-grip" title="drag to reorder">⠿</span>'}
    <input type="${ex ? 'radio' : 'checkbox'}" class="blk-on"${ex ? ` name="${ex.radio}"` : ''}${(ex ? ex.checked : b.enabled) ? ' checked' : ''}>
    <div class="blk-main">
      <div class="blk-name">${esc(b.name)}${tags.join('')}</div>
      ${b.why && !compact ? `<div class="blk-why">${esc(b.why)}</div>` : ''}
    </div>
    <span class="blk-tok">${b.kind === 'marker' ? '·' : tok.toLocaleString()}</span>
    <button class="mini-btn blk-up" title="move up">↑</button>
    <button class="mini-btn blk-down" title="move down">↓</button>
    ${compact ? '' : '<button class="mini-btn blk-edit" title="edit">✎</button>'}`;

  const idx = () => S.blk.list.findIndex((x) => x.id === b.id);
  row.querySelector('.blk-on').onchange = (e) => {
    if (ex) {
      // Radio semantics across the WHOLE list, not just this painted set:
      // an imported preset can scatter one exclusive group across display
      // groups, and enabling here must still disable the copy over there —
      // the server would only send the first anyway.
      for (const x of S.blk.list) {
        if (x.exclusive === b.exclusive) x.enabled = (x === b);
      }
    } else {
      b.enabled = e.target.checked;
    }
    S.blk.dirty = true; renderBlocks();
  };
  row.querySelector('.blk-up').onclick = () => move(idx(), -1);
  row.querySelector('.blk-down').onclick = () => move(idx(), 1);
  if (!compact) row.querySelector('.blk-edit').onclick = () => editBlock(b, row);

  row.ondragstart = (e) => { e.dataTransfer.setData('text/plain', b.id); row.classList.add('dragging'); };
  row.ondragend = () => row.classList.remove('dragging');
  row.ondragover = (e) => { e.preventDefault(); row.classList.add('over'); };
  row.ondragleave = () => row.classList.remove('over');
  row.ondrop = (e) => {
    e.preventDefault();
    row.classList.remove('over');
    const from = S.blk.list.findIndex((x) => x.id === e.dataTransfer.getData('text/plain'));
    const to = idx();
    if (from < 0 || to < 0 || from === to) return;
    const [m] = S.blk.list.splice(from, 1);
    S.blk.list.splice(to, 0, m);
    S.blk.dirty = true;
    renderBlocks();
  };
  return row;
}

function move(i, d) {
  const j = i + d;
  if (i < 0 || j < 0 || j >= S.blk.list.length) return;
  const [m] = S.blk.list.splice(i, 1);
  S.blk.list.splice(j, 0, m);
  S.blk.dirty = true;
  renderBlocks();
}

function editBlock(b, row) {
  const open = row.nextElementSibling;
  if (open && open.classList.contains('blk-edit-pane')) { open.remove(); return; }
  const pane = document.createElement('div');
  pane.className = 'blk-edit-pane';
  const roles = S.blk.cat.roles.map((r) =>
    `<option value="${r}"${b.role === r ? ' selected' : ''}>${r}</option>`).join('');
  pane.innerHTML = `
    <label>name <input class="be-name" value="${esc(b.name)}"></label>
    <label>note to self <input class="be-why" value="${esc(b.why || '')}"></label>
    ${b.kind === 'marker'
      ? `<p class="hint">This is a slot CoomKit fills with <b>${esc(b.marker)}</b>. Move it to change where that content lands; there is nothing to edit.</p>`
      : `<label>sent as <select class="be-role">${roles}</select></label>
         <label>placement <select class="be-place">
           <option value="order"${b.place !== 'depth' ? ' selected' : ''}>in order</option>
           <option value="depth"${b.place === 'depth' ? ' selected' : ''}>at depth (from the end)</option>
         </select></label>
         <label>depth <input class="be-depth" type="number" min="0" value="${b.depth || 0}"></label>
         <label>exclusive group <span class="lbl-hint">blocks sharing a name are a radio set, only the first enabled one is sent</span>
           <input class="be-ex" value="${esc(b.exclusive || '')}" placeholder="e.g. pov"></label>
         <label>content <textarea class="be-content" rows="8">${esc(b.content || '')}</textarea></label>`}
    <div class="row-btns">
      <button class="primary-btn be-ok">apply</button>
      ${b.builtin ? '' : '<button class="ghost-btn danger-btn be-del">delete</button>'}
    </div>`;
  row.after(pane);
  pane.querySelector('.be-ok').onclick = () => {
    b.name = pane.querySelector('.be-name').value.trim() || b.name;
    b.why = pane.querySelector('.be-why').value.trim();
    if (b.kind !== 'marker') {
      b.role = pane.querySelector('.be-role').value;
      b.place = pane.querySelector('.be-place').value;
      b.depth = Number(pane.querySelector('.be-depth').value) || 0;
      b.exclusive = pane.querySelector('.be-ex').value.trim();
      b.content = pane.querySelector('.be-content').value;
    }
    S.blk.dirty = true;
    renderBlocks();
  };
  const del = pane.querySelector('.be-del');
  if (del) del.onclick = () => {
    S.blk.list = S.blk.list.filter((x) => x.id !== b.id);
    S.blk.dirty = true;
    renderBlocks();
  };
}

$('blkAdd').onclick = () => {
  const id = 'my.' + Date.now().toString(36);
  // Before the history marker: a new block almost always means "steer the
  // reply", and after the history is the expert move, not the default.
  const at = S.blk.list.findIndex((b) => b.marker === 'history');
  const nb = { id, name: 'New block', group: 'style', why: '', content: '',
    role: 'system', kind: 'text', marker: '', place: 'order', depth: 0,
    enabled: true, exclusive: '', models: [], builtin: false, layer: '' };
  S.blk.list.splice(at < 0 ? S.blk.list.length : at, 0, nb);
  S.blk.dirty = true;
  renderBlocks();
  const row = [...document.querySelectorAll('.blk-row')].find((r) => r.dataset.id === id);
  if (row) row.querySelector('.blk-edit').click();
};

$('blkLibBtn').onclick = () => {
  const box = $('blkLib');
  box.hidden = !box.hidden;
  if (box.hidden) return;
  const have = new Set(S.blk.list.map((b) => b.id));
  const fam = (S.llm.model || '').toLowerCase();
  const list = $('blkLibList');
  list.innerHTML = '';
  for (const b of S.blk.cat.library) {
    const applies = !(b.models || []).length
      || b.models.some((m) => fam.includes(m) || m === 'local' || m === 'remote');
    const el = document.createElement('div');
    el.className = 'blk-lib-row' + (applies ? '' : ' dim');
    el.innerHTML = `<div><b>${esc(b.name)}</b>
      <span class="blk-why">${esc(b.why)}</span></div>
      <span class="blk-tok">${TOK(b.content).toLocaleString()}</span>`;
    const add = document.createElement('button');
    add.className = 'mini-btn';
    add.textContent = have.has(b.id) ? 'added' : '+ add';
    add.disabled = have.has(b.id);
    add.onclick = () => {
      const at = S.blk.list.findIndex((x) => x.marker === 'history');
      S.blk.list.splice(at < 0 ? S.blk.list.length : at, 0,
        Object.assign({}, b, { enabled: true }));
      S.blk.dirty = true;
      add.textContent = 'added'; add.disabled = true;
      renderBlocks();
    };
    el.appendChild(add);
    list.appendChild(el);
  }
};
$('blkLibClose').onclick = () => { $('blkLib').hidden = true; };

$('blkStarter').onclick = async () => {
  if (!S.blk.presetId) return;
  const kind = /openrouter|anthropic|openai|http(s)?:\/\/(?!127|localhost)/i
    .test(S.llm.backend || '') ? 'remote' : 'local';
  const r = await post(`/api/presets/${S.blk.presetId}/blocks/starter`, { kind });
  if (r.error) { toast('failed: ' + r.error); return; }
  toast(`added ${r.added} ${kind} blocks`);
  await loadPresets();
  await loadBlocksFor(S.blk.presetId);
};

// One save path. The rail, the settings tab and the inspector's turn-off
// button all land here rather than each posting their own version.
async function saveBlocks(quiet) {
  if (!S.blk.presetId) return false;
  const r = await post(`/api/presets/${S.blk.presetId}/blocks`,
    { blocks: S.blk.list, context: blkContext() });
  if (r.error) { toast('failed: ' + r.error); return false; }
  S.blk.dirty = false;
  await loadPresets();
  if (!quiet) toast('prompt order saved');
  return true;
}
$('blkSave').onclick = () => saveBlocks();

// ── SillyTavern import ───────────────────────────────────────────
$('blkImport').onclick = () => $('blkFile').click();
$('blkFile').onchange = async (ev) => {
  const f = ev.target.files[0];
  if (!f) return;
  ev.target.value = '';
  $('blkNote').textContent = 'reading…';
  const b64 = await fileToB64(f);
  const pre = await post('/api/presets/import-st', { b64, dry_run: true });
  if (pre.error) { $('blkNote').textContent = pre.error; return; }
  const s2 = pre.summary;
  const d = s2.dropped;
  const ctx = pre.context || blkContext();
  const warnings = (s2.notes || []).map((n) =>
    `<p class="imp-warn">⚠ ${esc(n)}</p>`).join('');
  const pct = Math.round((s2.tokens / ctx) * 100);
  const rows = s2.biggest.map((b) =>
    `<tr><td>${esc(b.name)}</td><td class="num">${b.tokens.toLocaleString()}</td></tr>`).join('');
  const body = `
    <p class="hint">Importing <b>${esc(f.name)}</b></p>
    <div class="imp-stat"><b>${s2.blocks}</b><span>blocks (${s2.text_blocks} text, ${s2.markers} markers)</span></div>
    <div class="imp-stat ${pct >= 40 ? 'bad' : ''}"><b>~${s2.tokens.toLocaleString()}</b>
      <span>tokens before your character is described, ${pct}% of a ${(ctx / 1000).toFixed(0)}k context</span></div>
    <div class="imp-stat"><b>${d.separators + d.disabled}</b>
      <span>dropped: ${d.separators} empty separators, ${d.disabled} disabled</span></div>
    ${warnings}
    <table class="imp-table"><thead><tr><th>biggest blocks</th><th class="num">tokens</th></tr></thead>
      <tbody>${rows}</tbody></table>`;
  const name = await dialog({
    title: 'Import from SillyTavern',
    body, input: 'name this preset',
    value: f.name.replace(/\.json$/i, '').slice(0, 60),
    ok: 'import',
  });
  if (!name) { $('blkNote').textContent = 'import cancelled'; return; }
  const r = await post('/api/presets/import-st', { b64, name });
  if (r.error) { $('blkNote').textContent = 'failed: ' + r.error; return; }
  await loadPresets();
  $('blkPreset').value = r.preset.id;
  await loadBlocksFor(r.preset.id);
  toast(`imported ${r.summary.blocks} blocks`);
};

// ── dialogs ──────────────────────────────────────────────────────
// confirm()/prompt() block the event loop, cannot be styled, and are the very
// first thing a new user meets on the import path. This is the same three
// calls with a real element behind them.

function dialog({ title, body, input, value, placeholder, ok = 'ok',
                  cancel = 'cancel', danger = false }) {
  return new Promise((resolve) => {
    $('dlgTitle').textContent = title || '';
    const box = $('dlgBody');
    box.innerHTML = '';
    if (body instanceof Node) box.appendChild(body);
    else box.innerHTML = body || '';
    $('dlgInputWrap').hidden = !input;
    if (input) {
      $('dlgInputLabel').textContent = input;
      $('dlgInput').value = value || '';
      $('dlgInput').placeholder = placeholder || '';
    }
    $('dlgYes').textContent = ok;
    $('dlgYes').className = 'primary-btn' + (danger ? ' danger-btn' : '');
    $('dlgNo').textContent = cancel;
    $('dlgBack').classList.add('on');
    if (input) setTimeout(() => $('dlgInput').select(), 30);

    const done = (val) => {
      $('dlgBack').classList.remove('on');
      $('dlgYes').onclick = $('dlgNo').onclick = $('dlgX').onclick = null;
      document.removeEventListener('keydown', onKey);
      resolve(val);
    };
    const accept = () => done(input ? ($('dlgInput').value.trim() || null) : true);
    function onKey(e) {
      if (e.key === 'Escape') done(null);
      if (e.key === 'Enter' && input) { e.preventDefault(); accept(); }
    }
    $('dlgYes').onclick = accept;
    $('dlgNo').onclick = $('dlgX').onclick = () => done(null);
    document.addEventListener('keydown', onKey);
  });
}
$('dlgBack').onclick = (e) => { if (e.target === $('dlgBack')) $('dlgNo').click(); };

// ── the phone ────────────────────────────────────────────────────
// A genuinely separate window with its own chat, not a skin over the main
// stream. That is the whole feature: she can text you while a scene is open
// behind it, and the two threads stay independent.

S.phone = { chat: null, busy: false, attachments: [], lastRole: null, seen: 0 };

const EMOJI = ['😂', '🥺', '😳', '😩', '😏', '😉', '😘', '🥵', '😈', '👀',
  '💦', '🍆', '🍑', '💋', '❤️', '🔥', '✨', '💅', '😭', '🙄', '😤', '🤤',
  '😴', '🫦', '🤭', '👉', '👈', '🤏', '🙏', '😅', '🥰', '😌', '😔', '🫠',
  '😇', '🤡', '💀', '🎀', '🌸', '💖', '😬', '🫣', '😮\u200d💨', '🥴'];

function phoneClock() {
  const d = new Date();
  $('phoneClock').textContent =
    `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
}

async function openPhone(charId) {
  const c = S.chars.find((x) => x.id === charId) || (S.chat
    && S.chars.find((x) => x.id === S.chat.charId));
  if (!c) { toast('pick a character first'); return; }
  phoneClock();
  setInterval(phoneClock, 30000);
  $('phoneName').textContent = c.name;
  if (c.avatar) {
    $('phoneAva').src = `/api/avatars/${c.avatar}`;
    $('phoneAva').hidden = false; $('phoneAvaBlank').hidden = true;
  } else { $('phoneAva').hidden = true; $('phoneAvaBlank').hidden = false; }

  // Reuse this character's sms thread if one exists — texting is supposed to
  // be a continuous side-channel, not a fresh conversation each time.
  const key = c.id + ':sms';
  let chatId = S.chatsByChar[key];
  // Same rule as openChat: the server knows what threads exist, the browser
  // only caches which one was last open. Without this a cleared localStorage
  // silently started a second thread beside the real one.
  if (!chatId) {
    const rows = await chatsFor(c.id, 'sms');
    if (rows.length) chatId = rows[0].id;
  }
  if (!chatId) {
    const r = await post('/api/chats/new', { character_id: c.id, mode: 'sms' });
    if (r.error) { toast('failed: ' + r.error); return; }
    chatId = r.chat_id;
  }
  S.chatsByChar[key] = chatId;
  saveUI();
  S.phone.chat = { id: chatId, charId: c.id, name: c.name, avatar: c.avatar };
  $('phone').hidden = false;
  $('phoneTab').hidden = true;
  await loadPhone();
  $('phoneInput').focus();
}

async function loadPhone() {
  if (!S.phone.chat) return;
  const d = await api(`/api/chats/${S.phone.chat.id}`);
  // Blank is a legitimate state now, so "no messages" and "that chat id is
  // gone" stopped being distinguishable at the render site. Recover the same
  // way restoreChat does rather than showing an empty thread forever.
  if (!d || d.error || !Array.isArray(d.messages)) {
    delete S.chatsByChar[S.phone.chat.charId + ':sms'];
    saveUI();
    const rows = await chatsFor(S.phone.chat.charId, 'sms');
    const id = rows.length ? rows[0].id
      : (await post('/api/chats/new', { character_id: S.phone.chat.charId, mode: 'sms' })).chat_id;
    if (!id) { toast('could not open that thread'); return; }
    S.phone.chat.id = id;
    S.chatsByChar[S.phone.chat.charId + ':sms'] = id;
    saveUI();
    return loadPhone();
  }
  const box = $('phoneThread');
  box.innerHTML = '';
  S.phone.lastRole = null;
  const msgs = d.messages;
  if (msgs.length) {
    const t = document.createElement('div');
    t.className = 'pm-time';
    t.textContent = 'today';
    box.appendChild(t);
  } else if (!S.phone.openingDismissed) {
    phoneOpeningCard(box);
  }
  const lastHer = msgs.map((m) => m.role).lastIndexOf('assistant');
  msgs.forEach((m, i) => {
    const wrap = phoneBubble(m.role, stripBlocks(m.content), false, m.id);
    for (const a of m.assets || []) phoneMedia(a, m.role, wrap);
    phoneTools(wrap, m, i === lastHer && i === msgs.length - 1);
  });
  S.phone.seen = msgs.length;
  S.phone.aware = !!(d && d.aware);
  S.phone.texting = (d && d.texting) || {};
  S.phone.lastAt = (d && d.last_at) || 0;
  S.phone.unpromptedToday = (d && d.unprompted_today) || 0;
  syncPhoneAware();
  syncNudge();
  startNudging();
  box.scrollTop = box.scrollHeight;
}

// A blank thread needs a first move. Inheriting first_mes was the wrong one:
// that is prose written to open a scene, and it lands in an iMessage bubble
// as narration.
function phoneOpeningCard(box) {
  const card = document.createElement('div');
  card.className = 'pm-open';
  const h = document.createElement('b');
  h.textContent = 'Nothing here yet.';
  const p = document.createElement('p');
  p.textContent = 'Someone has to go first. Not it.';
  card.append(h, p);

  const row = document.createElement('div');
  row.className = 'pm-open-btns';

  const mine = document.createElement('button');
  mine.className = 'mini-btn';
  mine.textContent = '✎ write her first text';
  mine.onclick = () => {
    row.hidden = true;
    const ta = document.createElement('textarea');
    ta.rows = 3;
    ta.placeholder = 'hey. you awake?';
    const save = document.createElement('button');
    save.className = 'mini-btn primary-btn';
    save.textContent = 'that’s her opener';
    save.onclick = async () => {
      const text = ta.value.trim();
      if (!text) return;
      const r = await post(`/api/chats/${S.phone.chat.id}/opening`, { text });
      if (r.error) { toast('failed: ' + r.error); return; }
      await loadPhone();
    };
    card.append(ta, save);
    ta.focus();
  };

  const hers = document.createElement('button');
  hers.className = 'mini-btn';
  hers.textContent = '✦ let her open';
  hers.onclick = async () => {
    if (!LLM_READY() && !(await pickModel())) { toast('no model selected'); return; }
    hers.disabled = true;
    hers.textContent = 'thinking…';
    const r = await post('/api/chats/text-first', phoneBody(requestBody()));
    hers.disabled = false;
    hers.textContent = '✦ let her open';
    if (r.error) { toast('failed: ' + r.error); return; }
    if (!r.sent) { toast('she had nothing to say. rude.'); return; }
    await loadPhone();
  };

  const me = document.createElement('button');
  me.className = 'mini-btn';
  me.textContent = 'I’ll text first';
  me.onclick = () => {
    S.phone.openingDismissed = true;
    card.remove();
    $('phoneInput').focus();
  };

  row.append(mine, hers, me);
  card.appendChild(row);
  box.appendChild(card);
}

// Whether the phone knows about the roleplay. Off by default — some people
// want the sidechat to be a clean slate, and injecting the scene into a
// texting thread changes what she talks about completely.
function syncPhoneAware() {
  const b = $('phoneAware');
  b.style.opacity = S.phone.aware ? '1' : '.4';
  b.title = S.phone.aware
    ? "she knows what happened in your roleplay, click to forget it"
    : "she has no idea what happened in your roleplay, click to let her know";
}
$('phoneAware').onclick = async () => {
  if (!S.phone.chat) return;
  const r = await post(`/api/chats/${S.phone.chat.id}/aware`,
    { enabled: !S.phone.aware });
  if (r.error) { toast('failed: ' + r.error); return; }
  S.phone.aware = r.aware;
  syncPhoneAware();
  toast(r.aware ? 'she knows about the scene now' : 'phone is a clean slate');
};

// Texts arrive as separate bubbles the way they actually do — a reply with
// two sentences on two lines is two messages, not one paragraph.
// One message = one wrapper, however many bubbles it splits into. Without
// the wrapper a message had no identity in the DOM at all, which is why the
// phone had no per-message controls and why the streaming re-render had to
// walk backwards over previousElementSibling guessing where it started.
function phoneBubble(role, text, animate = true, msgId = null) {
  const box = $('phoneThread');
  const me = role === 'user';
  const wrap = document.createElement('div');
  wrap.className = 'pm-msg ' + (me ? 'me' : 'them');
  if (msgId) wrap.dataset.msgId = String(msgId);
  if (S.phone.lastRole && S.phone.lastRole !== role) wrap.classList.add('pm-gap');
  const parts = String(text || '').split(/\n{1,}/).map((x) => x.trim()).filter(Boolean);
  for (const part of parts) {
    const el = document.createElement('div');
    el.className = 'pm ' + (me ? 'me' : 'them');
    el.textContent = part;   // model output is never trusted as markup
    wrap.appendChild(el);
  }
  box.appendChild(wrap);
  S.phone.lastRole = role;
  box.scrollTop = box.scrollHeight;
  return wrap;
}

// Rewrite a wrapper's bubbles in place, for a swipe.
function phoneRewrite(wrap, text) {
  const me = wrap.classList.contains('me');
  wrap.querySelectorAll('.pm:not(.pm-media)').forEach((n) => n.remove());
  const tools = wrap.querySelector('.pm-tools');
  const parts = String(text || '').split(/\n{1,}/).map((x) => x.trim()).filter(Boolean);
  for (const part of parts) {
    const el = document.createElement('div');
    el.className = 'pm ' + (me ? 'me' : 'them');
    el.textContent = part;
    wrap.insertBefore(el, tools || null);
  }
}

// edit / delete, and swipes on her side. Same endpoints as the main chat —
// all three message routes are already chat-mode agnostic.
function phoneTools(wrap, m, isLast) {
  const row = document.createElement('div');
  row.className = 'pm-tools';
  const btn = (cls, label, title) => {
    const b = document.createElement('button');
    b.className = 'pm-tool ' + cls;
    b.textContent = label;
    b.title = title;
    row.appendChild(b);
    return b;
  };

  if (m.role === 'assistant') {
    let idx = m.swipes ? (m.swipe_index ?? 0) : 0;
    let tot = Math.max(1, m.swipes || 0);
    const left = btn('', '◀', 'Previous take');
    const info = document.createElement('span');
    info.className = 'swipe-info';
    info.textContent = `${idx + 1}/${tot}`;
    row.appendChild(info);
    const right = btn('', '▶', isLast ? 'Next take, or a new one' : 'Next take');
    const paint = (t) => phoneRewrite(wrap, stripBlocks(t));
    left.onclick = async () => {
      if (idx <= 0) { toast("that's the first take"); return; }
      const r = await swipe(m.id, idx - 1, wrap, paint);
      if (r && r.ok) { idx = r.index; tot = r.total; info.textContent = `${idx + 1}/${tot}`; }
    };
    right.onclick = async () => {
      if (idx < tot - 1) {
        const r = await swipe(m.id, idx + 1, wrap, paint);
        if (r && r.ok) { idx = r.index; tot = r.total; info.textContent = `${idx + 1}/${tot}`; }
        return;
      }
      // _prepare_request only accepts a regenerate when the LAST row is an
      // assistant turn, so offer it nowhere else.
      if (!isLast) { toast('only her latest text can be re-rolled'); return; }
      await phoneRegen();
    };
  }

  btn('', '✎', 'Edit').onclick = async () => {
    const box = document.createElement('div');
    box.innerHTML = '<label>text <textarea class="pe" rows="5"></textarea></label>';
    box.querySelector('.pe').value = m.content;   // the RAW stored text
    if (!await dialog({ title: 'Edit this text', body: box, ok: 'save' })) return;
    const text = box.querySelector('.pe').value.trim();
    if (!text) return;
    const r = await post('/api/messages/' + m.id, { content: text });
    if (r.error) { toast('failed: ' + r.error); return; }
    await loadPhone();
  };
  btn('danger-btn', '✕', 'Delete').onclick = async () => {
    if (!await dialog({ title: 'Delete this text?', body: 'Gone for good.',
                        ok: 'delete', danger: true })) return;
    await del('/api/messages/' + m.id);
    await loadPhone();
  };
  wrap.appendChild(row);
}

function phoneMedia(asset, role = 'assistant', wrap = null) {
  const box = $('phoneThread');
  const el = document.createElement('div');
  el.className = 'pm pm-media ' + (role === 'user' ? 'me' : 'them');
  el.appendChild(mediaEl(asset));
  if (wrap) wrap.appendChild(el);
  else {
    const solo = document.createElement('div');
    solo.className = 'pm-msg ' + (role === 'user' ? 'me' : 'them');
    solo.appendChild(el);
    box.appendChild(solo);
  }
  S.phone.lastRole = role;
  box.scrollTop = box.scrollHeight;
  return el;
}

function phoneTyping(on) {
  const box = $('phoneThread');
  let el = box.querySelector('.pm-typing');
  if (!on) { if (el) el.remove(); return; }
  if (el) return;
  el = document.createElement('div');
  el.className = 'pm-typing';
  el.innerHTML = '<i></i><i></i><i></i>';
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

async function phoneSend() {
  const inp = $('phoneInput');
  const text = inp.value.trim();
  if (S.phone.busy || !S.phone.chat) return;
  if (!text && !S.phone.attachments.length) return;
  if (!LLM_READY()) { pickModel(); return; }

  S.phone.busy = true;
  $('phoneSend').disabled = true;
  if (text) phoneBubble('user', text);
  for (const a of S.phone.attachments) {
    phoneMedia({ kind: 'image', url: a.dataUrl }, 'user');
  }
  inp.value = '';
  $('phoneEmoji').hidden = true;
  $('phoneStatus').textContent = 'typing…';
  phoneTyping(true);

  const body = {
    chat_id: S.phone.chat.id,
    backend: S.llm.backend, model: S.llm.model,
    tools: S.tools, samplers: samplersFromInputs(),
    thinking_mode: $('thinkMode').value,
    text: text || '(sent a photo)',
  };
  if (S.presetId) body.preset_id = +S.presetId;
  if (S.phone.attachments.length) {
    body.images = S.phone.attachments.map((a) => ({ name: a.name, b64: a.b64 }));
  }
  S.phone.attachments = [];
  renderPhoneAttach();

  await phoneStream(body);
}

// The phone's one SSE reader. phoneSend and phoneRegen both use it — the
// main chat learned this lesson already (streamInto), and this consumer is
// the one that silently discarded every think frame for want of a second
// look at it.
async function phoneStream(body) {
  let raw = '', wrap = null;
  let phoneScroll = $('phoneThread').scrollTop;
  try {
    const resp = await fetch('/api/chats/send', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data:')) continue;
        const payload = line.slice(5).trim();
        if (!payload || payload === '[DONE]') continue;
        let chunk;
        try { chunk = JSON.parse(payload); } catch { continue; }
        if (chunk.error) { toast('error: ' + chunk.error); continue; }
        if (chunk.studio_pending) { phoneStudio(chunk.studio_pending); continue; }
        if (chunk.tool_pending) { showTool(chunk.tool_pending); continue; }
        if (chunk.done || chunk.think !== undefined || chunk.notice) continue;
        if (chunk.text) {
          raw += chunk.text;
          phoneTyping(false);
          // Re-render as it streams so the split into separate texts settles
          // into its final shape rather than jumping around. One wrapper to
          // remove, instead of walking backwards over siblings guessing
          // where the message began.
          if (wrap) wrap.remove();
          const near = nearBottom($('phoneThread'));
          wrap = phoneBubble('assistant', stripBlocks(raw));
          if (!near) $('phoneThread').scrollTop = phoneScroll;
        }
      }
    }
  } catch (e) {
    toast('connection died');
  }
  phoneTyping(false);
  $('phoneStatus').textContent = 'online';
  S.phone.busy = false;
  $('phoneSend').disabled = false;
  await loadPhone();
  return raw;
}

// Another take of her latest text. requestBody hardcodes the MAIN chat's id,
// hence the override — the camera handler and the nudge scheduler already
// use exactly this pattern.
async function phoneRegen() {
  if (S.phone.busy || !S.phone.chat) return;
  if (!LLM_READY() && !(await pickModel())) { toast('no model selected'); return; }
  S.phone.busy = true;
  $('phoneSend').disabled = true;
  $('phoneStatus').textContent = 'typing…';
  phoneTyping(true);
  await phoneStream(phoneBody(requestBody(true)));
}

// requestBody() is built for the MAIN chat: overriding chat_id points it at
// the phone thread, but the director channel belongs to the scene the bar is
// open over — stage direction for the main adventure must not steer her
// texting sidechat. Strip it wherever the phone borrows the main body.
function phoneBody(base) {
  const b = Object.assign(base, { chat_id: S.phone.chat.id });
  delete b.director;
  delete b.director_notes;
  return b;
}

// She asked to send something. Approve it in the phone rather than the main
// chat — the whole point is that this window stands alone.
function phoneStudio(p) {
  const box = $('phoneThread');
  const card = document.createElement('div');
  card.className = 'pm them pm-gap';
  card.innerHTML = `<b>${esc(p.recipe.replace(/-/g, ' '))}</b><br>`
    + `<span style="font-size:12px;opacity:.75">wants to send you a ${esc(p.kind)} · ${esc(p.label)}</span>`;
  const go = document.createElement('button');
  go.className = 'mini-btn';
  go.textContent = 'let her';
  go.style.marginTop = '6px';
  const no = document.createElement('button');
  no.className = 'mini-btn';
  no.textContent = 'nah';
  go.onclick = async () => {
    go.disabled = no.disabled = true;
    $('phoneStatus').textContent = 'sending a photo…';
    const r = await post('/api/studio/approve', { id: p.id });
    $('phoneStatus').textContent = 'online';
    if (r.error) { card.innerHTML = `<span style="opacity:.7">couldn't send: ${esc(r.error)}</span>`; return; }
    card.remove();
    for (const a of r.assets) phoneMedia(a, 'assistant');
    loadGallery();
  };
  no.onclick = async () => { await post('/api/studio/reject', { id: p.id }); card.remove(); };
  card.append(document.createElement('br'), go, no);
  box.appendChild(card);
  box.scrollTop = box.scrollHeight;
}

function renderPhoneAttach() {
  const box = $('phoneAttach');
  box.innerHTML = '';
  box.hidden = !S.phone.attachments.length;
  S.phone.attachments.forEach((a, i) => {
    const img = document.createElement('img');
    img.src = a.dataUrl;
    img.title = 'remove';
    img.onclick = () => { S.phone.attachments.splice(i, 1); renderPhoneAttach(); };
    box.appendChild(img);
  });
}

$('phoneSend').onclick = phoneSend;
$('phoneInput').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); phoneSend(); }
});
$('phonePlus').onclick = () => $('phoneFile').click();
$('phoneFile').onchange = async (ev) => {
  for (const f of [...ev.target.files].slice(0, 3)) {
    const b64 = await fileToB64(f);
    S.phone.attachments.push({ name: f.name, b64,
      dataUrl: `data:${f.type || 'image/png'};base64,${b64}` });
  }
  ev.target.value = '';
  renderPhoneAttach();
};
// The camera asks *her* for a selfie — same studio path as the rail.
$('phoneCam').onclick = async () => {
  if (!S.phone.chat) return;
  if (!LLM_READY()) { pickModel(); return; }
  $('phoneStatus').textContent = 'asking her…';
  // Through phoneBody so the main chat's director ride-alongs are stripped —
  // the draft route ignores them today, but a future recipe that reads them
  // must not inherit another scene's direction by accident. The composer
  // text goes too, for the same reason.
  const camBody = Object.assign(phoneBody(requestBody()), {
    recipe: 'selfie', opts: {}, character_id: S.phone.chat.charId,
  });
  delete camBody.text;
  const d = await post('/api/studio/draft', camBody);
  $('phoneStatus').textContent = 'online';
  if (d.error) { toast('failed: ' + d.error); return; }
  phoneStudio(d);
};
$('phoneEmojiBtn').onclick = () => {
  const box = $('phoneEmoji');
  box.hidden = !box.hidden;
  if (box.hidden || box.childElementCount) return;
  for (const e of EMOJI) {
    const b = document.createElement('button');
    b.textContent = e;
    b.onclick = () => {
      const inp = $('phoneInput');
      inp.value += e;
      inp.focus();
    };
    box.appendChild(b);
  }
};
$('phoneClose').onclick = () => { $('phone').hidden = true; $('phoneTab').hidden = true; };
$('phoneMin').onclick = () => {
  $('phone').hidden = true;
  $('phoneTabName').textContent = S.phone.chat ? S.phone.chat.name : 'messages';
  $('phoneTab').hidden = false;
};
$('phoneTab').onclick = () => {
  $('phone').hidden = false;
  $('phoneTab').hidden = true;
  $('phoneTabDot').hidden = true;
  $('phoneThread').scrollTop = $('phoneThread').scrollHeight;
};

// Drag it by the status bar. It is a window; it should move like one.
(() => {
  let dx = 0, dy = 0, on = false;
  $('phoneDrag').addEventListener('mousedown', (e) => {
    const r = $('phone').getBoundingClientRect();
    dx = e.clientX - r.left; dy = e.clientY - r.top; on = true;
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!on) return;
    const el = $('phone');
    el.style.left = Math.max(0, Math.min(window.innerWidth - 120, e.clientX - dx)) + 'px';
    el.style.top = Math.max(0, Math.min(window.innerHeight - 60, e.clientY - dy)) + 'px';
    el.style.right = 'auto'; el.style.bottom = 'auto';
  });
  document.addEventListener('mouseup', () => { on = false; });
})();

// ── first run ────────────────────────────────────────────────────
// Every step does a real thing — probes a backend, pings ComfyUI, installs
// blocks, sets the GPU policy. Gemma-chan narrates because a setup screen is
// the one place a project gets to have a personality, and because "detected
// 20,736 tokens" lands better with someone rude standing next to it.

S.wiz = { step: 0, backends: [], picked: null, comfy: null, vram: 'off', ctx: 0,
          wfAdded: 0, stPreset: null };

const WIZ = [
  { id: 'hello',  label: 'hello',      icon: '✦' },
  { id: 'brain',  label: 'her brain',  icon: '🧠' },
  { id: 'gpu',    label: 'the GPU',    icon: '🎨' },
  { id: 'vram',   label: 'vram',       icon: '⚡' },
  // Optional, and sits before `blocks` on purpose: an imported SillyTavern
  // preset becomes the preset that the blocks step then targets.
  { id: 'bring',  label: 'bring your own', icon: '📦' },
  { id: 'blocks', label: 'her prompt', icon: '🧩' },
  { id: 'done',   label: 'done',       icon: '★' },
];

// The mascot is an OPTIONAL asset — a build that ships without her (or a
// failed load) must degrade to no picture, not a broken-image icon. `onerror`
// in the markup survives src reassignment; this clears the flag on each swap
// so one miss does not hide her permanently.
function gemma(mood) {
  const el = $('wizGemma');
  el.hidden = false;
  el.src = `/img/gemma/${mood}.png`;
}

function wizSay(html, mood = 'smug') {
  gemma(mood);
  $('wizSay').innerHTML = html;
}

function renderWizSteps() {
  $('wizSteps').innerHTML = WIZ.map((w, i) =>
    `<div class="wiz-step${i === S.wiz.step ? ' on' : ''}${i < S.wiz.step ? ' done' : ''}">`
    + `<i>${i < S.wiz.step ? '✓' : w.icon}</i>${esc(w.label)}</div>`).join('');
}

async function openWizard() {
  S.wiz.step = 0;
  $('wizBack').classList.add('on');
  await wizStep();
}

async function wizStep() {
  renderWizSteps();
  const step = WIZ[S.wiz.step];
  const body = $('wizBody');
  body.innerHTML = '';
  $('wizNote').textContent = '';
  $('wizBack2').disabled = S.wiz.step === 0;
  $('wizNext').textContent = S.wiz.step === WIZ.length - 1 ? 'finish ★' : 'next';
  $('wizTitle').textContent = {
    hello: 'CoomKit', brain: 'Pick her brain', gpu: 'Pictures',
    vram: 'One card, two tenants', bring: 'Bring your own',
    blocks: 'What goes in the prompt', done: "That's it",
  }[step.id];

  if (step.id === 'hello') {
    wizSay(`So you found this. <b>Fine.</b> I'm Gemma-chan and apparently I'm
      running your smut now.<br><br>Four questions, then you can go be weird in
      peace, six if you've got things of your own to bring. I'll do the actual
      work, you just point at things.`, 'happy');
    body.innerHTML = `<div class="wiz-card"><b>What this is</b>
      <p>Local-first adult roleplay. Text, images, video, voice, music, your
      models, your ComfyUI, your machine. Nothing leaves it unless you point
      it somewhere hosted, and I'll tell you when that happens.</p></div>
      <div class="wiz-card"><b>What I need from you</b>
      <p>A model to think with, and optionally a ComfyUI to draw with. That's
      genuinely it.</p></div>`;
  }

  if (step.id === 'brain') {
    wizSay(`Something has to do the thinking, and it isn't going to be you.
      Let me look.`, 'smug');
    body.innerHTML = '<p class="hint">probing the usual ports…</p>';
    const d = await api('/api/backends');
    S.wiz.backends = (d.backends || []).filter((b) => (b.models || []).length);
    if (!S.wiz.backends.length) {
      wizSay(`Nothing. <b>Nothing at all.</b><br><br>Start LM Studio, or
        llama.cpp, or literally anything that speaks OpenAI, then hit retry.
        I'll wait. Not patiently.`, 'flat');
      body.innerHTML = `<div class="wiz-card warn"><b>No backend found</b>
        <p>Checked LM Studio (1234), llama.cpp (8080), Ollama (11434),
        KoboldCpp (5001), TabbyAPI (5000), vLLM (8000).</p></div>`;
      const b = document.createElement('button');
      b.className = 'ghost-btn'; b.textContent = 'retry';
      b.onclick = wizStep;
      body.appendChild(b);
      return;
    }
    const local = S.wiz.backends.filter((b) => !b.remote);
    wizSay(local.length
      ? `There. <b>${local.length} local backend${local.length > 1 ? 's' : ''}</b>
         with something loaded. Local means the prefills actually work and
         nobody's reading your chat logs. Pick one.`
      : `Only hosted ones. That'll work, but they lie to you about prefills and
         they'll refuse things. Your funeral.`, local.length ? 'proud' : 'flat');
    const wrap = document.createElement('div');
    wrap.className = 'wiz-pick';
    // NO CAP. This used to be .slice(0, 6) with no scroll container, so
    // someone who started llama-server with their whole model folder was
    // shown six of them and had no way to reach the rest — it read as a
    // missing scrollbar, but the list was genuinely truncated. The box
    // scrolls now (see .wiz-pick in style.css) and the count says how many
    // there are, so a silent truncation cannot come back unnoticed.
    const total = S.wiz.backends.reduce((n, b) => n + (b.models || []).length, 0);
    S.wiz.backends.forEach((b) => {
      (b.models || []).forEach((m) => {
        const o = document.createElement('label');
        o.className = 'wiz-opt';
        o.innerHTML = `<input type="radio" name="wizmodel">
          <div><b>${esc(m)}</b><span>${esc(b.label || b.url)}${b.remote ? ' · hosted' : ' · local'}</span></div>`;
        o.querySelector('input').onchange = () => {
          [...wrap.children].forEach((c) => c.classList.remove('on'));
          o.classList.add('on');
          S.wiz.picked = { backend: b.url, model: m, remote: !!b.remote };
        };
        o.dataset.hay = (m + ' ' + (b.label || b.url)).toLowerCase();
        wrap.appendChild(o);
      });
    });
    body.innerHTML = '';
    // Past a dozen, scrolling to find one is worse than typing three letters.
    if (total > 12) {
      const find = document.createElement('input');
      find.type = 'search';
      find.className = 'wiz-find';
      find.placeholder = `filter ${total} models…`;
      find.oninput = () => {
        const q = find.value.trim().toLowerCase();
        [...wrap.children].forEach((c) => {
          c.hidden = !!q && !c.dataset.hay.includes(q);
        });
      };
      body.appendChild(find);
    }
    body.appendChild(wrap);
    const count = document.createElement('p');
    count.className = 'hint';
    count.textContent = total === 1 ? '1 model' : `${total} models, all of them`;
    body.appendChild(count);
    if (!S.wiz.picked) wrap.querySelector('input').click();
  }

  if (step.id === 'gpu') {
    wizSay(`Now the fun half. Point me at a ComfyUI and she can send you
      pictures, clips, voice notes, entire songs. Skip it and she's just very
      good at typing.`, 'happy');
    body.innerHTML = `<label>ComfyUI address
      <input id="wizComfy" placeholder="http://127.0.0.1:8188" value="${esc((S.cfg && S.cfg.comfyui_url) || 'http://127.0.0.1:8188')}"></label>
      <div class="row-btns"><button class="ghost-btn" id="wizComfyTest">test it</button></div>
      <div id="wizComfyOut"></div>`;
    const testBtn = body.querySelector('#wizComfyTest');
    const out = body.querySelector('#wizComfyOut');
    testBtn.onclick = async () => {
      const url = body.querySelector('#wizComfy').value.trim();
      out.innerHTML = '<p class="hint">knocking…</p>';
      const r = await post('/api/comfy/ping', { url });
      if (!r.ok) {
        S.wiz.comfy = null;
        wizSay(`Nothing there. Either it isn't running or you typed it wrong,
          and I know which one I'd bet on.`, 'flat');
        out.innerHTML = `<div class="wiz-card warn"><b>No answer</b>
          <p>${esc(r.error || 'unreachable')}</p></div>`;
        return;
      }
      S.wiz.comfy = url;
      await post('/api/config', { comfyui_url: url });
      const dev = (r.devices || [])[0] || 'a GPU';
      wizSay(`Oh, <b>nice card</b>. Alright, I'm impressed. Slightly.`, 'proud');
      out.innerHTML = `<div class="wiz-card good"><b>Connected</b>
        <p>${esc(dev)}</p></div>`;
    };
    testBtn.click();
  }

  if (step.id === 'vram') {
    const cfg = await api('/api/config');
    const v = await api('/api/vram');
    const total = (v.gpu && v.gpu.vram_total_gb) || 0;
    S.wiz.ctx = 0;
    // The honest threshold is not "is this a big card", it is "does a chat
    // model plus the heaviest workflow fit". H3 alone wants 26 GB and a 12B
    // at long context is another 7, so anything under about 40 needs parking
    // whether or not it feels like a lot.
    if (total && total < 20) {
      wizSay(`${total} gigs. <b>Oh no.</b> 😂<br><br>You're a VRAMlet, sweetheart.
        Your chat model and a video model are not fitting on that thing
        together, one of them has to get off the card. Let me handle it or
        just enjoy your out-of-memory errors.`, 'laugh');
    } else if (total && total < 40) {
      wizSay(`${total} gigs. Respectable! Not <b>enough</b>, but respectable.
        😏<br><br>The video model alone wants 26, and your chat model is
        sitting on another seven. Do the maths, something has to move, and
        it's going to be me moving it.`, 'smug');
    } else if (total) {
      wizSay(`${total} gigs. Look at <b>Mr. Moneybags</b> over here. You can
        genuinely leave this off. Show-off.`, 'flat');
    } else {
      wizSay(`No GPU reported, so this is academic. Leave it off.`, 'flat');
    }
    body.innerHTML = `<div class="wiz-pick" id="wizVram"></div>
      <div class="wiz-card"><b>What it actually does</b>
      <p>Before a big render, I ask your chat model to step off the GPU, then
      put it back afterwards, at the context length you had it on, not the
      default, because reloading it wrong silently truncates every later
      chat.</p></div>`;
    const opts = [
      ['auto', 'Park it when I have to', 'Only when the job needs more room than is free. The sensible one.'],
      ['always', 'Park it every time', 'Slower, but nothing ever OOMs.'],
      ['off', "Don't touch my models", 'For two-GPU show-offs and people who enjoy suffering.'],
    ];
    const pick = body.querySelector('#wizVram');
    opts.forEach(([val, name, why]) => {
      const o = document.createElement('label');
      o.className = 'wiz-opt';
      o.innerHTML = `<input type="radio" name="wizvram"><div><b>${esc(name)}</b><span>${esc(why)}</span></div>`;
      o.querySelector('input').onchange = () => {
        [...pick.children].forEach((c) => c.classList.remove('on'));
        o.classList.add('on');
        S.wiz.vram = val;
      };
      pick.appendChild(o);
    });
    // Default to parking for anyone who will actually need it.
    const want = (total && total < 40) ? 0 : 2;
    pick.children[want].querySelector('input').click();
  }

  if (step.id === 'bring') {
    wizSay(`Optional. Skip it and nothing breaks, I already ship fourteen
      ComfyUI graphs and a prompt that works.<br><br>But if you've got your own
      graphs, or one of those <b>enormous</b> SillyTavern presets you're
      emotionally attached to, this is where they come in. I'll read the
      preset and tell you what it actually costs before you commit to it.`,
      'smug');
    body.innerHTML = `
      <div class="wiz-card"><b>ComfyUI workflows</b>
        <p>Exported with <b>Save (API format)</b>, not the normal save. Pick as
        many as you like. They go in alongside the shipped ones, for the
        graphs she drives herself.</p>
        <div class="row-btns"><button class="ghost-btn" id="wizWfPick">choose files…</button></div>
        <div id="wizWfOut"></div></div>
      <div class="wiz-card"><b>A SillyTavern preset</b>
        <p>Chat-completion presets only, the prompt-manager kind. I'll show
        you the damage first.</p>
        <div class="row-btns"><button class="ghost-btn" id="wizStPick">choose a preset…</button></div>
        <div id="wizStOut"></div></div>`;

    const wfOut = body.querySelector('#wizWfOut');
    const stOut = body.querySelector('#wizStOut');
    body.querySelector('#wizWfPick').onclick = () => $('wizWfFile').click();
    body.querySelector('#wizStPick').onclick = () => $('wizStFile').click();

    // API format is exactly: an object whose every value carries class_type
    // and inputs. The UI export is a different shape entirely (nodes/links
    // arrays) and is the file people actually reach for first, so it is
    // worth naming rather than letting it 500 downstream.
    const isApiFormat = (g) => g && typeof g === 'object' && !Array.isArray(g)
      && Object.values(g).length
      && Object.values(g).every((n) => n && typeof n === 'object'
        && typeof n.class_type === 'string' && typeof n.inputs === 'object');

    $('wizWfFile').onchange = async (ev) => {
      const files = [...(ev.target.files || [])];
      ev.target.value = '';
      if (!files.length) return;
      // Names are unique in the table and upsert by name, and ComfyUI's
      // default export filename is the same every time — so without this a
      // multi-file import silently overwrites itself down to one row.
      const taken = new Set(S.workflows.map((w) => w.name.toLowerCase()));
      const lines = [];
      for (const f of files) {
        let graph;
        try { graph = JSON.parse(await f.text()); }
        catch (e) { lines.push(`<li class="bad">${esc(f.name)}, not JSON</li>`); continue; }
        if (!isApiFormat(graph)) {
          lines.push(`<li class="bad">${esc(f.name)}, that's the normal save. `
            + `Use <b>Save (API format)</b>.</li>`);
          continue;
        }
        let name = f.name.replace(/\.json$/i, '').slice(0, 80) || 'workflow';
        let n = 2;
        while (taken.has(name.toLowerCase())) name = `${f.name.replace(/\.json$/i, '')} ${n++}`;
        taken.add(name.toLowerCase());
        const kind = /music/i.test(name) ? 'music'
          : /(tts|voice|asmr|speech)/i.test(name) ? 'tts'
          : /(video|i2v|t2v|wan|ltx|h3)/i.test(name) ? 'video' : 'image';
        const r = await post('/api/workflows', { name, kind, data: { workflow: graph } });
        if (r.error) { lines.push(`<li class="bad">${esc(name)}, ${esc(r.error)}</li>`); continue; }
        S.wiz.wfAdded += 1;
        lines.push(`<li class="good">${esc(name)} · ${esc(kind)} · ${Object.keys(graph).length} nodes</li>`);
      }
      await loadWorkflows();
      wfOut.innerHTML = `<ul class="wiz-list">${lines.join('')}</ul>`;
      if (S.wiz.wfAdded) wizSay(`${S.wiz.wfAdded} in. Fine. <b>Show-off.</b>`, 'proud');
    };

    $('wizStFile').onchange = async (ev) => {
      const f = (ev.target.files || [])[0];
      ev.target.value = '';
      if (!f) return;
      stOut.innerHTML = '<p class="hint">reading it…</p>';
      const b64 = await fileToB64(f);
      const dry = await post('/api/presets/import-st', { b64, dry_run: true });
      if (dry.error) {
        stOut.innerHTML = `<div class="wiz-card warn"><b>Nope</b><p>${esc(dry.error)}</p></div>`;
        wizSay(`That isn't one. Chat-completion presets have a
          <code>prompts</code> list in them; whatever that is, doesn't.`, 'flat');
        return;
      }
      const sm = dry.summary || {};
      S.wiz.stPreset = { b64, name: f.name.replace(/\.json$/i, '').slice(0, 80),
                         regex: sm.regex_scripts || 0 };
      const dropped = Object.values(sm.dropped || {}).reduce((a, b) => a + b, 0);
      wizSay(sm.tokens > 10000
        ? `<b>${(sm.tokens || 0).toLocaleString()} tokens.</b> Every single
           turn. 😂 I'll take it if you want it, but you're paying for that
           in context you could've spent on remembering your own scene.`
        : `${(sm.tokens || 0).toLocaleString()} tokens across ${sm.blocks || 0}
           blocks. That's reasonable, actually. I'm almost disappointed.`,
        sm.tokens > 10000 ? 'laugh' : 'happy');
      stOut.innerHTML = `<div class="wiz-card"><b>${esc(f.name)}</b>
        <p>${sm.text_blocks || 0} text blocks + ${sm.markers || 0} slots ·
        ~${(sm.tokens || 0).toLocaleString()} tokens
        ${dropped ? `· ${dropped} dropped` : ''}</p>
        ${(sm.biggest || []).length ? `<p class="hint">worst offenders: `
          + sm.biggest.slice(0, 3).map((b) => `${esc(b.name)} (${b.tokens})`).join(', ')
          + `</p>` : ''}
        <label>call it <input id="wizStName" value="${esc(S.wiz.stPreset.name)}"></label>
        ${sm.regex_scripts ? `<label class="inline"><input type="checkbox" id="wizStRegex" checked>
          also take its ${sm.regex_scripts} find/replace rules</label>
          <p class="hint">Those are what fold its ledgers and hide its thinking. Without
          them you get the raw markup.</p>` : ''}
        <div class="row-btns"><button class="primary-btn" id="wizStGo">import it</button></div>
        <p class="note" id="wizStNote"></p></div>`;
      stOut.querySelector('#wizStGo').onclick = async () => {
        const name = stOut.querySelector('#wizStName').value.trim() || S.wiz.stPreset.name;
        const note = stOut.querySelector('#wizStNote');
        note.textContent = 'importing…';
        const wantRx = stOut.querySelector('#wizStRegex');
        const r = await post('/api/presets/import-st', { b64: S.wiz.stPreset.b64, name });
        if (r.error) { note.textContent = r.error; note.className = 'note bad'; return; }
        if (wantRx && wantRx.checked) {
          const rx = await post('/api/regex/import', { b64: S.wiz.stPreset.b64 });
          if (!rx.error) await loadRegex();
        }
        // Make it the LIVE preset, so the blocks step that follows targets it
        // rather than quietly configuring a different one.
        await loadPresets();
        if (r.preset && r.preset.id) {
          $('presetSel').value = r.preset.id;
          S.presetId = String(r.preset.id);
          syncSceneFromPreset();
          saveUI();
        }
        S.wiz.stPreset = null;
        note.textContent = `"${r.preset.name}" is live`;
        note.className = 'note ok';
        wizSay(`Loaded, and it's the one she's using now.`, 'fluster');
      };
    };
  }

  if (step.id === 'blocks') {
    const remote = S.wiz.picked && S.wiz.picked.remote;
    const cat = await loadBlockCat();
    const kind = remote ? 'remote' : 'local';
    const ids = new Set(cat.starters[kind] || []);
    const picked = cat.library.filter((b) => ids.has(b.id));
    // Count and cost read off the actual starter set — quoting a number that
    // disagrees with the list right underneath it is exactly the sort of thing
    // this project is supposed to not do.
    const cost = picked.reduce((n, b) => n + TOK(b.content), 0);
    wizSay(remote
      ? `Hosted model, so you get the full kit, the fiction framing, the
         anti-slop patches, all of it. <b>${picked.length} blocks,
         ~${cost.toLocaleString()} tokens.</b><br><br>People trade presets that
         cost <b>twenty-four thousand</b>. I'm not doing that to you.`
      : `Local model, so you barely need any of this. <b>${picked.length}
         blocks, ~${cost.toLocaleString()} tokens.</b><br><br>Those enormous
         presets people trade exist to argue with hosted models that don't want
         to write this. Yours already does.`, remote ? 'smug' : 'proud');
    body.innerHTML = `<div class="wiz-card"><b>${remote ? 'Hosted' : 'Local'} starter set</b>
      <p>Goes into your preset as an ordered list you can see, reorder and
      switch off. Nothing hidden.</p></div>
      <div id="wizBlockList"></div>`;
    body.querySelector('#wizBlockList').innerHTML = picked
      .map((b) => `<div class="wiz-card"><b>${esc(b.name)}</b><p>${esc(b.why)}</p></div>`).join('');
  }

  if (step.id === 'done') {
    wizSay(`Done. Drag a character card onto the roster, or hit <b>☆ forge</b>
      and I'll invent one built around you.<br><br>Try not to embarrass us
      both. …I'll be here. Obviously.`, 'fluster');
    body.innerHTML = `<div class="wiz-card good"><b>Set up</b>
      <p>${esc((S.wiz.picked && S.wiz.picked.model) || 'no model')}
      ${S.wiz.comfy ? '· ComfyUI connected' : '· no ComfyUI (text only)'}
      · VRAM: ${esc(S.wiz.vram)}</p></div>`
      // Only when the probe declined to hand us a number. Silence here is
      // what let an unloaded model's architectural maximum become the
      // history budget without anybody reading it.
      + (S.wiz.ctxNote ? `<div class="wiz-card"><b>One thing</b>
      <p>${esc(S.wiz.ctxNote)} I've left the window at
      ${esc(String(S.wiz.ctx || (S.cfg && S.cfg.defaults
        && S.cfg.defaults.context_tokens) || 8192))} — ⚙ → prompt blocks
      to change it.</p></div>` : '')
      + `<div class="wiz-card"><b>Where things are</b>
      <p><b>studio</b>, one-click shots, and the GPU widget.<br>
      <b>gallery</b>, everything you've made with her.<br>
      <b>prompt blocks</b> in ⚙, the entire prompt, in order, with the token
      bill.<br>
      <b>💬</b> on any character, she texts you.</p></div>`;
  }
}

$('wizNext').onclick = async () => {
  const step = WIZ[S.wiz.step];
  if (step.id === 'brain') {
    if (!S.wiz.picked) { $('wizNote').textContent = 'pick one first'; return; }
    // Route through setModel: the old path set S.llm and then called
    // applyModelSel(), which re-read the topbar select and silently put the
    // pick back to whatever the select happened to hold.
    setModel(MODEL_OPTS.find((o) => o.backend === S.wiz.picked.backend
                                 && o.model === S.wiz.picked.model)
             || { backend: S.wiz.picked.backend, model: S.wiz.picked.model },
             true);
  }
  if (step.id === 'vram') {
    await post('/api/config', {
      vram: { policy: S.wiz.vram,
              driver: S.wiz.vram === 'off' ? 'none' : 'lmstudio' } });
  }
  if (step.id === 'blocks') {
    // Install into whichever preset is live, and detect the real context
    // while we are here — it was defaulting to 8k and quietly trimming.
    await loadPresets();
    let pid = S.presetId || (S.presets[0] && S.presets[0].id);
    if (!pid) {
      // Belt and braces. The server seeds the shipped library into an empty
      // database at startup, so this should never fire — but this step used
      // to silently install nothing when it did, which made setup *look*
      // like it worked and configured nothing at all.
      const made = await post('/api/presets', {
        name: 'CoomKit', data: { mode: 'chat', template: 'gemma4',
                                 thinking: true, thinking_mode: 'normal' } });
      if (made && made.id) { pid = made.id; await loadPresets(); }
    }
    if (pid) {
      const kind = (S.wiz.picked && S.wiz.picked.remote) ? 'remote' : 'local';
      await post(`/api/presets/${pid}/blocks/starter`, { kind });
      const probe = await post('/api/context/probe', {
        backend: S.llm.backend, model: S.llm.model });
      // `probe.context` is 0 when the backend reported only what the weights
      // support and not what is loaded. Writing that here is the worst place
      // it could land: it is the first-run path, so the number is never
      // reviewed, and it both stops history ever being trimmed and reaches
      // `lms load --context-length` on the first send. Leave the preset's
      // default standing and tell them in the summary instead.
      if (probe.ok && probe.context) {
        // Context only — passing a blocks array here would overwrite the
        // starter set that was just installed with a stale copy.
        await post(`/api/presets/${pid}/blocks`, { context: probe.context });
        S.wiz.ctx = probe.context;
      } else if (probe.ok && probe.note) {
        S.wiz.ctxNote = probe.note;
      }
      await loadPresets();
      $('presetSel').value = pid;
      S.presetId = String(pid);
      syncSceneFromPreset();
      saveUI();
    }
  }
  if (S.wiz.step >= WIZ.length - 1) { finishWizard(); return; }
  S.wiz.step += 1;
  await wizStep();
};
$('wizBack2').onclick = async () => {
  if (S.wiz.step > 0) { S.wiz.step -= 1; await wizStep(); }
};
$('wizSkip').onclick = () => finishWizard();
$('openWizard').onclick = () => openWizard();

function finishWizard() {
  $('wizBack').classList.remove('on');
  markSetupDone();
  loadStudio();
  // Straight into the tour the first time, so setup ends by showing you the
  // thing you just set up rather than dropping you on an empty screen.
  if (!localStorage.getItem('coomkit.tour.v1')) setTimeout(startTour, 400);
}

// ── walkthrough ──────────────────────────────────────────────────
// Points at the real controls rather than describing them. Every step has a
// `before` that opens whatever pane it is about, so the thing being pointed
// at is actually on screen when she points at it.

const TOUR = [
  { el: '#roster', mood: 'happy', title: 'Your roster',
    after: () => $('forgeBack').classList.remove('on'),
    text: `Everyone you've got. Search it, ★ the ones you actually use.
      Click to talk, <b>💬</b> to make her text you instead.` },
  { el: '#importBtn', mood: 'smug', title: 'Bring your cards',
    text: `<b>+ card</b> takes a SillyTavern PNG or JSON, v1, v2, v3. Export
      re-embeds it, so the card still works in ST afterwards. Nothing you
      already own is stranded here.` },

  // ── the multimodal upgrade: the actual reason this exists ──
  { el: '#forgeTabs', inModal: true, mood: 'proud', title: 'The Forge',
    before: () => openForge('cards'),
    text: `Everything about <i>who she is</i> lives here, her card, inventing
      one from nothing, the scene she's in, and you. Settings is for how the
      machine runs; this is the cast.` },
  { el: '#cvAppearance', inModal: true, mood: 'smug', title: 'Make her multimodal, 1: a face',
    when: () => S.chars.length,
    before: () => { openForge('cards'); if (S.chars[0]) openCardEditor(S.chars[0].id); },
    text: `Here's the part your old card doesn't have. Describe how she looks
      in <b>plain English</b>, I translate it into whatever your image model
      speaks, booru tags or prose. Pin a seed and it's the same woman in every
      picture, forever. Drop in a reference photo and it's <i>really</i> her.` },
  { el: '#cvVoicePreset', inModal: true, mood: 'happy', title: '2: a voice',
    when: () => S.chars.length,
    before: () => { openForge('cards'); if (S.chars[0]) openCardEditor(S.chars[0].id); },
    text: `Pick a shipped voice or upload three seconds of anyone talking and
      she'll be cloned from it. <b>Listen before you commit</b>, a clone
      sounds exactly like whatever you feed it, and that cuts both ways.` },
  { el: '#loraRows', inModal: true, mood: 'flat', title: '3: her LoRAs',
    when: () => S.chars.length,
    before: () => { openForge('cards'); if (S.chars[0]) openCardEditor(S.chars[0].id); },
    text: `Read live from <i>your</i> ComfyUI's models/loras. Stack them in
      order. That's a boring text card upgraded into something that can look
      at you and talk back, which is the whole point of this program.` },
  { el: '#forgeTabs .modal-tab[data-ftab="character"]', inModal: true, mood: 'proud',
    title: 'Or skip all that',
    before: () => openForge('character'),
    text: `Nine empty textareas is homework. Describe what you're in the mood
      for, or nothing at all, and I'll invent her whole: card, looks, voice,
      a pinned seed and her portrait, in one go.` },
  { el: '#forgeTabs .modal-tab[data-ftab="scene"]', inModal: true, mood: 'smug',
    title: 'And where she is',
    before: () => openForge('scene'),
    text: `One <code>first_mes</code> gets old fast. Argue with me about a
      fresh situation instead, and the chat starts already in motion —
      remembering what you two have done, if you let it.` },

  { el: '.rail-tab[data-rail="studio"]', mood: 'smug', title: 'The studio',
    before: () => document.querySelector('.rail-tab[data-rail="studio"]').click(),
    text: `Pick a shot, she drafts the prompt, you approve it, your GPU does
      the rest. Selfie, modelling photo, the filthy ones, ASMR, an entire
      song. She can ask for these herself mid-scene too.` },
  { el: '#recipeGrid', mood: 'proud', title: 'Ten shots',
    before: () => document.querySelector('.rail-tab[data-rail="studio"]').click(),
    text: `Each one knows what it's asking for, a selfie is deliberately badly
      lit and badly framed, because that's what makes it read as real. And if
      none of them fit, <b>🪄 Describe it</b> takes plain English and writes it
      in your model's own dialect. You always see the prompt before anything
      runs.` },
  { el: '#vramBadge', mood: 'laugh', title: 'The GPU',
    before: () => document.querySelector('.rail-tab[data-rail="studio"]').click(),
    text: `How much room you've got, and whether I have to shove your chat
      model off the card to fit a video model. Which, on most of your
      hardware, I do. 😏` },
  { el: '.rail-tab[data-rail="gallery"]', mood: 'happy', title: 'Her gallery',
    before: () => document.querySelector('.rail-tab[data-rail="gallery"]').click(),
    text: `Everything you've ever made with her, across every chat. Not per
      scene, that'd just be a folder. This is the two of you.` },
  { el: '.rail-tab[data-rail="memory"]', mood: 'smug', title: 'What she remembers',
    before: () => document.querySelector('.rail-tab[data-rail="memory"]').click(),
    text: `Three scopes: about <b>you</b>, about <b>you and her</b>, and about
      <b>this scene</b>. Edit any of it. <b>♥ remember this</b> makes her read
      the whole evening and keep what mattered.` },
  { el: '#stream', mood: 'smug', title: 'This is a demo',
    text: `Obviously. Nobody types like that. …Some of you type like that.
      <br><br>Real replies stream in, you can swipe for alternatives, edit
      anything she says, or regenerate it.` },
  { el: '#btnInspect', mood: 'proud', title: 'The whole prompt',
    text: `Every word going to the model, before it goes. No harness should
      hide this from you and I'm not going to.` },
  { el: '#openSettings', mood: 'flat', title: 'Prompt blocks',
    text: `⚙ → <b>prompt blocks</b>. Your entire prompt as an ordered list you
      can reorder and switch off, with the token bill on top. Import your old
      SillyTavern preset and watch me tell you what it costs.` },
  { el: '#chatList', mood: 'happy', title: 'Her chats',
    text: `Every adventure with her, side by side. Start another whenever you
      like, the old one stays exactly where it was. Nothing gets thrown out
      unless you say so, and I'll ask twice.` },
  { el: '#openWizard', mood: 'fluster', title: "That's everything",
    text: `Setup's here if you want it again. Now go on. …I'll be around.
      Obviously.` },
];

// A throwaway conversation so the tour has something to point at on a fresh
// install. Never stored, never sent — it is wallpaper. The lines are the
// oldest jokes on the board because a demo that takes itself seriously is
// worse than no demo.
const TOUR_DEMO = [
  ['user', 'ahh ahh mistress'],
  ['assistant', "*She doesn't look up.* \"...That's it? That's your opener?\""],
  ['user', '*unzips pants*'],
  ['assistant', '*She sighs, the way you sigh at a dog that has brought you a rock.*\n\n"Incredible. Genuinely. Three seconds."'],
  ['user', 'is this thing on'],
  ['assistant', '"Unfortunately." *She finally looks over, chin propped on one hand.* "Go on then. Impress me. I\'ll wait."'],
];

function tourDemoOn() {
  if (S.chat) return false;          // never paint over a real conversation
  const box = $('stream');
  S.tourDemoWas = {
    stream: box.hidden, empty: $('emptyState').hidden,
    composer: $('composer').hidden, head: $('chatHead').hidden,
    who: $('chatWho').textContent, sub: $('chatSub').textContent,
    html: box.innerHTML,
  };
  $('emptyState').hidden = true;
  box.hidden = false;
  $('composer').hidden = false;
  $('chatHead').hidden = false;
  $('chatWho').textContent = 'Gemma-chan';
  $('chatSub').textContent = 'demo, not a real chat';
  box.innerHTML = '';
  for (const [role, text] of TOUR_DEMO) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    div.innerHTML = `<div class="msg-ava">${role === 'user' ? '☻' : '♡'}</div>`
      + `<div class="msg-body"><div class="bubble">${fmt(text)}</div></div>`;
    box.appendChild(div);
  }
  box.scrollTop = box.scrollHeight;
  return true;
}

function tourDemoOff() {
  const w = S.tourDemoWas;
  if (!w) return;
  $('stream').innerHTML = w.html;
  $('stream').hidden = w.stream;
  $('emptyState').hidden = w.empty;
  $('composer').hidden = w.composer;
  $('chatHead').hidden = w.head;
  $('chatWho').textContent = w.who;
  $('chatSub').textContent = w.sub;
  S.tourDemoWas = null;
}

S.tour = 0;
S.tourSteps = [];

// A target inside a collapsed pane measures 0x0, and the ring then frames
// nothing while she talks about it. Steps whose subject isn't on screen are
// dropped rather than shown pointing at the void — with no chat open, for
// instance, there is no composer to point the inspector out on.
function tourVisible(sel) {
  const el = document.querySelector(sel);
  if (!el) return false;
  const r = el.getBoundingClientRect();
  return r.width > 4 && r.height > 4;
}

async function tourAt(i) {
  const step = S.tourSteps[i];
  if (!step) { endTour(); return; }
  const prev = S.tourSteps[S.tourLast];
  if (prev && prev.after && prev !== step) {
    try { prev.after(); } catch { /* nothing to close */ }
  }
  S.tourLast = i;
  if (step.before) {
    try { step.before(); } catch { /* pane may not exist */ }
    // Let the pane paint before measuring it.
    await new Promise((r) => setTimeout(r, 160));
  }
  // A modal backdrop is already 72% black with a blur on it. Laying the
  // tour's own 72% dim over the top lands at ~92% and two stacked blurs,
  // which is what "wayyy too dark" was.
  // Close a modal the tour opened when the next step is not inside one.
  // An `after` hook only fires if you walk in order; jumping (or resizing,
  // which re-renders the current step) would otherwise leave the Forge
  // sitting over the thing being pointed at.
  if (!step.inModal) {
    $('forgeBack').classList.remove('on');
    $('settingsBack').classList.remove('on');
  }
  $('tour').classList.toggle('tour-over-modal',
                             !!document.querySelector('.modal-back.on'));
  let target = document.querySelector(step.el);
  // A `before` is a PROMISE that the target becomes visible, not a guarantee:
  // the card-editor steps cannot open an editor on an install with no cards,
  // and a hidden element measures 0x0 — which pins the bubble to the corner
  // pointing at nothing. Skip forward instead.
  if (step.el && (!target || !target.getClientRects().length)) {
    if (S.tourDir < 0 ? i > 0 : i < S.tourSteps.length - 1) {
      return tourAt(i + (S.tourDir < 0 ? -1 : 1));
    }
    endTour();
    return;
  }
  const ring = $('tourRing');
  const pop = $('tourPop');
  $('tourGemma').hidden = false;
  $('tourGemma').src = `/img/gemma/${step.mood}.png`;
  $('tourTitle').textContent = step.title;
  $('tourText').innerHTML = step.text;
  $('tourCount').textContent = `${i + 1} / ${S.tourSteps.length}`;
  $('tourNext').textContent = i === S.tourSteps.length - 1 ? 'done ★' : 'go on';

  if (!target) { ring.style.width = '0'; ring.style.height = '0'; }
  else {
    // NOT smooth: a smooth scroll inside an overflow-y:auto container has
    // not resolved by the time getBoundingClientRect runs, so the ring was
    // measured against the pre-scroll position — measured at 1852px on a
    // 720px viewport for a target inside an open modal. Instant lands it.
    target.scrollIntoView({ block: 'center', behavior: 'instant' });
    const r = target.getBoundingClientRect();
    const pad = 6;
    ring.style.top = (r.top - pad) + 'px';
    ring.style.left = (r.left - pad) + 'px';
    ring.style.width = (r.width + pad * 2) + 'px';
    ring.style.height = (r.height + pad * 2) + 'px';
  }
  // Place the bubble on whichever side has room, so it never covers the thing
  // it is pointing at.
  const r = target ? target.getBoundingClientRect() : { top: 90, bottom: 90, left: 90, right: 90, width: 0 };
  const w = Math.min(430, window.innerWidth * 0.92);
  let left = Math.min(Math.max(12, r.left + r.width / 2 - w / 2), window.innerWidth - w - 12);
  let top = r.bottom + 16;
  if (top + 190 > window.innerHeight) top = Math.max(12, r.top - 206);
  pop.style.left = left + 'px';
  pop.style.top = top + 'px';
}

async function startTour() {
  tourDemoOn();
  // Open every pane the tour visits once, so their contents exist and can be
  // measured, then put the rail back where it was.
  const rail = document.querySelector('.rail-tab.on');
  for (const t of ['studio', 'gallery', 'memory']) {
    const tab = document.querySelector(`.rail-tab[data-rail="${t}"]`);
    if (tab) tab.click();
  }
  await new Promise((r) => setTimeout(r, 250));
  S.tourLast = -1;
  S.tourDir = 1;
  S.tourSteps = TOUR.filter(
    (s) => (!s.when || s.when()) && (!s.el || tourVisible(s.el) || s.before));
  if (rail) rail.click();
  S.tour = 0;
  $('tour').hidden = false;
  await tourAt(0);
}
function endTour() {
  $('tour').hidden = true;
  tourDemoOff();
  // The tour opens modals to point inside them; leaving one up afterwards
  // reads as the app being stuck.
  $('forgeBack').classList.remove('on');
  $('settingsBack').classList.remove('on');
  S.tourLast = -1;
  localStorage.setItem('coomkit.tour.v1', '1');
}
$('tourNext').onclick = async () => {
  S.tourDir = 1;
  S.tour += 1;
  if (S.tour >= S.tourSteps.length) { endTour(); toast('go be weird'); return; }
  await tourAt(S.tour);
};
$('tourQuit').onclick = endTour;
// ── themes ───────────────────────────────────────────────────────────────
// Tokens only: every theme redefines the same names and no rule below :root
// knows which is on. Stored under its own key rather than in
// coomkit.session.v1 because the <head> script has to read it before any of
// this file has parsed, and parsing the whole session blob there would be
// slower and would couple boot to the session schema.
const THEMES = [
  ['rose', 'Rose / violet'],
  ['hunter', 'Hunter green'],
];

function applyTheme(name) {
  const known = THEMES.some((t) => t[0] === name) ? name : 'rose';
  if (known === 'rose') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', known);
  try { localStorage.setItem('coomkit.theme.v1', known); } catch { /* private mode */ }
  S.theme = known;
  const b = $('toggleTheme');
  const next = THEMES[(THEMES.findIndex((t) => t[0] === known) + 1) % THEMES.length];
  if (b) b.title = `Theme: ${THEMES.find((t) => t[0] === known)[1]} — click for ${next[1]}`;
}

$('toggleTheme').onclick = () => {
  const i = THEMES.findIndex((t) => t[0] === (S.theme || 'rose'));
  applyTheme(THEMES[(i + 1) % THEMES.length][0]);
};

$('openTour').onclick = startTour;
window.addEventListener('resize', () => { if (!$('tour').hidden) tourAt(S.tour); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('tour').hidden) endTour();
});

// ── she texts first ──────────────────────────────────────────────
// Driven from the browser rather than a server timer: the page already knows
// which backend and model to use, and it means she only messages you while
// you actually have CoomKit open — which is the honest behaviour anyway.
//
// The restraint matters more than the mechanism. A gap she has to clear, a
// daily cap, nothing while she is mid-reply, and the model itself is allowed
// to decide it has nothing worth saying.

S.nudge = { timer: null, checking: false };
const NUDGE_TICK = 60000;

function syncNudge() {
  const t = (S.phone.texting || {});
  const b = $('phoneNudge');
  b.style.opacity = t.enabled ? '1' : '.4';
  b.title = t.enabled
    ? `she'll text you unprompted (no sooner than every ${t.gap_minutes || 45} min), click to stop`
    : "she never messages first, click to let her";
}

$('phoneNudge').onclick = async () => {
  if (!S.phone.chat) return;
  const on = !((S.phone.texting || {}).enabled);
  const r = await post(`/api/chats/${S.phone.chat.id}/texting`, {
    enabled: on, gap_minutes: (S.phone.texting || {}).gap_minutes || 45,
    daily_cap: (S.phone.texting || {}).daily_cap || 6,
  });
  if (r.error) { toast('failed: ' + r.error); return; }
  S.phone.texting = r.texting;
  syncNudge();
  toast(on ? "she'll text you when she feels like it" : 'she waits for you now');
};

async function nudgeCheck() {
  const t = S.phone.texting || {};
  if (!t.enabled || !S.phone.chat || S.phone.busy || S.nudge.checking) return;
  if (!LLM_READY()) return;
  // A blank thread makes lastAt 0, so `since` is fifty-six years and she
  // texts first within sixty seconds of the thread being created — before
  // the user has decided who opens.
  if (!S.phone.seen) return;
  const gap = (t.gap_minutes || 45) * 60;
  const since = (Date.now() / 1000) - (S.phone.lastAt || 0);
  if (since < gap) return;
  if ((S.phone.unpromptedToday || 0) >= (t.daily_cap || 6)) return;

  S.nudge.checking = true;
  try {
    const r = await post('/api/chats/text-first', phoneBody(requestBody()));
    // "sent: false" is a real answer — she looked and had nothing to say. Push
    // the clock forward anyway so we don't ask again in sixty seconds.
    S.phone.lastAt = Date.now() / 1000;
    if (r.sent) {
      S.phone.unpromptedToday = (S.phone.unpromptedToday || 0) + 1;
      if ($('phone').hidden) {
        $('phoneTabName').textContent = S.phone.chat.name;
        $('phoneTab').hidden = false;
        $('phoneTabDot').hidden = false;
        toast(`${S.phone.chat.name} texted you`);
      } else {
        phoneBubble('assistant', stripBlocks(r.text));
      }
    }
  } catch { /* offline or backend gone; try again next tick */ }
  S.nudge.checking = false;
}

function startNudging() {
  if (S.nudge.timer) clearInterval(S.nudge.timer);
  S.nudge.timer = setInterval(nudgeCheck, NUDGE_TICK);
}

boot();

// ── the image export: turn a log into something postable ─────────────────
//
// Screenshots lose to this on four counts: they carry your browser chrome and
// your tab bar, they stop at the viewport, they clip .think-body at its 260px
// scroll clamp and code at the right edge, and they have your persona's real
// name in them.
//
// The pipeline is: build a clean offscreen subtree using the app's OWN classes
// and the app's OWN fmt(), serialise it into an SVG <foreignObject> with
// /style.css inlined, rasterise that through an Image, then composite the
// photos on afterwards. Reusing fmt() is the whole point — a second renderer
// for message content would drift from the bubbles on screen and nothing would
// ever detect it.
//
// Measured in Firefox 140 ESR and Chromium 151, through this exact path:
//   · without the .ck-export animation override the output is ONE COLOUR
//   · a same-origin <img> inside a foreignObject does not paint at all —
//     byte-identical PNG with and without the src, in both engines
//   · a data: URL does not taint the canvas; a blob: URL taints it in Chrome
//   · Firefox refuses a canvas past 32767 per side; Chromium goes further
//   · toBlob ignores the quality argument for PNG, and both engines will
//     happily encode image/webp, which 4chan rejects

const CK_MAX_TILE_H = 9000;      // 4chan rejects anything over 10000 per side
const CK_ENGINE_MAX = 32767;     // Firefox's hard per-side ceiling
const CK_CAP_BYTES  = 3900000;   // /g/ stops at "4096 KB" — leave margin
const CK_FOOT_H     = 30;
const CK_THUMB_H    = 125;       // 4chan's reply thumbnail, longest side

class CkExportError extends Error {
  constructor(code, detail) { super(code + (detail ? ': ' + detail : '')); this.code = code; }
}

// C0/C1 controls make the SVG invalid XML and the ONLY symptom is img.onerror
// with no message on it. A lone surrogate (a token-truncated emoji) does the
// same. \t \n \r are deliberately spared. Measured: one \x1B anywhere in a
// bubble takes the whole export to a blank canvas.
const CK_CTRL = /[\x00-\x08\x0B\x0C\x0E-\x1F\x7F-\x9F]/g;
function xmlSafe(s) {
  return String(s == null ? '' : s)
    .replace(CK_CTRL, '')
    .replace(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/g, '�')
    .replace(/(^|[^\uD800-\uDBFF])([\uDC00-\uDFFF])/g, '$1�');
}

let CK_CSS = null;
async function ckCss() {
  if (CK_CSS === null) CK_CSS = await (await fetch('/style.css')).text();
  return CK_CSS;
}
// Colours come from the stylesheet, never from a literal here — two sources of
// truth for the palette is how an export slowly stops matching the app.
const ckTok = (n) => getComputedStyle(document.documentElement)
  .getPropertyValue(n).trim();

// ── raster: DOM subtree -> canvas-ready Image ────────────────────────────
async function ckRaster(el, W, H) {
  const css = await ckCss();
  const xhtml = new XMLSerializer().serializeToString(el);
  // style.css contains & and < inside its comments. Unescaped, the SVG is
  // invalid XML and you get img.onerror with nothing to report.
  const safeCss = css.replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + W + '" height="' + H + '">'
    + '<foreignObject width="100%" height="100%">'
    + '<div xmlns="http://www.w3.org/1999/xhtml"><style>' + safeCss + '</style>'
    + xhtml + '</div></foreignObject></svg>';
  const bytes = new TextEncoder().encode(svg);
  // fromCharCode(...bytes) blows the stack on a 60KB SVG
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000)
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  const img = new Image();
  // NEVER blob: — Chrome taints the canvas when a foreignObject SVG comes from
  // one, and NEVER crossOrigin — same-origin with no ACAO fails the load.
  const ok = await new Promise((r) => {
    img.onload = () => r(true);
    img.onerror = () => r(false);          // carries no detail; do not try/catch
    img.src = 'data:image/svg+xml;base64,' + btoa(bin);
  });
  if (!ok) throw new CkExportError('svg-decode', bytes.length + ' bytes');
  return img;
}

// A stage that is positioned and clipped, and a host that is NOT. Only the
// host is serialised: any position/left on the serialised element carries the
// offset into the SVG viewport and the content lands outside it.
// Every palette token, so the export can carry the ACTIVE theme.
const CK_TOKENS = ['accent', 'accent-lit', 'accent-deep', 'second', 'second-lit',
  'second-deep', 'gold', 'gold-lit', 'ink', 'bg', 'surface-0', 'surface',
  'surface-2', 'surface-3', 'line', 'line-lit', 'text', 'text-dim', 'text-mute',
  'on-accent', 'ok', 'bad', 'bad-lit', 'bad-line',
  'accent-rgb', 'accent-lit-rgb', 'accent-deep-rgb', 'second-rgb',
  'second-deep-rgb', 'gold-rgb', 'gold-lit-rgb', 'ok-rgb', 'ink-rgb', 'bg-rgb',
  'surface-rgb'];

function ckStage(W, cls) {
  const stage = document.createElement('div');
  stage.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;'
    + 'overflow:hidden;opacity:0;pointer-events:none;z-index:-1';
  document.body.appendChild(stage);
  const host = document.createElement('div');
  host.className = cls;
  host.style.width = W + 'px';   // width and nothing else
  // THE THEME HAS TO TRAVEL IN THE MARKUP. ckRaster builds its own document —
  // svg > foreignObject > div > [style, host] — so there is no <html> and no
  // <body> in it, and `:root[data-theme=…]` cannot match: `:root` resolves to
  // the <svg>, which never carries the attribute. Today's palette survives
  // only because bare `:root{--bg:…}` matches that <svg> and custom properties
  // inherit through foreignObject. Qualify the selector and the inheritance
  // stops, silently — you get a themed canvas (ckTok reads the LIVE page) with
  // a default-palette title card and footer, and ckCanary cannot see it
  // because it only counts distinct colours. Measured: with the rule in
  // style.css, an attribute on the serialised element works and one on <html>
  // does not. So snapshot the resolved values inline instead; they win over
  // the :root defaults the <svg> still supplies, and both consumers then read
  // the same getComputedStyle call and cannot diverge.
  const cs = getComputedStyle(document.documentElement);
  for (const t of CK_TOKENS) {
    const v = cs.getPropertyValue('--' + t).trim();
    if (v) host.style.setProperty('--' + t, v);
  }
  stage.appendChild(host);
  return { stage, host };
}

// ── the canary ───────────────────────────────────────────────────────────
// This feature is a second consumer of style.css and it fails silently and
// totally when the two diverge. Rather than hand someone a black rectangle
// six months from now, render a known fixture and look at the pixels.
let CK_CANARY = null;
async function ckCanary() {
  if (CK_CANARY !== null) return CK_CANARY;
  const { stage, host } = ckStage(240, 'ck-export');
  try {
    const stream = document.createElement('div');
    stream.className = 'stream';
    const msg = document.createElement('div');
    msg.className = 'msg assistant';
    const body = document.createElement('div');
    body.className = 'msg-body';
    const b = document.createElement('div');
    b.className = 'bubble';
    b.textContent = 'canary';
    body.appendChild(b); msg.appendChild(body); stream.appendChild(msg);
    host.appendChild(stream);
    const H = Math.ceil(host.getBoundingClientRect().height) || 80;
    const img = await ckRaster(host, 240, H);
    const c = document.createElement('canvas');
    c.width = 240; c.height = H;
    const ctx = c.getContext('2d', { alpha: false });
    ctx.fillStyle = ckTok('--bg');
    ctx.fillRect(0, 0, 240, H);
    ctx.drawImage(img, 0, 0);
    const px = ctx.getImageData(0, 0, 240, H).data;
    const seen = new Set();
    for (let i = 0; i < px.length; i += 4)
      seen.add((px[i] << 16) | (px[i + 1] << 8) | px[i + 2]);
    CK_CANARY = seen.size >= 3;
    if (!CK_CANARY) console.error('ckCanary: ' + seen.size + ' distinct colours');
  } catch (e) {
    console.error('ckCanary', e);
    CK_CANARY = false;
  } finally { stage.remove(); }
  return CK_CANARY;
}

// ── media: inlined as data: URIs ─────────────────────────────────────────
// An <img> pointing at a same-origin URL renders EMPTY inside a foreignObject
// — measured in both engines, byte-identical output with and without the src.
// A data: URI paints (Chromium 8121 distinct colours, Firefox 6962) and does
// not taint the canvas.
//
// The alternative was compositing the bitmaps onto the canvas afterwards using
// getBoundingClientRect coordinates. That was built first and it was WRONG:
// the live page and the SVG viewport lay text out fractionally differently, so
// the error accumulates down the sheet — measured 0px at the first bubble and
// 33px by the fifth, which put every photo a visible notch below its own
// frame. Inlining hands the layout back to the browser, which is the only
// thing that knows where it actually put the box.
function ckLoadImage(url) {
  return new Promise((res) => {
    const im = new Image();               // no crossOrigin: same origin, no ACAO
    im.onload = () => res(im);
    im.onerror = () => res(null);
    im.src = url;
  });
}
// Nothing in this repo stores a video thumbnail, so the only honest still is a
// frame pulled off the clip itself.
function ckLoadVideo(url) {
  return new Promise((res) => {
    const v = document.createElement('video');
    v.muted = true; v.preload = 'metadata'; v.src = url;
    const bail = setTimeout(() => res(null), 4000);
    v.onerror = () => { clearTimeout(bail); res(null); };
    v.onloadeddata = () => {
      v.onseeked = () => { clearTimeout(bail); res(v); };
      try { v.currentTime = Math.min(0.1, (v.duration || 1) / 4); }
      catch (e) { clearTimeout(bail); res(v); }
    };
  });
}
function ckLoadDuration(url) {
  return new Promise((res) => {
    const a = document.createElement('audio');
    a.preload = 'metadata'; a.src = url;
    const bail = setTimeout(() => res(0), 4000);
    a.onloadedmetadata = () => { clearTimeout(bail); res(a.duration || 0); };
    a.onerror = () => { clearTimeout(bail); res(0); };
  });
}
const ckClock = (s) => !s ? '' : Math.floor(s / 60) + ':'
  + String(Math.round(s % 60)).padStart(2, '0');

// Re-encode at the size it will be shown at. A full-size render inlined raw
// would be ~2 MB of base64 in the SVG for one photo; at 220px it is ~12 KB.
// JPEG over PNG for the same reason, on an opaque background so a PNG with
// alpha does not come back with black holes in it.
function ckInline(el, long) {
  const nw = el.naturalWidth || el.videoWidth || 0;
  const nh = el.naturalHeight || el.videoHeight || 0;
  // Firefox will not upscale a 1x1 source into a 220px box — it paints
  // nothing, where Chromium stretches the single pixel. Anything this small
  // is a broken or placeholder asset rather than a picture, so it takes the
  // ♪/▶ card fallback and says what it was instead of leaving an empty frame.
  if (nw < 16 || nh < 16) return null;
  const sc = Math.min(1, long / Math.max(nw, nh));
  const w = Math.max(1, Math.round(nw * sc));
  const h = Math.max(1, Math.round(nh * sc));
  const c = document.createElement('canvas');
  c.width = w; c.height = h;
  const ctx = c.getContext('2d', { alpha: false });
  ctx.fillStyle = ckTok('--bg');
  ctx.fillRect(0, 0, w, h);
  try { ctx.drawImage(el, 0, 0, w, h); } catch (e) { return null; }
  return { src: c.toDataURL('image/jpeg', 0.82), w: nw, h: nh };
}

// ── build the offscreen subtree ──────────────────────────────────────────
function ckAvatar(cls, file, glyph, media) {
  const inl = file && media.get('ava:/api/avatars/' + file);
  if (inl && inl.src) {
    const im = document.createElement('img');
    im.className = cls;
    im.src = inl.src;            // data: URI — a plain URL would not paint
    return im;
  }
  const d = document.createElement('div');
  d.className = cls; d.textContent = glyph;
  return d;
}

async function ckBuildHost(d, picks, opts, media) {
  const W = opts.width;
  const { stage, host } = ckStage(W, 'ck-export');
  const her = d.character || 'her';

  if (opts.header) {
    const head = document.createElement('div');
    head.className = 'ck-head';
    head.appendChild(ckAvatar('ck-head-ava', d.avatar, '♡', media));
    const who = document.createElement('div');
    const b = document.createElement('b');
    b.textContent = xmlSafe(her);
    const sm = document.createElement('small');
    sm.textContent = xmlSafe(d.title || '');
    who.appendChild(b); who.appendChild(sm);
    head.appendChild(who);
    const right = document.createElement('div');
    right.className = 'ck-head-right';
    right.textContent = opts.stamp ? ckProvenance(d, picks) : '';
    head.appendChild(right);
    host.appendChild(head);
  }

  const stream = document.createElement('div');
  stream.className = 'stream' + (d.chat && d.chat.mode === 'sms' ? ' sms' : '');
  host.appendChild(stream);

  let prevIdx = -1;
  for (const p of picks) {
    const m = p.m;
    if (prevIdx >= 0 && p.i !== prevIdx + 1) {
      // A cap with silent cuts is how a poster gets accused of hiding the
      // swipe where she refused. Make the cut visible in the picture.
      const gap = document.createElement('div');
      gap.className = 'ck-gap';
      gap.textContent = '⋯';
      stream.appendChild(gap);
    }
    prevIdx = p.i;

    const div = document.createElement('div');
    div.className = 'msg ' + m.role;
    const isHer = m.role === 'assistant';
    // Whoever actually wrote it, exactly as buildMsg resolves it on screen.
    // This used to be the lead's name and the lead's face on EVERY assistant
    // bubble, so an exported two-hander labelled both women the same — in the
    // one place the log gets posted in public. The routing chip is
    // deliberately NOT carried across: who spoke belongs in the picture, why
    // she was picked is a thing about CoomKit.
    const spk = isHer && m.speaker
      ? (d.cast || []).find((c) => String(c.character_id) === String(m.speaker))
      : null;
    div.appendChild(ckAvatar('msg-ava', isHer ? (spk ? spk.avatar : d.avatar) : '',
                             isHer ? '♡' : '☻', media));
    const body = document.createElement('div');
    body.className = 'msg-body';

    const whoEl = document.createElement('div');
    whoEl.className = 'msg-who';
    let whoText = isHer ? (spk ? spk.name : her) : 'you';
    if (opts.times) {
      // swipes:0 means ONE take, exactly as buildMsg computes it
      const tot = Math.max(1, m.swipes || 0);
      const cur = (m.swipes ? (m.swipe_index ?? 0) : 0) + 1;
      if (tot > 1) whoText += ' · take ' + cur + '/' + tot;
    }
    whoEl.textContent = xmlSafe(whoText);
    body.appendChild(whoEl);

    // Her reasoning is opt-in and NEVER emitted closed: a closed <details> in
    // a still image is a lie about what is in the file.
    if (opts.think && m.think) {
      const det = document.createElement('details');
      det.className = 'think'; det.open = true;
      const sum = document.createElement('summary');
      sum.textContent = 'her thoughts';
      const tb = document.createElement('div');
      tb.className = 'think-body';
      tb.textContent = xmlSafe(m.think);
      det.appendChild(sum); det.appendChild(tb);
      body.appendChild(det);
    }

    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    const safe = xmlSafe(m.content);
    // ONE renderer. fmt() is what the bubbles on screen use; the export must
    // never grow its own. stripBlocks defensively, because _message_edit
    // stores whatever the user typed and pre-strip rows exist.
    bubble.innerHTML = m.html ? fmtHtml(safe) : fmt(stripBlocks(safe));
    // A display-scope rule that folds reasoning into <details> was installed
    // to HIDE it. There is no disclosure triangle in a PNG, so "closed" is not
    // a state that exists — keep the summary, drop the body.
    for (const det of bubble.querySelectorAll('details')) {
      if (opts.think) { det.open = true; continue; }
      const sum = det.querySelector(':scope > summary');
      det.textContent = '';
      if (sum) det.appendChild(sum);
      det.classList.add('ck-fold');
    }
    body.appendChild(bubble);

    if (opts.times && m.created) {
      const ts = document.createElement('span');
      ts.className = 'ck-ts';
      ts.textContent = new Date(m.created * 1000)
        .toISOString().replace('T', ' ').slice(0, 16);
      body.appendChild(ts);
    }

    const shots = opts.pics === 'off' ? [] : (m.assets || []);
    if (shots.length) {
      const slots = document.createElement('div');
      slots.className = 'ck-slots';
      const long = opts.pics === 'big' ? Math.min(560, W - 120) : 220;
      for (const a of shots) {
        const mm = media.get(a.id);
        if (mm && mm.src) {
          const sc = long / Math.max(mm.w, mm.h);
          const wrap = document.createElement('div');
          wrap.className = 'ck-slot';
          wrap.style.width = Math.round(mm.w * sc) + 'px';
          wrap.style.height = Math.round(mm.h * sc) + 'px';
          const im = document.createElement('img');
          im.src = mm.src;
          wrap.appendChild(im);
          if (mm.kind === 'video') {
            // So a still is never passed off as a photo.
            const play = document.createElement('span');
            play.className = 'ck-play';
            play.textContent = '▶ ' + (ckClock(mm.dur) || 'video');
            wrap.appendChild(play);
          }
          slots.appendChild(wrap);
        } else {
          // audio, or a container the browser will not decode (.mkv, .opus)
          const card = document.createElement('div');
          card.className = 'ck-card';
          const t = document.createElement('b');
          const dur = mm && mm.dur ? ' · ' + ckClock(mm.dur) : '';
          const mark = a.kind === 'audio' ? '♪ ' : (a.kind === 'video' ? '▶ ' : '▣ ');
          t.textContent = mark + (a.recipe || a.kind) + dur;
          const s = document.createElement('span');
          s.textContent = (a.prompt || '').slice(0, 90);
          card.appendChild(t); card.appendChild(s);
          slots.appendChild(card);
        }
      }
      if (slots.children.length) body.appendChild(slots);
    }

    div.appendChild(body);
    stream.appendChild(div);
  }
  return { stage, host, stream };
}

// ── provenance ───────────────────────────────────────────────────────────
// On /lmg/ "what model" is the first reply to every log. Prefer what actually
// wrote the messages; fall back to the current settings and say so honestly.
function ckSamplerLine(s) {
  if (!s) return '';
  const bits = [];
  if (s.temperature !== undefined) bits.push('t' + (+s.temperature).toFixed(2));
  if (s.top_p !== undefined) bits.push('p' + (+s.top_p).toFixed(2));
  if (s.min_p) bits.push('mp' + (+s.min_p).toFixed(2));
  if (s.top_k) bits.push('k' + s.top_k);
  if (s.repetition_penalty) bits.push('rep' + (+s.repetition_penalty).toFixed(2));
  return bits.join(' ');
}
// A self-hosted endpoint in a posted screencap is a doxx, and a key would be
// worse. Keys never reach the browser, but scrub anyway — it costs nothing.
const ckScrub = (t) => String(t || '')
  .replace(/sk-[A-Za-z0-9_-]{10,}/g, '…')
  .replace(/https?:\/\/\S+/g, '…');

function ckProvenance(d, picks) {
  const counts = new Map();
  let unstamped = 0, any = null;
  for (const p of picks) {
    if (p.m.role !== 'assistant') continue;
    const g = p.m.gen;
    if (!g || !g.model) { unstamped++; continue; }
    counts.set(g.model, (counts.get(g.model) || 0) + 1);
    any = g;
  }
  const lines = [];
  if (counts.size) {
    const models = [...counts.entries()].sort((a, b) => b[1] - a[1]);
    lines.push('written by ' + (models.length === 1 ? models[0][0]
      : models.map(([m, n]) => m + ' ×' + n).join(' · ')));
    const tail = [any.backend, any.mode].filter(Boolean).join(' · ');
    if (tail) lines.push(tail);
    const sl = ckSamplerLine(any.samplers);
    if (sl) lines.push(sl + (any.preset ? '  ·  ' + any.preset : ''));
    if (unstamped) lines.push(unstamped + ' unstamped');
  } else {
    // Everything here predates the gen stamp. "exported with", never
    // "generated with" — we genuinely do not know what wrote it.
    const back = (BACKENDS.find((b) => b.url === S.llm.backend) || {}).label || '';
    lines.push('exported with ' + (S.llm.model || 'no model'));
    if (back) lines.push(back);
    const p = activePreset();
    const sl = ckSamplerLine(samplersFromInputs());
    if (sl) lines.push(sl + (p ? '  ·  ' + p.name : ''));
  }
  return ckScrub(lines.join('\n'));
}

// ── tiling ───────────────────────────────────────────────────────────────
// Where the gaps between messages actually are IN THE RASTER.
//
// getBoundingClientRect answers for the live page, and the SVG viewport wraps
// text fractionally differently — measured 0px of disagreement at the first
// bubble and 33px by the fifth. Over a 9000px sheet that is enough to put a
// "message boundary" cut through the middle of a bubble, which is the one
// thing this feature promises not to do. So the cut points come from the
// picture, not from the DOM: a row is a legal cut if every pixel across the
// bubble column is the page background, which is true in the 12px gap between
// messages and false anywhere inside one.
async function ckBgRows(img, W, H) {
  const ref = document.createElement('canvas');
  ref.width = 1; ref.height = 1;
  const rctx = ref.getContext('2d', { alpha: false });
  rctx.fillStyle = ckTok('--bg');
  rctx.fillRect(0, 0, 1, 1);
  const [br, bg, bb] = rctx.getImageData(0, 0, 1, 1).data;

  const BAND = 2000;                       // 900x32767 in one getImageData is
  const c = document.createElement('canvas');   // ~118 MB; band it instead
  c.width = W; c.height = Math.min(BAND, H);
  const ctx = c.getContext('2d', { alpha: false });
  const x0 = 58, x1 = Math.max(x0 + 1, W - 18);   // skip the avatar gutter
  const cuts = [];
  for (let top = 0; top < H; top += BAND) {
    const h = Math.min(BAND, H - top);
    ctx.fillStyle = ckTok('--bg');
    ctx.fillRect(0, 0, W, h);
    ctx.drawImage(img, 0, -top);
    const px = ctx.getImageData(x0, 0, x1 - x0, h).data;
    const wide = x1 - x0;
    for (let y = 0; y < h; y++) {
      let clear = true;
      for (let x = 0; x < wide; x++) {
        const i = (y * wide + x) * 4;
        if (px[i] !== br || px[i + 1] !== bg || px[i + 2] !== bb) { clear = false; break; }
      }
      if (clear) cuts.push(top + y);
    }
  }
  c.width = 0;
  return cuts;
}

function ckPlanTiles(H, cuts, maxH) {
  const budget = Math.min(maxH, CK_MAX_TILE_H) - CK_FOOT_H;
  const tiles = []; const notes = [];
  let top = 0;
  while (top < H) {
    if (H - top <= budget) { tiles.push({ top, h: H - top }); break; }
    let cut = -1;
    for (const c of cuts) if (c > top && c <= top + budget) cut = c;
    if (cut < 0) {                      // one message taller than a whole sheet
      cut = top + budget;
      notes.push('one message is taller than a whole image, I cut it, and '
        + 'it looks like it');
    }
    tiles.push({ top, h: cut - top });
    top = cut;
  }
  return { tiles, notes };
}

async function ckPaintTile(img, tile, W, foot) {
  const c = document.createElement('canvas');
  c.width = W; c.height = tile.h + CK_FOOT_H;
  const ctx = c.getContext('2d', { alpha: false });
  // MANDATORY: rows the SVG does not cover come back fully transparent, which
  // reads as pure black once the alpha is dropped.
  ctx.fillStyle = ckTok('--bg');
  ctx.fillRect(0, 0, c.width, c.height);
  ctx.drawImage(img, 0, -tile.top);
  if (foot) ctx.drawImage(foot, 0, tile.h);
  return c;
}

async function ckFooter(W, left, right) {
  const { stage, host } = ckStage(W, 'ck-foot-host');
  try {
    const l = document.createElement('span');
    l.textContent = xmlSafe(left);
    const r = document.createElement('span');
    r.textContent = xmlSafe(right);
    host.appendChild(l); host.appendChild(r);
    return await ckRaster(host, W, CK_FOOT_H);
  } finally { stage.remove(); }
}

// ── encode ───────────────────────────────────────────────────────────────
function ckToBlob(canvas, type, q) {
  return new Promise((res) => {
    try { canvas.toBlob(res, type, q); }        // Chrome: null. Firefox: throws.
    catch (e) { res(null); }
  });
}
async function ckEncode(canvas) {
  let blob = await ckToBlob(canvas, 'image/png');
  if (!blob) throw new CkExportError('canvas-limit', canvas.width + 'x' + canvas.height);
  if (blob.type !== 'image/png') throw new CkExportError('encode', blob.type);
  if (blob.size <= CK_CAP_BYTES) return blob;
  // JPEG only from here — toBlob ignores the quality argument for PNG, so a
  // quality search on PNG is a loop that measures the same number every time.
  let lo = 0.5, hi = 0.95, best = null;
  for (let i = 0; i < 4; i++) {
    const q = (lo + hi) / 2;
    const j = await ckToBlob(canvas, 'image/jpeg', q);
    if (!j || j.type !== 'image/jpeg') break;
    if (j.size <= CK_CAP_BYTES) { best = j; lo = q; } else { hi = q; }
  }
  return best || blob;
}

// ── save / clipboard ─────────────────────────────────────────────────────
function ckSave(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = name;
  document.body.appendChild(a);        // a detached <a>.click() is a FF no-op
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}
const ckSlug = (s) => String(s || 'log').replace(/[^A-Za-z0-9_-]+/g, '_')
  .replace(/^_+|_+$/g, '').slice(0, 40) || 'log';
function ckStampName() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate())
    + '-' + p(d.getHours()) + p(d.getMinutes());
}

// ── the modal ────────────────────────────────────────────────────────────
const EXP = {
  d: null,          // last /api/chats/<id> payload (redacted as asked)
  key: '',          // the redaction params that payload was fetched with
  media: new Map(), // asset id -> {el, kind, dur}
  tiles: [],        // [{canvas, blob}]
  page: 0,
  busy: false,
  again: false,
};

function ckOpts() {
  const nameMode = $('exportName').value;
  const custom = ($('exportNameCustom').value || 'Anon').trim().slice(0, 40);
  return {
    range: $('exportRange').value,
    shape: $('exportShape').value,
    width: +$('exportWidth').value || 900,
    pics: $('exportPics').value,
    think: $('exportThink').checked,
    times: $('exportTimes').checked,
    stamp: $('exportStamp').checked,
    header: true,
    userAs: nameMode === 'real' ? '' : (nameMode === 'custom' ? custom : 'Anon'),
    aliases: ($('exportAliases').value || '').trim(),
  };
}

function ckPicks(msgs, opts) {
  if (opts.range === 'pick') {
    const on = new Set([...$('exportList').querySelectorAll('input:checked')]
      .map((i) => +i.dataset.idx));
    return msgs.map((m, i) => ({ m, i })).filter((p) => on.has(p.i));
  }
  const n = opts.shape === 'quote' ? Math.min(6, msgs.length)
    : (opts.range === 'all' ? msgs.length : +opts.range || 10);
  const start = Math.max(0, msgs.length - n);
  return msgs.slice(start).map((m, k) => ({ m, i: start + k }));
}

async function ckPreloadMedia(picks, opts, avatars) {
  const jobs = [];
  for (const url of (avatars || [])) {
    const key = 'ava:' + url;
    if (EXP.media.has(key)) continue;
    EXP.media.set(key, {});
    // 96px source for a 48px and a 32px box — scaling down is what makes them
    // look drawn rather than resampled, and it costs about 8 KB.
    jobs.push(ckLoadImage(url).then((el) =>
      EXP.media.set(key, (el && ckInline(el, 96)) || {})));
  }
  if (opts.pics !== 'off') {
    const long = opts.pics === 'big' ? Math.min(560, opts.width - 120) : 220;
    for (const p of picks) {
      for (const a of (p.m.assets || [])) {
        if (EXP.media.has(a.id)) continue;
        EXP.media.set(a.id, { kind: a.kind });
        if (a.kind === 'image') {
          jobs.push(ckLoadImage(a.url).then((el) => EXP.media.set(a.id,
            Object.assign({ kind: 'image' }, el ? ckInline(el, long) : null))));
        } else if (a.kind === 'video') {
          jobs.push(ckLoadVideo(a.url).then((el) => EXP.media.set(a.id,
            Object.assign({ kind: 'video', dur: el ? el.duration : 0 },
                          el ? ckInline(el, long) : null))));
        } else {
          jobs.push(ckLoadDuration(a.url).then((dur) =>
            EXP.media.set(a.id, { kind: a.kind, dur })));
        }
      }
    }
  }
  await Promise.all(jobs);
}

const ckMB = (n) => (n / 1048576).toFixed(n > 1048576 ? 2 : 2) + ' MB';

let ckTimer = null;
const ckRenderSoon = () => { clearTimeout(ckTimer); ckTimer = setTimeout(ckRender, 180); };

async function ckRender() {
  if (EXP.busy) { EXP.again = true; return; }
  EXP.busy = true;
  const st = $('exportStatus');
  try {
    if (!(await ckCanary())) {
      st.textContent = '';
      $('exportWarn').hidden = false;
      $('exportWarn').textContent = 'something in the stylesheet is eating the '
        + 'render, check the .ck-export block in style.css. I am not handing '
        + 'you a black rectangle and calling it a screencap.';
      return;
    }
    const opts = ckOpts();
    st.textContent = 'laying out…';
    const key = opts.userAs + '|' + opts.aliases;
    if (key !== EXP.key || !EXP.d) {
      const qs = opts.userAs
        ? '?user_as=' + encodeURIComponent(opts.userAs)
          + '&aliases=' + encodeURIComponent(opts.aliases)
        : '';
      EXP.d = await api('/api/chats/' + S.chat.id + qs);
      EXP.key = key;
      ckFillPicker(EXP.d.messages || []);
    }
    const d = EXP.d;
    const msgs = d.messages || [];
    const picks = ckPicks(msgs, opts);
    if (!picks.length) {
      st.textContent = 'pick at least one message, genius';
      EXP.tiles = []; ckPaintPreview();
      return;
    }
    await ckPreloadMedia(picks, opts,
                         d.avatar ? ['/api/avatars/' + d.avatar] : []);

    const built = await ckBuildHost(d, picks, opts, EXP.media);
    let W = opts.width, tiles, notes = [], img = null, H = 0;
    try {
      const hr = built.host.getBoundingClientRect();
      H = Math.ceil(hr.height);
      st.textContent = 'rasterising…';
      if (H > CK_ENGINE_MAX) throw new CkExportError('canvas-limit', H + 'px tall');
      img = await ckRaster(built.host, W, H);
      // Only a log long enough to split pays for the scan.
      const cuts = H > CK_MAX_TILE_H - CK_FOOT_H
        ? await ckBgRows(img, W, H)
        : [H];
      const plan = ckPlanTiles(H, cuts, CK_MAX_TILE_H);
      tiles = plan.tiles; notes = plan.notes;
    } finally { built.stage.remove(); }

    const out = [];
    for (let i = 0; i < tiles.length; i++) {
      st.textContent = 'painting ' + (i + 1) + '/' + tiles.length + '…';
      const right = (tiles.length > 1 ? 'part ' + (i + 1) + '/' + tiles.length + '  ·  ' : '')
        + new Date().toISOString().slice(0, 16).replace('T', ' ');
      const foot = await ckFooter(W, 'CoomKit  ·  ' + (d.character || 'her'), right);
      const canvas = await ckPaintTile(img, tiles[i], W, foot);
      const blob = await ckEncode(canvas);
      out.push({ canvas, blob });
    }
    EXP.tiles = out;
    EXP.page = Math.min(EXP.page, out.length - 1);

    const biggest = Math.max(...out.map((t) => t.blob.size));
    const jpeg = out.some((t) => t.blob.type === 'image/jpeg');
    $('exportStats').textContent = W + ' × ' + H.toLocaleString() + '  ·  '
      + (out.length === 1 ? 'one image' : out.length + ' images')
      + '  ·  ' + (out.length === 1 ? ckMB(biggest) : 'biggest is ' + ckMB(biggest));
    $('exportBadge').textContent = picks.length + ' msg'
      + (picks.length === 1 ? '' : 's');

    const warn = [];
    if (out.length > 1) {
      warn.push("That's " + H.toLocaleString() + ' tall, so I split it into '
        + out.length + '. You weren\'t going to.');
    }
    const thumbW = Math.round(W * CK_THUMB_H / (tiles[0].h + CK_FOOT_H));
    if (thumbW < 45) {
      warn.push('In a reply that thumbnails to about ' + thumbW + 'px wide, a pink '
        + 'smear. Click-through only. Use a quote card if you want it read in the thread.');
    }
    if (jpeg) {
      warn.push('Over 4 MB, so it went out as JPEG. The pink text will ring a bit. '
        + 'Blame Hiro.');
    }
    if (d.redacted) {
      warn.push('Swapped your name out ' + d.redacted + ' time'
        + (d.redacted === 1 ? '' : 's') + '. Anything I don\'t know about is still in there, read it before you post.');
    }
    warn.push(...notes);
    $('exportWarn').hidden = !warn.length;
    $('exportWarn').textContent = warn.join('  ');
    ckPaintPreview();
    st.textContent = '';
  } catch (e) {
    console.error('export', e);
    st.textContent = 'export failed (' + (e.code || 'unknown') + '), see the console';
  } finally {
    EXP.busy = false;
    if (EXP.again) { EXP.again = false; ckRenderSoon(); }
  }
}

// Two previews. The small one is the point: nobody checks what their 9000px
// log looks like at 4chan's 125px reply thumbnail until after they post it.
function ckPaintPreview() {
  const big = $('exportPreview'), small = $('exportThumb');
  const t = EXP.tiles[EXP.page];
  if (!t) {
    big.width = 10; big.height = 10; small.width = 10; small.height = 10;
    $('exportPageInfo').textContent = '0 / 0';
    return;
  }
  big.width = t.canvas.width; big.height = t.canvas.height;
  big.getContext('2d').drawImage(t.canvas, 0, 0);
  const tw = Math.max(1, Math.round(t.canvas.width * CK_THUMB_H / t.canvas.height));
  small.width = tw; small.height = CK_THUMB_H;
  const sc = small.getContext('2d');
  sc.imageSmoothingQuality = 'high';
  sc.drawImage(t.canvas, 0, 0, tw, CK_THUMB_H);
  $('exportPageInfo').textContent = (EXP.page + 1) + ' / ' + EXP.tiles.length;
}

function ckFillPicker(msgs) {
  const box = $('exportList');
  box.innerHTML = '';
  msgs.forEach((m, i) => {
    const lab = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.dataset.idx = String(i);
    cb.checked = i >= msgs.length - 10;
    cb.onchange = ckRenderSoon;
    const sp = document.createElement('span');
    // model text, never innerHTML
    sp.textContent = (m.role === 'assistant' ? '♡ ' : '☻ ')
      + String(m.content || '').replace(/\s+/g, ' ').slice(0, 70);
    lab.appendChild(cb); lab.appendChild(sp);
    box.appendChild(lab);
  });
}

function ckSyncControls() {
  const pick = $('exportRange').value === 'pick';
  $('exportPickWrap').hidden = !pick;
  $('exportNameCustom').hidden = $('exportName').value !== 'custom';
  const quote = $('exportShape').value === 'quote';
  $('exportShapeHint').textContent = quote
    ? 'One good exchange, short enough to read in the thread. Six messages, no splitting.'
    : 'The whole run. Long logs get split into several images on message boundaries.';
  // what the signature will actually say, before you commit to it
  $('exportStampLine').textContent = $('exportStamp').checked && EXP.d
    ? ckProvenance(EXP.d, ckPicks(EXP.d.messages || [], ckOpts())).replace(/\n/g, '  ·  ')
    : 'unsigned, nobody will know what wrote it';
  const persona = S.personas.find((p) => EXP.d && EXP.d.chat
    && String(p.id) === String(EXP.d.chat.persona_id));
  const real = persona && persona.name;
  $('exportNameHint').textContent = !real ? ''
    : ($('exportName').value === 'real'
      ? 'Leaving "' + real + '" in the picture. Your call.'
      : 'It says "' + real + '" in there. That\'s you, genius. I\'m binding it '
        + 'server-side before the text ever reaches the browser.');
}

function openExport() {
  EXP.d = null; EXP.key = ''; EXP.tiles = []; EXP.page = 0; EXP.media.clear();
  $('exportBack').classList.add('on');
  $('exportWarn').hidden = true;
  ckSyncControls();
  ckRender().then(ckSyncControls);
}
const closeExport = () => $('exportBack').classList.remove('on');

$('btnExport').onclick = () => {
  if (!S.chat) { toast('open a chat first, genius'); return; }
  openExport();
};
$('closeExport').onclick = closeExport;
$('exportBack').onclick = (e) => { if (e.target === $('exportBack')) closeExport(); };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('exportBack').classList.contains('on')) closeExport();
});
for (const id of ['exportRange', 'exportShape', 'exportWidth', 'exportPics',
                  'exportThink', 'exportTimes', 'exportStamp', 'exportName',
                  'exportNameCustom', 'exportAliases']) {
  $(id).addEventListener('change', () => { ckSyncControls(); ckRenderSoon(); });
}
$('exportAliases').addEventListener('input', ckRenderSoon);
$('exportPrev').onclick = () => {
  if (EXP.page > 0) { EXP.page--; ckPaintPreview(); }
};
$('exportNext').onclick = () => {
  if (EXP.page < EXP.tiles.length - 1) { EXP.page++; ckPaintPreview(); }
};

$('exportSave').onclick = async () => {
  if (!EXP.tiles.length) { toast('nothing to save yet'); return; }
  const her = ckSlug((EXP.d && EXP.d.character) || 'log');
  const stamp = ckStampName();
  const n = EXP.tiles.length;
  for (let i = 0; i < n; i++) {
    const t = EXP.tiles[i];
    const ext = t.blob.type === 'image/jpeg' ? '.jpg' : '.png';
    const part = n > 1 ? '-' + (i + 1) + 'of' + n : '';
    ckSave(t.blob, 'coomkit-' + her + '-' + stamp + part + ext);
    // browsers throttle rapid programmatic downloads
    if (i < n - 1) await new Promise((r) => setTimeout(r, 300));
  }
  toast(n === 1 ? 'saved' : 'saved ' + n + ' files');
  if ($('exportKeep').checked) await ckKeep();
};

$('exportCopy').onclick = async () => {
  const t = EXP.tiles[EXP.page];
  if (!t) { toast('nothing to copy yet'); return; }
  // Measured: ClipboardItem.supports('image/jpeg') is false in both engines,
  // so re-encode as PNG rather than hand over something it will refuse.
  try {
    const png = t.blob.type === 'image/png' ? t.blob : await ckToBlob(t.canvas, 'image/png');
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })]);
    toast('copied, paste it into 4chanX');
  } catch (e) {
    toast('clipboard said no (needs a secure context, 127.0.0.1 counts, a LAN IP does not)');
  }
};

// Nothing at 14px survives a 125px thumbnail, so the model line belongs in the
// post body, not only in the picture.
$('exportPostText').onclick = async () => {
  const d = EXP.d;
  if (!d) { toast('nothing to describe yet'); return; }
  const text = ckProvenance(d, ckPicks(d.messages || [], ckOpts()))
    .split('\n').join('\n') + '\nCoomKit';
  try {
    await navigator.clipboard.writeText(text);
    toast('post text copied');
  } catch (e) { toast('clipboard said no'); }
};

async function ckKeep() {
  const t = EXP.tiles[EXP.page];
  if (!t) return;
  const b64 = await new Promise((res) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(',')[1] || '');
    fr.readAsDataURL(t.blob);
  });
  const r = await post('/api/chats/' + S.chat.id + '/export/save',
                       { b64, part: EXP.page + 1, parts: EXP.tiles.length });
  toast(r && r.url ? 'tucked into her gallery' : 'could not save that one');
}
