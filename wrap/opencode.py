"""OpenCode CLI for wrap: one message = one `opencode run --auto`."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from shutil import which
from typing import Any

import transcripts as tr

PASTE_IMG_RE = re.compile(
    r"(^|\s)(/\S+\.wrap-pastes/\S+\.(?:png|jpe?g|gif|webp))",
    re.I,
)
PLACEHOLDER_TITLE_RE = re.compile(r"^New session(\s+-|$)", re.I)

_memo_lock = threading.Lock()
_memo: dict[str, tuple[float, Any]] = {}
_live_lock = threading.Lock()
_live: dict[str, list[dict[str, Any]]] = {}
_gen: dict[str, int] = {}


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    print(f"{ts} {msg}", flush=True)


def binary() -> str:
    return which("opencode") or "opencode"


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


def _time_sec(raw: Any) -> float:
    try:
        n = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if n > 1e12:
        return n / 1000.0
    return n


def _cli(
    args: list[str],
    cwd: Path | str | None = None,
    *,
    timeout: float = 60.0,
    stdout_path: Path | None = None,
) -> subprocess.CompletedProcess[bytes]:
    cmd = [binary(), *args]
    if stdout_path is not None:
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        with stdout_path.open("wb") as fh:
            return subprocess.run(
                cmd,
                cwd=str(cwd) if cwd else None,
                stdout=fh,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def is_placeholder_title(title: str, cwd: Path | str = "") -> bool:
    t = (title or "").strip()
    if not t or tr.is_wrap_default_title(t) or PLACEHOLDER_TITLE_RE.match(t):
        return True
    proj = Path(str(cwd)).name if cwd else ""
    return bool(proj) and t == proj


def display_title(row: dict[str, Any], cwd: Path | str = "") -> str:
    directory = str(row.get("directory") or cwd or "")
    title = str(row.get("title") or "").strip()
    if title and not is_placeholder_title(title, directory):
        return title
    return str(row.get("slug") or "").strip()


def _db_path() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    return root / "opencode" / "opencode.db"


def all_sessions() -> list[dict[str, Any]]:
    return _cached("sesslist:all", 3.0, _fetch_all_sessions)


def _fetch_all_sessions() -> list[dict[str, Any]]:
    path = _db_path()
    if not path.is_file():
        return []
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            SELECT id, title, slug, directory, parent_id, time_updated, time_archived
            FROM session
            """
        ).fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if row["time_archived"] or row["parent_id"]:
            continue
        sid = str(row["id"] or "")
        if not sid:
            continue
        out.append(
            {
                "id": sid,
                "directory": str(row["directory"] or ""),
                "title": str(row["title"] or "").strip(),
                "slug": str(row["slug"] or "").strip(),
                "updated": _time_sec(row["time_updated"]),
                "parentID": str(row["parent_id"] or ""),
            }
        )
    return out


def list_sessions(cwd: Path | str) -> list[dict[str, Any]]:
    directory = str(Path(cwd).resolve()) if cwd else ""
    out = []
    for row in all_sessions():
        d = str(row.get("directory") or "")
        if not directory or d == directory or d.rstrip("/") == directory.rstrip("/"):
            out.append(row)
    return out


def get_session(oc_id: str, cwd: Path | str) -> dict[str, Any]:
    if not oc_id:
        return {}
    for row in list_sessions(cwd):
        if row.get("id") == oc_id:
            return row
    return {}


def session_title(oc_id: str, cwd: Path | str) -> str:
    if not oc_id:
        return ""
    return display_title(get_session(oc_id, cwd), cwd)


def inferred_title(oc_id: str, cwd: Path | str) -> str:
    if not oc_id:
        return ""
    info = get_session(oc_id, cwd)
    named = display_title(info, cwd)
    slug = str((info or {}).get("slug") or "").strip()
    if named and named != slug:
        return named
    for msg in list_messages(oc_id, cwd, limit=30):
        if msg.get("role") != "user":
            continue
        text = str(msg.get("text") or "").strip()
        if not text:
            continue
        line = text.split("\n")[0].strip()
        if len(line) > 72:
            line = line[:69].rstrip() + "…"
        return line
    return named or slug


def session_busy(_oc_id: str, _cwd: Path | str = "") -> bool:
    """Busy is wrap's headless process, not a serve status map."""
    return False


def children(_oc_id: str, _cwd: Path | str) -> list[dict[str, Any]]:
    return []


def subagents(_oc_id: str, _cwd: Path | str) -> list[dict[str, Any]]:
    return []


def pending_choice(_oc_id: str, _cwd: Path | str) -> dict[str, Any] | None:
    return None


def abort(_oc_id: str, _cwd: Path | str) -> None:
    return None


def wait(_oc_id: str, timeout: float = 0.5) -> None:
    time.sleep(max(0.05, timeout))


def split_prompt(text: str) -> tuple[str, list[str]]:
    """Text plus wrap-paste image paths for `opencode run --file`."""
    files: list[str] = []
    bits: list[str] = []
    src = text or ""
    last = 0
    for m in PASTE_IMG_RE.finditer(src):
        prefix = (src[last : m.start()] + m.group(1)).strip()
        if prefix:
            bits.append(prefix)
        path = m.group(2)
        last = m.end()
        if Path(path).is_file():
            files.append(path)
        else:
            bits.append(path)
    tail = src[last:].strip()
    if tail:
        bits.append(tail)
    return "\n\n".join(bits).strip(), files


def run_argv(
    cwd: Path | str,
    text: str,
    *,
    session_id: str = "",
    model: str = "",
    effort: str = "",
    title: str = "",
) -> list[str]:
    message, files = split_prompt(text)
    args = [
        binary(),
        "run",
        "--format",
        "json",
        "--auto",
        "--dir",
        str(cwd),
    ]
    if session_id:
        args.extend(["--session", session_id])
    elif title and not tr.is_wrap_default_title(title):
        args.extend(["--title", title[:80]])
    ref = (model or "").strip()
    if ref:
        args.extend(["--model", ref])
    if effort:
        args.extend(["--variant", str(effort)])
    for path in files:
        args.extend(["--file", path])
    args.append("--")
    args.append(message or text or "")
    return args


def event_session_id(ev: dict[str, Any]) -> str:
    if not isinstance(ev, dict):
        return ""
    sid = str(ev.get("sessionID") or ev.get("sessionId") or "")
    if sid:
        return sid
    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    return str(part.get("sessionID") or part.get("sessionId") or "")


def event_parts(ev: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(ev, dict):
        return []
    kind = str(ev.get("type") or "")
    if kind == "error":
        err = ev.get("error")
        msg = ""
        if isinstance(err, dict):
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            msg = str(data.get("message") or err.get("message") or err.get("name") or "")
        elif err:
            msg = str(err)
        msg = msg.strip() or "opencode error"
        return [{"type": "text", "text": msg}]
    part = ev.get("part") if isinstance(ev.get("part"), dict) else None
    if not part:
        return []
    if kind == "tool_use" and part.get("type") != "tool":
        part = {**part, "type": "tool"}
    if kind in ("text", "tool_use", "tool", "part", "reasoning"):
        return convert_parts([part])
    return []


def set_live_messages(oc_id: str, messages: list[dict[str, Any]]) -> None:
    if not oc_id:
        return
    with _live_lock:
        _live[oc_id] = messages
        _gen[oc_id] = _gen.get(oc_id, 0) + 1


def clear_live_messages(oc_id: str) -> None:
    with _live_lock:
        _live.pop(oc_id, None)
        if oc_id:
            _gen[oc_id] = _gen.get(oc_id, 0) + 1
    _invalidate(f"msg:{oc_id}", "msg:", "sesslist:")


def live_generation(oc_id: str) -> int:
    with _live_lock:
        return _gen.get(oc_id, 0)


def list_messages(oc_id: str, cwd: Path | str, limit: int = 300) -> list[dict[str, Any]]:
    if not oc_id:
        return []
    with _live_lock:
        live = _live.get(oc_id)
    if live is not None:
        return live[-limit:]
    return _cached(
        f"msg:{cwd}:{oc_id}:{live_generation(oc_id)}",
        2.0,
        lambda: _export_messages(oc_id, cwd, limit),
    )


def _export_messages(oc_id: str, cwd: Path | str, limit: int) -> list[dict[str, Any]]:
    tmp = None
    try:
        fh = tempfile.NamedTemporaryFile(prefix="wrap-oc-", suffix=".json", delete=False)
        tmp = Path(fh.name)
        fh.close()
        try:
            r = _cli(["export", oc_id], cwd, timeout=90.0, stdout_path=tmp)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if r.returncode not in (0, None) and not tmp.is_file():
            return []
        try:
            raw = tmp.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return []
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
    rows = []
    if isinstance(data, dict):
        rows = data.get("messages") or []
        if not isinstance(rows, list):
            rows = []
    elif isinstance(data, list):
        rows = data
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
                title = str(state.get("title") or "").strip()
                hint = title if title and title != tool else tr.tool_hint(tool, inp)
                if hint:
                    item["detail"] = hint
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


def providers(cwd: Path | str | None = None) -> list[dict[str, str]]:
    _ = cwd
    return [{"id": "", "label": "CLI default"}]


def history_rows(
    projects: list[Path],
    skip: set[str],
    limit: int = 80,
    keep: set[str] | None = None,
) -> list[dict[str, Any]]:
    rows: list[tuple[float, dict[str, Any]]] = []
    seen: set[str] = set()
    allowed = {str(p.resolve()) for p in projects if p.is_dir()}
    for oc in all_sessions():
        sid = oc["id"]
        if not sid or sid in skip or sid in seen or oc.get("parentID"):
            continue
        directory = oc["directory"] or ""
        try:
            d = Path(directory).resolve()
        except OSError:
            continue
        if allowed and str(d) not in allowed:
            continue
        seen.add(sid)
        title = display_title(oc, directory)
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
    keep = keep or set()
    kept = [item for _, item in rows if str(item.get("native_id") or "") in keep]
    rest = [item for _, item in rows if str(item.get("native_id") or "") not in keep]
    return kept + rest[:limit]
