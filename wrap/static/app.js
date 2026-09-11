const $ = (id) => document.getElementById(id);

const AGENT_LABEL = { claude: "Claude", cursor: "Cursor", opencode: "OpenCode", hermes: "Hermes", console: "Console" };
const FILTER_KEY = "wrap.agentFilter";
const CWD_KEY = "wrap.cwd";
const DIFF_SESSION_KEY = "wrap.diffSession";

function readDiffSession() {
  try {
    const raw = localStorage.getItem(DIFF_SESSION_KEY);
    if (raw === null) return true;
    return raw !== "0";
  } catch {
    return true;
  }
}

function readStoredCwd() {
  try {
    return localStorage.getItem(CWD_KEY) || null;
  } catch {
    return null;
  }
}

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
  cwd: readStoredCwd(),
  session: null,
  draft: false,
  es: null,
  paneOpen: false,
  diffOpen: false,
  attachments: [],
  pingSid: "",
  settings: false,
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
  diff: null,
  diffFile: "",
  diffSession: readDiffSession(),
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
      const indent = listItem[1];
      const ordered = /^\d/.test(listItem[2]);
      const start = ordered ? parseInt(listItem[2], 10) || 1 : 1;
      const items = [];
      while (i < lines.length) {
        let j = i;
        while (j < lines.length && !String(lines[j]).trim()) j++;
        if (j >= lines.length) break;
        const m = lines[j].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!m) break;
        if (/^\d/.test(m[2]) !== ordered) break;
        if (m[1].length !== indent.length) break;
        i = j;
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
      const startAttr = ordered && start > 1 ? ` start="${start}"` : "";
      out.push(
        `<${tag}${startAttr}>${items.map((t) => `<li>${inlineMd(t)}</li>`).join("")}</${tag}>`,
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

const TERM_THEME = {
  background: "#0e0f0b",
  foreground: "#c9cbb8",
  cursor: "#c4a35a",
  cursorAccent: "#1a160c",
  selectionBackground: "#34362b",
  black: "#12130f",
  red: "#c45c4a",
  green: "#6f8f62",
  yellow: "#c4a35a",
  blue: "#7a8f9e",
  magenta: "#a67c9e",
  cyan: "#7ea39a",
  white: "#ece7d5",
  brightBlack: "#555748",
  brightRed: "#e07a6a",
  brightGreen: "#8fb88a",
  brightYellow: "#e0c07a",
  brightBlue: "#9bb0be",
  brightMagenta: "#c49bb8",
  brightCyan: "#a0c4bc",
  brightWhite: "#f4f0e0",
};

let tuiX = null;
let shX = null;
let diffTimer = 0;
const rawBuf = { tui: "", sh: "" };
const rawTimer = { tui: 0, sh: 0 };

function hasXterm() {
  return (
    typeof Terminal === "function" &&
    typeof FitAddon !== "undefined" &&
    typeof FitAddon.FitAddon === "function"
  );
}

function isConsole(sess) {
  return (sess || state.session)?.agent === "console";
}

function chatAgent() {
  return state.agent === "console" ? "claude" : state.agent;
}

function activeCwd() {
  return state.cwd || state.session?.cwd || "";
}

function persistCwd(cwd) {
  state.cwd = cwd || null;
  try {
    if (state.cwd) localStorage.setItem(CWD_KEY, state.cwd);
    else localStorage.removeItem(CWD_KEY);
  } catch (_) {
    /* ignore */
  }
}

function isMac() {
  return /Mac|iPhone|iPad/.test(navigator.platform || "");
}

function modSym() {
  return isMac() ? "⌘" : "Ctrl";
}

function isTextField(el) {
  if (!el || el === document.body) return false;
  const tag = (el.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return true;
  return Boolean(el.isContentEditable);
}

function makeTerm(el, kind) {
  const term = new Terminal({
    cursorBlink: true,
    fontFamily: '"IBM Plex Mono", ui-monospace, Menlo, monospace',
    fontSize: 13,
    lineHeight: 1.2,
    theme: TERM_THEME,
    scrollback: 0,
    allowProposedApi: false,
  });
  const fit = new FitAddon.FitAddon();
  term.loadAddon(fit);
  term.open(el);
  term.onData((data) => queueRaw(kind, data));
  const x = { term, fit, el, gen: 0 };
  const ro = new ResizeObserver(() => {
    if (el.offsetParent === null && el.getClientRects().length === 0) return;
    fitResize(kind);
  });
  ro.observe(el);
  return x;
}

function ensureTuiTerm() {
  if (tuiX || !hasXterm()) return tuiX;
  const el = $("tui-term");
  if (!el) return null;
  tuiX = makeTerm(el, "tui");
  return tuiX;
}

function ensureShTerm() {
  if (shX || !hasXterm()) return shX;
  const el = $("console-term");
  if (!el) return null;
  shX = makeTerm(el, "sh");
  return shX;
}

function paintScreen(x, screen) {
  if (!x?.term || !screen) return;
  x.gen += 1;
  const gen = x.gen;
  const cols = Math.max(2, Number(screen.cols) || x.term.cols);
  const rows = Math.max(1, Number(screen.rows) || x.term.rows);
  if (x.term.cols !== cols || x.term.rows !== rows) {
    try {
      x.term.resize(cols, rows);
    } catch (_) {
      /* ignore */
    }
  }
  let lines = String(screen.screen || "").replace(/\r/g, "").split("\n");
  if (lines.length && lines[lines.length - 1] === "") lines.pop();
  if (lines.length > rows) lines = lines.slice(0, rows);
  const text = lines.join("\r\n");
  x.term.reset();
  x.term.write("\x1b[H" + text, () => {
    if (gen !== x.gen) return;
    const cx = Math.min(cols, Math.max(0, Number(screen.cx) || 0) + 1);
    const cy = Math.min(rows, Math.max(0, Number(screen.cy) || 0) + 1);
    x.term.write(`\x1b[${cy};${cx}H`);
  });
}

function queueRaw(kind, data) {
  rawBuf[kind] += data;
  if (!rawTimer[kind]) rawTimer[kind] = setTimeout(() => flushRaw(kind), 16);
}

async function flushRaw(kind) {
  rawTimer[kind] = 0;
  const data = rawBuf[kind];
  rawBuf[kind] = "";
  if (!data) return;
  try {
    if (kind === "tui") {
      if (!state.session?.id || !state.session?.tmux) return;
      await api(`/api/sessions/${state.session.id}/input`, {
        method: "POST",
        body: JSON.stringify({ data }),
      });
    } else {
      if (!isConsole() || !state.session?.id) return;
      await api(`/api/sessions/${state.session.id}/input`, {
        method: "POST",
        body: JSON.stringify({ data }),
      });
    }
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

async function fitResize(kind) {
  const x = kind === "tui" ? tuiX : shX;
  if (!x?.fit) return;
  if (x.el && (x.el.offsetWidth < 40 || x.el.offsetHeight < 40)) return;
  try {
    x.fit.fit();
  } catch (_) {
    return;
  }
  const cols = x.term.cols;
  const rows = x.term.rows;
  if (!cols || !rows) return;
  try {
    if (kind === "tui") {
      if (!state.session?.id || !state.session?.tmux) return;
      const out = await api(`/api/sessions/${state.session.id}/resize`, {
        method: "POST",
        body: JSON.stringify({ cols, rows }),
      });
      if (out.screen) paintScreen(tuiX, out.screen);
    } else {
      if (!isConsole() || !state.session?.id) return;
      const out = await api(`/api/sessions/${state.session.id}/resize`, {
        method: "POST",
        body: JSON.stringify({ cols, rows }),
      });
      if (out.screen) paintScreen(shX, out.screen);
    }
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

function showConsoleTerm(sess) {
  if (!hasXterm()) {
    setStatus("xterm.js failed to load");
    return;
  }
  ensureShTerm();
  if (sess?.screen) paintScreen(shX, sess.screen);
  requestAnimationFrame(() => fitResize("sh"));
  shX?.term?.focus();
}

function stopDiffPoll() {
  if (diffTimer) {
    clearInterval(diffTimer);
    diffTimer = 0;
  }
}

function diffLineHtml(line) {
  return renderDiff(line);
}

function renderGitDiff() {
  const filesEl = $("diff-files");
  const pane = $("diff-pane");
  const branch = $("diff-branch");
  const meta = $("diff-meta");
  const data = state.diff;
  filesEl.innerHTML = "";
  if (!data) {
    branch.textContent = "—";
    meta.textContent = "";
    pane.innerHTML = `<span class="diff-file">Pick a project</span>`;
    return;
  }
  if (!data.ok) {
    branch.textContent = "git";
    meta.textContent = data.error || "unavailable";
    pane.innerHTML = `<span class="diff-del">${escapeHtml(data.error || "not a git repository")}</span>`;
    return;
  }
  branch.textContent = data.branch || "(detached)";
  const bits = [];
  if (data.upstream) bits.push(data.upstream);
  if (data.ahead) bits.push(`↑${data.ahead}`);
  if (data.behind) bits.push(`↓${data.behind}`);
  if (data.stat) bits.push(data.stat);
  const nested = data.nested || [];
  if (nested.length) {
    if (nested.length <= 8) {
      for (const n of nested) {
        let s = n.path;
        if (n.ahead) s += ` ↑${n.ahead}`;
        if (n.behind) s += ` ↓${n.behind}`;
        bits.push(s);
      }
    } else {
      bits.push(`${nested.length} nested`);
    }
  } else if (!(data.files || []).length) {
    bits.push("clean");
  }
  meta.textContent = bits.join(" · ");
  const files = data.files || [];
  if (!files.length) {
    pane.innerHTML = data.scope === "session"
      ? `<span class="diff-file">no edits in this session</span>`
      : `<span class="diff-file">working tree clean</span>`;
    return;
  }
  if (!state.diffFile || !files.some((f) => f.path === state.diffFile)) {
    state.diffFile = files[0].path;
  }
  for (const f of files) {
    const li = document.createElement("li");
    if (f.path === state.diffFile) li.className = "on";
    const st = document.createElement("span");
    st.className = "st " + (f.status === "?" ? "Q" : f.status || "M");
    st.textContent = f.status || "M";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = f.path;
    name.title = f.path;
    li.appendChild(st);
    li.appendChild(name);
    li.addEventListener("click", () => {
      state.diffFile = f.path;
      renderGitDiff();
    });
    filesEl.appendChild(li);
  }
  const cur = files.find((f) => f.path === state.diffFile) || files[0];
  if (cur.binary) {
    pane.innerHTML = `<span class="diff-file">${escapeHtml(cur.path)} (binary)</span>`;
    return;
  }
  if (!cur.diff) {
    pane.innerHTML = `<span class="diff-file">${escapeHtml(cur.path)} — no textual diff</span>`;
    return;
  }
  pane.innerHTML = diffLineHtml(cur.diff) + (cur.truncated ? `\n<span class="diff-file">… truncated</span>` : "");
}

async function loadDiff() {
  const cwd = activeCwd();
  const btn = $("btn-diff");
  const box = $("diff-session");
  const wrap = $("diff-session-wrap");
  const canScope = Boolean(state.session?.id) && !isConsole();
  if (wrap) wrap.hidden = !canScope;
  if (box) box.checked = Boolean(state.diffSession);
  if (!cwd || isConsole()) {
    state.diff = null;
    if (btn) {
      btn.hidden = true;
      btn.classList.remove("on");
    }
    if (state.diffOpen) setDiffOpen(false);
    return;
  }
  try {
    let url = "/api/git?cwd=" + encodeURIComponent(cwd);
    if (canScope && state.diffSession) {
      url += "&sid=" + encodeURIComponent(state.session.id);
    }
    state.diff = await api(url);
    const n = (state.diff?.ok && state.diff.files) ? state.diff.files.length : 0;
    const nestedN = (state.diff?.ok && state.diff.nested) ? state.diff.nested.length : 0;
    const dirtyTotal = Number(state.diff?.dirty_total);
    const dirty = (Number.isFinite(dirtyTotal) ? dirtyTotal : n) > 0 || nestedN > 0;
    if (btn) {
      btn.hidden = !dirty || state.diffOpen || state.paneOpen;
      btn.textContent = n > 1 ? `Diff · ${n}` : "Diff";
    }
    if (state.diffOpen) renderGitDiff();
    if (!dirty && state.diffOpen) setDiffOpen(false);
  } catch (err) {
    state.diff = { ok: false, error: err.message || String(err) };
    if (btn) btn.hidden = true;
  }
}

function setDiffOpen(on) {
  state.diffOpen = Boolean(on) && !isConsole();
  if (state.diffOpen) {
    state.paneOpen = false;
    renderGitDiff();
  }
  applyChrome();
}

function watchDiff() {
  stopDiffPoll();
  if (!activeCwd() || isConsole()) return;
  loadDiff();
  diffTimer = setInterval(loadDiff, 2500);
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
  return loadPrefsFor(chatAgent());
}

function loadPrefsFor(agent) {
  try {
    return JSON.parse(localStorage.getItem(prefsKey(agent)) || "{}");
  } catch {
    return {};
  }
}

function writePrefsFor(agent, prefs) {
  if (!agent || agent === "console") return;
  try {
    localStorage.setItem(
      prefsKey(agent),
      JSON.stringify({
        model: prefs.model || "",
        effort: prefs.effort || "",
        fast: Boolean(prefs.fast),
      }),
    );
  } catch (_) {
    /* ignore */
  }
  if (chatAgent() === agent && !(state.session && state.session.live)) applyCatalog();
}

function savePrefs() {
  writePrefsFor(chatAgent(), {
    model: $("model").value,
    effort: $("effort").value,
    fast: $("fast").checked,
  });
}

function catalogAgents() {
  const agents = state.catalog.agents || [
    { id: "claude", label: "Claude" },
    { id: "cursor", label: "Cursor" },
    { id: "opencode", label: "OpenCode" },
  ];
  return agents.filter((a) => a.id && a.id !== "console");
}

function fillSettings() {
  const root = $("settings-agents");
  if (!root) return;
  root.innerHTML = "";
  for (const a of catalogAgents()) {
    const cat = state.catalog[a.id] || { models: [], effort: [], fast: false };
    const prefs = loadPrefsFor(a.id);
    const card = document.createElement("article");
    card.className = "settings-agent";
    const title = document.createElement("h3");
    title.textContent = a.label || AGENT_LABEL[a.id] || a.id;
    card.appendChild(title);

    const modelLab = document.createElement("label");
    modelLab.append("Model");
    const modelSel = document.createElement("select");
    fillSelect(modelSel, sortModels(cat.models), prefs.model || "");
    modelSel.addEventListener("change", () => {
      writePrefsFor(a.id, { ...loadPrefsFor(a.id), model: modelSel.value });
    });
    modelLab.appendChild(modelSel);
    card.appendChild(modelLab);

    const effortLab = document.createElement("label");
    effortLab.append("Effort");
    const effortSel = document.createElement("select");
    fillSelect(effortSel, cat.effort, prefs.effort || "");
    effortSel.addEventListener("change", () => {
      writePrefsFor(a.id, { ...loadPrefsFor(a.id), effort: effortSel.value });
    });
    effortLab.appendChild(effortSel);
    card.appendChild(effortLab);

    if (cat.fast) {
      const fastLab = document.createElement("label");
      fastLab.className = "check";
      const fast = document.createElement("input");
      fast.type = "checkbox";
      fast.checked = Boolean(prefs.fast);
      fast.addEventListener("change", () => {
        writePrefsFor(a.id, { ...loadPrefsFor(a.id), fast: fast.checked });
      });
      fastLab.appendChild(fast);
      fastLab.append(" Fast");
      card.appendChild(fastLab);
    }
    root.appendChild(card);
  }
}

function openSettings() {
  closeSpotlight();
  state.settings = true;
  fillSettings();
  applyChrome();
  closeMenu();
}

function closeSettings() {
  if (!state.settings) return;
  state.settings = false;
  applyChrome();
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
  const cat = state.catalog[chatAgent()] || { models: [], effort: [], fast: false };
  const prefs = loadPrefsFor(chatAgent());
  const sess = state.session;
  // Closed history peeks have empty model/effort — keep wrap prefs (e.g. Grok 4.6)
  // instead of falling through to Cursor CLI "auto".
  const live = Boolean(sess && sess.live);
  const model = (live ? sess.model : "") || prefs.model || "";
  const effort = (live ? sess.effort : "") || prefs.effort || "";
  const fast = live ? Boolean(sess.fast) : Boolean(prefs.fast);
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
  const current = chatAgent() || el.value;
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
  const current = state.cwd || state.session?.cwd || "";
  for (const id of ["project"]) {
    const el = $(id);
    if (!el) continue;
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
}

function setHash() {
  const next = state.settings
    ? "settings"
    : state.session
      ? state.session.id
      : state.draft
        ? "new"
        : "";
  const cur = (location.hash || "#").slice(1);
  if (cur !== next) history.replaceState(null, "", next ? `#${next}` : location.pathname);
}

function canCompose() {
  if (state.sending || state.spawning || isConsole()) return false;
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
  $("input").disabled = !canCompose() || state.paneOpen || state.diffOpen;
  $("input").placeholder = draft
    ? "Send a message or paste a screenshot to start…"
    : viewing && !running
      ? "Send a message to resume this session…"
      : sess?.agent === "opencode"
        ? "Message OpenCode… paste a screenshot"
        : sess?.agent === "hermes"
          ? "Message Hermes… paste a screenshot"
          : "Message the native CLI… paste a screenshot";
  $("btn-send").disabled = !canCompose() || state.paneOpen || state.diffOpen;
  $("input").disabled = !canCompose() || state.paneOpen || state.diffOpen;
  $("btn-int").hidden = !running || isConsole(sess);
  $("btn-int").title = `Interrupt (${modSym()}+.)`;
  $("btn-clear").hidden = !(viewing && sess?.cwd && sess?.agent && sess.agent !== "console");
  $("btn-stop").hidden = !running || isConsole(sess);
  $("btn-pane").hidden = !(running && sess?.tmux) || isConsole(sess);
  $("btn-pane").title = `TUI (${modSym()}+U)`;
  $("btn-pane").classList.toggle("on", Boolean(state.paneOpen && running && !isConsole(sess) && !state.diffOpen));
  $("btn-diff").title = `Diff (${modSym()}+J)`;
  $("search").title = `Filter sidebar. ${modSym()}K opens Spotlight.`;
  $("btn-new").title = "New session";
  const showTui = running && sess?.tmux && state.paneOpen && !isConsole(sess) && !state.diffOpen;
  if (showTui) {
    $("tui").hidden = false;
    ensureTuiTerm();
  } else {
    $("tui").hidden = true;
  }
  $("chat").hidden = isConsole(sess);
  $("console").hidden = !isConsole(sess);
  $("diff").hidden = !state.diffOpen || isConsole(sess);
  $("composer").hidden = isConsole(sess);
  const nFiles = (state.diff?.ok && state.diff.files) ? state.diff.files.length : 0;
  const dirtyTotal = Number(state.diff?.dirty_total);
  const nDiff = Number.isFinite(dirtyTotal) ? dirtyTotal : nFiles;
  $("btn-diff").hidden = isConsole(sess) || state.diffOpen || state.paneOpen || nDiff === 0;
  if (viewing) {
    if (sess.agent && sess.agent !== "console") $("agent").value = sess.agent;
    fillProjects();
  } else if (draft) {
    $("agent").value = chatAgent();
    fillProjects();
  } else {
    $("log").innerHTML = `<div class="empty">New session — then choose project and agent</div>`;
    state.paneOpen = false;
    $("tui").hidden = true;
  }
  const title = $("mobile-title");
  if (state.settings) title.textContent = "Settings";
  else if (sess) title.textContent = sessionLabel(sess);
  else if (draft) title.textContent = "New session";
  else title.textContent = "wrap";
  $("btn-menu").classList.toggle("ping", Boolean(state.pingSid));
  const settings = Boolean(state.settings);
  $("settings").hidden = !settings;
  $("btn-settings").classList.toggle("on", settings);
  $("btn-settings").setAttribute("aria-pressed", settings ? "true" : "false");
  if (settings) {
    $("session-bar").hidden = true;
    $("chat").hidden = true;
    $("console").hidden = true;
    $("composer").hidden = true;
    $("status").hidden = true;
    $("tui").hidden = true;
    $("diff").hidden = true;
  } else if ($("status").textContent) {
    $("status").hidden = false;
  }
  setHash();
}

function openDraft(opts = {}) {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  state.settings = false;
  state.session = null;
  state.draft = true;
  state.paneOpen = false;
  state.diffOpen = false;
  if (opts.agent && opts.agent !== "console") state.agent = opts.agent;
  else if (state.agent === "console") state.agent = "claude";
  if (opts.cwd) persistCwd(opts.cwd);
  applyCatalog();
  clearAttachments();
  $("log").innerHTML = `<div class="empty">Pick a project and agent, then send a message</div>`;
  setStatus("");
  applyChrome();
  renderSessions();
  watchDiff();
  closeMenu();
  if (opts.cwd && state.agent) $("input").focus();
  else $("project").focus();
}

function clearMain() {
  if (state.es) {
    state.es.close();
    state.es = null;
  }
  state.settings = false;
  state.session = null;
  state.draft = false;
  state.paneOpen = false;
  state.diffOpen = false;
  clearAttachments();
  applyChrome();
  renderSessions();
  watchDiff();
  if (isNarrow()) setMenuOpen(true);
}

function isWrapDefaultTitle(t) {
  return / · (claude|cursor|opencode|hermes|console)( · |$)/i.test(t || "");
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
  if (s.agent === "console" && s.live && s.id) {
    li.classList.add("has-actions");
    const stop = document.createElement("button");
    stop.type = "button";
    stop.className = "sess-stop";
    stop.setAttribute("aria-label", "Stop console");
    stop.title = "Stop console";
    stop.innerHTML =
      '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true"><rect x="3" y="3" width="10" height="10" rx="1.5" fill="currentColor"/></svg>';
    stop.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      stopSession(s);
    });
    li.appendChild(stop);
  } else if (native && s.agent) {
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

function liveSessions() {
  const live = (state.sessions || []).filter((s) => s.live);
  return [...live.filter((s) => s.pinned), ...live.filter((s) => !s.pinned)];
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
    if (state.settings) fillSettings();
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
  if (!agent || agent === "console" || agent === state.agent) {
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
  persistCwd(cwd || null);
  fillProjects();
  applyChrome();
  renderSessions();
  watchDiff();
}

function renderSession(sess) {
  state.settings = false;
  state.session = sess;
  state.draft = false;
  if (sess.agent && sess.agent !== "console") state.agent = sess.agent;
  persistCwd(sess.cwd || state.cwd);
  fillProjects();
  applyCatalog();
  setStatus("");
  seedQueued(sess);
  if (isConsole(sess)) {
    state.paneOpen = false;
    state.diffOpen = false;
    showConsoleTerm(sess);
  } else {
    renderMessages(mergeMessages(sess.messages || []));
    if (sess.tmux && sess.screen && state.paneOpen) {
      ensureTuiTerm();
      paintScreen(tuiX, sess.screen);
      requestAnimationFrame(() => fitResize("tui"));
    } else if (!sess.tmux) {
      setPaneOpen(false);
    }
  }
  applyChrome();
  renderSessions();
  watchDiff();
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
        if (data.screen) state.session.screen = data.screen;
        if ("subagents" in data) state.session.subagents = data.subagents;
        if ("choice" in data) state.session.choice = data.choice;
        if (wasBusy !== data.busy || wasChoice !== JSON.stringify(data.choice || null)) {
          if (!isConsole()) renderMessages(mergeMessages(state.session.messages || []));
        }
      }
      if (data.screen) {
        if (isConsole()) paintScreen(shX, data.screen);
        else if (state.paneOpen) paintScreen(tuiX, data.screen);
      }
      renderSessions();
    } catch (_) {
      /* ignore */
    }
  });
  es.addEventListener("gone", () => {
    es.close();
    if (isConsole()) {
      clearMain();
      loadSessions();
      return;
    }
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
        resume: cfg.resume || "",
        title: cfg.title || "",
        text: cfg.text || "",
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

async function startSession(text) {
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
    text: text || "",
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
    persistCwd(cfg.cwd);
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
  persistCwd(item.cwd);
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

async function wakeClosed(peek, text) {
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
        model: $("model").value || loadPrefsFor(peek.agent).model || "",
        effort: $("effort").value || loadPrefsFor(peek.agent).effort || "",
        fast: $("fast").checked,
        title: peek.title || "",
        text: peek.agent === "cursor" ? text || "" : "",
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
    let viaArgv = false;
    if (!state.session) {
      const sess = await startSession(state.agent === "cursor" ? text : "");
      if (!sess) return;
      viaArgv = state.agent === "cursor" && Boolean(text);
    } else if (!state.session.live) {
      const agent = state.session.agent;
      const sess = await wakeClosed(state.session, agent === "cursor" ? text : "");
      if (!sess) return;
      viaArgv = agent === "cursor" && Boolean(text);
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
  state.paneOpen = Boolean(on) && Boolean(state.session?.tmux) && !isConsole();
  if (state.paneOpen) state.diffOpen = false;
  applyChrome();
  if (state.paneOpen) {
    if (state.session?.screen) paintScreen(tuiX, state.session.screen);
    requestAnimationFrame(() => {
      fitResize("tui");
      tuiX?.term?.focus();
    });
  }
}

function toggleTui() {
  if (!state.session?.live || !state.session?.tmux || isConsole() || state.settings) return;
  setPaneOpen(!state.paneOpen);
}

function toggleDiff() {
  if (isConsole() || state.settings || !activeCwd()) return;
  setDiffOpen(!state.diffOpen);
}

async function interruptSession() {
  if (!state.session?.live || isConsole()) return;
  try {
    await api(`/api/sessions/${state.session.id}/interrupt`, { method: "POST", body: "{}" });
  } catch (err) {
    setStatus(err.message || String(err));
  }
}

function cycleLiveSession(dir) {
  const list = liveSessions();
  if (!list.length) return;
  const cur = state.session?.live ? state.session.id : "";
  let i = list.findIndex((s) => s.id === cur);
  if (i < 0) i = dir > 0 ? -1 : 0;
  const next = list[(i + dir + list.length) % list.length];
  if (next?.id && next.id !== cur) attachSession(next.id);
}

const cmdk = {
  open: false,
  q: "",
  i: 0,
  items: [],
  pick: null,
};

function cmdkHay(parts) {
  return parts.filter(Boolean).join(" ").toLowerCase();
}

function cmdkHit(hay, q) {
  if (!q) return true;
  return hay.includes(q.toLowerCase());
}

function cmdkItems() {
  const q = (cmdk.q || "").trim().toLowerCase();
  const items = [];
  if (cmdk.pick) {
    const src = listedAgents().filter((a) => a.id && a.id !== "console");
    for (const a of src) {
      const label = a.label || agentLabel(a.id);
      if (!cmdkHit(cmdkHay([label, a.id]), q)) continue;
      items.push({
        kind: "agent",
        id: `agent:${a.id}`,
        title: label,
        meta: cmdk.pick.name,
        agent: a.id,
        cwd: cmdk.pick.path,
      });
    }
    return items;
  }
  for (const s of liveSessions()) {
    if (!cmdkHit(cmdkHay([sessionLabel(s), projectName(s.cwd), agentLabel(s.agent), s.snippet]), q)) {
      continue;
    }
    const bits = [agentLabel(s.agent), projectName(s.cwd)].filter(Boolean);
    let busy = "";
    if (s.choice) busy = "choose";
    else if (s.subagents?.length) busy = s.subagents.length === 1 ? "1 subagent" : `${s.subagents.length} subagents`;
    else if (s.busy) busy = "working";
    items.push({
      kind: "session",
      id: s.id,
      title: sessionLabel(s),
      meta: bits.join(" · "),
      busy,
      active: isActiveRow(s),
      sess: s,
    });
  }
  if (
    cmdkHit(cmdkHay(["new session", "draft", state.cwd && projectName(state.cwd), agentLabel(state.agent)]), q)
  ) {
    items.push({
      kind: "draft",
      id: "draft",
      title: "New session",
      meta: state.cwd
        ? `${projectName(state.cwd)} · ${agentLabel(state.agent)}`
        : "pick project & agent",
    });
  }
  for (const p of state.projects || []) {
    if (!cmdkHit(cmdkHay([p.name, p.path]), q)) continue;
    items.push({
      kind: "project",
      id: `proj:${p.path}`,
      title: p.name || projectName(p.path),
      meta: "new session",
      path: p.path,
      name: p.name || projectName(p.path),
    });
  }
  return items;
}

function paintCmdkSelection() {
  const list = $("cmdk-list");
  list.querySelectorAll(".cmdk-item").forEach((el) => {
    const on = Number(el.dataset.i) === cmdk.i;
    el.classList.toggle("on", on);
    el.setAttribute("aria-selected", on ? "true" : "false");
  });
  list.querySelector(".cmdk-item.on")?.scrollIntoView({ block: "nearest" });
}

function renderCmdk() {
  const list = $("cmdk-list");
  const input = $("cmdk-input");
  cmdk.items = cmdkItems();
  if (cmdk.i >= cmdk.items.length) cmdk.i = Math.max(0, cmdk.items.length - 1);
  if (cmdk.i < 0) cmdk.i = 0;
  input.placeholder = cmdk.pick
    ? `New session in ${cmdk.pick.name} — pick agent`
    : "Switch session or start in a project…";
  list.innerHTML = "";
  if (!cmdk.items.length) {
    const empty = document.createElement("div");
    empty.className = "cmdk-empty";
    empty.textContent = cmdk.pick ? "No matching agents" : "No matching sessions or projects";
    list.appendChild(empty);
    return;
  }
  let lastGroup = "";
  cmdk.items.forEach((item, idx) => {
    const group =
      item.kind === "session" ? "Open" : item.kind === "agent" ? "Agent" : "New";
    if (group !== lastGroup) {
      lastGroup = group;
      const head = document.createElement("div");
      head.className = "cmdk-head";
      head.textContent = group;
      list.appendChild(head);
    }
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cmdk-item" + (idx === cmdk.i ? " on" : "");
    btn.setAttribute("role", "option");
    btn.setAttribute("aria-selected", idx === cmdk.i ? "true" : "false");
    btn.dataset.i = String(idx);
    const title = document.createElement("span");
    title.className = "cmdk-title";
    title.textContent = item.title;
    const meta = document.createElement("span");
    meta.className = "cmdk-meta";
    if (item.busy) {
      meta.innerHTML = `<span class="busy">${escapeHtml(item.busy)}</span> · ${escapeHtml(item.meta || "")}`;
    } else {
      meta.textContent = item.meta || "";
    }
    btn.appendChild(title);
    btn.appendChild(meta);
    btn.addEventListener("mousemove", () => {
      if (cmdk.i === idx) return;
      cmdk.i = idx;
      paintCmdkSelection();
    });
    btn.addEventListener("mousedown", (e) => e.preventDefault());
    btn.addEventListener("click", () => runCmdk(item));
    list.appendChild(btn);
  });
  paintCmdkSelection();
}

function openSpotlight() {
  if (state.settings) closeSettings();
  setFilterOpen(false);
  closeMenu();
  cmdk.open = true;
  cmdk.q = "";
  cmdk.pick = null;
  $("cmdk").hidden = false;
  $("cmdk-input").value = "";
  cmdk.items = cmdkItems();
  const active = cmdk.items.findIndex((it) => it.active);
  cmdk.i = active >= 0 ? active : 0;
  renderCmdk();
  const m = modSym();
  $("cmdk-hint").innerHTML = `<span>↑↓ / Ctrl+N/P</span><span>↵ open</span><span>esc close</span><span>⌥J/K sessions</span><span>${m}K search</span><span>${m}. interrupt</span>`;
  requestAnimationFrame(() => $("cmdk-input").focus());
}

function closeSpotlight() {
  if (!cmdk.open) return;
  cmdk.open = false;
  cmdk.pick = null;
  $("cmdk").hidden = true;
  if (state.paneOpen) tuiX?.term?.focus();
  else if (!state.diffOpen && !$("input").disabled) $("input").focus();
}

function moveCmdk(dir) {
  if (!cmdk.items.length) return;
  cmdk.i = (cmdk.i + dir + cmdk.items.length) % cmdk.items.length;
  paintCmdkSelection();
}

function runCmdk(item) {
  if (!item) return;
  if (item.kind === "session") {
    closeSpotlight();
    if (item.sess?.live && item.sess.id !== state.session?.id) attachSession(item.sess.id);
    return;
  }
  if (item.kind === "draft") {
    closeSpotlight();
    openDraft();
    return;
  }
  if (item.kind === "project") {
    cmdk.pick = { path: item.path, name: item.name };
    cmdk.q = "";
    cmdk.i = 0;
    $("cmdk-input").value = "";
    renderCmdk();
    $("cmdk-input").focus();
    return;
  }
  if (item.kind === "agent") {
    closeSpotlight();
    openDraft({ cwd: item.cwd, agent: item.agent });
  }
}

function cmdkBack() {
  if (!cmdk.pick) {
    closeSpotlight();
    return;
  }
  cmdk.pick = null;
  cmdk.q = "";
  cmdk.i = 0;
  $("cmdk-input").value = "";
  renderCmdk();
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
  if (id === "settings") {
    if (!state.settings) openSettings();
    return;
  }
  if (state.settings) {
    state.settings = false;
    applyChrome();
  }
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
$("btn-console").addEventListener("click", async () => {
  if (!state.cwd) {
    setStatus("Pick a project first");
    return;
  }
  await spawnSession({ agent: "console", cwd: state.cwd });
});
$("btn-diff").addEventListener("click", () => setDiffOpen(!state.diffOpen));
$("btn-diff-close").addEventListener("click", () => setDiffOpen(false));
$("btn-diff-refresh").addEventListener("click", () => loadDiff());
$("diff-session").addEventListener("change", () => {
  state.diffSession = $("diff-session").checked;
  try {
    localStorage.setItem(DIFF_SESSION_KEY, state.diffSession ? "1" : "0");
  } catch (_) {
    /* ignore */
  }
  loadDiff();
});
$("model").addEventListener("change", savePrefs);
$("effort").addEventListener("change", savePrefs);
$("fast").addEventListener("change", savePrefs);
$("btn-new").addEventListener("click", openDraft);
$("btn-settings").addEventListener("click", () => {
  if (state.settings) closeSettings();
  else openSettings();
});
$("btn-settings-close").addEventListener("click", closeSettings);
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
$("btn-int").addEventListener("click", () => interruptSession());
$("btn-clear").addEventListener("click", () => {
  clearSession();
});
async function stopSession(sess) {
  if (!sess?.id) return;
  const stopMsg = isConsole(sess)
    ? "Stop this console? The shell exits and it disappears from the list."
    : sess.agent === "opencode"
      ? "Close this OpenCode session in wrap? It stays in OpenCode history."
      : sess.agent === "hermes"
        ? "Close this Hermes session in wrap? It stays in Hermes history."
        : "Stop this session? The CLI process exits. Other sessions stay up.";
  if (!confirm(stopMsg)) return;
  try {
    await api(`/api/sessions/${sess.id}`, { method: "DELETE" });
    if (state.session?.id === sess.id) clearMain();
    loadSessions();
  } catch (err) {
    setStatus(err.message || String(err));
  }
}
function stopCurrentSession() {
  return stopSession(state.session);
}
$("btn-stop").addEventListener("click", () => stopCurrentSession());
$("btn-pane").addEventListener("click", () => setPaneOpen(!state.paneOpen));
$("tui").addEventListener("click", (e) => {
  if (e.target.closest("button")) return;
  if (state.paneOpen) tuiX?.term?.focus();
});
$("cmdk").addEventListener("click", (e) => {
  if (e.target === $("cmdk")) closeSpotlight();
});
$("cmdk-input").addEventListener("input", () => {
  cmdk.q = $("cmdk-input").value;
  cmdk.i = 0;
  renderCmdk();
});
$("cmdk-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") {
    e.preventDefault();
    moveCmdk(1);
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    moveCmdk(-1);
  } else if (e.ctrlKey && !e.metaKey && !e.altKey && e.key.toLowerCase() === "n") {
    e.preventDefault();
    moveCmdk(1);
  } else if (e.ctrlKey && !e.metaKey && !e.altKey && e.key.toLowerCase() === "p") {
    e.preventDefault();
    moveCmdk(-1);
  } else if (e.key === "Tab") {
    e.preventDefault();
    moveCmdk(e.shiftKey ? -1 : 1);
  } else if (e.key === "Enter") {
    e.preventDefault();
    runCmdk(cmdk.items[cmdk.i]);
  } else if (e.key === "Backspace" && !$("cmdk-input").value && cmdk.pick) {
    e.preventDefault();
    cmdkBack();
  }
});
function isModKey(e) {
  return e.metaKey || e.ctrlKey;
}
function isAltLetter(e, letter) {
  return e.altKey && !e.metaKey && !e.ctrlKey && e.code === "Key" + letter;
}
function onGlobalKey(e) {
  // Option+J/K on Mac sets e.key to ∆/º — match the physical key instead.
  if (isAltLetter(e, "J") || isAltLetter(e, "K")) {
    e.preventDefault();
    e.stopPropagation();
    if (e.type === "keydown" && !cmdk.open) {
      cycleLiveSession(isAltLetter(e, "J") ? 1 : -1);
    }
    return;
  }
  if (e.type !== "keydown") return;
  if (e.isComposing) return;
  const mod = isModKey(e);
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;

  if (cmdk.open) {
    if (mod && !e.altKey && key === "k") {
      e.preventDefault();
      e.stopPropagation();
      closeSpotlight();
      return;
    }
    if (e.ctrlKey && !e.metaKey && !e.altKey && (key === "n" || key === "p")) {
      e.preventDefault();
      e.stopPropagation();
      moveCmdk(key === "n" ? 1 : -1);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      cmdkBack();
    }
    return;
  }

  if (mod && !e.altKey && !e.shiftKey && key === "k") {
    e.preventDefault();
    e.stopPropagation();
    openSpotlight();
    return;
  }

  if (!mod && !e.altKey && e.key === "/" && !isTextField(e.target) && !state.paneOpen) {
    e.preventDefault();
    e.stopPropagation();
    openSpotlight();
    return;
  }

  if (mod && !e.altKey && e.key === ".") {
    e.preventDefault();
    e.stopPropagation();
    interruptSession();
    return;
  }

  if (mod && !e.altKey && !e.shiftKey && key === "u") {
    e.preventDefault();
    e.stopPropagation();
    toggleTui();
    return;
  }

  if (mod && !e.altKey && !e.shiftKey && key === "j") {
    e.preventDefault();
    e.stopPropagation();
    toggleDiff();
    return;
  }

  if (e.key !== "Escape") return;
  if (!$("agent-filter").hidden) {
    e.preventDefault();
    e.stopPropagation();
    setFilterOpen(false);
    return;
  }
  if (document.body.classList.contains("menu-open")) {
    e.preventDefault();
    e.stopPropagation();
    closeMenu();
    return;
  }
  if (state.diffOpen) {
    e.preventDefault();
    e.stopPropagation();
    setDiffOpen(false);
    return;
  }
  if (state.paneOpen) return;
  if (state.session?.live && state.session.busy && !isConsole()) {
    e.preventDefault();
    e.stopPropagation();
    interruptSession();
  }
}
document.addEventListener("keydown", onGlobalKey, true);
document.addEventListener("keypress", onGlobalKey, true);
document.addEventListener("keydown", (e) => {
  if (e.isComposing || cmdk.open) return;
  if (!state.paneOpen || !state.session?.tmux) return;
  if (hasXterm()) return;
  if (e.target.closest("button, textarea, input, select")) return;
  const k = mapTuiKey(e);
  if (!k) return;
  e.preventDefault();
  queueKey(k);
});
$("tui-term").addEventListener("mousedown", () => {
  if (state.paneOpen) tuiX?.term?.focus();
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
      if (hasXterm()) return;
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
  watchDiff();
});
connectAlerts();
setInterval(loadHealth, 15000);
setInterval(loadSessions, 8000);
