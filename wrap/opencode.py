"""OpenCode serve HTTP client.

Talks to the same REST + SSE surface the TUI uses. No SQLite scrape.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from shutil import which
from typing import Any

import transcripts as tr

OC_PORT = int(os.environ.get("WRAP_OPENCODE_PORT", "4097"))
OC_URL = os.environ.get("WRAP_OPENCODE_URL", f"http://127.0.0.1:{OC_PORT}")
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

_proc: subprocess.Popen[bytes] | None = None
_proc_lock = threading.Lock()
_bus = None
_bus_lock = threading.Lock()
_memo_lock = threading.Lock()
_memo: dict[str, tuple[float, Any]] = {}


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} {msg}", flush=True)


def health() -> bool:
    try:
        with urllib.request.urlopen(f"{OC_URL}/global/health", timeout=1) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def ensure_serve() -> None:
    global _proc
    if health():
        start_bus()
        return
    with _proc_lock:
        if health():
            start_bus()
            return
        binary = which("opencode")
        if not binary:
            raise RuntimeError("opencode binary missing")
        log_path = Path(os.environ.get("WRAP_OPENCODE_LOG", "/var/log/opencode-serve.log"))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(log_path, "ab")
        _proc = subprocess.Popen(
            [binary, "serve", "--hostname", "127.0.0.1", "--port", str(OC_PORT)],
            stdout=fh,
            stderr=fh,
            start_new_session=True,
        )
        for _ in range(40):
            if health():
                log(f"opencode serve ready pid={_proc.pid} port={OC_PORT}")
                start_bus()
                return
            time.sleep(0.25)
        raise RuntimeError("opencode serve failed to start (see /var/log/opencode-serve.log)")


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


def request(
    method: str,
    path: str,
    cwd: Path | str | None = None,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 30.0,
) -> Any:
    ensure_serve()
    data = None if body is None else json.dumps(body).encode()
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if cwd:
        headers["x-opencode-directory"] = str(cwd)
    req = urllib.request.Request(f"{OC_URL}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return None
            return json.loads(raw.decode())
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"opencode {method} {path}: {exc.code} {err[:400]}") from exc


def _as_list(payload: Any) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "sessions", "messages", "providers"):
            val = payload.get(key)
            if isinstance(val, list):
                return val
    return []


def _as_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        inner = payload.get("data")
        if isinstance(inner, dict) and "id" not in payload:
            return inner
        return payload
    return {}


def _session_id(payload: Any) -> str:
    info = _as_dict(payload)
    sid = info.get("id") or info.get("sessionID") or (_as_dict(info.get("info")).get("id") if info.get("info") else "")
    return str(sid or "")


def create_session(cwd: Path, title: str) -> str:
    created = request("POST", "/session", cwd, {"title": title or cwd.name})
    sid = _session_id(created)
    if sid:
        return sid
    raise RuntimeError("opencode did not return a session id")


def get_session(oc_id: str, cwd: Path | str) -> dict[str, Any]:
    return _cached(
        f"sess:{cwd}:{oc_id}",
        2.0,
        lambda: _as_dict(request("GET", f"/session/{oc_id}", cwd)),
    )


def session_title(oc_id: str, cwd: Path | str) -> str:
    if not oc_id:
        return ""
    try:
        info = get_session(oc_id, cwd)
    except RuntimeError:
        return ""
    title = str(info.get("title") or "").strip()
    return "" if not title or tr.is_wrap_default_title(title) else title


def list_sessions(cwd: Path | str) -> list[dict[str, Any]]:
    try:
        rows = _as_list(request("GET", "/session", cwd))
    except RuntimeError:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sid = str(row.get("id") or "")
        if not sid:
            continue
        times = row.get("time") if isinstance(row.get("time"), dict) else {}
        if times.get("archived"):
            continue
        directory = str(row.get("directory") or cwd)
        updated = _time_sec(times.get("updated") or times.get("created") or row.get("time_updated"))
        out.append(
            {
                "id": sid,
                "directory": directory,
                "title": str(row.get("title") or "").strip(),
                "updated": updated,
                "parentID": str(row.get("parentID") or ""),
            }
        )
    return out


def session_status_map(cwd: Path | str) -> dict[str, Any]:
    return _cached(
        f"status:{cwd}",
        0.3,
        lambda: _fetch_status(cwd),
    )


def _fetch_status(cwd: Path | str) -> dict[str, Any]:
    try:
        payload = request("GET", "/session/status", cwd)
    except RuntimeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def is_busy_status(status: Any) -> bool:
    if status is True:
        return True
    if isinstance(status, dict):
        kind = str(status.get("type") or status.get("status") or "").lower()
        return kind in ("busy", "retry", "running", "pending")
    if isinstance(status, str):
        return status.lower() in ("busy", "retry", "running", "pending")
    return False


def session_busy(oc_id: str, cwd: Path | str) -> bool:
    if not oc_id:
        return False
    statuses = session_status_map(cwd)
    if oc_id in statuses:
        return is_busy_status(statuses.get(oc_id))
    return bus().busy(oc_id)


def children(oc_id: str, cwd: Path | str) -> list[dict[str, Any]]:
    return _cached(
        f"children:{cwd}:{oc_id}",
        1.0,
        lambda: _fetch_children(oc_id, cwd),
    )


def _fetch_children(oc_id: str, cwd: Path | str) -> list[dict[str, Any]]:
    try:
        rows = _as_list(request("GET", f"/session/{oc_id}/children", cwd))
    except RuntimeError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def subagents(oc_id: str, cwd: Path | str) -> list[dict[str, Any]]:
    statuses = session_status_map(cwd)
    out: list[dict[str, Any]] = []
    for child in children(oc_id, cwd):
        cid = str(child.get("id") or "")
        if not cid:
            continue
        if not is_busy_status(statuses.get(cid)):
            continue
        out.append(
            {
                "id": cid,
                "description": str(child.get("title") or child.get("slug") or cid),
            }
        )
    return out


def list_permissions(cwd: Path | str) -> list[dict[str, Any]]:
    return _cached(f"perm:{cwd}", 0.4, lambda: _fetch_list("/permission", cwd))


def list_questions(cwd: Path | str) -> list[dict[str, Any]]:
    return _cached(f"question:{cwd}", 0.4, lambda: _fetch_list("/question", cwd))


def _fetch_list(path: str, cwd: Path | str) -> list[dict[str, Any]]:
    try:
        rows = _as_list(request("GET", path, cwd))
    except RuntimeError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def pending_choice(oc_id: str, cwd: Path | str) -> dict[str, Any] | None:
    if not oc_id:
        return None
    cached = bus().choice(oc_id)
    if cached:
        return cached
    for row in list_questions(cwd):
        if str(row.get("sessionID") or "") != oc_id:
            continue
        mapped = _question_choice(row)
        if mapped:
            bus().set_question(oc_id, row)
            return mapped
    for row in list_permissions(cwd):
        if str(row.get("sessionID") or "") != oc_id:
            continue
        mapped = _permission_choice(row)
        if mapped:
            bus().set_permission(oc_id, row)
            return mapped
    return None


def _permission_choice(info: dict[str, Any]) -> dict[str, Any] | None:
    pid = str(info.get("id") or "")
    if not pid:
        return None
    perm = str(info.get("permission") or "permission")
    patterns = info.get("patterns") or []
    if not isinstance(patterns, list):
        patterns = []
    detail = ", ".join(str(p) for p in patterns if p) or perm
    return {
        "id": pid,
        "kind": "permission",
        "title": "Permission",
        "questions": [
            {
                "header": perm,
                "prompt": detail,
                "options": [
                    {"label": "Allow once", "reply": "once"},
                    {"label": "Always allow", "reply": "always"},
                    {"label": "Reject", "reply": "reject"},
                ],
            }
        ],
    }


def _question_choice(info: dict[str, Any]) -> dict[str, Any] | None:
    qid = str(info.get("id") or "")
    questions = info.get("questions") or []
    if not qid or not isinstance(questions, list) or not questions:
        return None
    mapped = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        opts = []
        for opt in q.get("options") or []:
            if isinstance(opt, dict):
                opts.append(
                    {
                        "label": str(opt.get("label") or ""),
                        "description": str(opt.get("description") or ""),
                    }
                )
            elif opt:
                opts.append({"label": str(opt)})
        mapped.append(
            {
                "header": str(q.get("header") or ""),
                "prompt": str(q.get("question") or q.get("prompt") or ""),
                "multi": bool(q.get("multiple")),
                "options": opts,
            }
        )
    if not mapped:
        return None
    return {"id": qid, "kind": "question", "title": "Question", "questions": mapped}


def reply_permission(oc_id: str, permission_id: str, cwd: Path | str, response: str) -> None:
    body = {"response": response}
    try:
        request("POST", f"/session/{oc_id}/permissions/{permission_id}", cwd, body)
    except RuntimeError:
        request("POST", f"/permission/{permission_id}/reply", cwd, {"reply": response})
    bus().clear_permission(oc_id, permission_id)
    _invalidate("perm:", "status:")


def reply_question(request_id: str, cwd: Path | str, answers: list[list[str]]) -> None:
    request("POST", f"/question/{request_id}/reply", cwd, {"answers": answers})
    bus().clear_question_id(request_id)
    _invalidate("question:", "status:")


def abort(oc_id: str, cwd: Path | str) -> None:
    request("POST", f"/session/{oc_id}/abort", cwd, {})
    bus().set_busy(str(oc_id), False)
    _invalidate("status:", "msg:")


def wait_idle(oc_id: str, cwd: Path | str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not session_busy(oc_id, cwd):
            return
        bus().wait(oc_id, timeout=0.4)
    raise RuntimeError("opencode session stayed busy")


def prompt_async(oc_id: str, cwd: Path | str, text: str, model: str = "", effort: str = "") -> None:
    body: dict[str, Any] = {"parts": prompt_parts(text)}
    ref = model_ref(model, effort)
    if ref:
        body["model"] = ref
    request("POST", f"/session/{oc_id}/prompt_async", cwd, body)
    bus().set_busy(str(oc_id), True)
    _invalidate("status:", f"msg:{cwd}:{oc_id}:")


def prompt_parts(text: str) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    src = text or ""
    last = 0
    for m in PASTE_IMG_RE.finditer(src):
        prefix = (src[last : m.start()] + m.group(1)).strip()
        if prefix:
            parts.append({"type": "text", "text": prefix})
        path = m.group(2)
        last = m.end()
        file_part = _file_part(path)
        parts.append(file_part if file_part else {"type": "text", "text": path})
    tail = src[last:].strip()
    if tail:
        parts.append({"type": "text", "text": tail})
    if not parts:
        parts.append({"type": "text", "text": src})
    return parts


def _file_part(path: str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    mime = MIME_BY_EXT.get(p.suffix.lower()) or "application/octet-stream"
    return {
        "type": "file",
        "mime": mime,
        "filename": p.name,
        "url": p.resolve().as_uri(),
    }


def model_ref(model: str, effort: str) -> dict[str, str] | None:
    model = (model or "").strip()
    if not model or "/" not in model:
        return None
    provider, model_id = model.split("/", 1)
    ref: dict[str, str] = {"providerID": provider, "modelID": model_id}
    if effort:
        ref["variant"] = effort
    return ref


def list_messages(oc_id: str, cwd: Path | str, limit: int = 300) -> list[dict[str, Any]]:
    if not oc_id:
        return []
    gen = bus().generation(oc_id)
    return _cached(
        f"msg:{cwd}:{oc_id}:{gen}",
        0.2,
        lambda: _fetch_messages(oc_id, cwd, limit),
    )


def _fetch_messages(oc_id: str, cwd: Path | str, limit: int) -> list[dict[str, Any]]:
    try:
        rows = _as_list(request("GET", f"/session/{oc_id}/message", cwd))
    except RuntimeError:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        msg = messages_from_api_row(row)
        if msg:
            out.append(msg)
    return tr.merge_turns(out)[-limit:]


def messages_from_api_row(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    info = row.get("info") if isinstance(row.get("info"), dict) else row
    raw_parts = row.get("parts")
    if not isinstance(raw_parts, list):
        raw_parts = info.get("parts") if isinstance(info.get("parts"), list) else []
    role = str(info.get("role") or "assistant")
    parts = convert_parts(raw_parts)
    if not parts:
        return None
    created = info.get("time") if isinstance(info.get("time"), dict) else {}
    text = "\n\n".join(p.get("text") or "" for p in parts if p.get("type") == "text").strip()
    return {
        "id": str(info.get("id") or ""),
        "role": "user" if role == "user" else "assistant",
        "text": text,
        "parts": parts,
        "ts": created.get("created") or "",
    }


def convert_parts(raw_parts: list[Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for part in raw_parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text") or "")
            if text.strip() and not part.get("ignored") and not part.get("synthetic"):
                parts.append({"type": "text", "text": text})
        elif kind == "tool":
            tool = str(part.get("tool") or part.get("name") or "tool")
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            inp = state.get("input") if isinstance(state.get("input"), dict) else part.get("input")
            hunks = tr.diffs_from_tool(tool, inp)
            if hunks:
                for h in hunks:
                    parts.append({"type": "diff", **h})
            else:
                item: dict[str, Any] = {"type": "tool", "name": tool}
                status = str(state.get("status") or "")
                if status:
                    item["status"] = status
                title = str(state.get("title") or "")
                if title and title != tool:
                    item["detail"] = title
                parts.append(item)
        elif kind == "file":
            mime = str(part.get("mime") or "")
            url = str(part.get("url") or "")
            filename = str(part.get("filename") or "")
            source = part.get("source") if isinstance(part.get("source"), dict) else {}
            path = str(source.get("path") or "")
            if mime.startswith("image/") and (url or path):
                parts.append(
                    {
                        "type": "image",
                        "url": url,
                        "path": path,
                        "filename": filename,
                    }
                )
            elif path or filename:
                parts.append({"type": "text", "text": path or filename})
        elif kind == "patch":
            for path in part.get("files") or []:
                if path:
                    parts.append({"type": "tool", "name": f"patch {path}"})
        elif kind == "subtask":
            label = str(part.get("description") or part.get("agent") or "subtask")
            parts.append({"type": "tool", "name": label, "status": "running"})
    return parts


def fingerprint(oc_id: str, cwd: Path | str, title: str = "") -> str:
    return bus().fingerprint(oc_id, cwd, title)


def wait(oc_id: str, timeout: float = 0.5) -> None:
    bus().wait(oc_id, timeout=timeout)


def providers(cwd: Path | str | None = None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = [{"id": "", "label": "CLI default"}]
    try:
        payload = request("GET", "/config/providers", cwd)
    except RuntimeError:
        return out
    seen: set[str] = set()
    rows = payload.get("providers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = _as_list(payload)
    for provider in rows:
        if not isinstance(provider, dict):
            continue
        pid = str(provider.get("id") or "")
        pname = str(provider.get("name") or pid)
        models = provider.get("models") if isinstance(provider.get("models"), dict) else {}
        for mid, meta in models.items():
            slug = f"{pid}/{mid}" if pid else str(mid)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            label = ""
            if isinstance(meta, dict):
                label = str(meta.get("name") or "")
            if not label:
                label = str(mid)
            if pname and pname.lower() not in label.lower():
                label = f"{label} ({pname})"
            out.append({"id": slug, "label": label})
    return out


def history_rows(projects: list[Path], skip: set[str], limit: int = 80) -> list[dict[str, Any]]:
    ensure_serve()
    rows: list[tuple[float, dict[str, Any]]] = []
    for cwd in projects:
        if not cwd.is_dir():
            continue
        for oc in list_sessions(cwd):
            sid = oc["id"]
            if not sid or sid in skip or oc.get("parentID"):
                continue
            directory = oc["directory"] or str(cwd)
            try:
                d = Path(directory).resolve()
            except OSError:
                continue
            title = oc["title"]
            if title and tr.is_wrap_default_title(title):
                title = ""
            rows.append(
                (
                    oc["updated"],
                    {
                        "id": f"h:opencode:{sid}",
                        "agent": "opencode",
                        "cwd": str(d),
                        "native_id": sid,
                        "title": title,
                        "transcript": None,
                        "updated": oc["updated"],
                        "live": False,
                    },
                )
            )
    rows.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in rows[:limit]]


def _time_sec(raw: Any) -> float:
    try:
        n = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if n > 1e12:
        return n / 1000.0
    return n


def start_bus() -> None:
    bus()


def bus() -> "EventBus":
    global _bus
    with _bus_lock:
        if _bus is None:
            _bus = EventBus()
            _bus.start()
        return _bus


class EventBus:
    """Subscribe to GET /global/event (fallback /event) and wake wrap streams."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._gen: dict[str, int] = {}
        self._all = 0
        self._busy: dict[str, bool] = {}
        self._perm: dict[str, dict[str, Any]] = {}
        self._question: dict[str, dict[str, Any]] = {}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="wrap-oc-events")
        self._thread.start()

    def bump(self, oc_id: str = "") -> None:
        with self._cv:
            self._all += 1
            if oc_id:
                self._gen[oc_id] = self._gen.get(oc_id, 0) + 1
            else:
                for key in list(self._gen):
                    self._gen[key] = self._gen.get(key, 0) + 1
            self._cv.notify_all()

    def wait(self, oc_id: str, timeout: float = 0.5) -> None:
        with self._cv:
            start = self._gen.get(oc_id, 0) + self._all
            self._cv.wait_for(lambda: self._gen.get(oc_id, 0) + self._all != start, timeout=timeout)

    def fingerprint(self, oc_id: str, cwd: Path | str, title: str = "") -> str:
        choice = self.choice(oc_id)
        cid = (choice or {}).get("id") or ""
        return f"oc:{oc_id}:{self._gen.get(oc_id, 0)}:{self._all}:{int(self.busy(oc_id))}:{cid}:{title}"

    def busy(self, oc_id: str) -> bool:
        return bool(self._busy.get(oc_id))

    def set_busy(self, oc_id: str, busy: bool) -> None:
        if self._busy.get(oc_id) == busy:
            return
        self._busy[oc_id] = busy
        self.bump(oc_id)

    def choice(self, oc_id: str) -> dict[str, Any] | None:
        q = self._question.get(oc_id)
        if q:
            return _question_choice(q)
        p = self._perm.get(oc_id)
        if p:
            return _permission_choice(p)
        return None

    def generation(self, oc_id: str) -> int:
        return self._gen.get(oc_id, 0) + self._all

    def set_permission(self, oc_id: str, info: dict[str, Any]) -> None:
        prev = self._perm.get(oc_id)
        self._perm[oc_id] = info
        if prev and prev.get("id") == info.get("id"):
            return
        self.bump(oc_id)

    def set_question(self, oc_id: str, info: dict[str, Any]) -> None:
        prev = self._question.get(oc_id)
        self._question[oc_id] = info
        if prev and prev.get("id") == info.get("id"):
            return
        self.bump(oc_id)

    def clear_permission(self, oc_id: str, permission_id: str = "") -> None:
        cur = self._perm.get(oc_id)
        if cur and permission_id and str(cur.get("id") or "") not in (permission_id, ""):
            return
        if oc_id in self._perm:
            self._perm.pop(oc_id, None)
            self.bump(oc_id)

    def clear_question_id(self, request_id: str) -> None:
        for oc_id, info in list(self._question.items()):
            if str(info.get("id") or "") == request_id:
                self._question.pop(oc_id, None)
                self.bump(oc_id)

    def _run(self) -> None:
        paths = ("/global/event", "/event")
        idx = 0
        while True:
            path = paths[idx % len(paths)]
            try:
                self._listen(path)
            except Exception as exc:  # noqa: BLE001
                log(f"opencode event stream {path}: {exc}")
            idx += 1
            time.sleep(1.0)

    def _listen(self, path: str) -> None:
        ensure_serve()
        req = urllib.request.Request(
            f"{OC_URL}{path}",
            headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            buf = ""
            while True:
                chunk = resp.read(256)
                if not chunk:
                    return
                buf += chunk.decode("utf-8", "replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    self._on_line(line.rstrip("\r"))

    def _on_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        typ = str(event.get("type") or "")
        props = event.get("properties") if isinstance(event.get("properties"), dict) else {}
        oc_id = str(props.get("sessionID") or props.get("sessionId") or "")
        if typ in ("session.status",):
            status = props.get("status")
            if oc_id:
                self.set_busy(oc_id, is_busy_status(status))
            return
        if typ in ("session.idle",):
            if oc_id:
                self.set_busy(oc_id, False)
            return
        if typ in ("permission.asked", "permission.updated"):
            if oc_id:
                self.set_permission(oc_id, props)
            return
        if typ in ("permission.replied",):
            if oc_id:
                self.clear_permission(oc_id, str(props.get("requestID") or props.get("id") or ""))
            return
        if typ in ("question.asked",):
            if oc_id:
                self.set_question(oc_id, props)
            return
        if typ in ("question.replied", "question.rejected"):
            self.clear_question_id(str(props.get("requestID") or props.get("id") or ""))
            return
        if typ.startswith("message.") or typ.startswith("session."):
            self.bump(oc_id)
