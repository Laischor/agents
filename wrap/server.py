#!/usr/bin/env python3
"""Native-session web wrap: tmux + transcripts (Claude/Cursor), OpenCode HTTP API.

No extra model harness — Claude/Cursor stay on the live CLI; OpenCode uses
`opencode serve` REST + SSE (the same surface as the TUI).
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from queue import Queue
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import opencode as oc  # noqa: E402
import transcripts as tr  # noqa: E402

HOST = os.environ.get("WRAP_HOST", "0.0.0.0")
PORT = int(os.environ.get("WRAP_PORT", "3780"))
HOST_PROJECTS = Path(os.environ.get("HOST_PROJECTS", "/Users/mr/projects")).resolve()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
CURSOR_HOME = Path.home() / ".cursor"
TMUX_SOCK = os.environ.get("WRAP_TMUX_SOCK", "/tmp/wrap.tmux.sock")
STATIC = ROOT / "static"
STATE_PATH = Path(os.environ.get("WRAP_STATE", "/var/lib/wrap/state.json"))
AGENTS = ("claude", "cursor", "opencode")
# Status-line only. Avoid matching chat text ("thinking") or Claude's idle "⏵⏵ auto mode".
BUSY_RE = re.compile(
    r"esc to interrupt|esc to cancel|ctrl\+c to interrupt|ctrl\+c to stop|"
    r"Running\s+\d|Generating\s+\d",
    re.I,
)
_ALERT_URGENT_RE = re.compile(
    r"permission|needs your permission|ask.?user|waiting for your permission",
    re.I,
)
ALERT_SETTLE_SEC = 6.0
ALERT_REPLAY_SEC = 45.0
BUSY_HOLD_SEC = 2.5
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
HIDDEN: set[str] = set()
_catalog_lock = threading.Lock()
_catalog_cache: dict[str, Any] = {"at": 0.0, "data": None}
_send_q: dict[str, Queue[str | None]] = {}
_send_workers: dict[str, threading.Thread] = {}
_sending: set[str] = set()
_busy_hold: dict[str, float] = {}
_alerts: deque[dict[str, Any]] = deque(maxlen=80)
_alert_cv = threading.Condition()
_alert_seq = 0
_alert_last: dict[str, float] = {}
_alert_timers: dict[str, threading.Timer] = {}
_alert_timer_lock = threading.Lock()


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


def resolve_alert_sid(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    parsed = parse_tmux_name(raw)
    if parsed:
        return parsed[1]
    return raw


def _alert_urgent(title: str, body: str, sid: str) -> bool:
    if _ALERT_URGENT_RE.search(f"{title} {body}"):
        return True
    if not sid:
        return True
    with _lock:
        sess = SESSIONS.get(sid)
    return bool(sess and session_choice(sess))


def _cancel_alert_timer(sid: str) -> None:
    with _alert_timer_lock:
        timer = _alert_timers.pop(sid, None)
    if timer is not None:
        timer.cancel()


def _emit_alert(item: dict[str, Any]) -> dict[str, Any]:
    global _alert_seq
    sid = str(item.get("sid") or "")
    key = sid or str(item.get("title") or "")
    now = time.time()
    if key and now - _alert_last.get(key, 0) < 1.2:
        return {}
    _alert_last[key] = now
    item = {**item, "ts": now}
    with _alert_cv:
        _alert_seq += 1
        item["seq"] = _alert_seq
        _alerts.append(item)
        _alert_cv.notify_all()
    return item


def _schedule_done_alert(sid: str, item: dict[str, Any]) -> None:
    def flush() -> None:
        with _alert_timer_lock:
            if _alert_timers.get(sid) is not timer:
                return
            _alert_timers.pop(sid, None)
        with _lock:
            sess = SESSIONS.get(sid)
        if sess and not session_choice(sess) and (
            session_is_working(sess, hold=False) or session_recently_wrote(sess)
        ):
            _schedule_done_alert(sid, item)
            return
        _emit_alert(item)

    timer = threading.Timer(ALERT_SETTLE_SEC, flush)
    timer.daemon = True
    with _alert_timer_lock:
        old = _alert_timers.pop(sid, None)
        _alert_timers[sid] = timer
    if old is not None:
        old.cancel()
    timer.start()


def push_alert(title: str, body: str, sid: str = "") -> dict[str, Any]:
    sid = resolve_alert_sid(sid)
    sess_title = ""
    with _lock:
        sess = SESSIONS.get(sid) if sid else None
        if sess:
            sess_title = str(sess.get("title") or "")
    item = {
        "sid": sid,
        "title": (title or sess_title or "wrap")[:80],
        "body": (body or "")[:160],
        "ts": time.time(),
    }
    if sid and not _alert_urgent(title, body, sid):
        _schedule_done_alert(sid, item)
        return item
    if sid:
        _cancel_alert_timer(sid)
    return _emit_alert(item)


def install_cmux_shim() -> None:
    """Copy wrap's cmux onto /usr/local/bin so wrap sessions skip the host bridge.

    The wrap tree is often a read-only bind mount, so we cannot chmod/symlink
    the source. Cursor also sanitizes PATH and drops WRAP_SESSION_ID — hooks
    must hit this installed binary, which detects wrap via the TMUX socket.
    """
    shim_src = ROOT / "bin" / "cmux"
    dest = Path("/usr/local/bin/cmux")
    backup = Path("/usr/local/bin/cmux.agents-host")
    if not shim_src.is_file():
        return
    marker = b"wrap tmux sessions alert the browser"
    try:
        src = shim_src.read_bytes()
        if dest.is_file() and not dest.is_symlink():
            try:
                if dest.read_bytes() == src:
                    return
            except OSError:
                pass
        if dest.exists() or dest.is_symlink():
            current = b""
            if dest.is_file() and not dest.is_symlink():
                try:
                    current = dest.read_bytes()
                except OSError:
                    current = b""
            if marker not in current and not backup.exists():
                dest.replace(backup)
            else:
                dest.unlink()
        dest.write_bytes(src)
        dest.chmod(0o755)
        log(f"cmux shim → wrap alerts ({dest})")
    except OSError as exc:
        log(f"cmux shim skip: {exc}")


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


IMAGE_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
IMAGE_MAGIC = {
    "image/png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda b: b[:3] == b"\xff\xd8\xff",
    "image/gif": lambda b: b.startswith(b"GIF87a") or b.startswith(b"GIF89a"),
    "image/webp": lambda b: len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WEBP",
}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BODY = 12 * 1024 * 1024
PASTE_MAX_AGE = 24 * 3600
_paste_sweep_at = 0.0


def sniff_image(data: bytes) -> str:
    for mime, check in IMAGE_MAGIC.items():
        if check(data):
            return mime
    raise ValueError("not a recognized image (png, jpeg, gif, webp)")


def paste_dir(cwd: Path) -> Path:
    d = cwd / ".wrap-pastes"
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        gi.write_text("*\n")
    return d


def cleanup_paste_dir(d: Path, now: float | None = None) -> int:
    if not d.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - PASTE_MAX_AGE
    n = 0
    try:
        names = list(d.iterdir())
    except OSError:
        return 0
    for p in names:
        if p.name == ".gitignore" or not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


def iter_paste_dirs() -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    roots: list[Path] = []
    if HOST_PROJECTS.is_dir():
        roots.append(HOST_PROJECTS)
        try:
            for p in HOST_PROJECTS.iterdir():
                if not p.is_dir() or p.name.startswith("."):
                    continue
                roots.append(p)
                try:
                    roots.extend(
                        q for q in p.iterdir() if q.is_dir() and not q.name.startswith(".")
                    )
                except OSError:
                    pass
        except OSError:
            pass
    with _lock:
        for sess in SESSIONS.values():
            cwd = sess.get("cwd")
            if cwd:
                roots.append(Path(str(cwd)))
    for root in roots:
        d = root / ".wrap-pastes"
        try:
            if not d.is_dir():
                continue
            key = str(d.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def sweep_pastes(force: bool = False) -> None:
    global _paste_sweep_at
    now = time.time()
    if not force and now - _paste_sweep_at < 3600:
        return
    _paste_sweep_at = now
    n = 0
    for d in iter_paste_dirs():
        n += cleanup_paste_dir(d, now)
    if n:
        log(f"wrap-pastes pruned {n} files older than 24h")


def _paste_sweeper() -> None:
    while True:
        time.sleep(3600)
        try:
            sweep_pastes(force=True)
        except Exception as exc:  # noqa: BLE001
            log(f"wrap-pastes sweep: {exc}")


def save_paste_image(cwd: Path, raw_b64: str) -> dict[str, Any]:
    blob_s = raw_b64.strip()
    if blob_s.startswith("data:"):
        blob_s = blob_s.split(",", 1)[-1]
    try:
        data = base64.b64decode(blob_s, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("invalid image data") from exc
    if not data:
        raise ValueError("empty image")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image too large (max 8 MB)")
    mime = sniff_image(data)
    dest_dir = paste_dir(cwd)
    cleanup_paste_dir(dest_dir)
    dest = dest_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}{IMAGE_EXTS[mime]}"
    dest.write_bytes(data)
    sweep_pastes()
    return {"path": str(dest), "mime": mime, "bytes": len(data)}


def safe_paste_file(raw: str) -> Path:
    p = Path(raw).expanduser().resolve()
    root = HOST_PROJECTS.resolve()
    if p != root and root not in p.parents:
        raise ValueError("path outside HOST_PROJECTS")
    if ".wrap-pastes" not in p.parts:
        raise ValueError("not a paste file")
    if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
        raise ValueError("not an image")
    if not p.is_file():
        raise ValueError("not found")
    return p


def tmux(*args: str, input_bytes: bytes | None = None, check: bool = False) -> subprocess.CompletedProcess[bytes]:
    cmd = ["tmux", "-S", TMUX_SOCK, *args]
    return subprocess.run(cmd, input=input_bytes, capture_output=True, check=check)


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _oc_warmup() -> None:
    try:
        oc.ensure_serve()
    except Exception as exc:  # noqa: BLE001
        log(f"opencode warmup: {exc}")


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


def session_recently_wrote(sess: dict[str, Any], sec: float = ALERT_SETTLE_SEC) -> bool:
    """True if the native transcript was just written — another round may be incoming."""
    if sess.get("agent") == "opencode":
        return False
    raw = sess.get("transcript")
    path = Path(raw) if raw else pick_transcript(sess)
    if not path or not path.is_file():
        return False
    try:
        return time.time() - path.stat().st_mtime < sec
    except OSError:
        return False


def _hold_busy(sid: str, busy: bool) -> bool:
    now = time.time()
    if busy:
        _busy_hold[sid] = now + BUSY_HOLD_SEC
        return True
    return now < _busy_hold.get(sid, 0)


def session_is_working(sess: dict[str, Any], pane: str = "", *, hold: bool = True) -> bool:
    """True while the agent is mid-turn (pane, transcript, send queue, subagents)."""
    sid = str(sess.get("id") or "")
    busy = False
    if sid and sid in _sending:
        busy = True
    if sess.get("tmux"):
        text = pane if pane else tmux_capture(str(sess["tmux"]))
        busy = busy or pane_busy(text)
    elif sess.get("agent") == "opencode" and sess.get("oc_id"):
        busy = busy or oc.session_busy(str(sess["oc_id"]), sess.get("cwd") or "")
    if not busy and sess.get("agent") == "claude":
        raw = sess.get("transcript")
        path = Path(raw) if raw else pick_transcript(sess)
        if path and path.is_file():
            busy = tr.claude_turn_open(path)
    if not busy:
        busy = bool(session_subagents(sess))
    if hold and sid:
        return _hold_busy(sid, busy)
    return busy


def session_subagents(sess: dict[str, Any]) -> list[dict[str, Any]]:
    if sess.get("agent") == "opencode" and sess.get("oc_id"):
        return oc.subagents(str(sess["oc_id"]), sess.get("cwd") or "")
    if sess.get("agent") != "claude":
        return []
    raw = sess.get("transcript")
    path = Path(raw) if raw else pick_transcript(sess)
    if not path or not path.is_file():
        return []
    if sess.get("transcript") != str(path):
        sess["transcript"] = str(path)
    return tr.claude_subagents(path)


def session_choice(sess: dict[str, Any]) -> dict[str, Any] | None:
    agent = str(sess.get("agent") or "")
    if agent == "opencode" and sess.get("oc_id"):
        return oc.pending_choice(str(sess["oc_id"]), sess.get("cwd") or "")
    if agent not in ("claude", "cursor"):
        return None
    raw = sess.get("transcript")
    path = Path(raw) if raw else pick_transcript(sess)
    if not path or not path.is_file():
        return None
    if sess.get("transcript") != str(path):
        sess["transcript"] = str(path)
    return tr.pending_choice(agent, path)


def send_choice_keys(name: str, picks: list[int]) -> None:
    """Drive the CLI select UI: arrows to the option, Tab between questions, Enter."""
    for i, opt in enumerate(picks):
        keys = ["Up"] * 8 + ["Down"] * max(opt, 0)
        keys.append("Tab" if i < len(picks) - 1 else "Enter")
        tmux_send_keys(name, keys)
        if i < len(picks) - 1:
            time.sleep(0.15)


def apply_oc_choice(sess: dict[str, Any], choice: dict[str, Any], picks: list[int]) -> None:
    oc_id = str(sess.get("oc_id") or "")
    cwd = Path(sess["cwd"])
    kind = str(choice.get("kind") or "")
    questions = choice.get("questions") or []
    if kind == "permission":
        opts = (questions[0] or {}).get("options") or []
        reply = str((opts[picks[0]] or {}).get("reply") or "")
        if reply not in ("once", "always", "reject"):
            reply = ("once", "always", "reject")[picks[0]]
        oc.reply_permission(oc_id, str(choice["id"]), cwd, reply)
        return
    if kind == "question":
        answers: list[list[str]] = []
        for i, n in enumerate(picks):
            opts = (questions[i] or {}).get("options") or []
            label = str((opts[n] or {}).get("label") or "")
            answers.append([label] if label else [])
        oc.reply_question(str(choice["id"]), cwd, answers)
        return
    raise RuntimeError("unknown OpenCode choice")


def tmux_alive_command(name: str) -> str:
    r = tmux("display-message", "-t", name, "-p", "#{pane_current_command}")
    if r.returncode != 0:
        return ""
    return r.stdout.decode().strip()


def persist_state() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        payload = {"sessions": list(SESSIONS.values()), "hidden": sorted(HIDDEN)}
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
    if not isinstance(data, dict):
        return
    items = data.get("sessions")
    hidden = data.get("hidden")
    with _lock:
        HIDDEN.clear()
        if isinstance(hidden, list):
            for key in hidden:
                if isinstance(key, str) and ":" in key:
                    HIDDEN.add(key)
        if not isinstance(items, list):
            return
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
        "cli_session": sess.get("cli_session") or "",
        "native_id": sess.get("oc_id") or sess.get("cli_session") or "",
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
    if sess.get("agent") == "opencode" and sess.get("oc_id"):
        native = oc.inferred_title(str(sess["oc_id"]), sess.get("cwd") or "")
    elif sess.get("tmux") and tmux_has(str(sess["tmux"])):
        pane_title = tmux_pane_title(str(sess["tmux"]))
        if pane_title and not tr.is_wrap_default_title(pane_title):
            native = pane_title
    if not native and sess.get("agent") != "opencode":
        path = sess.get("transcript")
        native = tr.native_session_title(
            str(sess.get("agent") or ""),
            transcript=Path(path) if path else None,
            cli_session=str(sess.get("cli_session") or ""),
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
    model: str,
    effort: str,
    fast: bool,
    title: str,
    cli_session: str = "",
    resume_id: str = "",
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
        if title and not resume_id:
            args.extend(["--name", title[:40]])
        if resume_id:
            args.extend(["--resume", resume_id])
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
        if resume_id:
            args.append(f"--resume={resume_id}")
        extra_env = {
            "TERM": "xterm-256color",
            "DISPLAY": os.environ.get("DISPLAY", ":0"),
        }
    else:
        raise ValueError("tmux is only for claude/cursor")

    hook_bin = str(ROOT / "bin")
    path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if hook_bin not in path.split(":"):
        path = hook_bin + ":" + path
    extra_env["PATH"] = path
    extra_env["WRAP_SESSION_ID"] = sid
    extra_env["WRAP_URL"] = f"http://127.0.0.1:{PORT}"

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
    cwd = Path(sess["cwd"])
    oc.wait_idle(str(oc_id), cwd)
    oc.prompt_async(
        str(oc_id),
        cwd,
        text,
        model=str(sess.get("model") or ""),
        effort=str(sess.get("effort") or ""),
    )


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
            with _lock:
                _sending.add(sid)
            try:
                tmux_wait_idle(name)
                if not tmux_has(name):
                    continue
                tmux_send_text(name, text)
                tmux_wait_busy(name)
            finally:
                with _lock:
                    _sending.discard(sid)
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
    with _lock:
        _sending.discard(sid)
        _busy_hold.pop(sid, None)
    _cancel_alert_timer(sid)


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
            if cli_sid in p.name or cli_sid in p.parent.name:
                if str(p) not in claimed and p.is_file():
                    return p
    candidates: list[tuple[float, Path]] = []
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
    cli_session: str = "",
) -> Path | None:
    deadline = time.time() + timeout
    dummy = {
        "agent": agent,
        "cwd": str(cwd),
        "id": "",
        "seen_transcripts": before,
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
    oc_models = oc.providers(HOST_PROJECTS if HOST_PROJECTS.is_dir() else None)
    if len(oc_models) <= 1:
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
    cmd = ""
    if sess.get("tmux"):
        pane = tmux_capture(sess["tmux"])
        cmd = tmux_alive_command(sess["tmux"])
    messages = load_messages(sess)
    live = bool(sess.get("tmux") and tmux_has(sess["tmux"])) or (
        sess["agent"] == "opencode" and bool(sess.get("oc_id"))
    )
    subagents = session_subagents(sess)
    choice = session_choice(sess)
    busy = session_is_working(sess, pane) or bool(subagents) or bool(choice)
    return {
        **session_meta(sess),
        "pane": pane,
        "busy": busy,
        "subagents": subagents,
        "choice": choice,
        "command": cmd,
        "messages": messages,
        "live": live,
    }


def load_messages(sess: dict[str, Any]) -> list[dict[str, Any]]:
    if sess["agent"] == "opencode":
        oc_id = sess.get("oc_id")
        if not oc_id:
            return []
        return oc.list_messages(str(oc_id), sess.get("cwd") or "")
    path = ensure_transcript(sess)
    if not path:
        return []
    return tr.parse_jsonl(sess["agent"], path)


def fingerprint(sess: dict[str, Any]) -> str:
    if sess["agent"] == "opencode":
        oc_id = str(sess.get("oc_id") or "")
        cwd = sess.get("cwd") or ""
        choice = session_choice(sess)
        cid = (choice or {}).get("id") or ""
        msgs = load_messages(sess)
        last = msgs[-1] if msgs else {}
        return (
            f"oc:{oc_id}:{len(msgs)}:{last.get('id')}:{len(last.get('text') or '')}:"
            f"{int(oc.session_busy(oc_id, cwd))}:{cid}:{sess.get('title') or ''}"
        )
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
        subs = ",".join(s["id"] for s in session_subagents(sess))
        ch = (session_choice(sess) or {}).get("id") or ""
        return f"{st.st_mtime}:{st.st_size}:{sess.get('title') or ''}:sa:{subs}:ch:{ch}"
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
    dropped = False
    with _lock:
        for sid, sess in list(SESSIONS.items()):
            if sess.get("agent") == "opencode":
                continue
            if sid not in found:
                SESSIONS.pop(sid, None)
                dropped = True
    if dropped:
        persist_state()


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


def native_key(sess: dict[str, Any]) -> tuple[str, str]:
    agent = str(sess.get("agent") or "")
    if agent == "opencode":
        return agent, str(sess.get("oc_id") or "")
    native = str(sess.get("cli_session") or "")
    if not native:
        path = str(sess.get("transcript") or "")
        if path:
            p = Path(path)
            native = p.stem if agent == "claude" else p.parent.name
    return agent, native


def find_live_native(agent: str, native_id: str, cwd: Path) -> dict[str, Any] | None:
    native_id = (native_id or "").strip()
    if not native_id:
        return None
    with _lock:
        vals = list(SESSIONS.values())
    for sess in vals:
        if sess.get("agent") != agent:
            continue
        if str(sess.get("cwd") or "") != str(cwd):
            continue
        _, got = native_key(sess)
        if got != native_id:
            continue
        if agent == "opencode" and sess.get("oc_id"):
            return sess
        if sess.get("tmux") and tmux_has(str(sess["tmux"])):
            return sess
    return None


def hide_key(agent: str, native_id: str) -> str:
    return f"{agent}:{native_id}"


def history_hidden() -> set[tuple[str, str]]:
    with _lock:
        keys = list(HIDDEN)
    out: set[tuple[str, str]] = set()
    for key in keys:
        if ":" not in key:
            continue
        agent, native = key.split(":", 1)
        if agent and native:
            out.add((agent, native))
    return out


def hide_history(agent: str, native_id: str) -> None:
    agent = (agent or "").strip()
    native_id = (native_id or "").strip()
    if agent not in AGENTS or not native_id:
        raise ValueError("agent and native required")
    with _lock:
        HIDDEN.add(hide_key(agent, native_id))
    persist_state()


def live_native_skip() -> tuple[set[tuple[str, str]], set[str]]:
    skip: set[tuple[str, str]] = set()
    with _lock:
        vals = list(SESSIONS.values())
    for sess in vals:
        live = bool(sess.get("tmux") and tmux_has(str(sess.get("tmux") or ""))) or (
            sess.get("agent") == "opencode" and bool(sess.get("oc_id"))
        )
        if not live:
            continue
        key = native_key(sess)
        if key[1]:
            skip.add(key)
    oc_skip = {key[1] for key in skip if key[0] == "opencode"}
    return skip, oc_skip


def list_history() -> list[dict[str, Any]]:
    projects = [Path(p["path"]) for p in list_projects()]
    skip, oc_skip = live_native_skip()
    hidden = history_hidden()
    skip |= hidden
    oc_skip |= {n for a, n in hidden if a == "opencode"}
    cli = tr.list_native_history(
        projects,
        claude_home=CLAUDE_HOME,
        cursor_home=CURSOR_HOME,
        host_projects=HOST_PROJECTS,
        skip=skip,
    )
    try:
        oc_rows = oc.history_rows(projects, skip=oc_skip)
    except RuntimeError:
        oc_rows = []
    merged = cli + oc_rows
    merged.sort(key=lambda item: float(item.get("updated") or 0), reverse=True)
    return merged[:80]


def _history_blob(item: dict[str, Any]) -> str:
    cwd = str(item.get("cwd") or "")
    name = Path(cwd).name if cwd else ""
    return " ".join(
        [
            str(item.get("title") or ""),
            cwd,
            name,
            str(item.get("agent") or ""),
            str(item.get("native_id") or ""),
        ]
    ).lower()


def _fill_history_title(item: dict[str, Any]) -> None:
    if item.get("title") or str(item.get("agent") or "") == "opencode":
        return
    path = Path(item["transcript"]) if item.get("transcript") else None
    named = tr.native_session_title(
        str(item.get("agent") or ""),
        transcript=path,
        cli_session=str(item.get("native_id") or "") if item.get("agent") == "claude" else "",
        claude_home=CLAUDE_HOME,
        cursor_home=CURSOR_HOME,
    )
    if named:
        item["title"] = named


def search_history(query: str, limit: int = 40) -> list[dict[str, Any]]:
    q = " ".join((query or "").lower().split())
    if len(q) < 2:
        return []
    projects = [Path(p["path"]) for p in list_projects()]
    skip, oc_skip = live_native_skip()
    hidden = history_hidden()
    skip |= hidden
    oc_skip |= {n for a, n in hidden if a == "opencode"}
    cli = tr.list_native_history(
        projects,
        claude_home=CLAUDE_HOME,
        cursor_home=CURSOR_HOME,
        host_projects=HOST_PROJECTS,
        skip=skip,
        limit=2000,
        titles=False,
    )
    try:
        oc_rows = oc.history_rows(projects, skip=oc_skip, limit=2000)
    except RuntimeError:
        oc_rows = []
    merged = cli + oc_rows
    merged.sort(key=lambda item: float(item.get("updated") or 0), reverse=True)
    registry = tr.claude_registry_names(CLAUDE_HOME)
    for item in merged:
        if item.get("title") or str(item.get("agent") or "") != "claude":
            continue
        named = (registry.get(str(item.get("native_id") or "")) or {}).get("name") or ""
        if named and not tr.is_wrap_default_title(named):
            item["title"] = named
    hits: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for item in merged:
        if q in _history_blob(item):
            row = dict(item)
            row["snippet"] = ""
            hits.append(row)
            if len(hits) >= limit:
                break
        else:
            rest.append(item)
    deadline = time.time() + 3.0
    if len(hits) < limit:
        for item in rest:
            if time.time() > deadline:
                break
            path = item.get("transcript")
            if not path:
                continue
            found, snippet = tr.scan_transcript(Path(path), q)
            if not found:
                continue
            row = dict(item)
            row["snippet"] = snippet
            hits.append(row)
            if len(hits) >= limit:
                break
    for row in hits:
        _fill_history_title(row)
    return hits


def history_transcript(agent: str, cwd: Path, native_id: str) -> Path | None:
    native_id = (native_id or "").strip()
    if not native_id:
        return None
    for path in tr.list_transcripts(agent, cwd, CLAUDE_HOME, CURSOR_HOME):
        if agent == "claude" and path.stem == native_id:
            return path
        if agent == "cursor" and path.parent.name == native_id:
            return path
    return None


def history_public(agent: str, native_id: str, cwd: Path) -> dict[str, Any]:
    """Read a closed native session without starting the CLI."""
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent}")
    native_id = (native_id or "").strip()
    if not native_id:
        raise ValueError("native_id required")
    messages: list[dict[str, Any]] = []
    transcript = ""
    title = ""
    if agent == "opencode":
        messages = oc.list_messages(native_id, cwd)
        title = oc.inferred_title(native_id, cwd)
    else:
        path = history_transcript(agent, cwd, native_id)
        if not path:
            raise KeyError(native_id)
        transcript = str(path)
        messages = tr.parse_jsonl(agent, path)
        title = (
            tr.native_session_title(
                agent,
                transcript=path,
                cli_session=native_id if agent == "claude" else "",
                claude_home=CLAUDE_HOME,
                cursor_home=CURSOR_HOME,
            )
            or ""
        )
    if title and tr.is_wrap_default_title(title):
        title = ""
    return {
        "id": f"h:{agent}:{native_id}",
        "agent": agent,
        "cwd": str(cwd),
        "tmux": None,
        "oc_id": native_id if agent == "opencode" else "",
        "transcript": transcript,
        "title": title,
        "model": "",
        "effort": "",
        "fast": False,
        "created": "",
        "cli_session": native_id if agent in ("claude", "cursor") else "",
        "native_id": native_id,
        "pane": "",
        "busy": False,
        "subagents": [],
        "choice": None,
        "command": "",
        "messages": messages,
        "live": False,
    }


def open_session(
    agent: str,
    cwd: Path,
    *,
    model: str = "",
    effort: str = "",
    fast: bool = False,
    title: str = "",
    resume_id: str = "",
) -> dict[str, Any]:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent}")
    resume_id = (resume_id or "").strip()
    if resume_id:
        existing = find_live_native(agent, resume_id, cwd)
        if existing:
            return existing
    sid = new_session_id(agent)
    user_title = (title or "").strip()
    title = user_title or default_title(agent, cwd, model, effort)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if agent == "opencode":
        oc_id = resume_id or oc.create_session(cwd, user_title)
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
            "cli_session": "",
        }
        with _lock:
            SESSIONS[sid] = sess
        persist_state()
        return sess

    if not tmux_ok():
        raise RuntimeError("tmux is not installed — rebuild the agents image")

    before = snapshot_transcripts(agent, cwd)
    cli_session = resume_id if resume_id else (str(uuid.uuid4()) if agent == "claude" else "")
    name = start_tmux(
        sid,
        agent,
        cwd,
        model=model,
        effort=effort,
        fast=fast,
        title=user_title,
        cli_session=cli_session if not resume_id else "",
        resume_id=resume_id,
    )
    path = wait_transcript(
        agent,
        cwd,
        before,
        timeout=4.0,
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
        live = bool(sess.get("tmux") and tmux_has(sess["tmux"])) or (
            sess["agent"] == "opencode" and bool(sess.get("oc_id"))
        )
        if not live:
            continue
        subagents = session_subagents(sess)
        choice = session_choice(sess)
        pub = {
            **session_meta(sess),
            "busy": False,
            "live": True,
            "subagents": subagents,
            "choice": choice,
        }
        if sess.get("tmux"):
            pane = tmux_capture(sess["tmux"])
            pub["busy"] = session_is_working(sess, pane) or bool(subagents) or bool(choice)
        elif sess.get("agent") == "opencode" and sess.get("oc_id"):
            pub["busy"] = session_is_working(sess) or bool(subagents) or bool(choice)
        elif subagents or choice:
            pub["busy"] = True
        out.append(pub)
    if changed:
        persist_state()
    out.sort(key=lambda s: s.get("created") or "", reverse=True)
    return out


def kill_session(sid: str) -> None:
    stop_send_queue(sid)
    sess = get_session(sid)
    if sess.get("agent") == "opencode" and sess.get("oc_id"):
        try:
            oc.abort(str(sess["oc_id"]), Path(sess["cwd"]))
        except RuntimeError:
            pass
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

    def _read_json(self, max_n: int = 1_000_000) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or "0")
        if n > max_n:
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
                    "opencode_serve": oc.health(),
                    "host_projects": str(HOST_PROJECTS),
                }
                st, raw, ct = json_bytes(body)
                return self._send(st, raw, ct)
            if path == "/api/alerts":
                return self._alerts_stream()
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
                st, raw, ct = json_bytes(
                    {"sessions": list_sessions(cwd, agent), "history": list_history()}
                )
                return self._send(st, raw, ct)
            if path == "/api/history/search":
                q = str((qs.get("q") or [""])[0] or "")
                st, raw, ct = json_bytes({"hits": search_history(q)})
                return self._send(st, raw, ct)
            if path == "/api/history":
                agent = str((qs.get("agent") or [""])[0] or "")
                native = str((qs.get("native") or [""])[0] or "")
                cwd = safe_cwd(str((qs.get("cwd") or [""])[0] or ""))
                st, raw, ct = json_bytes(history_public(agent, native, cwd))
                return self._send(st, raw, ct)
            if path == "/api/file":
                raw_path = (qs.get("path") or [""])[0]
                p = safe_paste_file(raw_path)
                ctype = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".gif": "image/gif",
                    ".webp": "image/webp",
                }[p.suffix.lower()]
                body = p.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(body)
                return
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
        except ValueError as exc:
            st, raw, ct = json_bytes({"error": str(exc)}, 400)
            self._send(st, raw, ct)
        except Exception as exc:  # noqa: BLE001
            log(f"GET error: {exc}")
            st, raw, ct = json_bytes({"error": str(exc)}, 500)
            self._send(st, raw, ct)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            max_n = MAX_IMAGE_BODY if path == "/api/images" else 1_000_000
            data = self._read_json(max_n)
            if path == "/api/images":
                cwd = safe_cwd(str(data.get("cwd") or ""))
                saved = save_paste_image(cwd, str(data.get("data") or ""))
                st, raw, ct = json_bytes(saved, 201)
                return self._send(st, raw, ct)
            if path == "/api/internal/notify":
                item = push_alert(
                    str(data.get("title") or "wrap"),
                    str(data.get("body") or ""),
                    str(data.get("sid") or ""),
                )
                st, raw, ct = json_bytes({"ok": True, **item})
                return self._send(st, raw, ct)
            if path == "/api/history/hide":
                hide_history(str(data.get("agent") or ""), str(data.get("native") or ""))
                st, raw, ct = json_bytes({"ok": True})
                return self._send(st, raw, ct)
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
                    model=str(data.get("model") or ""),
                    effort=str(data.get("effort") or ""),
                    fast=bool(data.get("fast")),
                    title=str(data.get("title") or ""),
                    resume_id=str(data.get("resume") or ""),
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
                    oc.abort(str(sess["oc_id"]), Path(sess["cwd"]))
                elif sess.get("tmux"):
                    tmux_send_keys(sess["tmux"], ["Escape"])
                else:
                    raise RuntimeError("nothing to interrupt")
                st, raw, ct = json_bytes({"ok": True})
                return self._send(st, raw, ct)
            m = re.fullmatch(r"/api/sessions/([^/]+)/choose", path)
            if m:
                sess = get_session(m.group(1))
                choice = session_choice(sess)
                if not choice:
                    raise ValueError("no pending choice")
                questions = choice.get("questions") or []
                picks = data.get("picks")
                if picks is None and data.get("option") is not None:
                    picks = [data.get("option")]
                if not isinstance(picks, list) or len(picks) != len(questions):
                    raise ValueError("picks must have one index per question")
                idxs: list[int] = []
                for i, raw in enumerate(picks):
                    try:
                        n = int(raw)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("picks must be integers") from exc
                    opts = (questions[i] or {}).get("options") or []
                    if n < 0 or n >= len(opts):
                        raise ValueError("option out of range")
                    idxs.append(n)
                if sess["agent"] == "opencode":
                    apply_oc_choice(sess, choice, idxs)
                else:
                    if not sess.get("tmux"):
                        raise RuntimeError("no tmux pane for this session")
                    send_choice_keys(str(sess["tmux"]), idxs)
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

    def _alerts_stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        now = time.time()
        raw_id = (self.headers.get("Last-Event-ID") or "").strip()
        with _alert_cv:
            last = int(_alert_seq)
            backlog = list(_alerts)
        if raw_id.isdigit():
            want = int(raw_id)
            missed = [
                a
                for a in backlog
                if int(a.get("seq") or 0) > want
                and now - float(a.get("ts") or 0) <= ALERT_REPLAY_SEC
            ]
            if missed:
                last = want
        try:
            self.wfile.write(b": ping\n\n")
            self.wfile.flush()
            while True:
                with _alert_cv:
                    items = [a for a in _alerts if int(a.get("seq") or 0) > last]
                    if not items:
                        _alert_cv.wait(timeout=20)
                        items = [a for a in _alerts if int(a.get("seq") or 0) > last]
                if items:
                    last = int(items[-1]["seq"])
                    for a in items:
                        seq = int(a.get("seq") or 0)
                        chunk = (
                            f"id: {seq}\n"
                            f"event: alert\n"
                            f"data: {json.dumps(a, ensure_ascii=False)}\n\n"
                        )
                        self.wfile.write(chunk.encode("utf-8"))
                else:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

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
                if sess.get("agent") == "opencode":
                    payload = session_public(sess)
                    fp = json.dumps(
                        {
                            "t": payload.get("title"),
                            "b": payload.get("busy"),
                            "c": (payload.get("choice") or {}).get("id"),
                            "s": [x.get("id") for x in (payload.get("subagents") or [])],
                            "m": [
                                (
                                    m.get("id"),
                                    len(m.get("text") or ""),
                                    tuple(
                                        (p.get("type"), p.get("name"), p.get("status"), len(p.get("text") or ""))
                                        for p in (m.get("parts") or [])
                                    ),
                                )
                                for m in (payload.get("messages") or [])
                            ],
                        },
                        ensure_ascii=False,
                    )
                    if fp != last:
                        last = fp
                        chunk = f"event: sync\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                    oc.wait(str(sess.get("oc_id") or ""), timeout=0.35 if payload.get("busy") else 1.2)
                    continue
                fp = fingerprint(sess)
                pane = tmux_capture(sess["tmux"]) if sess.get("tmux") else ""
                subagents = session_subagents(sess)
                busy = session_is_working(sess, pane) or bool(subagents)
                pane_key = (pane, busy, tuple(s["id"] for s in subagents))
                if fp != last:
                    last = fp
                    payload = session_public(sess)
                    chunk = f"event: sync\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                    last_pane = (
                        payload.get("pane"),
                        bool(payload.get("busy")),
                        tuple(s.get("id") for s in (payload.get("subagents") or [])),
                    )
                elif pane_key != last_pane:
                    last_pane = pane_key
                    chunk = (
                        "event: pane\ndata: "
                        + json.dumps(
                            {"pane": pane, "busy": busy, "subagents": subagents},
                            ensure_ascii=False,
                        )
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
    install_cmux_shim()
    if shutil_which("opencode"):
        threading.Thread(target=_oc_warmup, daemon=True, name="wrap-oc-warmup").start()
    threading.Thread(target=_paste_sweeper, daemon=True, name="wrap-pastes").start()
    try:
        sweep_pastes(force=True)
    except Exception as exc:  # noqa: BLE001
        log(f"wrap-pastes sweep: {exc}")
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
