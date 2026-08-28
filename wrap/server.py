#!/usr/bin/env python3
"""Native-session web wrap: tmux + transcripts (Claude/Cursor), OpenCode HTTP API.

No extra model harness — the CLI process is the only API client.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
from queue import Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import transcripts as tr  # noqa: E402

HOST = os.environ.get("WRAP_HOST", "0.0.0.0")
PORT = int(os.environ.get("WRAP_PORT", "3780"))
HOST_PROJECTS = Path(os.environ.get("HOST_PROJECTS", "/Users/mr/projects")).resolve()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
CURSOR_HOME = Path.home() / ".cursor"
TMUX_SOCK = os.environ.get("WRAP_TMUX_SOCK", "/tmp/wrap.tmux.sock")
OC_PORT = int(os.environ.get("WRAP_OPENCODE_PORT", "4097"))
OC_URL = os.environ.get("WRAP_OPENCODE_URL", f"http://127.0.0.1:{OC_PORT}")
STATIC = ROOT / "static"
STATE_PATH = Path(os.environ.get("WRAP_STATE", "/var/lib/wrap/state.json"))
AGENTS = ("claude", "cursor", "opencode")
# Status-line only. Avoid matching chat text ("thinking") or Claude's idle "⏵⏵ auto mode".
BUSY_RE = re.compile(
    r"esc to interrupt|ctrl\+c to interrupt|ctrl\+c to stop|"
    r"Running\s+\d|Generating\s+\d",
    re.I,
)
CLAUDE_MODELS = [
    {"id": "", "label": "CLI default"},
    {"id": "sonnet", "label": "Sonnet"},
    {"id": "opus", "label": "Opus"},
    {"id": "fable", "label": "Fable"},
    {"id": "haiku", "label": "Haiku"},
]
EFFORT_LEVELS = [
    {"id": "", "label": "Default"},
    {"id": "low", "label": "Low"},
    {"id": "medium", "label": "Medium"},
    {"id": "high", "label": "High"},
    {"id": "xhigh", "label": "Extra high"},
    {"id": "max", "label": "Max"},
]

_lock = threading.RLock()
SESSIONS: dict[str, dict[str, Any]] = {}
_oc_proc: subprocess.Popen[bytes] | None = None
_oc_lock = threading.Lock()
_catalog_lock = threading.Lock()
_catalog_cache: dict[str, Any] = {"at": 0.0, "data": None}
_send_q: dict[str, Queue[str | None]] = {}
_send_workers: dict[str, threading.Thread] = {}


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} {msg}", flush=True)


def new_session_id(agent: str) -> str:
    return f"{agent}-{secrets.token_hex(4)}"


def tmux_name(sid: str) -> str:
    return "wrap_" + sid.replace("-", "_")


def parse_tmux_name(name: str) -> tuple[str, str] | None:
    if not name.startswith("wrap_"):
        return None
    rest = name[len("wrap_") :]
    parts = rest.split("_", 1)
    if len(parts) != 2 or parts[0] not in ("claude", "cursor"):
        return None
    return parts[0], f"{parts[0]}-{parts[1]}"


def safe_cwd(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = HOST_PROJECTS / path
    resolved = path.resolve()
    root = HOST_PROJECTS
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"path outside HOST_PROJECTS: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"not a directory: {resolved}")
    return resolved


def tmux(*args: str, input_bytes: bytes | None = None, check: bool = False) -> subprocess.CompletedProcess[bytes]:
    cmd = ["tmux", "-S", TMUX_SOCK, *args]
    return subprocess.run(cmd, input=input_bytes, capture_output=True, check=check)


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def tmux_ok() -> bool:
    return shutil_which("tmux") is not None


def tmux_has(name: str) -> bool:
    r = tmux("has-session", "-t", name)
    return r.returncode == 0


def tmux_capture(name: str) -> str:
    r = tmux("capture-pane", "-t", name, "-p", "-J")
    if r.returncode != 0:
        return ""
    return tr.strip_ansi(r.stdout.decode("utf-8", "replace"))


def pane_busy(pane: str) -> bool:
    # Cursor keeps "Running Nk tokens" above the current user prompt; keep a deep tail.
    tail = "\n".join((pane or "").splitlines()[-40:])
    return bool(BUSY_RE.search(tail))


def tmux_alive_command(name: str) -> str:
    r = tmux("display-message", "-t", name, "-p", "#{pane_current_command}")
    if r.returncode != 0:
        return ""
    return r.stdout.decode().strip()


def persist_state() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        payload = {"sessions": list(SESSIONS.values())}
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(STATE_PATH)


def load_state() -> None:
    if not STATE_PATH.is_file():
        return
    try:
        data = json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return
    items = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return
    with _lock:
        for sess in items:
            if not isinstance(sess, dict) or not sess.get("id"):
                continue
            SESSIONS[str(sess["id"])] = sess


def session_meta(sess: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": sess["id"],
        "agent": sess["agent"],
        "cwd": sess["cwd"],
        "tmux": sess.get("tmux"),
        "oc_id": sess.get("oc_id"),
        "transcript": sess.get("transcript"),
        "title": sess.get("title") or "",
        "model": sess.get("model") or "",
        "effort": sess.get("effort") or "",
        "fast": bool(sess.get("fast")),
        "created": sess.get("created") or "",
        "continue": bool(sess.get("continue")),
    }


def tmux_pane_title(name: str) -> str:
    r = tmux("display-message", "-t", name, "-p", "#{pane_title}")
    if r.returncode != 0:
        return ""
    raw = tr.strip_ansi(r.stdout.decode("utf-8", "replace")).strip()
    return re.sub(r"^[✳*●○▶►]+\s*", "", raw).strip()


def apply_native_title(
    sess: dict[str, Any],
    *,
    persist: bool = False,
    registry: dict[str, dict[str, str]] | None = None,
) -> bool:
    """Replace wrap's placeholder title with the name the agent assigned."""
    native = ""
    if sess.get("tmux") and tmux_has(str(sess["tmux"])):
        pane_title = tmux_pane_title(str(sess["tmux"]))
        if pane_title and not tr.is_wrap_default_title(pane_title):
            native = pane_title
    if not native:
        path = sess.get("transcript")
        native = tr.native_session_title(
            str(sess.get("agent") or ""),
            transcript=Path(path) if path else None,
            cli_session=str(sess.get("cli_session") or ""),
            oc_id=str(sess.get("oc_id") or ""),
            claude_home=CLAUDE_HOME,
            cursor_home=CURSOR_HOME,
            registry=registry,
        ) or ""
    if not native or sess.get("title") == native:
        return False
    sess["title"] = native
    if persist:
        persist_state()
    return True


def start_tmux(
    sid: str,
    agent: str,
    cwd: Path,
    *,
    continue_session: bool,
    model: str,
    effort: str,
    fast: bool,
    title: str,
    cli_session: str = "",
) -> str:
    name = tmux_name(sid)
    if tmux_has(name):
        return name
    if agent == "claude":
        binary = shutil_which("claude") or "claude"
        args = [binary]
        if model:
            args.extend(["--model", model])
        if effort:
            args.extend(["--effort", effort])
        if title:
            args.extend(["--name", title[:40]])
        if continue_session:
            args.append("--continue")
        elif cli_session:
            args.extend(["--session-id", cli_session])
        extra_env = {
            "CLAUDE_CODE_DISABLE_MOUSE": "1",
            "CLAUDE_CODE_NO_FLICKER": "1",
            "TERM": "xterm-256color",
            "IS_SANDBOX": os.environ.get("IS_SANDBOX", "1"),
        }
    elif agent == "cursor":
        binary = shutil_which("agent") or shutil_which("cursor-agent") or "agent"
        model_arg = cursor_model_arg(model, effort, fast)
        args = [binary, "--trust", "--approve-mcps"]
        if model_arg:
            args.extend(["--model", model_arg])
        if continue_session:
            args.append("--continue")
        extra_env = {
            "TERM": "xterm-256color",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
        }
    else:
        raise ValueError("tmux is only for claude/cursor")

    env_args: list[str] = []
    for k, v in extra_env.items():
        env_args.extend(["-e", f"{k}={v}"])
    r = tmux(
        "new-session",
        "-d",
        "-s",
        name,
        "-c",
        str(cwd),
        "-x",
        "140",
        "-y",
        "48",
        *env_args,
        "--",
        *args,
    )
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"tmux new-session failed: {err or r.returncode}")
    return name


def cursor_model_arg(model: str, effort: str, fast: bool) -> str:
    model = (model or "").strip()
    if not model:
        return ""
    opts: list[str] = []
    if effort:
        opts.append(f"effort={effort}")
    if fast:
        opts.append("fast=true")
    if not opts:
        return model
    if "[" in model:
        return model
    return f"{model}[{','.join(opts)}]"


def tmux_wait_idle(name: str, timeout: float = 180.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not tmux_has(name):
            return False
        if not pane_busy(tmux_capture(name)):
            time.sleep(0.2)
            if not pane_busy(tmux_capture(name)):
                return True
        time.sleep(0.3)
    return False


def tmux_wait_busy(name: str, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pane_busy(tmux_capture(name)):
            return True
        time.sleep(0.15)
    return False


def tmux_send_text(name: str, text: str) -> None:
    """Clear leftover composer text, paste as one block, then submit."""
    tmux("send-keys", "-t", name, "C-u")
    time.sleep(0.08)
    buf = "wrap_" + secrets.token_hex(3)
    r = tmux("load-buffer", "-b", buf, "-", input_bytes=text.encode("utf-8"))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace") or "load-buffer failed")
    r = tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", name)
    if r.returncode != 0:
        tmux("delete-buffer", "-b", buf)
        raise RuntimeError(r.stderr.decode("utf-8", "replace") or "paste-buffer failed")
    time.sleep(0.25)
    tmux("send-keys", "-t", name, "Enter")


def oc_send_text(sess: dict[str, Any], text: str) -> None:
    oc_id = sess.get("oc_id")
    if not oc_id:
        raise RuntimeError("no opencode session")
    body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
    ref = oc_model_ref(str(sess.get("model") or ""), str(sess.get("effort") or ""))
    if ref:
        body["model"] = ref
    try:
        oc_request("POST", f"/session/{oc_id}/prompt_async", Path(sess["cwd"]), body)
    except RuntimeError:
        oc_request("POST", f"/session/{oc_id}/message", Path(sess["cwd"]), body)


def enqueue_send(sid: str, text: str) -> None:
    with _lock:
        q = _send_q.get(sid)
        if q is None:
            q = Queue()
            _send_q[sid] = q
        q.put(text)
        worker = _send_workers.get(sid)
        if worker is None or not worker.is_alive():
            worker = threading.Thread(target=_drain_sends, args=(sid,), daemon=True, name=f"wrap-send-{sid}")
            _send_workers[sid] = worker
            worker.start()


def _drain_sends(sid: str) -> None:
    q = _send_q.get(sid)
    if q is None:
        return
    while True:
        text = q.get()
        if text is None:
            return
        try:
            sess = get_session(sid)
        except KeyError:
            return
        try:
            if sess["agent"] == "opencode":
                oc_send_text(sess, text)
                continue
            name = sess.get("tmux")
            if not name or not tmux_has(name):
                log(f"send dropped, tmux gone {sid}")
                continue
            tmux_wait_idle(name)
            if not tmux_has(name):
                continue
            tmux_send_text(name, text)
            tmux_wait_busy(name)
        except Exception as exc:  # noqa: BLE001
            log(f"send failed {sid}: {exc}")


def stop_send_queue(sid: str) -> None:
    with _lock:
        q = _send_q.pop(sid, None)
        _send_workers.pop(sid, None)
    if q is not None:
        try:
            q.put_nowait(None)
        except Exception:
            pass


def tmux_send_keys(name: str, keys: list[str]) -> None:
    named = {
        "Enter": "Enter",
        "Escape": "Escape",
        "Esc": "Escape",
        "Tab": "Tab",
        "BSpace": "BSpace",
        "Backspace": "BSpace",
        "DC": "DC",
        "Delete": "DC",
        "Left": "Left",
        "Right": "Right",
        "Up": "Up",
        "Down": "Down",
        "Home": "Home",
        "End": "End",
        "PPage": "PPage",
        "NPage": "NPage",
        "C-c": "C-c",
        "C-d": "C-d",
        "C-u": "C-u",
        "C-a": "C-a",
        "C-e": "C-e",
        "C-k": "C-k",
        "C-w": "C-w",
        "C-l": "C-l",
        "C-n": "C-n",
        "C-p": "C-p",
        "Space": "Space",
    }
    literal: list[str] = []

    def flush_literal() -> None:
        if not literal:
            return
        text = "".join(literal)
        literal.clear()
        tmux("send-keys", "-t", name, "-l", "--", text)

    for k in keys:
        if k in named:
            flush_literal()
            tmux("send-keys", "-t", name, named[k])
        elif k:
            literal.append(k)
    flush_literal()


def claimed_transcripts(except_sid: str | None = None) -> set[str]:
    with _lock:
        return {
            str(s["transcript"])
            for s in SESSIONS.values()
            if s.get("transcript") and s.get("id") != except_sid
        }


def pick_transcript(sess: dict[str, Any]) -> Path | None:
    """Bind only a jsonl this wrap session created (or resumed), never a sibling's."""
    agent = sess["agent"]
    if agent == "opencode":
        return None
    cwd = Path(sess["cwd"])
    seen = sess.get("seen_transcripts") or {}
    claimed = claimed_transcripts(sess.get("id"))
    current = sess.get("transcript")
    cli_sid = str(sess.get("cli_session") or "")
    if current and Path(current).is_file():
        if not cli_sid or cli_sid in Path(current).name:
            return Path(current)
    if cli_sid:
        for p in tr.list_transcripts(agent, cwd, CLAUDE_HOME, CURSOR_HOME):
            if cli_sid in p.name and str(p) not in claimed and p.is_file():
                return p
    candidates: list[tuple[float, Path]] = []
    resume = bool(sess.get("continue"))
    for p in tr.list_transcripts(agent, cwd, CLAUDE_HOME, CURSOR_HOME):
        sp = str(p)
        if sp in claimed:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        prev = seen.get(sp)
        if prev is None:
            candidates.append((st.st_mtime, p))
        elif resume and st.st_mtime > float(prev) + 0.05:
            candidates.append((st.st_mtime, p))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def ensure_transcript(sess: dict[str, Any]) -> Path | None:
    if sess.get("agent") == "opencode":
        return None
    found = pick_transcript(sess)
    if not found:
        return None
    if sess.get("transcript") != str(found):
        sess["transcript"] = str(found)
        persist_state()
    return found


def wait_transcript(
    agent: str,
    cwd: Path,
    before: dict[str, float],
    timeout: float = 3.0,
    *,
    continue_session: bool = False,
    cli_session: str = "",
) -> Path | None:
    deadline = time.time() + timeout
    dummy = {
        "agent": agent,
        "cwd": str(cwd),
        "id": "",
        "seen_transcripts": before,
        "continue": continue_session,
        "cli_session": cli_session,
    }
    while time.time() < deadline:
        found = pick_transcript(dummy)
        if found:
            return found
        time.sleep(0.25)
    return None


def snapshot_transcripts(agent: str, cwd: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    for p in tr.list_transcripts(agent, cwd, CLAUDE_HOME, CURSOR_HOME):
        try:
            out[str(p)] = p.stat().st_mtime
        except OSError:
            continue
    return out


def oc_health() -> bool:
    try:
        with urllib.request.urlopen(f"{OC_URL}/global/health", timeout=1) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_opencode_serve() -> None:
    global _oc_proc
    if oc_health():
        return
    with _oc_lock:
        if oc_health():
            return
        binary = shutil_which("opencode")
        if not binary:
            raise RuntimeError("opencode binary missing")
        log_path = Path(os.environ.get("WRAP_OPENCODE_LOG", "/var/log/opencode-serve.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log_path, "ab")
        _oc_proc = subprocess.Popen(
            [binary, "serve", "--hostname", "127.0.0.1", "--port", str(OC_PORT)],
            stdout=fh,
            stderr=fh,
            start_new_session=True,
        )
        for _ in range(40):
            if oc_health():
                log(f"opencode serve ready pid={_oc_proc.pid} port={OC_PORT}")
                return
            time.sleep(0.25)
        raise RuntimeError("opencode serve failed to start (see /var/log/opencode-serve.log)")


def oc_request(method: str, path: str, cwd: Path, body: dict[str, Any] | None = None) -> Any:
    ensure_opencode_serve()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{OC_URL}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "x-opencode-directory": str(cwd),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode())
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"opencode {method} {path}: {exc.code} {err[:400]}") from exc


def oc_create(cwd: Path, title: str) -> str:
    created = oc_request("POST", "/session", cwd, {"title": title or Path(cwd).name})
    if isinstance(created, dict):
        sid = created.get("id") or created.get("sessionID") or (created.get("info") or {}).get("id")
        if sid:
            return str(sid)
    raise RuntimeError("opencode did not return a session id")


def oc_model_ref(model: str, effort: str) -> dict[str, str] | None:
    model = (model or "").strip()
    if not model or "/" not in model:
        return None
    provider, model_id = model.split("/", 1)
    ref: dict[str, str] = {"providerID": provider, "modelID": model_id}
    if effort:
        ref["variant"] = effort
    return ref


def run_lines(cmd: list[str], timeout: float = 20.0) -> list[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    raw = r.stdout.decode("utf-8", "replace")
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def parse_labeled_models(lines: list[str]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for ln in lines:
        if " - " in ln:
            slug, label = ln.split(" - ", 1)
        else:
            slug, label = ln, ln
        slug = slug.strip()
        label = label.strip()
        if not slug or slug.lower().startswith("available") or slug in seen:
            continue
        seen.add(slug)
        out.append({"id": slug, "label": label if label != slug else slug})
    return out


def catalog() -> dict[str, Any]:
    now = time.time()
    with _catalog_lock:
        cached = _catalog_cache["data"]
        if cached and now - float(_catalog_cache["at"]) < 120:
            return cached

    cursor_models = [{"id": "", "label": "CLI default"}]
    cursor_models.extend(parse_labeled_models(run_lines(["agent", "models"])))
    oc_models = [{"id": "", "label": "CLI default"}]
    oc_models.extend(parse_labeled_models(run_lines(["opencode", "models"])))

    data = {
        "claude": {
            "models": CLAUDE_MODELS,
            "effort": EFFORT_LEVELS,
            "fast": False,
        },
        "cursor": {
            "models": cursor_models[:180],
            "effort": EFFORT_LEVELS,
            "fast": True,
        },
        "opencode": {
            "models": oc_models,
            "effort": [
                {"id": "", "label": "Default"},
                {"id": "high", "label": "High"},
                {"id": "max", "label": "Max"},
            ],
            "fast": False,
        },
    }
    with _catalog_lock:
        _catalog_cache["at"] = now
        _catalog_cache["data"] = data
    return data


def list_projects() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if HOST_PROJECTS.is_dir():
        dirs: list[Path] = []
        if (HOST_PROJECTS / ".git").is_dir():
            dirs.append(HOST_PROJECTS)
        try:
            dirs.extend(sorted(p for p in HOST_PROJECTS.iterdir() if p.is_dir() and not p.name.startswith(".")))
        except OSError:
            pass
        seen: set[str] = set()
        with _lock:
            sess_vals = list(SESSIONS.values())
        for p in dirs:
            key = str(p)
            if key in seen:
                continue
            seen.add(key)
            live = []
            for sess in sess_vals:
                if sess.get("cwd") != key:
                    continue
                live.append(
                    {
                        "id": sess["id"],
                        "agent": sess["agent"],
                        "title": sess.get("title") or "",
                        "model": sess.get("model") or "",
                    }
                )
            items.append({"path": key, "name": p.name, "live": live, "n": len(live)})
    return items


def session_public(sess: dict[str, Any]) -> dict[str, Any]:
    apply_native_title(sess, persist=True)
    pane = ""
    busy = False
    cmd = ""
    if sess.get("tmux"):
        pane = tmux_capture(sess["tmux"])
        cmd = tmux_alive_command(sess["tmux"])
        busy = pane_busy(pane)
    messages = load_messages(sess)
    live = bool(sess.get("tmux") and tmux_has(sess["tmux"])) or (
        sess["agent"] == "opencode" and bool(sess.get("oc_id"))
    )
    return {
        **session_meta(sess),
        "pane": pane,
        "busy": busy,
        "command": cmd,
        "messages": messages,
        "live": live,
    }


def load_messages(sess: dict[str, Any]) -> list[dict[str, Any]]:
    if sess["agent"] == "opencode":
        oc_id = sess.get("oc_id")
        if not oc_id:
            return []
        return tr.parse_opencode_session(tr.opencode_db_path(), oc_id)
    path = ensure_transcript(sess)
    if not path:
        return []
    return tr.parse_jsonl(sess["agent"], path)


def fingerprint(sess: dict[str, Any]) -> str:
    if sess["agent"] == "opencode":
        db = tr.opencode_db_path()
        try:
            st = db.stat()
            return f"oc:{sess.get('oc_id')}:{st.st_mtime}:{st.st_size}:{sess.get('title') or ''}"
        except OSError:
            return f"oc:{sess.get('oc_id')}"
    path = ensure_transcript(sess)
    if not path:
        files = tr.list_transcripts(sess["agent"], Path(sess["cwd"]), CLAUDE_HOME, CURSOR_HOME)
        stamp = 0.0
        for p in files:
            try:
                stamp = max(stamp, p.stat().st_mtime)
            except OSError:
                continue
        return f"looking:{len(files)}:{stamp}"
    try:
        st = path.stat()
        return f"{st.st_mtime}:{st.st_size}:{sess.get('title') or ''}"
    except OSError:
        return str(path)


def discover_tmux() -> None:
    if not tmux_ok():
        return
    r = tmux("list-sessions", "-F", "#{session_name}\t#{pane_current_path}")
    if r.returncode != 0:
        return
    found: set[str] = set()
    for line in r.stdout.decode().splitlines():
        if "\t" not in line:
            continue
        name, cwd = line.split("\t", 1)
        parsed = parse_tmux_name(name)
        if not parsed:
            continue
        agent, sid = parsed
        found.add(sid)
        try:
            cwd_path = safe_cwd(cwd)
        except ValueError:
            cwd_path = Path(cwd)
        with _lock:
            existing = SESSIONS.get(sid) or {}
            existing.update(
                {
                    "id": sid,
                    "agent": agent,
                    "cwd": str(cwd_path),
                    "tmux": name,
                }
            )
            SESSIONS[sid] = existing
    with _lock:
        for sid, sess in list(SESSIONS.items()):
            if sess.get("tmux") and sid not in found and sess.get("agent") != "opencode":
                sess["tmux"] = None


def default_title(agent: str, cwd: Path, model: str, effort: str) -> str:
    bits = [cwd.name, agent]
    if model:
        bits.append(model.split("/")[-1][:24])
    if effort:
        bits.append(effort)
    with _lock:
        n = sum(1 for s in SESSIONS.values() if s.get("cwd") == str(cwd) and s.get("agent") == agent)
    bits.append(f"#{n + 1}")
    return " · ".join(bits)


def open_session(
    agent: str,
    cwd: Path,
    *,
    continue_session: bool = False,
    model: str = "",
    effort: str = "",
    fast: bool = False,
    title: str = "",
) -> dict[str, Any]:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent}")
    sid = new_session_id(agent)
    user_title = (title or "").strip()
    title = user_title or default_title(agent, cwd, model, effort)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if agent == "opencode":
        oc_id = oc_create(cwd, user_title or cwd.name)
        sess = {
            "id": sid,
            "agent": "opencode",
            "cwd": str(cwd),
            "tmux": None,
            "oc_id": oc_id,
            "transcript": None,
            "title": title,
            "model": model,
            "effort": effort,
            "fast": False,
            "created": created,
        }
        with _lock:
            SESSIONS[sid] = sess
        persist_state()
        return sess

    if not tmux_ok():
        raise RuntimeError("tmux is not installed — rebuild the agents image")

    before = snapshot_transcripts(agent, cwd)
    cli_session = "" if continue_session or agent != "claude" else str(uuid.uuid4())
    name = start_tmux(
        sid,
        agent,
        cwd,
        continue_session=continue_session,
        model=model,
        effort=effort,
        fast=fast,
        title=user_title,
        cli_session=cli_session,
    )
    path = wait_transcript(
        agent,
        cwd,
        before,
        timeout=4.0,
        continue_session=continue_session,
        cli_session=cli_session,
    )
    sess = {
        "id": sid,
        "agent": agent,
        "cwd": str(cwd),
        "tmux": name,
        "transcript": str(path) if path else None,
        "seen_transcripts": before,
        "cli_session": cli_session,
        "continue": continue_session,
        "oc_id": None,
        "title": title,
        "model": model,
        "effort": effort,
        "fast": fast,
        "created": created,
    }
    with _lock:
        SESSIONS[sid] = sess
    persist_state()
    return sess


def get_session(sid: str) -> dict[str, Any]:
    with _lock:
        sess = SESSIONS.get(sid)
        if sess:
            return sess
    discover_tmux()
    with _lock:
        sess = SESSIONS.get(sid)
    if not sess:
        raise KeyError(sid)
    return sess


def list_sessions(cwd: Path | None = None, agent: str | None = None) -> list[dict[str, Any]]:
    discover_tmux()
    with _lock:
        vals = list(SESSIONS.values())
    registry = tr.claude_registry_names(CLAUDE_HOME)
    changed = False
    out = []
    for sess in vals:
        if cwd is not None and sess.get("cwd") != str(cwd):
            continue
        if agent and sess.get("agent") != agent:
            continue
        if apply_native_title(sess, persist=False, registry=registry):
            changed = True
        pub = {
            **session_meta(sess),
            "busy": False,
            "live": bool(sess.get("tmux") and tmux_has(sess["tmux"]))
            or (sess["agent"] == "opencode" and bool(sess.get("oc_id"))),
        }
        if pub["live"] and sess.get("tmux"):
            pane = tmux_capture(sess["tmux"])
            pub["busy"] = pane_busy(pane)
        out.append(pub)
    if changed:
        persist_state()
    out.sort(key=lambda s: s.get("created") or "", reverse=True)
    return out


def kill_session(sid: str) -> None:
    stop_send_queue(sid)
    sess = get_session(sid)
    if sess.get("tmux"):
        tmux("kill-session", "-t", sess["tmux"])
    with _lock:
        SESSIONS.pop(sid, None)
    persist_state()


def json_bytes(data: Any, status: int = 200) -> tuple[int, bytes, str]:
    return status, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log("%s - %s" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or "0")
        if n > 1_000_000:
            raise ValueError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("expected object")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._static("index.html", "text/html; charset=utf-8")
            if path.startswith("/static/"):
                name = Path(path[len("/static/") :]).name
                ctype = {
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".html": "text/html; charset=utf-8",
                }.get(Path(name).suffix, "application/octet-stream")
                return self._static(name, ctype)
            if path == "/api/health":
                body = {
                    "ok": True,
                    "tmux": tmux_ok(),
                    "opencode": shutil_which("opencode") is not None,
                    "opencode_serve": oc_health(),
                    "host_projects": str(HOST_PROJECTS),
                }
                st, raw, ct = json_bytes(body)
                return self._send(st, raw, ct)
            if path == "/api/catalog":
                st, raw, ct = json_bytes(catalog())
                return self._send(st, raw, ct)
            if path == "/api/projects":
                st, raw, ct = json_bytes({"projects": list_projects()})
                return self._send(st, raw, ct)
            if path == "/api/sessions":
                cwd = None
                if qs.get("cwd"):
                    cwd = safe_cwd(qs["cwd"][0])
                agent = (qs.get("agent") or [None])[0]
                st, raw, ct = json_bytes({"sessions": list_sessions(cwd, agent)})
                return self._send(st, raw, ct)
            m = re.fullmatch(r"/api/sessions/([^/]+)/stream", path)
            if m:
                return self._stream(m.group(1))
            m = re.fullmatch(r"/api/sessions/([^/]+)/pane", path)
            if m:
                sess = get_session(m.group(1))
                st, raw, ct = json_bytes({"pane": tmux_capture(sess["tmux"]) if sess.get("tmux") else ""})
                return self._send(st, raw, ct)
            m = re.fullmatch(r"/api/sessions/([^/]+)", path)
            if m:
                sess = get_session(m.group(1))
                st, raw, ct = json_bytes(session_public(sess))
                return self._send(st, raw, ct)
            self._send(404, b'{"error":"not found"}', "application/json")
        except KeyError:
            self._send(404, b'{"error":"session not found"}', "application/json")
        except Exception as exc:  # noqa: BLE001
            log(f"GET error: {exc}")
            st, raw, ct = json_bytes({"error": str(exc)}, 500)
            self._send(st, raw, ct)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            data = self._read_json()
            if path == "/api/sessions":
                attach = str(data.get("id") or "")
                if attach:
                    sess = get_session(attach)
                    st, raw, ct = json_bytes(session_public(sess), 200)
                    return self._send(st, raw, ct)
                agent = str(data.get("agent") or "")
                cwd = safe_cwd(str(data.get("cwd") or ""))
                sess = open_session(
                    agent,
                    cwd,
                    continue_session=bool(data.get("continue", False)),
                    model=str(data.get("model") or ""),
                    effort=str(data.get("effort") or ""),
                    fast=bool(data.get("fast")),
                    title=str(data.get("title") or ""),
                )
                st, raw, ct = json_bytes(session_public(sess), 201)
                return self._send(st, raw, ct)
            m = re.fullmatch(r"/api/sessions/([^/]+)/send", path)
            if m:
                return self._send_msg(m.group(1), data)
            m = re.fullmatch(r"/api/sessions/([^/]+)/keys", path)
            if m:
                sess = get_session(m.group(1))
                if not sess.get("tmux"):
                    raise RuntimeError("no tmux pane for this session")
                keys = data.get("keys") or []
                if not isinstance(keys, list) or not keys:
                    raise ValueError("keys must be a non-empty list")
                tmux_send_keys(sess["tmux"], [str(k) for k in keys])
                st, raw, ct = json_bytes({"ok": True})
                return self._send(st, raw, ct)
            m = re.fullmatch(r"/api/sessions/([^/]+)/interrupt", path)
            if m:
                sess = get_session(m.group(1))
                if sess["agent"] == "opencode" and sess.get("oc_id"):
                    oc_request("POST", f"/session/{sess['oc_id']}/abort", Path(sess["cwd"]), {})
                elif sess.get("tmux"):
                    tmux_send_keys(sess["tmux"], ["Escape"])
                else:
                    raise RuntimeError("nothing to interrupt")
                st, raw, ct = json_bytes({"ok": True})
                return self._send(st, raw, ct)
            self._send(404, b'{"error":"not found"}', "application/json")
        except KeyError:
            self._send(404, b'{"error":"session not found"}', "application/json")
        except ValueError as exc:
            st, raw, ct = json_bytes({"error": str(exc)}, 400)
            self._send(st, raw, ct)
        except Exception as exc:  # noqa: BLE001
            log(f"POST error: {exc}")
            st, raw, ct = json_bytes({"error": str(exc)}, 500)
            self._send(st, raw, ct)

    def do_DELETE(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        m = re.fullmatch(r"/api/sessions/([^/]+)", path)
        try:
            if not m:
                self._send(404, b'{"error":"not found"}', "application/json")
                return
            kill_session(m.group(1))
            st, raw, ct = json_bytes({"ok": True})
            self._send(st, raw, ct)
        except KeyError:
            self._send(404, b'{"error":"session not found"}', "application/json")
        except Exception as exc:  # noqa: BLE001
            st, raw, ct = json_bytes({"error": str(exc)}, 500)
            self._send(st, raw, ct)

    def _static(self, name: str, content_type: str) -> None:
        path = STATIC / name
        if not path.is_file() or path.resolve().parent != STATIC.resolve():
            self._send(404, b"not found", "text/plain")
            return
        body = path.read_bytes()
        self._send(200, body, content_type)

    def _send_msg(self, sid: str, data: dict[str, Any]) -> None:
        sess = get_session(sid)
        text = str(data.get("text") or "")
        if not text.strip():
            raise ValueError("empty message")
        if sess["agent"] == "opencode":
            if not sess.get("oc_id"):
                raise RuntimeError("no opencode session")
        elif not sess.get("tmux") or not tmux_has(sess["tmux"]):
            raise RuntimeError("tmux session is gone")
        enqueue_send(sid, text)
        st, raw, ct = json_bytes({"ok": True, "queued": True})
        self._send(st, raw, ct)

    def _stream(self, sid: str) -> None:
        sess = get_session(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = ""
        last_pane = None
        try:
            while True:
                try:
                    sess = get_session(sid)
                except KeyError:
                    self.wfile.write(b"event: gone\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                fp = fingerprint(sess)
                pane = tmux_capture(sess["tmux"]) if sess.get("tmux") else ""
                if fp != last:
                    last = fp
                    payload = session_public(sess)
                    chunk = f"event: sync\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                    last_pane = payload.get("pane")
                elif pane != last_pane:
                    last_pane = pane
                    busy = pane_busy(pane)
                    chunk = (
                        "event: pane\ndata: "
                        + json.dumps({"pane": pane, "busy": busy}, ensure_ascii=False)
                        + "\n\n"
                    )
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    load_state()
    discover_tmux()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    log(f"wrap listen {HOST}:{PORT} projects={HOST_PROJECTS}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
