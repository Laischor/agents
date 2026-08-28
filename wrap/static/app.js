const $ = (id) => document.getElementById(id);

const AGENT_LABEL = { claude: "Claude", cursor: "Cursor", opencode: "OpenCode" };

const state = {
  agent: "claude",
  cwd: null,
  session: null,
  draft: false,
  es: null,
  paneOpen: false,
  catalog: {},
  sessions: [],
  projects: [],
  pending: {},
};

const prefsKey = (agent) => `wrap.prefs.${agent}`;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function renderMarkdown(text) {
  const escaped = escapeHtml(text || "");
  const withCode = escaped.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, _lang, body) => {
    return `<pre><code>${body}</code></pre>`;
  });
  return withCode.replace(/`([^`]+)`/g, "<code>$1</code>");
}

function renderDiff(text) {
  return String(text || "")
    .split("\n")
    .map((line) => {
      let cls = "diff-ctx";
      if (line.startsWith("+++") || line.startsWith("---")) cls = "diff-file";
      else if (line.startsWith("@@")) cls = "diff-hunk";
      else if (line.startsWith("+")) cls = "diff-add";
      else if (line.startsWith("-")) cls = "diff-del";
      return `<span class="${cls}">${escapeHtml(line)}</span>`;
    })
    .join("\n");
}

function projectName(cwd) {
  return (cwd || "").split("/").filter(Boolean).pop() || cwd || "";
}

function agentLabel(agent) {
  return AGENT_LABEL[agent] || agent || "";
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(prefsKey(state.agent)) || "{}");
  } catch {
    return {};
  }
}

function savePrefs() {
  localStorage.setItem(
    prefsKey(state.agent),
    JSON.stringify({
      model: $("model").value,
      effort: $("effort").value,
      fast: $("fast").checked,
    }),
  );
}

function fillSelect(el, items, current) {
  el.innerHTML = "";
  for (const item of items || []) {
    const opt = document.createElement("option");
    opt.value = item.id;
    opt.textContent = item.label || item.id || "Default";
    el.appendChild(opt);
  }
  if ([...el.options].some((o) => o.value === current)) el.value = current;
}

function applyCatalog() {
  const cat = state.catalog[state.agent] || { models: [], effort: [], fast: false };
  const prefs = loadPrefs();
  const model = state.session ? state.session.model || "" : prefs.model || "";
  const effort = state.session ? state.session.effort || "" : prefs.effort || "";
  const fast = state.session ? Boolean(state.session.fast) : Boolean(prefs.fast);
  fillSelect($("model"), cat.models, model);
  fillSelect($("effort"), cat.effort, effort);
  $("fast-wrap").hidden = !cat.fast;
  $("fast").checked = cat.fast ? fast : false;
}

function fillProjects() {
  const el = $("project");
  const current = state.cwd || "";
  el.innerHTML = "";
  const blank = document.createElement("option");
  blank.value = "";
  blank.textContent = "Choose project…";
  el.appendChild(blank);
  for (const p of state.projects) {
    const opt = document.createElement("option");
    opt.value = p.path;
    opt.textContent = p.name;
    el.appendChild(opt);
  }
  if (current && ![...el.options].some((o) => o.value === current)) {
    const opt = document.createElement("option");
    opt.value = current;
    opt.textContent = projectName(current);
    el.appendChild(opt);
  }
  if ([...el.options].some((o) => o.value === current)) el.value = current;
}

function setHash() {
  const next = state.session ? state.session.id : state.draft ? "new" : "";
  const cur = (location.hash || "#").slice(1);
  if (cur !== next) history.replaceState(null, "", next ? `#${next}` : location.pathname);
}

function canCompose() {
  if (state.session) return true;
  return state.draft && Boolean(state.cwd) && Boolean(state.agent);
}

function setStatus(msg) {
  const el = $("status");
  const text = msg ? String(msg) : "";
  el.hidden = !text;
  el.textContent = text;
}

function applyChrome() {
  const live = Boolean(state.session);
  const draft = state.draft && !live;
  $("session-bar").hidden = !draft;
  $("resume-wrap").hidden = live;
  $("input").disabled = !canCompose() || state.paneOpen;
  $("input").placeholder = draft ? "Send a message to start…" : "Message the native CLI…";
  $("btn-send").disabled = !canCompose() || state.paneOpen;
  $("btn-int").hidden = !live;
  $("btn-stop").hidden = !live;
  $("btn-pane").hidden = !(live && state.session?.tmux);
  $("btn-pane").classList.toggle("on", Boolean(state.paneOpen && live));
  $("btn-yes").hidden = !(live && state.session?.tmux);
  $("btn-no").hidden = !(live && state.session?.tmux);
  if (live && state.session?.tmux && state.paneOpen) {
    $("tui").hidden = false;
  } else {
    $("tui").hidden = true;
  }
  if (live) {
    $("agent").value = state.session.agent || state.agent;
    if (state.session.cwd) $("project").value = state.session.cwd;
  } else if (draft) {
    $("agent").value = state.agent;
    $("project").value = state.cwd || "";
  } else {
    $("log").innerHTML = `<div class="empty">New session — then choose project and agent</div>`;
    state.paneOpen = false;
    $("tui").hidden = true;
  }
  setHash();
}

function openDraft() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  state.session = null;
  state.draft = true;
  state.paneOpen = false;
  applyCatalog();
  $("log").innerHTML = `<div class="empty">Pick a project and agent, then send a message</div>`;
  setStatus("");
  applyChrome();
  renderSessions();
  $("project").focus();
}

function clearMain() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  state.session = null;
  state.draft = false;
  state.paneOpen = false;
  applyChrome();
  renderSessions();
}

function isWrapDefaultTitle(t) {
  return / · (claude|cursor|opencode)( · |$)/i.test(t || "");
}

function sessionLabel(s) {
  const proj = projectName(s.cwd);
  const t = (s.title || "").trim();
  if (t && t !== proj && !isWrapDefaultTitle(t)) return t;
  return proj || t || (s.id || "").slice(-8);
}

function renderSessions() {
  const ul = $("sessions");
  ul.innerHTML = "";
  const live = state.sessions.filter((s) => s.live);
  if (state.draft) {
    const li = document.createElement("li");
    li.className = "draft on";
    li.innerHTML = `<button type="button"><span class="sess-title">New session</span><span class="sess-meta">${
      state.cwd ? `${escapeHtml(projectName(state.cwd))} · ${escapeHtml(agentLabel(state.agent))}` : "pick project &amp; agent"
    }</span></button>`;
    li.querySelector("button").addEventListener("click", () => openDraft());
    ul.appendChild(li);
  }
  if (!live.length && !state.draft) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "No active sessions";
    ul.appendChild(empty);
    return;
  }
  for (const s of live) {
    const li = document.createElement("li");
    if (state.session && s.id === state.session.id) li.classList.add("on");
    const name = sessionLabel(s);
    const bits = [agentLabel(s.agent)];
    const proj = projectName(s.cwd);
    if (proj && name !== proj) bits.push(proj);
    li.innerHTML = `<button type="button"><span class="sess-title">${escapeHtml(name)}</span><span class="sess-meta">${
      s.busy ? '<span class="busy">working</span> · ' : ""
    }${escapeHtml(bits.filter(Boolean).join(" · "))}</span></button>`;
    li.querySelector("button").addEventListener("click", () => attachSession(s.id));
    ul.appendChild(li);
  }
}

async function loadSessions() {
  try {
    const { sessions } = await api("/api/sessions");
    state.sessions = sessions || [];
    renderSessions();
  } catch (err) {
    $("health").textContent = String(err.message || err);
  }
}

async function loadProjects() {
  try {
    const { projects } = await api("/api/projects");
    state.projects = projects || [];
    fillProjects();
  } catch (err) {
    setStatus(err.message || err);
  }
}

async function loadCatalog() {
  try {
    state.catalog = await api("/api/catalog");
    applyCatalog();
  } catch (err) {
    $("health").textContent = String(err.message || err);
  }
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    const bits = [
      h.tmux ? "tmux ok" : "tmux missing",
      h.opencode_serve ? "opencode serve" : h.opencode ? "opencode idle" : "no opencode",
    ];
    $("health").textContent = bits.join(" · ");
  } catch (err) {
    $("health").textContent = String(err.message || err);
  }
}

function setAgent(agent) {
  if (!agent || agent === state.agent) {
    applyChrome();
    return;
  }
  savePrefs();
  state.agent = agent;
  applyCatalog();
  applyChrome();
  renderSessions();
}

function setProject(cwd) {
  state.cwd = cwd || null;
  applyChrome();
  renderSessions();
}

function renderSession(sess) {
  state.session = sess;
  state.draft = false;
  state.agent = sess.agent || state.agent;
  state.cwd = sess.cwd || state.cwd;
  fillProjects();
  applyCatalog();
  setStatus("");
  renderMessages(mergeMessages(sess.messages || []));
  if (sess.tmux) {
    $("pane").textContent = sess.pane || "";
    if (state.paneOpen) {
      $("tui").hidden = false;
      $("pane").scrollTop = $("pane").scrollHeight;
    }
  } else {
    setPaneOpen(false);
  }
  applyChrome();
  renderSessions();
}

function renderMessages(messages) {
  const log = $("log");
  log.innerHTML = "";
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = state.session?.agent === "opencode"
      ? "Session is up. Send a message — it goes to the native CLI."
      : "Waiting for the CLI transcript… send a message, bubbles show up here.";
    log.appendChild(empty);
    return;
  }
  for (const m of messages) {
    const el = document.createElement("article");
    el.className = `msg ${m.role}`;
    const who = document.createElement("div");
    who.className = "who";
    if (m.pending) el.classList.add("pending");
    who.innerHTML =
      (m.role === "assistant" && state.session?.busy ? `<span class="busy-dot"></span>` : "") +
      (m.pending ? "queued" : m.role);
    el.appendChild(who);
    if (m.text) {
      const body = document.createElement("div");
      body.innerHTML = renderMarkdown(m.text);
      el.appendChild(body);
    }
    for (const d of m.diffs || []) {
      const box = document.createElement("div");
      box.className = "diff";
      const head = document.createElement("div");
      head.className = "diff-head";
      const file = String(d.path || "").split("/").filter(Boolean).pop() || d.path || "file";
      head.textContent = (d.kind === "write" ? "write " : "") + file;
      if (d.path && file !== d.path) head.title = d.path;
      const pre = document.createElement("pre");
      pre.innerHTML = renderDiff(d.diff || "");
      box.appendChild(head);
      box.appendChild(pre);
      el.appendChild(box);
    }
    if (m.tools && m.tools.length) {
      const tools = document.createElement("div");
      tools.className = "tools";
      tools.textContent = m.tools.join(" · ");
      el.appendChild(tools);
    }
    log.appendChild(el);
  }
  log.scrollTop = log.scrollHeight;
}

function connectStream(id) {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  const es = new EventSource(`/api/sessions/${id}/stream`);
  state.es = es;
  es.addEventListener("sync", (ev) => {
    try {
      renderSession(JSON.parse(ev.data));
    } catch (_) {
      /* ignore */
    }
  });
  es.addEventListener("pane", (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (state.session) {
        state.session.busy = data.busy;
        state.session.pane = data.pane;
      }
      $("pane").textContent = data.pane || "";
      if (state.paneOpen) $("pane").scrollTop = $("pane").scrollHeight;
      renderSessions();
    } catch (_) {
      /* ignore */
    }
  });
  es.addEventListener("gone", () => {
    es.close();
    setStatus("session ended");
  });
}

async function attachSession(id) {
  try {
    const sess = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    renderSession(sess);
    connectStream(sess.id);
    loadSessions();
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

async function startSession() {
  if (!state.cwd) {
    setStatus("Pick a project first");
    return null;
  }
  savePrefs();
  setStatus("");
  $("btn-send").disabled = true;
  try {
    const sess = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        agent: state.agent,
        cwd: state.cwd,
        continue: $("resume").checked,
        model: $("model").value,
        effort: $("effort").value,
        fast: $("fast").checked,
      }),
    });
    $("resume").checked = false;
    renderSession(sess);
    connectStream(sess.id);
    loadSessions();
    return sess;
  } catch (err) {
    setStatus(err.message || String(err));
    applyChrome();
    return null;
  }
}

function mergeMessages(serverMsgs) {
  const sid = state.session?.id;
  const pending = sid ? state.pending[sid] || [] : [];
  const still = [];
  for (const p of pending) {
    const appeared = (serverMsgs || []).some(
      (m) =>
        m.role === "user" &&
        typeof m.text === "string" &&
        (m.text === p.text || m.text.endsWith(p.text)),
    );
    if (!appeared) still.push(p);
  }
  if (sid) state.pending[sid] = still;
  return (serverMsgs || []).concat(
    still.map((p) => ({ id: p.id, role: "user", text: p.text, tools: [], pending: true })),
  );
}

async function sendMessage(ev) {
  ev.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  if (!state.session) {
    const sess = await startSession();
    if (!sess) return;
  }
  $("input").value = "";
  const sid = state.session.id;
  if (!state.pending[sid]) state.pending[sid] = [];
  state.pending[sid].push({ id: "p-" + Date.now(), text });
  renderMessages(mergeMessages(state.session.messages || []));
  $("btn-send").disabled = true;
  try {
    await api(`/api/sessions/${sid}/send`, {
      method: "POST",
      body: JSON.stringify({ text }),
    });
  } catch (err) {
    state.pending[sid] = (state.pending[sid] || []).filter((p) => p.text !== text);
    $("input").value = text;
    setStatus(err.message || String(err));
    renderMessages(mergeMessages(state.session.messages || []));
  } finally {
    $("btn-send").disabled = !canCompose();
    $("input").focus();
  }
}

async function keys(list) {
  if (!state.session || !list.length) return;
  try {
    await api(`/api/sessions/${state.session.id}/keys`, {
      method: "POST",
      body: JSON.stringify({ keys: list }),
    });
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

function setPaneOpen(on) {
  state.paneOpen = Boolean(on) && Boolean(state.session?.tmux);
  $("tui").hidden = !state.paneOpen;
  $("btn-pane").classList.toggle("on", state.paneOpen);
  $("input").disabled = !canCompose() || state.paneOpen;
  $("btn-send").disabled = !canCompose() || state.paneOpen;
  if (state.paneOpen) {
    $("pane").focus();
    $("pane").scrollTop = $("pane").scrollHeight;
  }
}

function mapTuiKey(e) {
  if (e.metaKey || e.altKey) return null;
  if (e.ctrlKey) {
    const map = {
      c: "C-c",
      d: "C-d",
      u: "C-u",
      a: "C-a",
      e: "C-e",
      k: "C-k",
      w: "C-w",
      l: "C-l",
      n: "C-n",
      p: "C-p",
    };
    return map[e.key.toLowerCase()] || null;
  }
  switch (e.key) {
    case "Enter":
      return "Enter";
    case "Escape":
      return "Escape";
    case "Backspace":
      return "BSpace";
    case "Tab":
      return "Tab";
    case "ArrowUp":
      return "Up";
    case "ArrowDown":
      return "Down";
    case "ArrowLeft":
      return "Left";
    case "ArrowRight":
      return "Right";
    case "Home":
      return "Home";
    case "End":
      return "End";
    case "PageUp":
      return "PPage";
    case "PageDown":
      return "NPage";
    case "Delete":
      return "DC";
    case " ":
      return "Space";
    default:
      if (e.key.length === 1) return e.key;
      return null;
  }
}

let keyQueue = [];
let keyTimer = 0;

function flushKeys() {
  keyTimer = 0;
  if (!keyQueue.length) return;
  const batch = keyQueue;
  keyQueue = [];
  keys(batch);
}

function queueKey(k) {
  keyQueue.push(k);
  if (!keyTimer) keyTimer = setTimeout(flushKeys, 20);
}

function onHash() {
  const id = (location.hash || "#").slice(1);
  if (id === "new") {
    if (!state.draft) openDraft();
    return;
  }
  if (id && (!state.session || state.session.id !== id)) {
    attachSession(id);
    return;
  }
  if (!id && (state.session || state.draft)) clearMain();
}

$("agent").addEventListener("change", () => setAgent($("agent").value));
$("project").addEventListener("change", () => setProject($("project").value));
$("model").addEventListener("change", savePrefs);
$("effort").addEventListener("change", savePrefs);
$("fast").addEventListener("change", savePrefs);
$("btn-new").addEventListener("click", openDraft);
$("composer").addEventListener("submit", sendMessage);
$("input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    $("composer").requestSubmit();
  }
});
$("btn-int").addEventListener("click", async () => {
  if (!state.session) return;
  try {
    await api(`/api/sessions/${state.session.id}/interrupt`, { method: "POST", body: "{}" });
  } catch (err) {
    setStatus(err.message || String(err));
  }
});
$("btn-stop").addEventListener("click", async () => {
  if (!state.session) return;
  if (!confirm("Stop this session? The CLI process exits. Other sessions stay up.")) return;
  try {
    await api(`/api/sessions/${state.session.id}`, { method: "DELETE" });
    clearMain();
    loadSessions();
  } catch (err) {
    setStatus(err.message || String(err));
  }
});
$("btn-pane").addEventListener("click", () => setPaneOpen(!state.paneOpen));
$("btn-tui-close").addEventListener("click", () => setPaneOpen(false));
$("tui").addEventListener("click", (e) => {
  if (e.target.closest("button")) return;
  if (state.paneOpen) $("pane").focus();
});
document.addEventListener("keydown", (e) => {
  if (e.isComposing) return;
  if (!state.paneOpen || !state.session?.tmux) return;
  if (e.target.closest("button, textarea, input, select")) return;
  const k = mapTuiKey(e);
  if (!k) return;
  e.preventDefault();
  queueKey(k);
});
$("pane").addEventListener("paste", (e) => {
  e.preventDefault();
  const t = e.clipboardData?.getData("text") || "";
  if (t) keys([t]);
});
$("pane").addEventListener("mousedown", () => {
  if (state.paneOpen) $("pane").focus();
});
$("btn-yes").addEventListener("click", () => keys(["y", "Enter"]));
$("btn-no").addEventListener("click", () => keys(["n", "Enter"]));
window.addEventListener("hashchange", onHash);

applyChrome();
Promise.all([loadCatalog(), loadProjects(), loadHealth(), loadSessions()]).then(() => {
  if ((location.hash || "#").slice(1)) onHash();
});
setInterval(loadHealth, 15000);
setInterval(loadSessions, 8000);
