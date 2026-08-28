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

function applyChrome() {
  const live = Boolean(state.session);
  const draft = state.draft && !live;
  $("session-bar").hidden = !(live || draft);
  $("project").disabled = live;
  $("agent").disabled = live;
  $("model").disabled = live;
  $("effort").disabled = live;
  $("fast").disabled = live;
  $("resume-wrap").hidden = live;
  $("btn-start").hidden = !draft;
  $("btn-start").disabled = !(draft && state.cwd);
  $("input").disabled = !canCompose();
  $("btn-send").disabled = !canCompose();
  $("btn-int").disabled = !live;
  $("btn-stop").hidden = !live;
  $("btn-pane").hidden = !(live && state.session?.tmux);
  $("btn-yes").hidden = !(live && state.session?.tmux);
  $("btn-no").hidden = !(live && state.session?.tmux);
  if (live) {
    $("agent").value = state.session.agent || state.agent;
    if (state.session.cwd) $("project").value = state.session.cwd;
  } else if (draft) {
    $("agent").value = state.agent;
    $("project").value = state.cwd || "";
    $("title").textContent = "New session";
    const proj = projectName(state.cwd);
    $("subtitle").textContent = proj
      ? `${agentLabel(state.agent)} · ${proj} — Start or send a message`
      : "Pick a project and agent";
  } else {
    $("title").textContent = "Sessions";
    $("subtitle").textContent = "New session, then pick a project and agent";
    $("log").innerHTML = `<div class="empty">New session — then choose project and agent</div>`;
    $("pane").hidden = true;
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
  $("log").innerHTML = `<div class="empty">Pick a project and agent, then Start</div>`;
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
    $("subtitle").textContent = String(err.message || err);
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
  const name = sessionLabel(sess);
  $("title").textContent = name;
  const bits = [agentLabel(sess.agent), projectName(sess.cwd)];
  if (sess.model) bits.push(sess.model);
  if (sess.effort) bits.push(sess.effort);
  if (sess.fast) bits.push("fast");
  bits.push(sess.busy ? "working" : "idle");
  $("subtitle").textContent = bits.filter(Boolean).join(" · ");
  renderMessages(mergeMessages(sess.messages || []));
  if (sess.tmux) {
    $("pane").hidden = !state.paneOpen;
    $("pane").textContent = sess.pane || "";
  } else {
    $("pane").hidden = true;
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
        const bits = [agentLabel(state.session.agent), projectName(state.session.cwd)];
        if (state.session.model) bits.push(state.session.model);
        bits.push(data.busy ? "working" : "idle");
        $("subtitle").textContent = bits.filter(Boolean).join(" · ");
      }
      $("pane").textContent = data.pane || "";
      renderSessions();
    } catch (_) {
      /* ignore */
    }
  });
  es.addEventListener("gone", () => {
    es.close();
    $("subtitle").textContent = "session ended";
  });
}

async function attachSession(id) {
  $("title").textContent = "Opening…";
  try {
    const sess = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ id }),
    });
    renderSession(sess);
    connectStream(sess.id);
    loadSessions();
  } catch (err) {
    $("subtitle").textContent = err.message || String(err);
  }
}

async function startSession() {
  if (!state.cwd) {
    $("subtitle").textContent = "Pick a project first";
    return null;
  }
  savePrefs();
  $("title").textContent = "Starting…";
  $("subtitle").textContent = `${agentLabel(state.agent)} in ${projectName(state.cwd)}`;
  $("btn-start").disabled = true;
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
    $("subtitle").textContent = err.message || String(err);
    applyChrome();
    return null;
  } finally {
    $("btn-start").disabled = false;
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
    $("subtitle").textContent = err.message || String(err);
    renderMessages(mergeMessages(state.session.messages || []));
  } finally {
    $("btn-send").disabled = !canCompose();
    $("input").focus();
  }
}

async function keys(list) {
  if (!state.session) return;
  try {
    await api(`/api/sessions/${state.session.id}/keys`, {
      method: "POST",
      body: JSON.stringify({ keys: list }),
    });
  } catch (err) {
    $("subtitle").textContent = err.message || String(err);
  }
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
$("btn-start").addEventListener("click", () => startSession());
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
    $("subtitle").textContent = err.message || String(err);
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
    $("subtitle").textContent = err.message || String(err);
  }
});
$("btn-pane").addEventListener("click", () => {
  state.paneOpen = !state.paneOpen;
  $("pane").hidden = !state.paneOpen;
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
