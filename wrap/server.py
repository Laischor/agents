#!/usr/bin/env python3
"""Native-session web wrap: headless CLI turns, Hermes API, PTY console."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import select
import signal
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

import hermes as hm  # noqa: E402
import opencode as oc  # noqa: E402
import ptyio  # noqa: E402
import titles  # noqa: E402
import transcripts as tr  # noqa: E402
import workspace as ws  # noqa: E402

HOST = os.environ.get("WRAP_HOST", "0.0.0.0")
PORT = int(os.environ.get("WRAP_PORT", "3000"))
HOST_PROJECTS = Path(os.environ.get("HOST_PROJECTS", "/Users/mr/projects")).resolve()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")))
CURSOR_HOME = Path.home() / ".cursor"
STATIC = ROOT / "static"
STATE_PATH = Path(os.environ.get("WRAP_STATE", "/var/lib/wrap/state.json"))
AGENTS = ("claude", "cursor", "opencode", "hermes", "console")
AGENT_LABELS = {
    "claude": "Claude",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "hermes": "Hermes",
    "console": "Console",
}
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
PINNED: list[str] = []  # agent:native_id, most recently pinned last
_catalog_lock = threading.Lock()
_catalog_cache: dict[str, Any] = {"at": 0.0, "data": None}
# A "fresh" catalog still reuses a build this recent, so opening the settings
# pane repeatedly cannot re-run the CLI model probes every time.
CATALOG_FRESH_DEBOUNCE_S = 20.0
_send_q: dict[str, Queue[str | None]] = {}
_send_buf: dict[str, list[str]] = {}
_send_workers: dict[str, threading.Thread] = {}
_sending: set[str] = set()
_busy_hold: dict[str, float] = {}
# Headless claude -p: running query processes per wrap sid (PID file handles).
_hl_procs: dict[str, subprocess.Popen[bytes]] = {}
# Pending can_use_tool request awaiting a web answer (public choice + wire payload).
_hl_choices: dict[str, dict[str, Any]] = {}
_hl_pending: dict[str, dict[str, Any]] = {}
_hl_stdin_lock = threading.Lock()
_title_inflight: set[str] = set()
_title_tried_at: dict[str, float] = {}
_TITLE_MAX = 4
_TITLE_RETRY_SEC = 90.0
_TITLE_EMPTY_SEC = 8.0
# Fallback namer for Cursor / OpenCode / Hermes. Empty agent = off.
TITLE_FALLBACK: dict[str, str] = {
    "agent": "claude" if titles.enabled() else "",
    "model": titles.model() if titles.enabled() else "",
}
_alerts: deque[dict[str, Any]] = deque(maxlen=80)
_alert_cv = threading.Condition()
_alert_seq = 0
_alert_last: dict[str, float] = {}
_alert_timers: dict[str, threading.Timer] = {}
_alert_timer_lock = threading.Lock()

# The session list is polled every 8 s and walks ~590 transcript files (103 ms,
# mostly stats over the slow bind mount). It only changes when a turn starts or
# ends, so it is cached; the generation stamp is bumped when that happens and
# whenever the pin/hide sets are written.
HISTORY_TTL_S = 45.0
_history_cache: dict[str, Any] = {"t": 0.0, "gen": -1, "rows": []}
_history_gen = 0

# Turn boundaries are inferred from the busy flag the sync stream already
# computes. A turn that changed state emits "turnend" once its writes have
# settled — that is the only moment the diff view has to refresh, so the client
# no longer polls git at all.
TURN_SETTLE_S = 2.0
_turn_state: dict[str, dict[str, Any]] = {}
_turn_lock = threading.Lock()


def bump_history_gen() -> None:
    global _history_gen
    with _lock:
        _history_gen += 1


def history_gen() -> int:
    with _lock:
        return _history_gen


def turn_seq(sid: str) -> int:
    """Latest turn-end sequence for a session, so a new stream starts caught up."""
    with _turn_lock:
        st = _turn_state.get(sid)
        return int((st or {}).get("seq") or 0)


def turn_step(
    sid: str, busy: bool, fp: str, gen: Any
) -> tuple[bool, dict[str, Any] | None, Any]:
    """Advance the turn state machine for one stream iteration.

    Returns (session_list_changed, latest_turn_end, gen_when_turn_opened).
    The turn-end is broadcast: it carries a sequence number that every attached
    stream compares against its own high-water mark, so all tabs refresh rather
    than whichever one happened to run the tick that detected the transition.
    """
    with _turn_lock:
        st = _turn_state.get(sid)
        if st is None:
            # A turn already running when we attach: we cannot know whether it
            # wrote anything, so assume it did. A spurious refresh is cheap.
            st = {
                "busy": busy,
                "n": 1 if busy else 0,
                "edits": 0,
                "gen_start": gen,
                "settle": 0.0,
                "prev": None,
                "seq": 0,
                "last": None,
            }
            _turn_state[sid] = st
        bump_hist = False
        # Order matters: the turn-opening branch resets the change counter, so it
        # must run before this iteration's change is counted — otherwise the very
        # first busy sample that carries the change would have it wiped again.
        if busy and not st.get("busy"):
            st["gen_start"] = gen
            st["n"] = 0
            bump_hist = True
        if fp != st.get("prev"):
            st["prev"] = fp
            if busy:
                st["n"] = int(st.get("n") or 0) + 1
        if busy:
            st["busy"] = True
            st["settle"] = 0.0
        elif st.get("busy"):
            st["busy"] = False
            bump_hist = True
            st["edits"] = int(st.get("n") or 0)
            st["n"] = 0
            # Writes routinely land just after the turn flag drops.
            st["settle"] = time.monotonic() + TURN_SETTLE_S
        gen_start = None
        if st.get("settle") and time.monotonic() >= float(st["settle"]):
            st["settle"] = 0.0
            if st.get("edits"):
                gen_start = st.get("gen_start")
                st["seq"] = int(st.get("seq") or 0) + 1
                st["last"] = {
                    "seq": st["seq"],
                    "edits": int(st["edits"]),
                    "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            st["edits"] = 0
        return bump_hist, st.get("last"), gen_start


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} {msg}", flush=True)


def hermes_on() -> bool:
    return hm.enabled()


def http_live(sess: dict[str, Any]) -> bool:
    agent = sess.get("agent")
    if agent == "opencode":
        return True
    if agent == "hermes":
        return bool(sess.get("hm_id"))
    if agent in ("claude", "cursor"):
        return bool(sess.get("cli_session"))
    if agent == "console":
        return ptyio.alive(str(sess.get("id") or ""))
    return False


def new_session_id(agent: str) -> str:
    return f"{agent}-{secrets.token_hex(4)}"


def resolve_alert_sid(raw: str) -> str:
    return (raw or "").strip()


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
    must hit this installed binary, which detects wrap via WRAP_SESSION_ID.
    """
    shim_src = ROOT / "bin" / "cmux"
    dest = Path("/usr/local/bin/cmux")
    backup = Path("/usr/local/bin/cmux.agents-host")
    if not shim_src.is_file():
        return
    marker = b"wrap sessions alert the browser"
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


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def session_recently_wrote(sess: dict[str, Any], sec: float = ALERT_SETTLE_SEC) -> bool:
    """True if the native transcript was just written — another round may be incoming."""
    if sess.get("agent") == "opencode":
        return False
    if sess.get("agent") == "hermes":
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


def session_is_working(sess: dict[str, Any], *, hold: bool = True) -> bool:
    """True while the agent is mid-turn (transcript, send queue, subagents)."""
    sid = str(sess.get("id") or "")
    busy = False
    if sid and sid in _sending:
        busy = True
    if sess.get("agent") in ("claude", "cursor", "opencode"):
        busy = busy or hl_running(sid) or bool(hl_choice(sess))
    elif sess.get("agent") == "hermes" and sess.get("hm_id"):
        busy = busy or hm.session_busy(str(sess["hm_id"]))
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
    if sess.get("agent") == "hermes":
        return []
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
    if agent == "claude":
        # Headless permissions come from can_use_tool, not jsonl (which would
        # double-prompt AskUserQuestion and cannot be answered after -p exits).
        return hl_choice(sess)
    return None


def persist_state() -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        payload = {
            "sessions": [s for s in SESSIONS.values() if s.get("agent") != "console"],
            "hidden": sorted(HIDDEN),
            "pinned": list(PINNED),
            "hermes_cwd": hm.cwd_map(),
            "title_fallback": dict(TITLE_FALLBACK),
        }
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
    pinned = data.get("pinned")
    hermes_cwd = data.get("hermes_cwd")
    if isinstance(hermes_cwd, dict):
        hm.load_cwd_map({str(k): str(v) for k, v in hermes_cwd.items() if k and v})
    raw_fb = data.get("title_fallback")
    if isinstance(raw_fb, dict):
        TITLE_FALLBACK.update(_normalize_title_fallback(raw_fb))
    with _lock:
        HIDDEN.clear()
        PINNED.clear()
        if isinstance(hidden, list):
            for key in hidden:
                if isinstance(key, str) and ":" in key:
                    HIDDEN.add(key)
        if isinstance(pinned, list):
            seen: set[str] = set()
            for key in pinned:
                if isinstance(key, str) and ":" in key and key not in seen:
                    PINNED.append(key)
                    seen.add(key)
        if not isinstance(items, list):
            return
        for sess in items:
            if not isinstance(sess, dict) or not sess.get("id"):
                continue
            if sess.get("agent") == "console":
                continue
            SESSIONS[str(sess["id"])] = sess
            if sess.get("agent") == "hermes" and sess.get("hm_id") and sess.get("cwd"):
                hm.remember_cwd(str(sess["hm_id"]), str(sess["cwd"]))


def session_meta(sess: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": sess["id"],
        "agent": sess["agent"],
        "cwd": sess["cwd"],
        "oc_id": sess.get("oc_id"),
        "hm_id": sess.get("hm_id"),
        "transcript": sess.get("transcript"),
        "title": sess.get("title") or "",
        "model": sess.get("model") or "",
        "effort": sess.get("effort") or "",
        "fast": bool(sess.get("fast")),
        "created": sess.get("created") or "",
        "cli_session": sess.get("cli_session") or "",
        "native_id": (
            sess["id"]
            if sess.get("agent") == "console"
            else (sess.get("hm_id") or sess.get("oc_id") or sess.get("cli_session") or "")
        ),
    }


def apply_native_title(
    sess: dict[str, Any],
    *,
    persist: bool = False,
    registry: dict[str, dict[str, str]] | None = None,
) -> bool:
    """Swap wrap's placeholder tab name for the agent's own title."""
    if sess.get("agent") == "console":
        return False
    if sess.get("title_source") in ("llm", "user"):
        return False
    native = ""
    cwd = sess.get("cwd") or ""
    if sess.get("agent") == "opencode" and sess.get("oc_id"):
        info = oc.get_session(str(sess["oc_id"]), cwd)
        native = oc.display_title(info, cwd)
        if oc.is_placeholder_title(native, cwd):
            native = ""
    elif sess.get("agent") == "hermes" and sess.get("hm_id"):
        info = hm.get_session(str(sess["hm_id"]))
        native = str((info or {}).get("title") or "").strip()
        if not native or tr.is_wrap_default_title(native):
            native = ""
        proj = Path(str(cwd)).name if cwd else ""
        if proj and native == proj:
            native = ""
    if not native and sess.get("agent") not in ("opencode", "hermes"):
        path = sess.get("transcript")
        native = tr.native_session_title(
            str(sess.get("agent") or ""),
            transcript=Path(path) if path else None,
            cli_session=str(sess.get("cli_session") or ""),
            claude_home=CLAUDE_HOME,
            cursor_home=CURSOR_HOME,
            registry=registry,
        ) or ""
    if native:
        if sess.get("title") == native:
            return False
        sess["title"] = native
        sess["title_source"] = "native"
        if persist:
            persist_state()
        return True
    if sess.get("title_source") in ("llm", "user"):
        return False
    if str(sess.get("agent") or "") == "claude":
        path = sess.get("transcript")
        first = tr.first_user_title(Path(path)) if path else None
        cur = str(sess.get("title") or "")
        placeholder = (
            not cur
            or tr.is_wrap_default_title(cur)
            or tr.is_claude_derived_title(cur, sess.get("cwd"))
        )
        if first and placeholder and cur != first:
            sess["title"] = first
            sess["title_source"] = "prompt"
            if persist:
                persist_state()
            return True
        if tr.is_claude_derived_title(cur, sess.get("cwd")):
            restored = ""
            if path:
                restored = tr.last_jsonl_custom_title(Path(path)) or ""
            if not restored or tr.is_claude_derived_title(restored, sess.get("cwd")):
                restored = default_title(
                    "claude",
                    Path(str(sess.get("cwd") or ".")),
                    str(sess.get("model") or ""),
                    str(sess.get("effort") or ""),
                )
            if restored != cur:
                sess["title"] = restored
                sess["title_source"] = "wrap"
                if persist:
                    persist_state()
                return True
    return False


def _normalize_title_fallback(raw: dict[str, Any] | None) -> dict[str, str]:
    data = raw if isinstance(raw, dict) else {}
    agent = str(data.get("agent") or "").strip()
    if agent not in ("", "claude", "cursor", "opencode"):
        agent = ""
    model = str(data.get("model") or "").strip()[:120]
    if not agent:
        model = ""
    return {"agent": agent, "model": model}


def title_fallback() -> dict[str, str]:
    with _lock:
        return dict(TITLE_FALLBACK)


def set_title_fallback(raw: dict[str, Any] | None) -> dict[str, str]:
    parsed = _normalize_title_fallback(raw)
    with _lock:
        TITLE_FALLBACK.clear()
        TITLE_FALLBACK.update(parsed)
    persist_state()
    return parsed


def _title_needs_llm(sess: dict[str, Any]) -> bool:
    agent = str(sess.get("agent") or "")
    if agent in ("", "console"):
        return False
    if sess.get("title_source") in ("llm", "native", "user"):
        return False
    sid = str(sess.get("id") or "")
    if not sid:
        return False
    with _lock:
        if sid in _title_inflight:
            return False
        last = _title_tried_at.get(sid, 0.0)
    if last and time.time() - last < _TITLE_RETRY_SEC:
        return False
    if agent == "claude":
        if not titles.enabled():
            return False
        path = sess.get("transcript")
        if not path or not Path(path).is_file():
            return False
        if not tr.first_user_title(Path(path)):
            return False
        return True
    fb = title_fallback()
    if not fb.get("agent"):
        return False
    if agent == "opencode" and not sess.get("oc_id"):
        return False
    if agent == "hermes" and not sess.get("hm_id"):
        return False
    if agent == "cursor":
        path = sess.get("transcript")
        if not path or not Path(path).is_file():
            return False
    return True


def maybe_title_session(sess: dict[str, Any]) -> None:
    """Name a wrap tab from the first turns (Haiku or settings fallback)."""
    if not _title_needs_llm(sess):
        return
    sid = str(sess["id"])
    with _lock:
        if sid in _title_inflight or len(_title_inflight) >= _TITLE_MAX:
            return
        _title_inflight.add(sid)
        _title_tried_at[sid] = time.time()
    threading.Thread(
        target=_title_worker,
        args=(sid,),
        daemon=True,
        name=f"wrap-title-{sid}",
    ).start()


def _title_worker(sid: str) -> None:
    try:
        try:
            sess = get_session(sid)
        except KeyError:
            return
        agent = str(sess.get("agent") or "")
        msgs = load_messages(sess)
        blob = titles.snippet(msgs)
        if len(blob) < 8:
            with _lock:
                _title_tried_at[sid] = time.time() - _TITLE_RETRY_SEC + _TITLE_EMPTY_SEC
            return
        if agent == "claude":
            name = titles.generate(blob, agent="claude", model=titles.model())
        else:
            fb = title_fallback()
            name = titles.generate(blob, agent=fb.get("agent") or "", model=fb.get("model") or "")
        if not name:
            log(f"title skip {sid}: empty")
            return
        try:
            sess = get_session(sid)
        except KeyError:
            return
        if sess.get("title_source") in ("native", "user"):
            return
        with _lock:
            sess["title"] = name
            sess["title_source"] = "llm"
        persist_state()
        log(f"title {sid}: {name}")
    except Exception as exc:  # noqa: BLE001
        log(f"title fail {sid}: {exc}")
    finally:
        with _lock:
            _title_inflight.discard(sid)


def cursor_model_arg(model: str, effort: str = "", fast: bool = False) -> str:
    """Strip `[effort=…]` overlays; Cursor CLI only accepts catalog slugs."""
    _ = effort, fast
    model = (model or "").strip()
    if not model:
        return ""
    if "[" in model:
        model = model.split("[", 1)[0].strip()
    return model


HL_PROC_DIR = STATE_PATH.parent / "headless"
HL_INIT_WAIT_SEC = 12.0


def hl_cli_session(sess: dict[str, Any]) -> str:
    """Native CLI session id: cli_session, else derived from the transcript."""
    cid = str(sess.get("cli_session") or "")
    if cid:
        return cid
    path = Path(sess.get("transcript") or "")
    if path.is_file():
        sess["cli_session"] = path.stem
        return path.stem
    sess["cli_session"] = str(uuid.uuid4())
    return sess["cli_session"]


def hl_proc_file(sid: str) -> Path:
    return HL_PROC_DIR / f"{sid}.pid"


def _hl_pid(sid: str) -> int | None:
    with _lock:
        proc = _hl_procs.get(sid)
    if proc is not None and proc.poll() is None:
        return proc.pid
    try:
        pid = int(hl_proc_file(sid).read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def hl_running(sid: str) -> bool:
    return _hl_pid(sid) is not None


def _hl_signal(pid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


def hl_interrupt(sid: str, *, kill: bool = False) -> None:
    """SIGINT ends the -p turn; SIGTERM is for tearing the session down."""
    pid = _hl_pid(sid)
    if pid is None:
        return
    _hl_signal(pid, signal.SIGTERM if kill else signal.SIGINT)
    if kill:
        _hl_signal(pid, signal.SIGKILL)


def hl_choice(sess: dict[str, Any]) -> dict[str, Any] | None:
    """Pending can_use_tool from the running claude -p query, if any."""
    sid = str(sess.get("id") or "")
    with _lock:
        choice = _hl_choices.get(sid)
    if not choice:
        return None
    if not hl_running(sid):
        with _lock:
            _hl_choices.pop(sid, None)
            _hl_pending.pop(sid, None)
        return None
    return choice


def _hl_cleanup(sid: str, proc: subprocess.Popen[bytes] | None = None) -> None:
    with _lock:
        if proc is None or _hl_procs.get(sid) is proc:
            _hl_procs.pop(sid, None)
        _hl_choices.pop(sid, None)
        _hl_pending.pop(sid, None)
    try:
        hl_proc_file(sid).unlink()
    except OSError:
        pass


def _hl_reap_orphan(sid: str) -> None:
    """A previous wrap process may have left a -p child; SIGINT it before reuse."""
    with _lock:
        if sid in _hl_procs:
            return
    pid = _hl_pid(sid)
    if pid is None:
        try:
            hl_proc_file(sid).unlink()
        except OSError:
            pass
        return
    log(f"hl orphan {sid} pid={pid}, interrupting")
    _hl_signal(pid, signal.SIGINT)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        _hl_signal(pid, signal.SIGTERM)
    try:
        hl_proc_file(sid).unlink()
    except OSError:
        pass


def _hl_write(proc: subprocess.Popen[bytes], obj: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise RuntimeError("claude stdin gone")
    line = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
    with _hl_stdin_lock:
        proc.stdin.write(line)
        proc.stdin.flush()


def _hl_log_stderr(sid: str, proc: subprocess.Popen[bytes]) -> None:
    if proc.stderr is None:
        return
    for raw in proc.stderr:
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            log(f"hl {sid}: {line}")


def claude_prompt(sess: dict[str, Any], text: str) -> None:
    """One headless `claude -p` turn; follow-ups wait in wrap's send queue."""
    sid = str(sess["id"])
    _hl_reap_orphan(sid)
    if hl_running(sid):
        raise RuntimeError("claude query already running")
    cwd = Path(sess["cwd"])
    cid = hl_cli_session(sess)
    fresh = bool(sess.get("hl_fresh", True))
    binary = shutil_which("claude") or "claude"
    args = [
        binary,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-prompts",
        "host",
    ]
    if sess.get("model"):
        args.extend(["--model", str(sess["model"])])
    if sess.get("effort"):
        args.extend(["--effort", str(sess["effort"])])
    title = str(sess.get("title") or "").strip()
    # Placeholder --name skips Claude's ai-title and overwrites the wrap tab.
    if title and fresh and not tr.is_wrap_default_title(title):
        args.extend(["--name", title[:40]])
    if fresh:
        args.extend(["--session-id", cid])
        sess["hl_fresh"] = False
    else:
        args.extend(["--resume", cid])
    env = dict(os.environ)
    hook_bin = str(ROOT / "bin")
    path = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if hook_bin not in path.split(":"):
        path = hook_bin + ":" + path
    env["PATH"] = path
    env["CLAUDE_CODE_DISABLE_MOUSE"] = "1"
    env["CLAUDE_CODE_NO_FLICKER"] = "1"
    env["IS_SANDBOX"] = os.environ.get("IS_SANDBOX", "1")
    env["WRAP_SESSION_ID"] = sid
    env["WRAP_URL"] = f"http://127.0.0.1:{PORT}"
    HL_PROC_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
        bufsize=0,
    )
    with _lock:
        _hl_procs[sid] = proc
    hl_proc_file(sid).write_text(f"{proc.pid}\n")
    threading.Thread(target=_hl_log_stderr, args=(sid, proc), daemon=True, name=f"wrap-hl-err-{sid}").start()
    try:
        _hl_run(sid, proc, text)
    finally:
        if proc.poll() is None:
            hl_interrupt(sid)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                hl_interrupt(sid, kill=True)
                proc.wait(timeout=2)
        _hl_cleanup(sid, proc)
        ensure_transcript(sess)
        maybe_title_session(sess)


def _hl_run(sid: str, proc: subprocess.Popen[bytes], text: str) -> None:
    """Initialize the control protocol, send the user turn, handle can_use_tool."""
    init_id = f"wrap-init-{secrets.token_hex(4)}"
    try:
        _hl_write(
            proc,
            {
                "type": "control_request",
                "request_id": init_id,
                "request": {"subtype": "initialize", "hooks": None},
            },
        )
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"claude stdin gone: {exc}") from exc
    sent_user = False
    init_deadline = time.time() + HL_INIT_WAIT_SEC

    def send_user() -> None:
        nonlocal sent_user
        if sent_user:
            return
        _hl_write(
            proc,
            {
                "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            },
        )
        sent_user = True

    assert proc.stdout is not None
    stdout = proc.stdout
    while True:
        if sent_user:
            raw = stdout.readline()
            if not raw:
                break
        else:
            wait = max(0.05, min(1.0, init_deadline - time.time()))
            ready, _, _ = select.select([stdout], [], [], wait)
            if not ready:
                if proc.poll() is not None:
                    rest = stdout.read() or b""
                    if rest:
                        raw = rest.split(b"\n", 1)[0] + b"\n"
                    else:
                        break
                elif time.time() >= init_deadline:
                    log(f"claude {sid}: initialize timed out, sending prompt")
                    try:
                        send_user()
                    except (OSError, RuntimeError) as exc:
                        raise RuntimeError(f"claude stdin gone: {exc}") from exc
                    continue
                else:
                    continue
            else:
                raw = stdout.readline()
                if not raw:
                    break
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = str(ev.get("type") or "")
        if kind == "control_response":
            resp = ev.get("response") or {}
            if not sent_user and str(resp.get("request_id") or "") == init_id:
                try:
                    send_user()
                except (OSError, RuntimeError) as exc:
                    raise RuntimeError(f"claude stdin gone: {exc}") from exc
            continue
        if kind in ("control_request", "sdk_control_request"):
            _hl_on_control(sid, proc, ev)
            continue
        if kind == "system" and str(ev.get("subtype") or "") == "init" and not sent_user:
            try:
                send_user()
            except (OSError, RuntimeError) as exc:
                raise RuntimeError(f"claude stdin gone: {exc}") from exc
            continue
        if kind == "result":
            break
    if not sent_user:
        raise RuntimeError("claude -p exited before accepting the prompt")
    try:
        if proc.stdin is not None:
            with _hl_stdin_lock:
                proc.stdin.close()
    except OSError:
        pass
    proc.wait()
    if proc.returncode not in (0, -signal.SIGINT, -signal.SIGTERM, 130, 143):
        log(f"claude {sid}: exit {proc.returncode}")


def _hl_on_control(sid: str, proc: subprocess.Popen[bytes], ev: dict[str, Any]) -> None:
    req_id = str(ev.get("request_id") or "")
    req = ev.get("request") if isinstance(ev.get("request"), dict) else {}
    subtype = str(req.get("subtype") or "")
    if subtype == "can_use_tool":
        public = _hl_choice_public(req_id, req)
        if not public:
            _hl_control_reply(proc, req_id, {"behavior": "deny", "message": "empty prompt"})
            return
        with _lock:
            _hl_choices[sid] = public
            _hl_pending[sid] = {
                "request_id": req_id,
                "tool_name": str(req.get("tool_name") or ""),
                "input": req.get("input") if isinstance(req.get("input"), dict) else {},
            }
        return
    _hl_control_error(proc, req_id, f"unsupported control subtype {subtype or 'unknown'}")


def _hl_control_reply(proc: subprocess.Popen[bytes], request_id: str, payload: dict[str, Any]) -> None:
    _hl_write(
        proc,
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": request_id,
                "response": payload,
            },
        },
    )


def _hl_control_error(proc: subprocess.Popen[bytes], request_id: str, err: str) -> None:
    try:
        _hl_write(
            proc,
            {
                "type": "control_response",
                "response": {
                    "subtype": "error",
                    "request_id": request_id,
                    "error": err,
                },
            },
        )
    except (OSError, RuntimeError):
        pass


def _hl_tool_prompt(tool: str, inp: dict[str, Any], req: dict[str, Any]) -> str:
    title = str(req.get("title") or req.get("display_name") or "").strip()
    if title:
        return title[:200]
    if tool == "Bash":
        cmd = str(inp.get("command") or "").strip()
        return (cmd or "Allow Bash?")[:200]
    path = str(inp.get("file_path") or inp.get("path") or "").strip()
    if path:
        return f"{tool} {path}"[:200]
    return f"Allow {tool}?"[:200]


def _hl_choice_public(req_id: str, req: dict[str, Any]) -> dict[str, Any] | None:
    """Map a can_use_tool request to the wrap choice shape."""
    tool = str(req.get("tool_name") or "tool")
    inp = req.get("input") if isinstance(req.get("input"), dict) else {}
    cid = req_id or f"hl:{secrets.token_hex(4)}"
    if tool in tr.ASK_TOOLS:
        questions = []
        for q in inp.get("questions") or []:
            if not isinstance(q, dict):
                continue
            raw_opts = q.get("options") or []
            opts = []
            for i, o in enumerate(raw_opts):
                if isinstance(o, dict):
                    label = str(o.get("label") or o.get("id") or "").strip()
                else:
                    label = str(o).strip()
                if label:
                    opts.append({"label": label[:80], "key": str(i)})
            if len(opts) < 2:
                continue
            questions.append(
                {
                    "prompt": str(q.get("question") or q.get("prompt") or "")[:200],
                    "options": opts,
                }
            )
        if not questions:
            return None
        return {
            "id": f"hl:{cid}",
            "kind": "question",
            "drive": "",
            "title": str(inp.get("title") or questions[0]["prompt"] or "Question")[:200],
            "questions": questions,
        }
    prompt = _hl_tool_prompt(tool, inp, req)
    return {
        "id": f"hl:{cid}",
        "kind": "permission",
        "drive": "",
        "title": prompt,
        "questions": [
            {
                "prompt": prompt,
                "options": [
                    {"label": "Allow once", "key": "allow"},
                    {"label": "Allow always", "key": "allow_always"},
                    {"label": "Deny", "key": "deny"},
                ],
            }
        ],
    }


def hl_reply_choice(sid: str, choice: dict[str, Any], picks: list[int]) -> None:
    """Answer a pending can_use_tool by writing a control_response to stdin."""
    with _lock:
        proc = _hl_procs.get(sid)
        pending = _hl_pending.get(sid) or {}
    if proc is None or proc.poll() is not None:
        raise RuntimeError("claude query is gone")
    req_id = str(pending.get("request_id") or "")
    if not req_id:
        cid = str(choice.get("id") or "")
        req_id = cid.split(":", 1)[1] if cid.startswith("hl:") else cid
    tool = str(pending.get("tool_name") or "")
    original = pending.get("input") if isinstance(pending.get("input"), dict) else {}
    kind = str(choice.get("kind") or "")
    if kind == "question" or tool in tr.ASK_TOOLS:
        answers: dict[str, Any] = {}
        for i, q in enumerate(choice.get("questions") or []):
            opt = q.get("options") or []
            label = ""
            if i < len(picks) and 0 <= picks[i] < len(opt):
                label = str(opt[picks[i]].get("label") or "")
            prompt = str(q.get("prompt") or "")
            src = (original.get("questions") or [{}])
            key = str(src[i].get("question") or prompt) if i < len(src) and isinstance(src[i], dict) else prompt
            if key:
                answers[key] = label
        payload = {
            "behavior": "allow",
            "updatedInput": {"questions": original.get("questions") or [], "answers": answers},
        }
    else:
        key = "allow"
        opt = ((choice.get("questions") or [{}])[0].get("options") or [])
        try:
            key = str(opt[picks[0]].get("key") or "allow")
        except (IndexError, TypeError):
            pass
        if key == "deny":
            payload = {"behavior": "deny", "message": "User denied this action"}
        else:
            payload = {"behavior": "allow", "updatedInput": original}
            if key == "allow_always" and tool:
                payload["updatedPermissions"] = [
                    {
                        "type": "addRules",
                        "rules": [{"toolName": tool}],
                        "behavior": "allow",
                        "destination": "session",
                    }
                ]
    try:
        _hl_control_reply(proc, req_id, payload)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"claude stdin gone: {exc}") from exc
    with _lock:
        _hl_choices.pop(sid, None)
        _hl_pending.pop(sid, None)


def oc_send_text(sess: dict[str, Any], text: str) -> None:
    oc_prompt(sess, text)


def cursor_create_chat(cwd: Path) -> str:
    binary = shutil_which("agent") or shutil_which("cursor-agent") or "agent"
    try:
        r = subprocess.run(
            [binary, "create-chat"],
            cwd=str(cwd),
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return str(uuid.uuid4())
    blob = ((r.stdout or b"") + b"\n" + (r.stderr or b"")).decode("utf-8", "replace")
    m = re.search(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        blob,
        re.I,
    )
    return m.group(0) if m else str(uuid.uuid4())


def _hl_spawn(
    sid: str,
    args: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    stdin: Any = subprocess.PIPE,
) -> subprocess.Popen[bytes]:
    _hl_reap_orphan(sid)
    if hl_running(sid):
        raise RuntimeError("query already running")
    HL_PROC_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        args,
        cwd=str(cwd),
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=True,
        bufsize=0,
    )
    with _lock:
        _hl_procs[sid] = proc
    hl_proc_file(sid).write_text(f"{proc.pid}\n")
    threading.Thread(target=_hl_log_stderr, args=(sid, proc), daemon=True, name=f"wrap-hl-err-{sid}").start()
    return proc


def _hl_env(sid: str) -> dict[str, str]:
    env = dict(os.environ)
    hook_bin = str(ROOT / "bin")
    path = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if hook_bin not in path.split(":"):
        path = hook_bin + ":" + path
    env["PATH"] = path
    env["WRAP_SESSION_ID"] = sid
    env["WRAP_URL"] = f"http://127.0.0.1:{PORT}"
    return env


def cursor_prompt(sess: dict[str, Any], text: str) -> None:
    """One headless `agent -p` turn. Print mode has all tools."""
    sid = str(sess["id"])
    cwd = Path(sess["cwd"])
    cid = hl_cli_session(sess)
    binary = shutil_which("agent") or shutil_which("cursor-agent") or "agent"
    args = [
        binary,
        "-p",
        "--output-format",
        "stream-json",
        "--trust",
        "--approve-mcps",
        "--workspace",
        str(cwd),
        f"--resume={cid}",
    ]
    model = cursor_model_arg(
        str(sess.get("model") or ""),
        str(sess.get("effort") or ""),
        bool(sess.get("fast")),
    )
    if model:
        args.extend(["--model", model])
    prompt = (text or "").strip()
    if prompt.startswith("-"):
        args.extend(["--", prompt])
    else:
        args.append(prompt)
    env = _hl_env(sid)
    env["TERM"] = "xterm-256color"
    env["DISPLAY"] = os.environ.get("DISPLAY", ":0")
    proc = _hl_spawn(sid, args, cwd, env)
    try:
        if proc.stdout is not None:
            for _raw in proc.stdout:
                pass
        proc.wait()
        if proc.returncode not in (0, -signal.SIGINT, -signal.SIGTERM, 130, 143):
            log(f"cursor {sid}: exit {proc.returncode}")
    finally:
        if proc.poll() is None:
            hl_interrupt(sid)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                hl_interrupt(sid, kill=True)
                proc.wait(timeout=2)
        _hl_cleanup(sid, proc)
        ensure_transcript(sess)
        maybe_title_session(sess)


def oc_prompt(sess: dict[str, Any], text: str) -> None:
    """One `opencode run --format json --auto` turn."""
    sid = str(sess["id"])
    cwd = Path(sess["cwd"])
    oc_id = str(sess.get("oc_id") or "")
    title = str(sess.get("title") or "")
    prior = oc.list_messages(oc_id, cwd) if oc_id else []
    wrap_id = f"wrap-{sid}"
    user_msg = {
        "id": f"u-{sid}-{len(prior)}",
        "role": "user",
        "text": text,
        "parts": [{"type": "text", "text": text}],
        "ts": "",
    }
    base = prior + [user_msg]
    live_id = oc_id or wrap_id
    oc.set_live_messages(live_id, base)
    args = oc.run_argv(
        cwd,
        text,
        session_id=oc_id,
        model=str(sess.get("model") or ""),
        effort=str(sess.get("effort") or ""),
        title=title,
    )
    # OpenCode reads all of stdin when it is not a TTY (`await Bun.stdin.text()`).
    # An open PIPE without EOF blocks forever — never create a session, never emit JSON.
    proc = _hl_spawn(sid, args, cwd, _hl_env(sid), stdin=subprocess.DEVNULL)
    live_parts: list[dict[str, Any]] = []
    got_id = oc_id

    def publish() -> None:
        key = got_id or wrap_id
        assistant = None
        if live_parts:
            assistant = {
                "id": f"live-{sid}",
                "role": "assistant",
                "text": "\n\n".join(
                    p.get("text") or "" for p in live_parts if p.get("type") == "text"
                ).strip(),
                "parts": list(live_parts),
                "ts": "",
            }
        oc.set_live_messages(key, base + ([assistant] if assistant else []))

    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            eid = oc.event_session_id(ev)
            if eid and eid != got_id:
                if got_id != eid and live_id == wrap_id:
                    oc.clear_live_messages(wrap_id)
                got_id = eid
                with _lock:
                    sess["oc_id"] = eid
                persist_state()
            bits = oc.event_parts(ev)
            if bits:
                live_parts.extend(bits)
            if eid or bits:
                publish()
        proc.wait()
        if proc.returncode not in (0, -signal.SIGINT, -signal.SIGTERM, 130, 143):
            log(f"opencode {sid}: exit {proc.returncode}")
    finally:
        if proc.poll() is None:
            hl_interrupt(sid)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                hl_interrupt(sid, kill=True)
                proc.wait(timeout=2)
        _hl_cleanup(sid, proc)
        oc.clear_live_messages(wrap_id)
        if got_id:
            oc.clear_live_messages(got_id)
            with _lock:
                sess["oc_id"] = got_id
        maybe_title_session(sess)


def hermes_send_text(sess: dict[str, Any], text: str) -> None:
    hm_id = sess.get("hm_id")
    if not hm_id:
        raise RuntimeError("no hermes session")
    cwd = Path(sess["cwd"])
    # Hermes CLI default busy_input_mode is interrupt: a follow-up while
    # working redirects the turn. wrap used to wait_idle (queue-until-done).
    if hm.session_busy(str(hm_id)):
        log(f"hermes interrupt {hm_id} for queued send")
        hm.abort(str(hm_id))
    hm.wait_idle(str(hm_id), timeout=30.0)
    hm.prompt_async(
        str(hm_id),
        cwd,
        text,
        model=str(sess.get("model") or ""),
        effort=str(sess.get("effort") or ""),
    )


def queued_sends(sid: str) -> list[str]:
    with _lock:
        return list(_send_buf.get(sid) or [])


def _queued_fp(sid: str) -> str:
    q = queued_sends(sid)
    return f"{len(q)}:" + ",".join(str(len(t)) for t in q)


def _pop_send_buf(sid: str, text: str) -> None:
    with _lock:
        buf = _send_buf.get(sid)
        if not buf:
            return
        try:
            buf.remove(text)
        except ValueError:
            pass
        if not buf:
            _send_buf.pop(sid, None)


def enqueue_send(sid: str, text: str) -> None:
    with _lock:
        q = _send_q.get(sid)
        if q is None:
            q = Queue()
            _send_q[sid] = q
        _send_buf.setdefault(sid, []).append(text)
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
            try:
                sess = get_session(sid)
            except KeyError:
                return
            try:
                if sess["agent"] == "hermes":
                    hermes_send_text(sess, text)
                    continue
                if sess["agent"] in ("claude", "cursor", "opencode"):
                    with _lock:
                        _sending.add(sid)
                    try:
                        if sess["agent"] == "opencode":
                            oc_send_text(sess, text)
                        elif sess["agent"] == "cursor":
                            ensure_transcript(sess)
                            cursor_prompt(sess, text)
                        else:
                            ensure_transcript(sess)
                            claude_prompt(sess, text)
                    finally:
                        with _lock:
                            _sending.discard(sid)
                    continue
                log(f"send dropped, no handler {sid} {sess.get('agent')}")
            except Exception as exc:  # noqa: BLE001
                log(f"send failed {sid}: {exc}")
        finally:
            _pop_send_buf(sid, text)


def stop_send_queue(sid: str) -> None:
    with _lock:
        q = _send_q.pop(sid, None)
        _send_workers.pop(sid, None)
        _send_buf.pop(sid, None)
    if q is not None:
        try:
            q.put_nowait(None)
        except Exception:
            pass
    with _lock:
        _sending.discard(sid)
        _busy_hold.pop(sid, None)
    _cancel_alert_timer(sid)


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
    if agent in ("opencode", "hermes"):
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
    if agent in ("claude", "cursor"):
        # Headless transcripts are bound by cli_session; mtime would steal siblings.
        return None
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
    if sess.get("agent") in ("opencode", "hermes", "console"):
        return None
    found = pick_transcript(sess)
    if not found:
        return None
    if sess.get("transcript") != str(found):
        sess["transcript"] = str(found)
        persist_state()
    return found


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


def catalog(*, fresh: bool = False) -> dict[str, Any]:
    """Model catalog. Cached 120 s; `fresh` bypasses it but stays debounced.

    A cold `opencode serve` makes `oc.providers()` return a single placeholder
    entry, and the CLI fallback then costs a few seconds — so callers that need
    a trustworthy model list (the settings pane) ask for a fresh one instead of
    silently showing whatever the 120 s cache froze.
    """
    now = time.time()
    with _catalog_lock:
        cached = _catalog_cache["data"]
        age = now - float(_catalog_cache["at"] or 0.0)
        # Even a fresh request reuses a very recent build, so repeated pane opens
        # cannot hammer the CLIs.
        if cached and age < (CATALOG_FRESH_DEBOUNCE_S if fresh else 120.0):
            return _catalog_with_title(cached)

    cursor_models = [{"id": "", "label": "CLI default"}]
    cursor_models.extend(parse_labeled_models(run_lines(["agent", "models"])))
    oc_models = oc.providers(HOST_PROJECTS if HOST_PROJECTS.is_dir() else None)
    oc_stale = False
    if len(oc_models) <= 1:
        # Cold `opencode serve`: providers() gives nothing, so fall back to the
        # CLI. Log it — a silently short model list is what makes the settings
        # pane look like it lost its configured model.
        log("catalog: opencode serve cold, probing CLI for models")
        oc_models.extend(parse_labeled_models(run_lines(["opencode", "models"])))
        # Still nothing useful: the list cannot be trusted, say so instead of
        # letting the UI imply the configured model no longer exists.
        oc_stale = len(oc_models) <= 1
    hermes_models = [{"id": "", "label": "Gateway default"}]
    if hermes_on():
        try:
            hermes_models = hm.providers() or hermes_models
        except RuntimeError as exc:
            log(f"hermes models: {exc}")

    agents = [
        {"id": key, "label": AGENT_LABELS[key]}
        for key in AGENTS
        if key not in ("console", "hermes") or (key == "hermes" and hermes_on())
    ]
    data = {
        "agents": agents,
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
            "stale": oc_stale,
            "effort": [
                {"id": "", "label": "Default"},
                {"id": "high", "label": "High"},
                {"id": "max", "label": "Max"},
            ],
            "fast": False,
        },
    }
    if hermes_on():
        data["hermes"] = {
            "models": hermes_models,
            "effort": EFFORT_LEVELS,
            "fast": False,
        }
    with _catalog_lock:
        _catalog_cache["at"] = now
        _catalog_cache["data"] = data
    return _catalog_with_title(data)


def _catalog_with_title(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    out["title"] = {
        "fallback": title_fallback(),
        "agents": [
            {"id": "", "label": "Off"},
            {"id": "claude", "label": "Claude"},
            {"id": "cursor", "label": "Cursor"},
            {"id": "opencode", "label": "OpenCode"},
        ],
    }
    return out


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
    maybe_title_session(sess)
    messages = load_messages(sess)
    live = http_live(sess)
    subagents = session_subagents(sess)
    choice = session_choice(sess)
    busy = session_is_working(sess) or bool(subagents) or bool(choice)
    return {
        **session_meta(sess),
        "busy": busy,
        "subagents": subagents,
        "choice": choice,
        "messages": messages,
        "queued": queued_sends(sess["id"]),
        "live": live,
        "pinned": native_is_pinned(*native_key(sess)),
    }


def load_messages(sess: dict[str, Any]) -> list[dict[str, Any]]:
    if sess["agent"] == "console":
        return []
    if sess["agent"] == "opencode":
        oc_id = str(sess.get("oc_id") or "")
        cwd = sess.get("cwd") or ""
        if oc_id:
            return oc.list_messages(oc_id, cwd)
        return oc.list_messages(f"wrap-{sess['id']}", cwd)
    if sess["agent"] == "hermes":
        hm_id = sess.get("hm_id")
        if not hm_id:
            return []
        return hm.list_messages(str(hm_id)) or []
    path = ensure_transcript(sess)
    if not path:
        return []
    return tr.parse_jsonl(sess["agent"], path)


def fingerprint(sess: dict[str, Any]) -> str:
    if sess["agent"] == "opencode":
        oc_id = str(sess.get("oc_id") or "")
        live_id = oc_id or f"wrap-{sess.get('id') or ''}"
        cwd = sess.get("cwd") or ""
        choice = session_choice(sess)
        cid = (choice or {}).get("id") or ""
        msgs = load_messages(sess)
        last = msgs[-1] if msgs else {}
        return (
            f"oc:{oc_id}:{len(msgs)}:{last.get('id')}:{len(last.get('text') or '')}:"
            f"{int(hl_running(str(sess.get('id') or '')))}:{oc.live_generation(live_id)}:"
            f"{cid}:{sess.get('title') or ''}:"
            f"q:{_queued_fp(sess['id'])}"
        )
    if sess["agent"] == "hermes":
        hm_id = str(sess.get("hm_id") or "")
        msgs = load_messages(sess)
        last = msgs[-1] if msgs else {}
        return (
            f"hm:{hm_id}:{len(msgs)}:{last.get('id')}:{len(last.get('text') or '')}:"
            f"{int(hm.session_busy(hm_id))}:{sess.get('title') or ''}:"
            f"q:{_queued_fp(sess['id'])}"
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
        return (
            f"{st.st_mtime}:{st.st_size}:{sess.get('title') or ''}:"
            f"sa:{subs}:ch:{ch}:q:{_queued_fp(sess['id'])}"
        )
    except OSError:
        return str(path)


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
    if agent == "hermes":
        return agent, str(sess.get("hm_id") or "")
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
        if agent == "hermes" and sess.get("hm_id"):
            return sess
        if agent in ("claude", "cursor") and sess.get("cli_session"):
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
    key = hide_key(agent, native_id)
    with _lock:
        HIDDEN.add(key)
        PINNED[:] = [item for item in PINNED if item != key]
    bump_history_gen()
    persist_state()


def history_pinned() -> set[tuple[str, str]]:
    with _lock:
        keys = list(PINNED)
    out: set[tuple[str, str]] = set()
    for key in keys:
        if ":" not in key:
            continue
        agent, native = key.split(":", 1)
        if agent and native:
            out.add((agent, native))
    return out


def native_is_pinned(agent: str, native_id: str) -> bool:
    agent = (agent or "").strip()
    native_id = (native_id or "").strip()
    if not agent or not native_id:
        return False
    with _lock:
        return hide_key(agent, native_id) in PINNED


def set_pinned(agent: str, native_id: str, pinned: bool) -> list[str]:
    agent = (agent or "").strip()
    native_id = (native_id or "").strip()
    if agent not in AGENTS or not native_id:
        raise ValueError("agent and native required")
    key = hide_key(agent, native_id)
    with _lock:
        PINNED[:] = [item for item in PINNED if item != key]
        if pinned:
            PINNED.append(key)
            HIDDEN.discard(key)
        out = list(PINNED)
    bump_history_gen()
    persist_state()
    return out


def live_native_skip() -> tuple[set[tuple[str, str]], set[str], set[str]]:
    skip: set[tuple[str, str]] = set()
    with _lock:
        vals = list(SESSIONS.values())
    for sess in vals:
        live = http_live(sess)
        if not live:
            continue
        key = native_key(sess)
        if key[1]:
            skip.add(key)
    oc_skip = {key[1] for key in skip if key[0] == "opencode"}
    hm_skip = {key[1] for key in skip if key[0] == "hermes"}
    return skip, oc_skip, hm_skip


def list_history() -> list[dict[str, Any]]:
    """Cached history listing — see HISTORY_TTL_S for why this is not recomputed."""
    now = time.monotonic()
    gen = history_gen()
    if (
        _history_cache["gen"] == gen
        and now - float(_history_cache["t"] or 0.0) < HISTORY_TTL_S
    ):
        return _history_cache["rows"]
    rows = _list_history_uncached()
    _history_cache["t"] = now
    _history_cache["gen"] = gen
    _history_cache["rows"] = rows
    return rows


def _list_history_uncached() -> list[dict[str, Any]]:
    projects = [Path(p["path"]) for p in list_projects()]
    skip, oc_skip, hm_skip = live_native_skip()
    pinned = history_pinned()
    hidden = history_hidden() - pinned
    skip |= hidden
    oc_skip |= {n for a, n in hidden if a == "opencode"}
    hm_skip |= {n for a, n in hidden if a == "hermes"}
    keep = {key for key in pinned if key not in skip}
    cli = tr.list_native_history(
        projects,
        claude_home=CLAUDE_HOME,
        cursor_home=CURSOR_HOME,
        host_projects=HOST_PROJECTS,
        skip=skip,
        keep=keep,
    )
    try:
        oc_rows = oc.history_rows(
            projects,
            skip=oc_skip,
            keep={n for a, n in keep if a == "opencode"},
        )
    except RuntimeError:
        oc_rows = []
    try:
        hm_rows = hm.history_rows(
            projects,
            skip=hm_skip,
            keep={n for a, n in keep if a == "hermes"},
        )
    except RuntimeError:
        hm_rows = []
    merged = cli + oc_rows + hm_rows
    for item in merged:
        key = (str(item.get("agent") or ""), str(item.get("native_id") or ""))
        item["pinned"] = key in pinned
    merged.sort(key=lambda item: float(item.get("updated") or 0), reverse=True)
    with _lock:
        rank = {key: i for i, key in enumerate(PINNED)}
    pinned_rows = [item for item in merged if item.get("pinned")]
    rest = [item for item in merged if not item.get("pinned")]
    pinned_rows.sort(
        key=lambda item: rank.get(
            hide_key(str(item.get("agent") or ""), str(item.get("native_id") or "")),
            -1,
        ),
        reverse=True,
    )
    return pinned_rows + rest[:80]


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
    if item.get("title") or str(item.get("agent") or "") in ("opencode", "hermes"):
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
    skip, oc_skip, hm_skip = live_native_skip()
    pinned = history_pinned()
    hidden = history_hidden() - pinned
    skip |= hidden
    oc_skip |= {n for a, n in hidden if a == "opencode"}
    hm_skip |= {n for a, n in hidden if a == "hermes"}
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
    try:
        hm_rows = hm.history_rows(projects, skip=hm_skip, limit=2000)
    except RuntimeError:
        hm_rows = []
    merged = cli + oc_rows + hm_rows
    merged.sort(key=lambda item: float(item.get("updated") or 0), reverse=True)
    registry = tr.claude_registry_names(CLAUDE_HOME)
    for item in merged:
        if item.get("title") or str(item.get("agent") or "") != "claude":
            continue
        named = registry.get(str(item.get("native_id") or "")) or {}
        if str(named.get("nameSource") or "") != "user":
            continue
        title = str(named.get("name") or "").strip()
        if title and not tr.is_wrap_default_title(title):
            item["title"] = title
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
        key = (str(row.get("agent") or ""), str(row.get("native_id") or ""))
        row["pinned"] = key in pinned
    hits.sort(key=lambda item: (not item.get("pinned"), -float(item.get("updated") or 0)))
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


def _oc_touched_paths(oc_id: str, cwd: Path) -> list[str]:
    if not oc_id:
        return []
    try:
        msgs = oc.list_messages(oc_id, cwd)
    except RuntimeError:
        return []
    return tr.touched_paths_from_messages(msgs)


def session_mutate_paths(sid: str, cwd: Path) -> list[str]:
    """Write/Edit/Delete paths from the wrap session, or empty to mean 'no filter'."""
    sid = (sid or "").strip()
    if not sid:
        return []
    if sid.startswith("h:"):
        parts = sid.split(":", 2)
        if len(parts) != 3:
            return []
        agent, native = parts[1], parts[2]
        if agent == "opencode":
            return _oc_touched_paths(native, cwd)
        if agent == "hermes":
            try:
                return tr.touched_paths_from_messages(hm.list_messages(native))
            except RuntimeError:
                return []
        path = history_transcript(agent, cwd, native)
        return tr.touched_paths_jsonl(path)
    try:
        sess = get_session(sid)
    except KeyError:
        return []
    agent = str(sess.get("agent") or "")
    if agent == "opencode":
        return _oc_touched_paths(str(sess.get("oc_id") or ""), Path(sess.get("cwd") or cwd))
    if agent == "hermes":
        try:
            return tr.touched_paths_from_messages(hm.list_messages(str(sess.get("hm_id") or "")))
        except RuntimeError:
            return []
    raw = sess.get("transcript")
    path = Path(raw) if raw else pick_transcript(sess)
    return tr.touched_paths_jsonl(path)


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
    elif agent == "hermes":
        messages = hm.list_messages(native_id)
        title = hm.inferred_title(native_id, cwd)
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
        "oc_id": native_id if agent == "opencode" else "",
        "hm_id": native_id if agent == "hermes" else "",
        "transcript": transcript,
        "title": title,
        "model": "",
        "effort": "",
        "fast": False,
        "created": "",
        "cli_session": native_id if agent in ("claude", "cursor") else "",
        "native_id": native_id,
        "busy": False,
        "subagents": [],
        "choice": None,
        "messages": messages,
        "live": False,
        "pinned": native_is_pinned(agent, native_id),
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
    prompt: str = "",
) -> dict[str, Any]:
    if agent not in AGENTS:
        raise ValueError(f"unknown agent: {agent}")
    if agent == "hermes" and not hermes_on():
        raise ValueError("Hermes is disabled — set HERMES=1 in .env")
    resume_id = (resume_id or "").strip()
    prompt = (prompt or "").strip()
    if resume_id:
        existing = find_live_native(agent, resume_id, cwd)
        if existing:
            return existing
    sid = new_session_id(agent)
    user_title = (title or "").strip()
    title = user_title or default_title(agent, cwd, model, effort)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    if agent == "opencode":
        oc_id = resume_id
        sess = {
            "id": sid,
            "agent": "opencode",
            "cwd": str(cwd),
            "oc_id": oc_id,
            "hm_id": None,
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
        if prompt:
            enqueue_send(sid, prompt)
        return sess

    if agent == "hermes":
        if not hm.api_key():
            raise RuntimeError("HERMES_API_SERVER_KEY is empty")
        if not hm.health():
            raise RuntimeError("Hermes gateway is not reachable at " + hm.HM_URL)
        hm_id = resume_id or hm.create_session(cwd, user_title, model, effort)
        hm.remember_cwd(hm_id, cwd)
        sess = {
            "id": sid,
            "agent": "hermes",
            "cwd": str(cwd),
            "oc_id": None,
            "hm_id": hm_id,
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
        if prompt:
            enqueue_send(sid, prompt)
        return sess

    if agent == "console":
        ptyio.start(sid, cwd)
        sess = {
            "id": sid,
            "agent": "console",
            "cwd": str(cwd),
            "oc_id": None,
            "hm_id": None,
            "transcript": None,
            "title": title,
            "model": "",
            "effort": "",
            "fast": False,
            "created": created,
            "cli_session": "",
        }
        with _lock:
            SESSIONS[sid] = sess
        return sess

    if agent == "claude":
        cli_session = resume_id if resume_id else str(uuid.uuid4())
        transcript = None
        if resume_id:
            for p in tr.list_transcripts(agent, cwd, CLAUDE_HOME, CURSOR_HOME):
                if p.stem == resume_id and p.is_file():
                    transcript = str(p)
                    break
        sess = {
            "id": sid,
            "agent": "claude",
            "cwd": str(cwd),
            "oc_id": None,
            "hm_id": None,
            "transcript": transcript,
            "seen_transcripts": snapshot_transcripts(agent, cwd),
            "title": title,
            "model": model,
            "effort": effort,
            "fast": fast,
            "created": created,
            "cli_session": cli_session,
            "hl_fresh": not bool(resume_id),
            "boot_prompt": prompt if prompt else "",
            "title_source": "user" if user_title else "wrap",
        }
        with _lock:
            SESSIONS[sid] = sess
        persist_state()
        if prompt:
            enqueue_send(sid, prompt)
        return sess

    if agent == "cursor":
        cli_session = resume_id if resume_id else cursor_create_chat(cwd)
        transcript = None
        if resume_id:
            path = history_transcript("cursor", cwd, resume_id)
            if path:
                transcript = str(path)
        sess = {
            "id": sid,
            "agent": "cursor",
            "cwd": str(cwd),
            "oc_id": None,
            "hm_id": None,
            "transcript": transcript,
            "seen_transcripts": snapshot_transcripts(agent, cwd),
            "title": title,
            "model": model,
            "effort": effort,
            "fast": fast,
            "created": created,
            "cli_session": cli_session,
            "hl_fresh": not bool(transcript),
            "title_source": "user" if user_title else "wrap",
        }
        with _lock:
            SESSIONS[sid] = sess
        persist_state()
        if prompt:
            enqueue_send(sid, prompt)
        # A new session leaves the history list (it is live now).
        bump_history_gen()
        return sess

    raise RuntimeError(f"unknown agent: {agent}")


def get_session(sid: str) -> dict[str, Any]:
    with _lock:
        sess = SESSIONS.get(sid)
    if not sess:
        raise KeyError(sid)
    return sess


def list_sessions(cwd: Path | None = None, agent: str | None = None) -> list[dict[str, Any]]:
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
        maybe_title_session(sess)
        live = http_live(sess)
        if not live:
            if sess.get("agent") == "console":
                ptyio.kill(str(sess.get("id") or ""))
                with _lock:
                    SESSIONS.pop(str(sess.get("id") or ""), None)
                changed = True
            continue
        subagents = session_subagents(sess)
        choice = session_choice(sess)
        pub = {
            **session_meta(sess),
            "busy": session_is_working(sess) or bool(subagents) or bool(choice),
            "live": True,
            "subagents": subagents,
            "choice": choice,
            "pinned": native_is_pinned(*native_key(sess)),
        }
        out.append(pub)
    if changed:
        persist_state()
    out.sort(key=lambda s: s.get("created") or "", reverse=True)
    out.sort(key=lambda s: 0 if s.get("pinned") else 1)
    return out


def kill_session(sid: str) -> None:
    stop_send_queue(sid)
    sess = get_session(sid)
    if sess.get("agent") == "hermes" and sess.get("hm_id"):
        try:
            hm.abort(str(sess["hm_id"]))
        except RuntimeError:
            pass
    if sess.get("agent") in ("claude", "cursor", "opencode"):
        hl_interrupt(sid, kill=True)
        with _lock:
            _hl_choices.pop(sid, None)
            _hl_pending.pop(sid, None)
        oc_id = str(sess.get("oc_id") or "")
        if oc_id:
            oc.clear_live_messages(oc_id)
    if sess.get("agent") == "console":
        ptyio.kill(sid)
    with _lock:
        SESSIONS.pop(sid, None)
    # Closing a session turns it back into a history entry.
    _turn_state.pop(sid, None)
    bump_history_gen()
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
                rel = path[len("/static/") :]
                suffix = Path(rel).suffix.lower()
                ctype = {
                    ".css": "text/css; charset=utf-8",
                    ".js": "application/javascript; charset=utf-8",
                    ".svg": "image/svg+xml",
                    ".html": "text/html; charset=utf-8",
                    ".map": "application/json",
                }.get(suffix, "application/octet-stream")
                return self._static(rel, ctype)
            if path == "/api/health":
                body = {
                    "ok": True,
                    "opencode": shutil_which("opencode") is not None,
                    "opencode_serve": False,
                    "hermes": hermes_on() and hm.health(),
                    "hermes_enabled": hermes_on(),
                    "host_projects": str(HOST_PROJECTS),
                }
                st, raw, ct = json_bytes(body)
                return self._send(st, raw, ct)
            if path == "/api/alerts":
                return self._alerts_stream()
            if path == "/api/catalog":
                fresh = str((qs.get("fresh") or [""])[0] or "").strip() in ("1", "true", "yes")
                st, raw, ct = json_bytes(catalog(fresh=fresh))
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
            if path == "/api/git":
                cwd = safe_cwd((qs.get("cwd") or [""])[0])
                sid = str((qs.get("sid") or [""])[0] or "").strip()
                want = str((qs.get("diffs") or [""])[0] or "").strip() in ("1", "true", "yes")
                only = session_mutate_paths(sid, cwd) if sid else None
                st, raw, ct = json_bytes(
                    ws.git_view(
                        cwd,
                        HOST_PROJECTS,
                        only_paths=only or None,
                        with_diffs=want,
                    )
                )
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
            if path == "/api/history/pin":
                pinned = data.get("pinned")
                if not isinstance(pinned, bool):
                    raise ValueError("pinned must be true or false")
                keys = set_pinned(
                    str(data.get("agent") or ""),
                    str(data.get("native") or ""),
                    pinned,
                )
                st, raw, ct = json_bytes({"ok": True, "pinned": keys})
                return self._send(st, raw, ct)
            if path == "/api/settings":
                fb = set_title_fallback(data.get("title_fallback") if isinstance(data.get("title_fallback"), dict) else data)
                st, raw, ct = json_bytes({"ok": True, "title_fallback": fb})
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
                    prompt=str(data.get("text") or ""),
                )
                st, raw, ct = json_bytes(session_public(sess), 201)
                return self._send(st, raw, ct)
            m = re.fullmatch(r"/api/sessions/([^/]+)/send", path)
            if m:
                return self._send_msg(m.group(1), data)
            m = re.fullmatch(r"/api/sessions/([^/]+)/input", path)
            if m:
                sess = get_session(m.group(1))
                if sess.get("agent") != "console":
                    raise RuntimeError("input is only for console sessions")
                ptyio.write(m.group(1), str(data.get("data") or ""))
                st, raw, ct = json_bytes({"ok": True})
                return self._send(st, raw, ct)
            m = re.fullmatch(r"/api/sessions/([^/]+)/resize", path)
            if m:
                sess = get_session(m.group(1))
                if sess.get("agent") != "console":
                    raise RuntimeError("resize is only for console sessions")
                ptyio.resize(m.group(1), int(data.get("cols") or 140), int(data.get("rows") or 48))
                st, raw, ct = json_bytes({"ok": True})
                return self._send(st, raw, ct)
            m = re.fullmatch(r"/api/sessions/([^/]+)/interrupt", path)
            if m:
                sess = get_session(m.group(1))
                if sess["agent"] == "hermes" and sess.get("hm_id"):
                    hm.abort(str(sess["hm_id"]))
                elif sess["agent"] in ("claude", "cursor", "opencode"):
                    hl_interrupt(m.group(1))
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
                if sess["agent"] != "claude":
                    raise RuntimeError("no pending choice handler")
                hl_reply_choice(m.group(1), choice, idxs)
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

    def _static(self, rel: str, content_type: str) -> None:
        if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
            self._send(404, b"not found", "text/plain")
            return
        root = STATIC.resolve()
        path = (STATIC / rel).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            self._send(404, b"not found", "text/plain")
            return
        body = path.read_bytes()
        self._send(200, body, content_type)

    def _send_msg(self, sid: str, data: dict[str, Any]) -> None:
        sess = get_session(sid)
        text = str(data.get("text") or "")
        if not text.strip():
            raise ValueError("empty message")
        if sess["agent"] == "hermes":
            if not sess.get("hm_id"):
                raise RuntimeError("no hermes session")
        elif sess["agent"] == "console":
            raise RuntimeError("console has no chat")
        elif sess["agent"] not in ("claude", "cursor", "opencode"):
            raise RuntimeError("unknown agent")
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

    def _sse(self, event: str, obj: dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False)
        self.wfile.write(f"event: {event}\ndata: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _stream(self, sid: str) -> None:
        sess = get_session(sid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if sess.get("agent") == "console":
            self._console_stream(sid)
            return
        last = ""
        # Every attached stream tracks which turn-end it has already reported, so
        # a transition detected by one tab reaches all of them.
        seen_seq = turn_seq(sid)
        try:
            while True:
                try:
                    sess = get_session(sid)
                except KeyError:
                    _turn_state.pop(sid, None)
                    self._sse("gone", {})
                    return
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
                        "q": [len(t) for t in (payload.get("queued") or [])],
                    },
                    ensure_ascii=False,
                )
                if fp != last:
                    last = fp
                    chunk = f"event: sync\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                busy = bool(payload.get("busy"))
                bump_hist, turn_end, gen_start = turn_step(
                    sid, busy, fp, ws.cache_gen()
                )
                if bump_hist:
                    # Turn boundaries are when the session list can change.
                    bump_history_gen()
                if turn_end and int(turn_end.get("seq") or 0) > seen_seq:
                    seen_seq = int(turn_end["seq"])
                    ws.invalidate(gen_start)
                    # Only the stream that ran the transition invalidates; the
                    # others just refresh off the broadcast.
                    self._sse("turnend", {"sid": sid, **turn_end})
                if sess.get("agent") == "hermes":
                    hm.wait(str(sess.get("hm_id") or ""), timeout=0.35 if busy else 1.2)
                else:
                    time.sleep(0.4 if busy else 1.2)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    def _console_stream(self, sid: str) -> None:
        pos = 0
        try:
            payload = session_public(get_session(sid))
            chunk = f"event: sync\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            self.wfile.write(chunk.encode("utf-8"))
            self.wfile.flush()
            while True:
                pos, data = ptyio.since(sid, pos)
                if data:
                    body = json.dumps({"b": base64.b64encode(data).decode("ascii")})
                    self.wfile.write(f"event: term\ndata: {body}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    continue
                if not ptyio.alive(sid):
                    self.wfile.write(b"event: gone\ndata: {}\n\n")
                    self.wfile.flush()
                    with _lock:
                        SESSIONS.pop(sid, None)
                    ptyio.kill(sid)
                    return
                ptyio.wait(sid, pos, timeout=0.4)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    load_state()
    ptyio.reap_orphans()
    install_cmux_shim()
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
        ptyio.kill_all()
        httpd.server_close()


if __name__ == "__main__":
    main()
