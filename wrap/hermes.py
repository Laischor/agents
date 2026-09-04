"""Hermes Agent gateway client for wrap.

Talks to the OpenAI-compatible API + sessions/runs surface on :8642.
Shown as a wrap agent only when HERMES=1 (env or the agents-repo .env).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import transcripts as tr

HM_URL = os.environ.get("WRAP_HERMES_URL", "http://hermes:8642").rstrip("/")
PASTE_IMG_RE = re.compile(
    r"(^|\s)(/\S+\.wrap-pastes/\S+\.(?:png|jpe?g|gif|webp))",
    re.I,
)
MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
HOST_PROJECTS = Path(os.environ.get("HOST_PROJECTS", "/Users/mr/projects")).resolve()

_lock = threading.RLock()
_cv = threading.Condition(_lock)
_busy: dict[str, str] = {}  # hm_id -> run_id (empty string = chat in flight)
_gen: dict[str, int] = {}
_all = 0
_cwd: dict[str, str] = {}
_memo_lock = threading.Lock()
_memo: dict[str, tuple[float, Any]] = {}
_dotenv_once = False
_dotenv: dict[str, str] = {}


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} {msg}", flush=True)


def _truthy(raw: str | None) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        val = val.strip().strip("'").strip('"')
        if key:
            out[key] = val
    return out


def _agents_env_paths() -> list[Path]:
    paths: list[Path] = []
    explicit = os.environ.get("AGENTS_DIR") or os.environ.get("WRAP_AGENTS_DIR")
    if explicit:
        paths.append(Path(explicit) / ".env")
    if HOST_PROJECTS.is_dir():
        paths.append(HOST_PROJECTS / "agents" / ".env")
        if (HOST_PROJECTS / "wrap").is_dir() and (HOST_PROJECTS / "docker-compose.yml").is_file():
            paths.append(HOST_PROJECTS / ".env")
        try:
            for child in HOST_PROJECTS.iterdir():
                envf = child / ".env"
                compose = child / "docker-compose.yml"
                wrap_dir = child / "wrap"
                if envf.is_file() and compose.is_file() and wrap_dir.is_dir():
                    paths.append(envf)
        except OSError:
            pass
    seen: set[str] = set()
    uniq: list[Path] = []
    for p in paths:
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def _dotenv_get(key: str, default: str = "") -> str:
    global _dotenv_once, _dotenv
    env = os.environ.get(key)
    if env:
        return env
    if not _dotenv_once:
        merged: dict[str, str] = {}
        for path in _agents_env_paths():
            if path.is_file():
                merged.update(_parse_dotenv(path))
        _dotenv = merged
        _dotenv_once = True
    return _dotenv.get(key, default)


def enabled() -> bool:
    return _truthy(_dotenv_get("HERMES", "0"))


def api_key() -> str:
    return (_dotenv_get("HERMES_API_SERVER_KEY") or os.environ.get("API_SERVER_KEY") or "").strip()


def remember_cwd(hm_id: str, cwd: Path | str) -> None:
    hm_id = (hm_id or "").strip()
    if not hm_id:
        return
    with _lock:
        _cwd[hm_id] = str(cwd)


def load_cwd_map(rows: dict[str, str]) -> None:
    with _lock:
        _cwd.clear()
        for key, val in (rows or {}).items():
            if key and val:
                _cwd[str(key)] = str(val)


def cwd_map() -> dict[str, str]:
    with _lock:
        return dict(_cwd)


def cwd_digest(cwd: Path | str) -> str:
    return hashlib.sha1(str(Path(cwd)).encode("utf-8")).hexdigest()[:10]


def lookup_cwd(hm_id: str, projects: list[Path] | None = None) -> str:
    hm_id = (hm_id or "").strip()
    with _lock:
        known = _cwd.get(hm_id, "")
    if known:
        return known
    parts = hm_id.split("_")
    if len(parts) >= 3 and parts[0] == "wrap":
        digest = parts[1]
        for p in projects or []:
            if cwd_digest(p) == digest:
                return str(p)
    return ""


def health() -> bool:
    try:
        req = urllib.request.Request(f"{HM_URL}/health", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if not (200 <= resp.status < 300):
                return False
            raw = resp.read()
            if not raw:
                return True
            data = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(data, dict) and data.get("status"):
                return str(data.get("status")).lower() in {"ok", "healthy", "ready"}
            return True
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return False


def available() -> bool:
    return enabled() and bool(api_key())


def _cached(key: str, ttl: float, fn: Any) -> Any:
    now = time.time()
    with _memo_lock:
        hit = _memo.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _memo_lock:
        _memo[key] = (time.time(), val)
    return val


def _invalidate(*prefixes: str) -> None:
    with _memo_lock:
        for key in list(_memo):
            if any(key.startswith(p) for p in prefixes):
                _memo.pop(key, None)


def _as_list(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "sessions", "messages", "providers", "models"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def _as_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        inner = payload.get("session")
        if isinstance(inner, dict) and "id" not in payload:
            return inner
        inner = payload.get("data")
        if isinstance(inner, dict) and "id" not in payload:
            return inner
        return payload
    return {}


def request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> Any:
    key = api_key()
    if not key:
        raise RuntimeError("HERMES_API_SERVER_KEY is empty")
    q = ""
    if query:
        q = "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = None if body is None else json.dumps(body).encode()
    hdrs = {
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
    }
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(f"{HM_URL}{path}{q}", data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            ctype = resp.headers.get("Content-Type") or ""
            if "json" in ctype or raw[:1] in (b"{", b"["):
                return json.loads(raw.decode())
            return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"hermes {method} {path}: {exc.code} {err[:400]}") from exc


def _session_id(payload: Any) -> str:
    info = _as_dict(payload)
    sid = info.get("id") or info.get("session_id") or ""
    if not sid and isinstance(payload, dict):
        sess = payload.get("session")
        if isinstance(sess, dict):
            sid = sess.get("id") or ""
    return str(sid or "")


def _cwd_prompt(cwd: Path | str) -> str:
    path = str(cwd)
    return (
        f"You are working in the project directory: {path}\n"
        "That path is mounted into this Hermes container at the same location. "
        "Use it as the working directory for shell, file, and execute_code tools."
    )


def split_model(model: str) -> tuple[str, str]:
    model = (model or "").strip()
    if not model or model in {"hermes-agent", "default"}:
        return "", ""
    if "/" in model and not model.startswith("/"):
        provider, mid = model.split("/", 1)
        return provider.strip(), mid.strip()
    return "", model


def model_body(model: str, effort: str) -> dict[str, Any]:
    provider, mid = split_model(model)
    body: dict[str, Any] = {}
    if mid:
        body["model"] = mid
    if provider:
        body["provider"] = provider
    if effort:
        body["model_options"] = {"reasoning_effort": effort}
    return body


def create_session(cwd: Path, title: str, model: str = "", effort: str = "") -> str:
    sid = f"wrap_{cwd_digest(cwd)}_{secrets.token_hex(4)}"
    body: dict[str, Any] = {
        "id": sid,
        "source": "api_server",
        "system_prompt": _cwd_prompt(cwd),
    }
    t = (title or "").strip()
    if t:
        body["title"] = t[:120]
    body.update(model_body(model, effort))
    created = request("POST", "/api/sessions", body)
    got = _session_id(created) or sid
    remember_cwd(got, cwd)
    _invalidate("sess:", "list:")
    bump(got)
    return got


def get_session(hm_id: str) -> dict[str, Any]:
    if not hm_id:
        return {}
    return _cached(
        f"sess:{hm_id}",
        2.0,
        lambda: _as_dict(request("GET", f"/api/sessions/{hm_id}")),
    )


def list_gateway_sessions(limit: int = 200) -> list[dict[str, Any]]:
    def _fetch() -> list[dict[str, Any]]:
        try:
            payload = request("GET", "/api/sessions", query={"limit": str(limit), "offset": "0"})
        except RuntimeError:
            return []
        out: list[dict[str, Any]] = []
        for row in _as_list(payload):
            if not isinstance(row, dict):
                continue
            sid = str(row.get("id") or "")
            if not sid:
                continue
            if row.get("archived") or row.get("hidden"):
                continue
            if row.get("parent_session_id"):
                continue
            out.append(row)
        return out

    return _cached("list:sessions", 3.0, _fetch)


def _time_sec(raw: Any) -> float:
    try:
        n = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if n > 1e12:
        return n / 1000.0
    return n


def inferred_title(hm_id: str, cwd: Path | str = "") -> str:
    if not hm_id:
        return ""
    try:
        info = get_session(hm_id)
    except RuntimeError:
        info = {}
    title = str((info or {}).get("title") or "").strip()
    if title and not tr.is_wrap_default_title(title):
        proj = Path(str(cwd)).name if cwd else ""
        if not proj or title != proj:
            return title
    for msg in list_messages(hm_id, limit=30):
        if msg.get("role") != "user":
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        line = text.split("\n")[0].strip()
        if len(line) > 72:
            line = line[:69].rstrip() + "…"
        return line
    return title


def session_busy(hm_id: str) -> bool:
    if not hm_id:
        return False
    with _lock:
        return hm_id in _busy


def bump(hm_id: str = "") -> None:
    global _all
    with _cv:
        _all += 1
        if hm_id:
            _gen[hm_id] = _gen.get(hm_id, 0) + 1
        _cv.notify_all()


def wait(hm_id: str, timeout: float = 0.5) -> None:
    with _cv:
        start = _gen.get(hm_id, 0) + _all
        _cv.wait_for(lambda: _gen.get(hm_id, 0) + _all != start, timeout=timeout)


def wait_idle(hm_id: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not session_busy(hm_id):
            return
        wait(hm_id, timeout=0.4)
    raise RuntimeError("hermes session stayed busy")


_TERMINAL_RUN = frozenset({"completed", "failed", "cancelled", "canceled", "stopped", "error"})


def _current_run(hm_id: str) -> str | None:
    """Return run id, empty string if starting, or None if idle."""
    with _lock:
        if hm_id not in _busy:
            return None
        return _busy.get(hm_id) or ""


def _wait_run_id(hm_id: str, timeout: float = 3.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cur = _current_run(hm_id)
        if cur is None:
            return ""
        if cur:
            return cur
        wait(hm_id, timeout=0.1)
    cur = _current_run(hm_id)
    return cur or ""


def _wait_run_done(run_id: str, timeout: float = 20.0) -> None:
    if not run_id:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            info = request("GET", f"/v1/runs/{run_id}", timeout=8.0)
        except RuntimeError:
            return
        status = str((info or {}).get("status") or "").lower() if isinstance(info, dict) else ""
        if status in _TERMINAL_RUN:
            return
        time.sleep(0.25)


def abort(hm_id: str) -> None:
    """Stop the in-flight gateway run (Hermes /stop), then clear wrap busy."""
    run_id = _wait_run_id(hm_id, timeout=3.0)
    if run_id:
        try:
            request("POST", f"/v1/runs/{run_id}/stop", {})
        except RuntimeError as exc:
            log(f"hermes stop {run_id}: {exc}")
        _wait_run_done(run_id, timeout=20.0)
    with _lock:
        cur = _busy.get(hm_id)
        if cur == run_id or cur == "":
            _busy.pop(hm_id, None)
    _invalidate("msg:", "sess:")
    bump(hm_id)


def prompt_parts(text: str) -> Any:
    src = text or ""
    parts: list[dict[str, Any]] = []
    last = 0
    for m in PASTE_IMG_RE.finditer(src):
        prefix = (src[last : m.start()] + m.group(1)).strip()
        if prefix:
            parts.append({"type": "text", "text": prefix})
        path = m.group(2)
        last = m.end()
        img = _image_part(path)
        parts.append(img if img else {"type": "text", "text": path})
    tail = src[last:].strip()
    if tail:
        parts.append({"type": "text", "text": tail})
    if not parts:
        return src
    if len(parts) == 1 and parts[0].get("type") == "text":
        return parts[0].get("text") or src
    return parts


def _image_part(path: str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    mime = MIME_BY_EXT.get(p.suffix.lower())
    if not mime:
        return None
    try:
        raw = p.read_bytes()
    except OSError:
        return None
    b64 = base64.b64encode(raw).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def _history_for_run(hm_id: str) -> list[dict[str, str]]:
    """Prior user/assistant text (Open WebUI-style). Tool-only rows are omitted."""
    out: list[dict[str, str]] = []
    try:
        rows = _fetch_messages(hm_id, limit=500)
    except Exception:  # noqa: BLE001
        return out
    for msg in rows:
        role = str(msg.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        out.append({"role": role, "content": text})
    return out


def prompt_async(hm_id: str, cwd: Path | str, text: str, model: str = "", effort: str = "") -> None:
    if not hm_id:
        raise RuntimeError("no hermes session")
    with _lock:
        _busy[hm_id] = ""
    bump(hm_id)
    worker = threading.Thread(
        target=_run_turn,
        args=(hm_id, cwd, text, model, effort),
        daemon=True,
        name=f"wrap-hermes-{hm_id[-8:]}",
    )
    worker.start()


def _run_turn(hm_id: str, cwd: Path | str, text: str, model: str, effort: str) -> None:
    _invalidate("msg:", f"sess:{hm_id}")
    history = _history_for_run(hm_id)
    body: dict[str, Any] = {
        "input": prompt_parts(text),
        "session_id": hm_id,
        "instructions": _cwd_prompt(cwd),
    }
    if history:
        body["conversation_history"] = history
    body.update(model_body(model, effort))
    run_id = ""
    try:
        log(f"hermes turn {hm_id} history={len(history)}")
        started = request(
            "POST",
            "/v1/runs",
            body,
            timeout=60.0,
            headers={"X-Hermes-Session-Id": hm_id},
        )
        info = started if isinstance(started, dict) else {}
        run_id = str(info.get("run_id") or info.get("id") or "")
        if run_id:
            with _lock:
                if hm_id in _busy:
                    _busy[hm_id] = run_id
            bump(hm_id)
            _poll_run(hm_id, run_id)
        else:
            request(
                "POST",
                f"/api/sessions/{hm_id}/chat",
                {"message": body["input"], **model_body(model, effort)},
                timeout=300.0,
            )
    except Exception as exc:  # noqa: BLE001
        log(f"hermes turn failed {hm_id}: {exc}")
    finally:
        with _lock:
            cur = _busy.get(hm_id)
            if cur == run_id or (cur == "" and not run_id):
                _busy.pop(hm_id, None)
        _invalidate("msg:", f"sess:{hm_id}", "list:")
        bump(hm_id)


def _poll_run(hm_id: str, run_id: str) -> None:
    deadline = time.time() + 600.0
    while time.time() < deadline:
        with _lock:
            if _busy.get(hm_id) != run_id:
                return
        try:
            info = request("GET", f"/v1/runs/{run_id}", timeout=15.0)
        except RuntimeError:
            time.sleep(0.8)
            continue
        status = str((info or {}).get("status") or "").lower() if isinstance(info, dict) else ""
        if status in _TERMINAL_RUN:
            return
        bump(hm_id)
        time.sleep(0.6)


def list_messages(hm_id: str, limit: int = 300) -> list[dict[str, Any]]:
    if not hm_id:
        return []
    gen = 0
    with _lock:
        gen = _gen.get(hm_id, 0) + _all
    return _cached(
        f"msg:{hm_id}:{gen}",
        0.25,
        lambda: _fetch_messages(hm_id, limit),
    ) or []


def _fetch_messages(hm_id: str, limit: int) -> list[dict[str, Any]]:
    try:
        payload = request(
            "GET",
            f"/api/sessions/{hm_id}/messages",
            query={"limit": str(min(limit, 500)), "order": "oldest"},
        )
    except RuntimeError:
        return []
    out: list[dict[str, Any]] = []
    for row in _as_list(payload):
        msg = message_from_api(row)
        if msg:
            out.append(msg)
    return tr.merge_turns(out)[-limit:]


def _content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for part in content:
            if isinstance(part, str):
                bits.append(part)
            elif isinstance(part, dict):
                if part.get("text"):
                    bits.append(str(part.get("text") or ""))
                elif isinstance(part.get("content"), str):
                    bits.append(str(part.get("content") or ""))
        return "\n".join(b for b in bits if b)
    return str(content)


def message_from_api(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    role = str(row.get("role") or "assistant")
    if role == "tool":
        name = str(row.get("tool_name") or row.get("name") or "tool")
        text = _content_text(row.get("content"))
        return {
            "id": str(row.get("id") or row.get("tool_call_id") or ""),
            "role": "assistant",
            "text": "",
            "parts": [{"type": "tool", "name": name, "detail": text[:200] if text else "", "status": "completed"}],
            "ts": row.get("timestamp") or "",
        }
    if role not in ("user", "assistant", "system"):
        role = "assistant"
    if role == "system":
        return None
    parts: list[dict[str, Any]] = []
    content = row.get("content")
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                if part:
                    parts.append({"type": "text", "text": str(part)})
                continue
            kind = str(part.get("type") or "")
            if kind in ("text", "output_text", "input_text") and part.get("text"):
                parts.append({"type": "text", "text": str(part.get("text") or "")})
            elif kind in ("image_url", "input_image", "image"):
                url = ""
                img = part.get("image_url")
                if isinstance(img, dict):
                    url = str(img.get("url") or "")
                elif isinstance(img, str):
                    url = img
                url = url or str(part.get("url") or part.get("image_url") or "")
                if url:
                    parts.append({"type": "image", "url": url})
            elif part.get("text"):
                parts.append({"type": "text", "text": str(part.get("text") or "")})
    else:
        text = _content_text(content)
        if text:
            parts.append({"type": "text", "text": text})
    for call in row.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(fn.get("name") or call.get("name") or "tool")
        parts.append({"type": "tool", "name": name, "status": "completed"})
    if not parts:
        return None
    text = "\n\n".join(p.get("text") or "" for p in parts if p.get("type") == "text").strip()
    return {
        "id": str(row.get("id") or ""),
        "role": "user" if role == "user" else "assistant",
        "text": text,
        "parts": parts,
        "ts": row.get("timestamp") or "",
    }


def providers() -> list[dict[str, str]]:
    out: list[dict[str, str]] = [{"id": "", "label": "Gateway default"}]
    seen: set[str] = set()

    def add(slug: str, label: str) -> None:
        slug = (slug or "").strip()
        if not slug or slug in seen:
            return
        seen.add(slug)
        out.append({"id": slug, "label": label or slug})

    try:
        payload = request("GET", "/api/model/options")
    except RuntimeError:
        payload = None
    if isinstance(payload, dict):
        rows = payload.get("providers")
        if not isinstance(rows, list):
            rows = payload.get("data") if isinstance(payload.get("data"), list) else []
        for provider in rows:
            if not isinstance(provider, dict):
                continue
            pid = str(provider.get("id") or provider.get("provider") or provider.get("slug") or "")
            pname = str(provider.get("name") or provider.get("label") or pid)
            models = provider.get("models")
            if isinstance(models, dict):
                for mid, meta in models.items():
                    label = ""
                    if isinstance(meta, dict):
                        label = str(meta.get("name") or meta.get("label") or "")
                        mid = str(meta.get("id") or mid)
                    slug = f"{pid}/{mid}" if pid else str(mid)
                    if pname and label and pname.lower() not in label.lower():
                        label = f"{label} ({pname})"
                    add(slug, label or slug)
            elif isinstance(models, list):
                for meta in models:
                    if isinstance(meta, dict):
                        mid = str(meta.get("id") or meta.get("model") or "")
                        label = str(meta.get("name") or meta.get("label") or mid)
                    else:
                        mid, label = str(meta), str(meta)
                    if not mid:
                        continue
                    slug = f"{pid}/{mid}" if pid and "/" not in mid else mid
                    if pname and label and pname.lower() not in label.lower():
                        label = f"{label} ({pname})"
                    add(slug, label)
            elif pid:
                add(pid, pname)

    if len(out) <= 1:
        try:
            payload = request("GET", "/v1/models")
        except RuntimeError:
            payload = None
        for row in _as_list(payload):
            if isinstance(row, dict):
                mid = str(row.get("id") or "")
                label = str(row.get("name") or row.get("id") or "")
                add(mid, label)
            elif row:
                add(str(row), str(row))
    return out[:180]


def history_rows(
    projects: list[Path],
    skip: set[str],
    limit: int = 80,
    keep: set[str] | None = None,
) -> list[dict[str, Any]]:
    if not available():
        return []
    keep = keep or set()
    proj_paths = {str(p) for p in projects}
    rows: list[tuple[float, dict[str, Any]]] = []
    seen: set[str] = set()
    try:
        sessions = list_gateway_sessions()
    except RuntimeError:
        return []
    for row in sessions:
        sid = str(row.get("id") or "")
        if not sid or sid in skip or sid in seen:
            continue
        if not sid.startswith("wrap_"):
            continue
        seen.add(sid)
        cwd = lookup_cwd(sid, projects)
        if cwd and proj_paths and cwd not in proj_paths and sid not in keep:
            continue
        if not cwd:
            cwd = str(HOST_PROJECTS)
        title = str(row.get("title") or "").strip()
        if not title or tr.is_wrap_default_title(title):
            title = inferred_title(sid, cwd) or title
        updated = _time_sec(row.get("last_active") or row.get("started_at") or row.get("ended_at"))
        rows.append(
            (
                updated,
                {
                    "id": f"h:hermes:{sid}",
                    "agent": "hermes",
                    "cwd": cwd,
                    "native_id": sid,
                    "title": title,
                    "transcript": None,
                    "updated": updated,
                    "live": False,
                },
            )
        )
    rows.sort(key=lambda item: item[0], reverse=True)
    kept = [item for _, item in rows if str(item.get("native_id") or "") in keep]
    rest = [item for _, item in rows if str(item.get("native_id") or "") not in keep]
    return kept + rest[:limit]
