const $ = (id) => document.getElementById(id);

const AGENT_LABEL = { claude: "Claude", cursor: "Cursor", opencode: "OpenCode", hermes: "Hermes" };
const FILTER_KEY = "wrap.agentFilter";

function readAgentFilter() {
  try {
    const raw = JSON.parse(localStorage.getItem(FILTER_KEY) || "null");
    if (!Array.isArray(raw)) return null;
    return new Set(raw.map(String).filter(Boolean));
  } catch {
    return null;
  }
}

const state = {
  agent: "claude",
  cwd: null,
  session: null,
  draft: false,
  es: null,
  paneOpen: false,
  attachments: [],
  pingSid: "",
  catalog: {},
  sessions: [],
  history: [],
  query: "",
  searchHits: null,
  searchGen: 0,
  agentFilter: readAgentFilter(),
  projects: [],
  pending: {},
  acked: {},
  sending: false,
  spawning: false,
};

const PENDING_KEY = "wrap.pending";

const prefsKey = (agent) => `wrap.prefs.${agent}`;

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

const MD_FENCE = "\0FENCE";

function inlineMd(s) {
  const codes = [];
  s = String(s || "").replace(/`([^`]+)`/g, (_, c) => {
    codes.push(`<code>${c}</code>`);
    return `\0C${codes.length - 1}\0`;
  });
  s = s.replace(
    /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    (_, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`,
  );
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");
  s = s.replace(/(^|[\s(])_([^_\n]+)_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>");
  s = s.replace(/\0C(\d+)\0/g, (_, n) => codes[Number(n)]);
  return s;
}

function splitTableRow(line) {
  let s = String(line || "").trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  const cells = [];
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    if (s[i] === "\\" && s[i + 1] === "|") {
      cur += "|";
      i++;
      continue;
    }
    if (s[i] === "|") {
      cells.push(cur.trim());
      cur = "";
      continue;
    }
    cur += s[i];
  }
  cells.push(cur.trim());
  return cells;
}

function isSepRow(line) {
  const cells = splitTableRow(line);
  return cells.length > 0 && cells.every((c) => /^:?-{3,}:?$/.test(c));
}

function tableAlign(cell) {
  const left = cell.startsWith(":");
  const right = cell.endsWith(":");
  if (left && right) return "c";
  if (right) return "r";
  return "";
}

function isTableStart(lines, i) {
  if (i + 1 >= lines.length) return false;
  if (lines[i].indexOf("|") === -1) return false;
  if (lines[i].trim().startsWith(MD_FENCE)) return false;
  return isSepRow(lines[i + 1]);
}

function parseTable(lines, i) {
  const header = splitTableRow(lines[i]);
  const aligns = splitTableRow(lines[i + 1]).map(tableAlign);
  const cols = Math.max(header.length, aligns.length);
  const body = [];
  let r = i + 2;
  while (r < lines.length) {
    const line = lines[r];
    if (!line.trim() || line.trim().startsWith(MD_FENCE)) break;
    if (line.indexOf("|") === -1) break;
    if (isSepRow(line)) break;
    body.push(splitTableRow(line));
    r++;
  }
  const cell = (tag, text, ai) => {
    const cls = aligns[ai] || "";
    const attr = cls ? ` class="${cls}"` : "";
    return `<${tag}${attr}>${inlineMd(text || "")}</${tag}>`;
  };
  const pad = (row) => {
    const out = row.slice(0, cols);
    while (out.length < cols) out.push("");
    return out;
  };
  let html = '<div class="md-table-wrap"><table><thead><tr>';
  pad(header).forEach((c, ai) => {
    html += cell("th", c, ai);
  });
  html += "</tr></thead>";
  if (body.length) {
    html += "<tbody>";
    for (const row of body) {
      html += "<tr>";
      pad(row).forEach((c, ai) => {
        html += cell("td", c, ai);
      });
      html += "</tr>";
    }
    html += "</tbody>";
  }
  html += "</table></div>";
  return [html, r];
}

function renderMdBlocks(src) {
  const lines = String(src || "").split("\n");
  const out = [];
  let i = 0;
  const fenceRe = new RegExp(`^${MD_FENCE}(\\d+)$`);

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = line.trim().match(fenceRe);
    if (fence) {
      out.push(`${MD_FENCE}${fence[1]}`);
      i++;
      continue;
    }
    if (isTableStart(lines, i)) {
      const [html, next] = parseTable(lines, i);
      out.push(html);
      i = next;
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+?)(?:\s+#*)?$/);
    if (heading) {
      const n = heading[1].length;
      out.push(`<h${n}>${inlineMd(heading[2])}</h${n}>`);
      i++;
      continue;
    }
    if (/^\s*([-*_]\s*){3,}$/.test(line) && line.indexOf("|") === -1) {
      out.push("<hr>");
      i++;
      continue;
    }
    if (/^(&gt; ?)/.test(line)) {
      const chunk = [];
      while (i < lines.length && /^(&gt; ?)/.test(lines[i])) {
        chunk.push(lines[i].replace(/^(&gt; ?)/, ""));
        i++;
      }
      out.push(`<blockquote>${renderMdBlocks(chunk.join("\n"))}</blockquote>`);
      continue;
    }
    const listItem = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (listItem) {
      const ordered = /^\d/.test(listItem[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!m) break;
        if (/^\d/.test(m[2]) !== ordered) break;
        let text = m[3];
        i++;
        while (i < lines.length) {
          const cont = lines[i];
          if (!cont.trim()) break;
          if (/^\s*([-*+]|\d+[.)])\s+/.test(cont)) break;
          if (isTableStart(lines, i) || /^#{1,6}\s+/.test(cont) || fenceRe.test(cont.trim())) break;
          if (/^(&gt; ?)/.test(cont)) break;
          text += " " + cont.trim();
          i++;
        }
        items.push(text);
      }
      const tag = ordered ? "ol" : "ul";
      out.push(
        `<${tag}>${items.map((t) => `<li>${inlineMd(t)}</li>`).join("")}</${tag}>`,
      );
      continue;
    }
    const buf = [];
    while (i < lines.length && lines[i].trim()) {
      if (fenceRe.test(lines[i].trim())) break;
      if (isTableStart(lines, i)) break;
      if (/^#{1,6}\s+/.test(lines[i])) break;
      if (/^(&gt; ?)/.test(lines[i])) break;
      if (/^\s*([-*_]\s*){3,}$/.test(lines[i]) && lines[i].indexOf("|") === -1) break;
      if (/^\s*[-*+]\s+/.test(lines[i]) || /^\s*\d+[.)]\s+/.test(lines[i])) break;
      buf.push(lines[i]);
      i++;
    }
    out.push(`<p>${inlineMd(buf.join("\n")).replace(/\n/g, "<br>")}</p>`);
  }
  return out.join("");
}

function renderMarkdown(text) {
  const fences = [];
  let escaped = escapeHtml(text || "").replace(/\r\n/g, "\n");
  escaped = escaped.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, _lang, body) => {
    const n = fences.length;
    fences.push(
      `<div class="code-block"><button type="button" class="code-copy" aria-label="Copy code">Copy</button><pre><code>${body}</code></pre></div>`,
    );
    return `\n${MD_FENCE}${n}\n`;
  });
  return renderMdBlocks(escaped).replace(
    new RegExp(`${MD_FENCE}(\\d+)`, "g"),
    (_, n) => fences[Number(n)] || "",
  );
}

function copyText(text) {
  if (navigator.clipboard?.writeText) {
    return navigator.clipboard.writeText(text);
  }
  return new Promise((resolve, reject) => {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      if (document.execCommand("copy")) resolve();
      else reject(new Error("copy failed"));
    } catch (err) {
      reject(err);
    } finally {
      ta.remove();
    }
  });
}

function onCodeCopy(btn) {
  const text = btn.closest(".code-block")?.querySelector("code")?.textContent ?? "";
  const reset = () => {
    btn.textContent = "Copy";
    btn.classList.remove("copied");
  };
  copyText(text)
    .then(() => {
      btn.textContent = "Copied";
      btn.classList.add("copied");
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(reset, 1600);
    })
    .catch(() => {
      btn.textContent = "Failed";
      clearTimeout(btn._copyTimer);
      btn._copyTimer = setTimeout(reset, 1600);
    });
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

const PASTE_IMG_RE = /(^|\s)(\/\S+\.wrap-pastes\/\S+\.(?:png|jpe?g|gif|webp))/gi;

function renderMessageBody(text) {
  const wrap = document.createElement("div");
  const images = [];
  const rest = String(text || "")
    .replace(PASTE_IMG_RE, (_, sp, p) => {
      images.push(p);
      return sp;
    })
    .trim();
  for (const p of images) {
    const img = document.createElement("img");
    img.className = "msg-img";
    img.src = "/api/file?path=" + encodeURIComponent(p);
    img.alt = p.split("/").filter(Boolean).pop() || "image";
    wrap.appendChild(img);
  }
  if (rest) {
    const body = document.createElement("div");
    body.className = "md";
    body.innerHTML = renderMarkdown(rest);
    wrap.appendChild(body);
  }
  return wrap;
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

function sortModels(items) {
  const head = [];
  const rest = [];
  for (const item of items || []) {
    if (!item.id) head.push(item);
    else rest.push(item);
  }
  rest.sort((a, b) =>
    (a.label || a.id || "").localeCompare(b.label || b.id || "", undefined, { sensitivity: "base" }),
  );
  return head.concat(rest);
}

function listedAgents() {
  const seen = new Map();
  const add = (id, label) => {
    if (!id || seen.has(id)) return;
    seen.set(id, { id, label: label || AGENT_LABEL[id] || id });
  };
  const fromCat = state.catalog.agents;
  if (fromCat && fromCat.length) {
    for (const a of fromCat) add(a.id, a.label);
  } else {
    add("claude", "Claude");
    add("cursor", "Cursor");
    add("opencode", "OpenCode");
  }
  for (const s of [...(state.sessions || []), ...(state.history || [])]) {
    add(s.agent);
  }
  return [...seen.values()];
}

function filterActive() {
  return state.agentFilter instanceof Set;
}

function matchesAgent(s) {
  if (!filterActive()) return true;
  return state.agentFilter.has(s.agent || "");
}

function saveAgentFilter() {
  if (!filterActive()) localStorage.removeItem(FILTER_KEY);
  else localStorage.setItem(FILTER_KEY, JSON.stringify([...state.agentFilter]));
}

function syncFilterBtn() {
  const btn = $("btn-filter");
  if (!btn) return;
  btn.classList.toggle("on", filterActive());
  btn.title = filterActive()
    ? state.agentFilter.size
      ? `Filter: ${[...state.agentFilter].map(agentLabel).join(", ")}`
      : "Filter: none"
    : "Filter by agent";
}

function fillAgentFilter() {
  const panel = $("agent-filter");
  if (!panel) return;
  panel.innerHTML = "";
  const agents = listedAgents();
  const sel = state.agentFilter;
  const allOn = !filterActive();
  for (const a of agents) {
    const row = document.createElement("label");
    row.className = "agent-filter-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = a.id;
    cb.checked = allOn || (sel && sel.has(a.id));
    cb.addEventListener("change", () => {
      const boxes = [...panel.querySelectorAll("input[type=checkbox]")];
      const next = new Set(boxes.filter((el) => el.checked).map((el) => el.value));
      state.agentFilter = next.size === boxes.length ? null : next;
      saveAgentFilter();
      syncFilterBtn();
      renderSessions();
    });
    const span = document.createElement("span");
    span.textContent = a.label;
    row.appendChild(cb);
    row.appendChild(span);
    panel.appendChild(row);
  }
}

function setFilterOpen(open) {
  const panel = $("agent-filter");
  const btn = $("btn-filter");
  if (!panel || !btn) return;
  panel.hidden = !open;
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) fillAgentFilter();
}

function applyCatalog() {
  fillAgents();
  if (!$("agent-filter").hidden) fillAgentFilter();
  syncFilterBtn();
  const cat = state.catalog[state.agent] || { models: [], effort: [], fast: false };
  const prefs = loadPrefs();
  const model = state.session ? state.session.model || "" : prefs.model || "";
  const effort = state.session ? state.session.effort || "" : prefs.effort || "";
  const fast = state.session ? Boolean(state.session.fast) : Boolean(prefs.fast);
  fillSelect($("model"), sortModels(cat.models), model);
  fillSelect($("effort"), cat.effort, effort);
  $("fast-wrap").hidden = !cat.fast;
  $("fast").checked = cat.fast ? fast : false;
}

function fillAgents() {
  const el = $("agent");
  if (!el) return;
  const agents = state.catalog.agents || [
    { id: "claude", label: "Claude" },
    { id: "cursor", label: "Cursor" },
    { id: "opencode", label: "OpenCode" },
  ];
  const current = state.agent || el.value;
  el.innerHTML = "";
  for (const a of agents) {
    const opt = document.createElement("option");
    opt.value = a.id;
    opt.textContent = a.label || AGENT_LABEL[a.id] || a.id;
    el.appendChild(opt);
  }
  if ([...el.options].some((o) => o.value === current)) el.value = current;
  else if (el.options.length) {
    state.agent = el.options[0].value;
    el.value = state.agent;
  }
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
  if (state.sending || state.spawning) return false;
  if (state.session) return true;
  return state.draft && Boolean(state.cwd) && Boolean(state.agent);
}

function setStatus(msg) {
  const el = $("status");
  const text = msg ? String(msg) : "";
  el.hidden = !text;
  el.textContent = text;
}

const narrowMq = window.matchMedia("(max-width: 800px)");
function isNarrow() {
  return narrowMq.matches;
}
function setMenuOpen(on) {
  const open = Boolean(on) && isNarrow();
  document.body.classList.toggle("menu-open", open);
  $("scrim").hidden = !open;
  $("btn-menu").setAttribute("aria-expanded", open ? "true" : "false");
  $("btn-menu").textContent = open ? "Close" : "Sessions";
}
function closeMenu() {
  setMenuOpen(false);
}

function applyChrome() {
  const sess = state.session;
  const running = Boolean(sess?.live);
  const viewing = Boolean(sess);
  const draft = state.draft && !viewing;
  $("session-bar").hidden = !draft;
  $("input").disabled = !canCompose() || state.paneOpen;
  $("input").placeholder = draft
    ? "Send a message or paste a screenshot to start…"
    : viewing && !running
      ? "Send a message to resume this session…"
      : sess?.agent === "opencode"
        ? "Message OpenCode… paste a screenshot"
        : sess?.agent === "hermes"
          ? "Message Hermes… paste a screenshot"
          : "Message the native CLI… paste a screenshot";
  $("btn-send").disabled = !canCompose() || state.paneOpen;
  $("btn-int").hidden = !running;
  $("btn-clear").hidden = !(viewing && sess?.cwd && (sess?.agent || state.agent));
  $("btn-stop").hidden = !running;
  $("btn-pane").hidden = !(running && sess?.tmux);
  $("btn-pane").classList.toggle("on", Boolean(state.paneOpen && running));
  if (running && sess?.tmux && state.paneOpen) {
    $("tui").hidden = false;
  } else {
    $("tui").hidden = true;
  }
  if (viewing) {
    $("agent").value = sess.agent || state.agent;
    if (sess.cwd) $("project").value = sess.cwd;
  } else if (draft) {
    $("agent").value = state.agent;
    $("project").value = state.cwd || "";
  } else {
    $("log").innerHTML = `<div class="empty">New session — then choose project and agent</div>`;
    state.paneOpen = false;
    $("tui").hidden = true;
  }
  const title = $("mobile-title");
  if (sess) title.textContent = sessionLabel(sess);
  else if (draft) title.textContent = "New session";
  else title.textContent = "wrap";
  $("btn-menu").classList.toggle("ping", Boolean(state.pingSid));
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
  clearAttachments();
  $("log").innerHTML = `<div class="empty">Pick a project and agent, then send a message</div>`;
  setStatus("");
  applyChrome();
  renderSessions();
  $("project").focus();
  closeMenu();
}

function clearMain() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  state.session = null;
  state.draft = false;
  state.paneOpen = false;
  clearAttachments();
  applyChrome();
  renderSessions();
  if (isNarrow()) setMenuOpen(true);
}

function isWrapDefaultTitle(t) {
  return / · (claude|cursor|opencode|hermes)( · |$)/i.test(t || "");
}

function sessionLabel(s) {
  const proj = projectName(s.cwd);
  const t = (s.title || "").trim();
  if (t && t !== proj && !isWrapDefaultTitle(t)) return t;
  return proj || t || (s.id || "").slice(-8);
}

function isActiveRow(s) {
  if (!state.session) return false;
  if (s.live) return s.id === state.session.id;
  const native = state.session.native_id || state.session.cli_session || state.session.oc_id || state.session.hm_id || "";
  return Boolean(s.native_id && s.native_id === native && s.agent === state.session.agent);
}

function sessionRow(s, onClick, onRemove) {
  const li = document.createElement("li");
  if (isActiveRow(s)) li.classList.add("on");
  if (s.live && s.id && s.id === state.pingSid) li.classList.add("ping");
  if (s.pinned) li.classList.add("pinned");
  const name = sessionLabel(s);
  const bits = [agentLabel(s.agent)];
  const proj = projectName(s.cwd);
  if (proj && name !== proj) bits.push(proj);
  const subs = s.subagents || [];
  const n = subs.length;
  let busy = "";
  if (s.choice) {
    busy = '<span class="busy">choose</span> · ';
  } else if (n) {
    const word = n === 1 ? "1 subagent" : `${n} subagents`;
    const labels = subs.map((x) => x.description || x.id).filter(Boolean).join(" · ");
    busy = `<span class="busy"${labels ? ` title="${escapeHtml(labels)}"` : ""}>${escapeHtml(word)}</span> · `;
  } else if (s.busy) {
    busy = '<span class="busy">working</span> · ';
  }
  const snip = s.snippet
    ? `<span class="sess-snip">${escapeHtml(s.snippet)}</span>`
    : "";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sess-open";
  btn.innerHTML = `<span class="sess-title">${escapeHtml(name)}</span><span class="sess-meta">${
    busy
  }${escapeHtml(bits.filter(Boolean).join(" · "))}</span>${snip}`;
  btn.addEventListener("click", onClick);
  li.appendChild(btn);
  const native = s.native_id || s.cli_session || s.oc_id || s.hm_id;
  if (native && s.agent) {
    li.classList.add("has-actions");
    const pin = document.createElement("button");
    pin.type = "button";
    pin.className = "sess-pin";
    pin.setAttribute("aria-label", s.pinned ? "Unpin session" : "Pin session");
    pin.setAttribute("aria-pressed", s.pinned ? "true" : "false");
    pin.title = s.pinned ? "Unpin from top" : "Pin to top";
    pin.innerHTML =
      '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><path fill="currentColor" d="M10.1 1.4 8.4 3.1l.7 3.1-2.4 2.4-1-.3L3.2 10.8l2.5-2.5-.3-1 2.4-2.4 3.1.7 1.7-1.7-.5-2.5zM4.2 12.2 7 9.4l.9.9-2.8 2.8-.9-.9z"/></svg>';
    pin.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      togglePin(s);
    });
    li.appendChild(pin);
  }
  if (onRemove) {
    li.classList.add("has-actions");
    const drop = document.createElement("button");
    drop.type = "button";
    drop.className = "sess-drop";
    drop.setAttribute("aria-label", "Remove from list");
    drop.title = "Remove from wrap list";
    drop.textContent = "×";
    drop.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      onRemove();
    });
    li.appendChild(drop);
  }
  return li;
}

function haySession(s) {
  return [sessionLabel(s), projectName(s.cwd), agentLabel(s.agent), s.snippet || ""]
    .join(" ")
    .toLowerCase();
}

function matchesQuery(s, q) {
  if (!q) return true;
  return haySession(s).includes(q.toLowerCase());
}

function nativePair(s) {
  return `${s.agent || ""}:${s.native_id || s.cli_session || s.oc_id || s.hm_id || ""}`;
}

function renderSessions() {
  const ul = $("sessions");
  ul.innerHTML = "";
  const q = (state.query || "").trim();
  const live = (state.sessions || [])
    .filter((s) => s.live)
    .filter((s) => matchesQuery(s, q) && matchesAgent(s));
  const closedSrc = (q
    ? state.searchHits || (state.history || []).filter((s) => matchesQuery(s, q))
    : state.history || []
  ).filter(matchesAgent);
  const liveKeys = new Set(live.map(nativePair).filter((k) => k !== ":"));
  const pinnedLive = live.filter((s) => s.pinned);
  const restLive = live.filter((s) => !s.pinned);
  const pinnedClosed = closedSrc.filter((s) => s.pinned && !liveKeys.has(nativePair(s)));
  const unpinnedClosed = closedSrc.filter((s) => !s.pinned);
  const searching = Boolean(q) && state.searchHits === null && q.length >= 2;
  let draftShown = false;
  if (
    state.draft &&
    matchesQuery({ title: "New session", cwd: state.cwd, agent: state.agent }, q) &&
    matchesAgent({ agent: state.agent })
  ) {
    draftShown = true;
    const li = document.createElement("li");
    li.className = "draft on";
    li.innerHTML = `<button type="button" class="sess-open"><span class="sess-title">New session</span><span class="sess-meta">${
      state.cwd ? `${escapeHtml(projectName(state.cwd))} · ${escapeHtml(agentLabel(state.agent))}` : "pick project &amp; agent"
    }</span></button>`;
    li.querySelector("button").addEventListener("click", () => openDraft());
    ul.appendChild(li);
  }
  if (!pinnedLive.length && !restLive.length && !pinnedClosed.length && !draftShown && !searching) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = q || filterActive() ? "No matching sessions" : "No active sessions";
    ul.appendChild(empty);
  }
  const openListed = (s) => {
    if (s.live) attachSession(s.id);
    else peekHistory(s);
  };
  for (const s of [...pinnedLive, ...pinnedClosed]) {
    ul.appendChild(sessionRow(s, () => openListed(s)));
  }
  for (const s of restLive) {
    ul.appendChild(sessionRow(s, () => attachSession(s.id)));
  }
  const closedUl = $("closed");
  const closedHead = $("closed-head");
  closedUl.innerHTML = "";
  closedHead.textContent = q ? "Results" : "Closed";
  const noHits =
    Boolean(q) &&
    !searching &&
    !pinnedLive.length &&
    !pinnedClosed.length &&
    !restLive.length &&
    !unpinnedClosed.length;
  const showClosed = unpinnedClosed.length > 0 || searching || noHits;
  closedHead.hidden = !showClosed;
  closedUl.hidden = !showClosed;
  if (searching || noHits) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = searching ? "Searching…" : "No matches";
    closedUl.appendChild(empty);
  }
  for (const s of unpinnedClosed) {
    closedUl.appendChild(sessionRow(s, () => peekHistory(s), () => hideClosed(s)));
  }
  $("btn-menu").classList.toggle("ping", Boolean(state.pingSid));
}

async function togglePin(item) {
  const native = item?.native_id || item?.cli_session || item?.oc_id || item?.hm_id;
  if (!native || !item.agent) return;
  const next = !item.pinned;
  try {
    await api("/api/history/pin", {
      method: "POST",
      body: JSON.stringify({ agent: item.agent, native, pinned: next }),
    });
    await loadSessions();
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

async function loadSessions() {
  try {
    const { sessions, history } = await api("/api/sessions");
    state.sessions = sessions || [];
    state.history = history || [];
    renderSessions();
  } catch (err) {
    $("health").textContent = String(err.message || err);
  }
}

async function hideClosed(item) {
  if (!item?.native_id || !item.agent) return;
  try {
    await api("/api/history/hide", {
      method: "POST",
      body: JSON.stringify({ agent: item.agent, native: item.native_id }),
    });
    const key = nativePair(item);
    state.history = (state.history || []).filter((s) => nativePair(s) !== key);
    if (state.searchHits) {
      state.searchHits = state.searchHits.filter((s) => nativePair(s) !== key);
    }
    const cur = state.session;
    const curNative = cur?.native_id || cur?.cli_session || cur?.oc_id || cur?.hm_id || "";
    if (cur && !cur.live && cur.agent === item.agent && curNative === item.native_id) {
      clearMain();
    } else {
      renderSessions();
    }
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

function clearSearch() {
  state.query = "";
  state.searchHits = null;
  state.searchGen += 1;
  renderSessions();
}

let searchTimer = 0;
async function runSearch(q, gen) {
  if (q.length < 2) {
    if (gen === state.searchGen) {
      state.searchHits = null;
      renderSessions();
    }
    return;
  }
  try {
    const { hits } = await api("/api/history/search?q=" + encodeURIComponent(q));
    if (gen !== state.searchGen) return;
    state.searchHits = hits || [];
    renderSessions();
  } catch (err) {
    if (gen !== state.searchGen) return;
    setStatus(err.message || String(err));
  }
}

function onSearchInput() {
  const q = ($("search").value || "").trim();
  state.query = q;
  state.searchHits = null;
  if (!q) {
    clearSearch();
    return;
  }
  renderSessions();
  const gen = ++state.searchGen;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => runSearch(q, gen), 280);
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
    if (h.hermes_enabled) {
      bits.push(h.hermes ? "hermes ok" : "hermes down");
    }
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
  seedQueued(sess);
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
  closeMenu();
}

function renderMessages(messages) {
  const log = $("log");
  log.innerHTML = "";
  const busy = Boolean(state.session?.busy);
  const choice = state.session?.choice;
  const rows = messages.slice();
  const lastReal = [...rows].reverse().find((m) => !m.pending);
  if (busy && !choice && (!lastReal || lastReal.role === "user")) {
    const streaming = { id: "streaming", role: "assistant", parts: [], text: "" };
    const pendingAt = rows.findIndex((m) => m.pending);
    if (pendingAt >= 0) rows.splice(pendingAt, 0, streaming);
    else rows.push(streaming);
  }
  if (!rows.length && !choice) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = !state.session?.live
      ? "This session is closed. Send a message to resume it."
      : state.session?.agent === "opencode"
        ? "Session is up. Send a message — it goes to OpenCode over HTTP."
        : state.session?.agent === "hermes"
          ? "Session is up. Send a message — it goes to the Hermes gateway."
          : "Waiting for the CLI transcript… send a message, bubbles show up here.";
    log.appendChild(empty);
    return;
  }
  for (const m of rows) {
    const el = document.createElement("article");
    el.className = `msg ${m.role}`;
    const who = document.createElement("div");
    who.className = "who";
    if (m.pending) el.classList.add("pending");
    who.innerHTML =
      (m.role === "assistant" && busy && !choice ? `<span class="busy-dot"></span>` : "") +
      (m.pending ? "queued" : m.role);
    el.appendChild(who);
    const parts = messageParts(m);
    let toolBuf = [];
    const flushTools = () => {
      if (!toolBuf.length) return;
      const tools = document.createElement("div");
      tools.className = "tools";
      tools.textContent = toolBuf.join(" · ");
      el.appendChild(tools);
      toolBuf = [];
    };
    for (const p of parts) {
      if (p.type === "tool") {
        if (p.name) {
          const running = p.status === "running" || p.status === "pending";
          toolBuf.push(running ? `${p.name}…` : p.name);
        }
        continue;
      }
      flushTools();
      if (p.type === "text" && p.text) el.appendChild(renderMessageBody(p.text));
      else if (p.type === "diff") el.appendChild(renderDiffBox(p));
      else if (p.type === "image") el.appendChild(renderImagePart(p));
    }
    flushTools();
    log.appendChild(el);
  }
  if (choice && choice.questions && choice.questions.length) {
    log.appendChild(renderChoice(choice));
  }
  log.scrollTop = log.scrollHeight;
}

function messageParts(m) {
  if (m.parts && m.parts.length) return m.parts;
  const out = [];
  if (m.text) out.push({ type: "text", text: m.text });
  for (const d of m.diffs || []) out.push({ type: "diff", ...d });
  for (const name of m.tools || []) out.push({ type: "tool", name });
  return out;
}

function renderImagePart(p) {
  const img = document.createElement("img");
  img.className = "msg-img";
  img.alt = p.filename || "image";
  let src = p.url || "";
  const path = p.path || "";
  if (path) {
    src = "/api/file?path=" + encodeURIComponent(path);
  } else if (src.startsWith("file://")) {
    let filePath = src.slice("file://".length);
    try {
      filePath = decodeURIComponent(filePath);
    } catch (_) {
      /* keep */
    }
    src = "/api/file?path=" + encodeURIComponent(filePath);
  } else if (src.startsWith("/") && !src.startsWith("/api/")) {
    src = "/api/file?path=" + encodeURIComponent(src);
  }
  img.src = src;
  return img;
}

function renderDiffBox(d) {
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
  return box;
}

function renderChoice(choice) {
  if (!choice) return null;
  const el = document.createElement("article");
  el.className = "msg assistant choice";
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = choice.title || "choose";
  el.appendChild(who);
  const questions = choice.questions;
  const picks = questions.map(() => -1);
  const single = questions.length === 1 && !questions.some((q) => q.multi);
  const compact =
    questions.length === 1 &&
    (questions[0].options || []).length <= 2;
  questions.forEach((q, qi) => {
    const box = document.createElement("div");
    box.className = "choice-q";
    const prompt = document.createElement("div");
    prompt.className = "choice-prompt";
    prompt.textContent = [q.header, q.prompt].filter(Boolean).join(" — ") || "Question";
    box.appendChild(prompt);
    const opts = document.createElement("div");
    opts.className = "choice-opts" + (compact ? " row" : "");
    (q.options || []).forEach((opt, oi) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "choice-opt";
      btn.innerHTML = escapeHtml(opt.label || "") +
        (opt.description ? `<small>${escapeHtml(opt.description)}</small>` : "");
      btn.addEventListener("click", () => {
        picks[qi] = oi;
        opts.querySelectorAll(".choice-opt").forEach((b, i) => b.classList.toggle("on", i === oi));
        if (single) submitChoice(picks);
        else {
          const go = el.querySelector(".choice-submit");
          if (go) go.disabled = picks.some((n) => n < 0);
        }
      });
      opts.appendChild(btn);
    });
    box.appendChild(opts);
    el.appendChild(box);
  });
  if (!single) {
    const go = document.createElement("button");
    go.type = "button";
    go.className = "choice-submit";
    go.textContent = "Submit";
    go.disabled = true;
    go.addEventListener("click", () => submitChoice(picks));
    el.appendChild(go);
  }
  return el;
}

async function submitChoice(picks) {
  if (!state.session || picks.some((n) => n < 0)) return;
  const el = document.querySelector(".msg.choice");
  el?.querySelectorAll("button").forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/sessions/${state.session.id}/choose`, {
      method: "POST",
      body: JSON.stringify({ picks }),
    });
  } catch (err) {
    el?.querySelectorAll("button").forEach((b) => { b.disabled = false; });
    setStatus(err.message || String(err));
  }
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
        const wasBusy = state.session.busy;
        const wasChoice = JSON.stringify(state.session.choice || null);
        state.session.busy = data.busy;
        state.session.pane = data.pane;
        if ("subagents" in data) state.session.subagents = data.subagents;
        if ("choice" in data) state.session.choice = data.choice;
        if (wasBusy !== data.busy || wasChoice !== JSON.stringify(data.choice || null)) {
          renderMessages(mergeMessages(state.session.messages || []));
        }
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

async function spawnSession(cfg) {
  if (state.spawning) return null;
  state.spawning = true;
  savePrefs();
  setStatus("");
  $("btn-send").disabled = true;
  try {
    const sess = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        agent: cfg.agent,
        cwd: cfg.cwd,
        model: cfg.model || "",
        effort: cfg.effort || "",
        fast: Boolean(cfg.fast),
      }),
    });
    renderSession(sess);
    connectStream(sess.id);
    loadSessions();
    return sess;
  } catch (err) {
    setStatus(err.message || String(err));
    applyChrome();
    return null;
  } finally {
    state.spawning = false;
  }
}

async function startSession() {
  if (!state.cwd) {
    setStatus("Pick a project first");
    return null;
  }
  return spawnSession({
    agent: state.agent,
    cwd: state.cwd,
    model: $("model").value,
    effort: $("effort").value,
    fast: $("fast").checked,
  });
}

function sessionConfig(sess) {
  return {
    agent: sess.agent || state.agent,
    cwd: sess.cwd || state.cwd,
    model: sess.model || $("model").value || "",
    effort: sess.effort || $("effort").value || "",
    fast: Boolean(sess.fast),
  };
}

async function clearSession() {
  const sess = state.session;
  if (!sess) return;
  const cfg = sessionConfig(sess);
  if (!cfg.cwd || !cfg.agent) {
    setStatus("Pick a project first");
    return;
  }
  const liveId = sess.live && sess.id && !String(sess.id).startsWith("h:") ? sess.id : "";
  $("btn-clear").disabled = true;
  $("btn-send").disabled = true;
  try {
    if (liveId) {
      try {
        await api(`/api/sessions/${liveId}`, { method: "DELETE" });
      } catch (_) {
        /* already gone */
      }
    }
    if (state.es) {
      state.es.close();
      state.es = null;
    }
    state.session = null;
    state.paneOpen = false;
    clearAttachments();
    state.agent = cfg.agent;
    state.cwd = cfg.cwd;
    fillProjects();
    applyCatalog();
    if ([...$("model").options].some((o) => o.value === (cfg.model || ""))) {
      $("model").value = cfg.model || "";
    }
    if ([...$("effort").options].some((o) => o.value === (cfg.effort || ""))) {
      $("effort").value = cfg.effort || "";
    }
    $("fast").checked = Boolean(cfg.fast);
    const next = await spawnSession(cfg);
    if (next) {
      if (!state.paneOpen) $("input").focus();
    } else {
      state.draft = true;
      applyChrome();
      renderSessions();
    }
  } catch (err) {
    setStatus(err.message || String(err));
    applyChrome();
  } finally {
    $("btn-clear").disabled = false;
  }
}

async function peekHistory(item) {
  if (!item?.native_id || !item.cwd) return;
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  setStatus("");
  state.agent = item.agent;
  state.cwd = item.cwd;
  applyCatalog();
  try {
    const q = new URLSearchParams({
      agent: item.agent,
      native: item.native_id,
      cwd: item.cwd,
    });
    const sess = await api(`/api/history?${q}`);
    renderSession(sess);
    closeMenu();
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

async function wakeClosed(peek) {
  if (!peek?.native_id && !peek?.cli_session && !peek?.oc_id && !peek?.hm_id) return null;
  savePrefs();
  setStatus("");
  $("btn-send").disabled = true;
  try {
    const sess = await api("/api/sessions", {
      method: "POST",
      body: JSON.stringify({
        agent: peek.agent,
        cwd: peek.cwd,
        resume: peek.native_id || peek.cli_session || peek.oc_id || peek.hm_id,
        model: $("model").value,
        effort: $("effort").value,
        fast: $("fast").checked,
        title: peek.title || "",
      }),
    });
    const oldId = peek.id;
    if (oldId && oldId !== sess.id && state.pending[oldId]) {
      state.pending[sess.id] = (state.pending[sess.id] || []).concat(state.pending[oldId]);
      delete state.pending[oldId];
    }
    if (oldId && oldId !== sess.id && state.acked[oldId]) {
      const next = state.acked[sess.id] || new Set();
      for (const t of state.acked[oldId]) next.add(t);
      state.acked[sess.id] = next;
      delete state.acked[oldId];
    }
    persistPending();
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

function userText(m) {
  if (typeof m?.text === "string" && m.text.trim()) return m.text;
  return (m?.parts || [])
    .filter((p) => p && p.type === "text" && p.text)
    .map((p) => p.text)
    .join("\n\n");
}

function sameUserText(a, b) {
  const x = String(a || "").trim();
  const y = String(b || "").trim();
  if (!x || !y) return false;
  if (x === y) return true;
  if (x.endsWith(y) || y.endsWith(x)) return true;
  const shorter = x.length <= y.length ? x : y;
  const longer = x.length <= y.length ? y : x;
  return shorter.length >= 8 && longer.includes(shorter);
}

function snapshotUsers(messages) {
  const users = (messages || []).filter((m) => m.role === "user");
  return {
    afterIds: new Set(users.map((m) => m.id).filter(Boolean)),
    afterCount: users.length,
  };
}

function isOldUser(p, m, i) {
  if (m.id && p.afterIds && p.afterIds.has(m.id)) return true;
  if (!m.id && typeof p.afterCount === "number" && i < p.afterCount) return true;
  return false;
}

function persistPending() {
  try {
    const dump = { pending: {}, acked: {} };
    for (const [sid, items] of Object.entries(state.pending)) {
      if (!items || !items.length) continue;
      dump.pending[sid] = items.map((p) => ({
        id: p.id,
        text: p.text,
        afterIds: p.afterIds ? [...p.afterIds] : [],
        afterCount: p.afterCount || 0,
      }));
    }
    for (const [sid, texts] of Object.entries(state.acked)) {
      const list = texts ? [...texts] : [];
      if (list.length) dump.acked[sid] = list.slice(-50);
    }
    sessionStorage.setItem(PENDING_KEY, JSON.stringify(dump));
  } catch (_) {
    /* ignore */
  }
}

function loadPending() {
  try {
    const dump = JSON.parse(sessionStorage.getItem(PENDING_KEY) || "{}");
    if (!dump || typeof dump !== "object") return;
    const pending = dump.pending && typeof dump.pending === "object" ? dump.pending : dump;
    const acked = dump.acked && typeof dump.acked === "object" ? dump.acked : {};
    for (const [sid, items] of Object.entries(pending)) {
      if (sid === "pending" || sid === "acked") continue;
      if (!Array.isArray(items)) continue;
      state.pending[sid] = items
        .filter((p) => p && p.text)
        .map((p) => ({
          id: p.id || "p-" + Date.now(),
          text: String(p.text || ""),
          afterIds: new Set(p.afterIds || []),
          afterCount: Number(p.afterCount) || 0,
        }));
    }
    for (const [sid, texts] of Object.entries(acked)) {
      if (!Array.isArray(texts)) continue;
      state.acked[sid] = new Set(texts.map(String));
    }
  } catch (_) {
    /* ignore */
  }
}

function ackPending(sid, text) {
  if (!sid || !text) return;
  if (!state.acked[sid]) state.acked[sid] = new Set();
  state.acked[sid].add(text);
}

function seedQueued(sess) {
  const sid = sess?.id;
  if (!sid || (state.pending[sid] && state.pending[sid].length)) return;
  const remote = sess.queued || [];
  if (!remote.length) return;
  const users = (sess.messages || []).filter((m) => m.role === "user");
  const snap = snapshotUsers(sess.messages);
  const acked = state.acked[sid] || new Set();
  const leftover = remote.filter((text) => {
    if (acked.has(text)) return false;
    return !users.some((m) => sameUserText(userText(m), text));
  });
  if (!leftover.length) return;
  state.pending[sid] = leftover.map((text, i) => ({
    id: "q-" + i + "-" + Date.now(),
    text,
    afterIds: snap.afterIds,
    afterCount: snap.afterCount,
  }));
  persistPending();
}

function mergeMessages(serverMsgs) {
  const sid = state.session?.id;
  const pending = sid ? state.pending[sid] || [] : [];
  const users = (serverMsgs || []).filter((m) => m.role === "user");
  const used = new Set();
  const still = [];
  for (const p of pending) {
    const fresh = [];
    for (let i = 0; i < users.length; i++) {
      if (used.has(i) || isOldUser(p, users[i], i)) continue;
      fresh.push(i);
    }
    let hit = fresh.find((i) => sameUserText(userText(users[i]), p.text));
    if (hit === undefined && fresh.length) hit = fresh[0];
    if (hit !== undefined) {
      used.add(hit);
      ackPending(sid, p.text);
    } else still.push(p);
  }
  if (sid) {
    if (still.length) state.pending[sid] = still;
    else delete state.pending[sid];
    persistPending();
  }
  return (serverMsgs || []).concat(
    still.map((p) => ({ id: p.id, role: "user", text: p.text, tools: [], pending: true })),
  );
}

async function sendMessage(ev) {
  ev.preventDefault();
  if (state.sending || state.spawning) return;
  const typed = $("input").value.trim();
  const attached = (state.attachments || []).slice();
  const bits = attached.map((a) => a.path);
  if (typed) bits.push(typed);
  const text = bits.join("\n\n");
  if (!text) return;
  state.sending = true;
  $("btn-send").disabled = true;
  try {
    if (!state.session) {
      const sess = await startSession();
      if (!sess) return;
    } else if (!state.session.live) {
      const sess = await wakeClosed(state.session);
      if (!sess) return;
    }
    $("input").value = "";
    fitInput();
    clearAttachments();
    const sid = state.session.id;
    if (!state.pending[sid]) state.pending[sid] = [];
    const snap = snapshotUsers(state.session.messages || []);
    state.pending[sid].push({ id: "p-" + Date.now(), text, ...snap });
    persistPending();
    renderMessages(mergeMessages(state.session.messages || []));
    try {
      await api(`/api/sessions/${sid}/send`, {
        method: "POST",
        body: JSON.stringify({ text }),
      });
    } catch (err) {
      state.pending[sid] = (state.pending[sid] || []).filter((p) => p.text !== text);
      persistPending();
      $("input").value = typed;
      fitInput();
      state.attachments = attached;
      renderAttach();
      setStatus(err.message || String(err));
      renderMessages(mergeMessages(state.session.messages || []));
    }
  } finally {
    state.sending = false;
    $("btn-send").disabled = !canCompose() || state.paneOpen;
    $("input").disabled = !canCompose() || state.paneOpen;
    if (!state.paneOpen) $("input").focus();
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

function clearAttachments() {
  for (const a of state.attachments || []) {
    if (a.preview) URL.revokeObjectURL(a.preview);
  }
  state.attachments = [];
  const el = $("attach");
  if (el) {
    el.innerHTML = "";
    el.hidden = true;
  }
}

function renderAttach() {
  const el = $("attach");
  el.innerHTML = "";
  const list = state.attachments || [];
  el.hidden = !list.length;
  for (const a of list) {
    const chip = document.createElement("div");
    chip.className = "attach-chip";
    const img = document.createElement("img");
    img.src = a.preview || "/api/file?path=" + encodeURIComponent(a.path);
    img.alt = a.name || "image";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "ghost tiny";
    btn.textContent = "×";
    btn.addEventListener("click", () => {
      if (a.preview) URL.revokeObjectURL(a.preview);
      state.attachments = state.attachments.filter((x) => x !== a);
      renderAttach();
    });
    chip.appendChild(img);
    chip.appendChild(btn);
    el.appendChild(chip);
  }
}

function fileToDataUrl(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result || ""));
    r.onerror = () => reject(r.error || new Error("read failed"));
    r.readAsDataURL(file);
  });
}

function clipboardImages(e) {
  const out = [];
  const cd = e.clipboardData;
  if (!cd) return out;
  for (const item of cd.items || []) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const f = item.getAsFile();
      if (f) out.push(f);
    }
  }
  if (!out.length) {
    for (const f of cd.files || []) {
      if (f.type.startsWith("image/")) out.push(f);
    }
  }
  return out;
}

async function ingestImages(files) {
  const cwd = state.session?.cwd || state.cwd;
  if (!cwd) {
    setStatus("Pick a project first");
    return [];
  }
  const saved = [];
  for (const file of files) {
    if (file.size > 8 * 1024 * 1024) {
      setStatus("Image too large (max 8 MB)");
      continue;
    }
    const dataUrl = await fileToDataUrl(file);
    const data = String(dataUrl).split(",")[1] || "";
    const out = await api("/api/images", {
      method: "POST",
      body: JSON.stringify({ cwd, data, mime: file.type }),
    });
    saved.push({
      path: out.path,
      mime: out.mime,
      preview: URL.createObjectURL(file),
      name: file.name || "image",
    });
  }
  return saved;
}

async function onImages(files) {
  if (!files.length) return;
  try {
    const saved = await ingestImages(files);
    if (!saved.length) return;
    setStatus("");
    if (state.paneOpen && state.session?.tmux) {
      const chunk = saved.map((s) => s.path).join(" ") + " ";
      keys([chunk]);
      for (const s of saved) {
        if (s.preview) URL.revokeObjectURL(s.preview);
      }
      return;
    }
    state.attachments = (state.attachments || []).concat(saved);
    renderAttach();
    if (!state.paneOpen) $("input").focus();
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

function onHash() {
  const id = (location.hash || "#").slice(1);
  if (id === "new") {
    if (!state.draft) openDraft();
    return;
  }
  if (id.startsWith("h:")) {
    if (state.session?.id === id) return;
    const item =
      (state.history || []).find((s) => s.id === id) ||
      (state.searchHits || []).find((s) => s.id === id);
    if (item) peekHistory(item);
    return;
  }
  if (id && (!state.session || state.session.id !== id)) {
    attachSession(id);
    return;
  }
  if (!id && (state.session || state.draft)) clearMain();
}

$("log").addEventListener("click", (e) => {
  const btn = e.target.closest(".code-copy");
  if (!btn) return;
  e.preventDefault();
  onCodeCopy(btn);
});
$("agent").addEventListener("change", () => setAgent($("agent").value));
$("project").addEventListener("change", () => setProject($("project").value));
$("model").addEventListener("change", savePrefs);
$("effort").addEventListener("change", savePrefs);
$("fast").addEventListener("change", savePrefs);
$("btn-new").addEventListener("click", openDraft);
$("search").addEventListener("input", onSearchInput);
$("search").addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("search").value) {
    e.preventDefault();
    e.stopPropagation();
    $("search").value = "";
    clearSearch();
  }
});
$("btn-filter").addEventListener("click", (e) => {
  e.preventDefault();
  e.stopPropagation();
  setFilterOpen($("agent-filter").hidden);
});
document.addEventListener("pointerdown", (e) => {
  if ($("agent-filter").hidden) return;
  if (e.target.closest("#search-row")) return;
  setFilterOpen(false);
});
$("btn-menu").addEventListener("click", () => {
  setMenuOpen(!document.body.classList.contains("menu-open"));
});
$("scrim").addEventListener("click", closeMenu);
narrowMq.addEventListener("change", () => {
  if (!isNarrow()) closeMenu();
});
function fitInput() {
  const el = $("input");
  el.style.height = "0px";
  el.style.height = el.scrollHeight + "px";
}

$("composer").addEventListener("submit", sendMessage);
$("input").addEventListener("input", fitInput);
window.addEventListener("resize", fitInput);
$("input").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    if (state.sending || state.spawning || $("btn-send").disabled) return;
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
$("btn-clear").addEventListener("click", () => {
  clearSession();
});
$("btn-stop").addEventListener("click", async () => {
  if (!state.session) return;
  const stopMsg =
    state.session.agent === "opencode"
      ? "Close this OpenCode session in wrap? It stays in OpenCode history."
      : state.session.agent === "hermes"
        ? "Close this Hermes session in wrap? It stays in Hermes history."
        : "Stop this session? The CLI process exits. Other sessions stay up.";
  if (!confirm(stopMsg)) return;
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
  if (e.key === "Escape" && !$("agent-filter").hidden) {
    e.preventDefault();
    setFilterOpen(false);
    return;
  }
  if (e.key === "Escape" && document.body.classList.contains("menu-open")) {
    e.preventDefault();
    closeMenu();
    return;
  }
  if (e.isComposing) return;
  if (!state.paneOpen || !state.session?.tmux) return;
  if (e.target.closest("button, textarea, input, select")) return;
  const k = mapTuiKey(e);
  if (!k) return;
  e.preventDefault();
  queueKey(k);
});
$("pane").addEventListener("mousedown", () => {
  if (state.paneOpen) $("pane").focus();
});
document.addEventListener(
  "paste",
  (e) => {
    const images = clipboardImages(e);
    if (images.length) {
      e.preventDefault();
      onImages(images);
      return;
    }
    if (state.paneOpen && state.session?.tmux) {
      if (e.target.closest("button, textarea, input, select")) return;
      const t = e.clipboardData?.getData("text") || "";
      if (!t) return;
      e.preventDefault();
      keys([t]);
    }
  },
  true,
);
function bindDrop(el) {
  el.addEventListener("dragover", (e) => {
    if ([...e.dataTransfer.items].some((i) => i.type.startsWith("image/"))) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "copy";
    }
  });
  el.addEventListener("drop", (e) => {
    const files = [...(e.dataTransfer?.files || [])].filter((f) => f.type.startsWith("image/"));
    if (!files.length) return;
    e.preventDefault();
    onImages(files);
  });
}
bindDrop($("chat"));
bindDrop($("composer"));
bindDrop($("tui"));
window.addEventListener("hashchange", onHash);

let audioCtx = null;
function unlockAudio() {
  const C = window.AudioContext || window.webkitAudioContext;
  if (!C) return;
  if (!audioCtx) audioCtx = new C();
  if (audioCtx.state === "suspended") audioCtx.resume();
  if (window.Notification && Notification.permission === "default") {
    Notification.requestPermission().catch(() => {});
  }
}
document.addEventListener("pointerdown", unlockAudio);

function playAlertSound() {
  unlockAudio();
  if (!audioCtx) return;
  const now = audioCtx.currentTime;
  for (const [i, freq] of [880, 1174].entries()) {
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.type = "sine";
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.07, now + 0.02 + i * 0.12);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.18 + i * 0.12);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(now + i * 0.12);
    osc.stop(now + 0.22 + i * 0.12);
  }
}

function onAlert(a) {
  const ts = Number(a.ts) || 0;
  if (ts && Date.now() / 1000 - ts > 30) return;
  const watching =
    document.visibilityState === "visible" && a.sid && state.session?.id === a.sid;
  if (watching) return;
  playAlertSound();
  const title = a.title || "wrap";
  const body = a.body || "";
  if (window.Notification && Notification.permission === "granted") {
    try {
      new Notification(title, { body, tag: a.sid || "wrap" });
    } catch (_) {
      /* ignore */
    }
  }
  const prev = document.title;
  document.title = "● " + title;
  setTimeout(() => {
    if (document.title.startsWith("● ")) document.title = prev;
  }, 4000);
  if (a.sid) {
    state.pingSid = a.sid;
    renderSessions();
    setTimeout(() => {
      if (state.pingSid === a.sid) {
        state.pingSid = "";
        renderSessions();
      }
    }, 2500);
  }
}

function connectAlerts() {
  const es = new EventSource("/api/alerts");
  es.addEventListener("alert", (ev) => {
    try {
      onAlert(JSON.parse(ev.data));
    } catch (_) {
      /* ignore */
    }
  });
}

applyChrome();
loadPending();
syncFilterBtn();
Promise.all([loadCatalog(), loadProjects(), loadHealth(), loadSessions()]).then(() => {
  const id = (location.hash || "#").slice(1);
  if (id) onHash();
  else if (isNarrow()) setMenuOpen(true);
});
connectAlerts();
setInterval(loadHealth, 15000);
setInterval(loadSessions, 8000);
