"""Parse native Claude / Cursor JSONL and OpenCode SQLite transcripts."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from difflib import unified_diff
from pathlib import Path
from typing import Any

USER_QUERY_RE = re.compile(r"<user_query>\s*([\s\S]*?)\s*</user_query>")
TIMESTAMP_RE = re.compile(r"<timestamp>[\s\S]*?</timestamp>\s*")
COMMAND_NAME_RE = re.compile(r"<command-name>([^<]+)</command-name>")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
WRAP_DEFAULT_TITLE_RE = re.compile(r" · (claude|cursor|opencode)( · |$)", re.I)
HARNESS_USER_TAG_RE = re.compile(
    r"^<(dynamic_tools|dynamic_tool_namespaces|system_notification|"
    r"agent_transcripts|user_info|git_status|agent_skills|"
    r"mcp_instructions|task-notification|local-command-stdout|"
    r"local-command-stderr|manually_attached_skills)\b",
    re.I,
)
FAKE_USER_QUERY_RE = re.compile(
    r"^Briefly inform the user about the task result\b",
    re.I,
)
INTERRUPT_RE = re.compile(r"^\[Request interrupted by user\]\s*$", re.I)
MAX_DIFF_LINES = 220
MAX_SIDE_LINES = 160


def merge_turns(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse consecutive assistant rows (one JSONL line per tool round)."""
    merged: list[dict[str, Any]] = []
    last_user_text = ""
    for msg in messages:
        if msg["role"] == "user":
            t = msg.get("text") or ""
            if t and t == last_user_text:
                continue
            last_user_text = t
        if merged and msg["role"] == "assistant" and merged[-1]["role"] == "assistant":
            prev = merged[-1]
            if msg["text"]:
                prev["text"] = f"{prev['text']}\n\n{msg['text']}".strip() if prev["text"] else msg["text"]
            for tool in msg["tools"]:
                if tool not in prev["tools"]:
                    prev["tools"].append(tool)
            for diff in msg.get("diffs") or []:
                if diff not in prev["diffs"]:
                    prev["diffs"].append(diff)
            continue
        merged.append({**msg, "tools": list(msg["tools"]), "diffs": list(msg.get("diffs") or [])})
    return merged


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text or "")


def extract_user_query(text: str) -> str:
    if not text:
        return ""
    m = USER_QUERY_RE.search(text)
    if m:
        return m.group(1).strip()
    cmd = COMMAND_NAME_RE.search(text)
    if cmd:
        return cmd.group(1).strip()
    return TIMESTAMP_RE.sub("", text).strip()


def is_injected_user_message(text: str) -> bool:
    """Harness / loop / tool notifications stored as role=user in native jsonl."""
    t = (text or "").strip()
    if not t:
        return False
    if FAKE_USER_QUERY_RE.match(t) or INTERRUPT_RE.match(t):
        return True
    return bool(HARNESS_USER_TAG_RE.match(t))


def _clip_lines(text: str, max_lines: int = MAX_SIDE_LINES) -> str:
    lines = (text or "").splitlines(keepends=True)
    if len(lines) <= max_lines:
        return text or ""
    omitted = len(lines) - max_lines
    return "".join(lines[:max_lines]) + f"\n… truncated {omitted} lines\n"


def _ensure_nl(lines: list[str]) -> list[str]:
    if lines and not lines[-1].endswith("\n"):
        lines = list(lines)
        lines[-1] += "\n"
    return lines


def unified_replace(path: str, old: str, new: str) -> str:
    old = _clip_lines(old)
    new = _clip_lines(new)
    a = _ensure_nl((old or "").splitlines(keepends=True))
    b = _ensure_nl((new or "").splitlines(keepends=True))
    label = (path[1:] if path.startswith("/") else path) or "file"
    hunks = list(
        unified_diff(a, b, fromfile=f"a/{label}", tofile=f"b/{label}", n=3, lineterm="\n")
    )
    if not hunks:
        return ""
    if len(hunks) > MAX_DIFF_LINES:
        extra = len(hunks) - MAX_DIFF_LINES
        hunks = hunks[:MAX_DIFF_LINES] + [f"… truncated {extra} diff lines\n"]
    return "".join(hunks)


def _input_path(inp: dict[str, Any]) -> str:
    return str(inp.get("path") or inp.get("file_path") or inp.get("filePath") or "").strip()


def diffs_from_tool(name: str, inp: Any) -> list[dict[str, str]]:
    if not isinstance(inp, dict):
        return []
    key = (name or "").split()[0].lower()
    path = _input_path(inp)
    out: list[dict[str, str]] = []
    if key in ("write",):
        new = str(inp.get("contents") if inp.get("contents") is not None else inp.get("content") or "")
        diff = unified_replace(path, "", new)
        if diff:
            out.append({"path": path, "kind": "write", "diff": diff})
        return out
    if key in ("edit", "strreplace", "searchreplace"):
        old = str(inp.get("old_string") or inp.get("oldString") or inp.get("old") or "")
        new = str(inp.get("new_string") or inp.get("newString") or inp.get("new") or "")
        if old == new:
            return out
        diff = unified_replace(path, old, new)
        if diff:
            out.append({"path": path, "kind": "edit", "diff": diff})
        return out
    if key == "multiedit":
        edits = inp.get("edits") or inp.get("replacements") or []
        if not isinstance(edits, list):
            return out
        for item in edits:
            if not isinstance(item, dict):
                continue
            old = str(item.get("old_string") or item.get("oldString") or "")
            new = str(item.get("new_string") or item.get("newString") or "")
            if old == new:
                continue
            diff = unified_replace(path, old, new)
            if diff:
                out.append({"path": path, "kind": "edit", "diff": diff})
        return out
    return out


def _content_blocks(content: Any) -> tuple[str | None, list[str], list[dict[str, str]]]:
    """Return (text or None to skip, tool names, code diffs)."""
    tools: list[str] = []
    diffs: list[dict[str, str]] = []
    if content is None:
        return "", tools, diffs
    if isinstance(content, str):
        if "local-command-caveat" in content:
            return None, tools, diffs
        return extract_user_query(content), tools, diffs
    if not isinstance(content, list):
        return str(content), tools, diffs
    texts: list[str] = []
    saw_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            t = block.get("text") or ""
            if t:
                texts.append(t)
        elif kind == "tool_use":
            name = str(block.get("name") or "tool")
            hunks = diffs_from_tool(name, block.get("input"))
            if hunks:
                diffs.extend(hunks)
            else:
                tools.append(name)
        elif kind == "tool_result":
            saw_tool_result = True
        elif kind == "thinking":
            continue
    if saw_tool_result and not texts and not diffs:
        return None, tools, diffs
    joined = "\n".join(texts).strip()
    text = extract_user_query(joined) if joined else ""
    return text, tools, diffs


def parse_claude_jsonl(path: Path, limit: int = 300) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("isSidechain") or rec.get("isMeta"):
                continue
            typ = rec.get("type")
            if typ not in ("user", "assistant"):
                continue
            msg = rec.get("message") or {}
            text, tools, diffs = _content_blocks(msg.get("content"))
            if text is None:
                continue
            if not text and not tools and not diffs:
                continue
            role = "user" if typ == "user" else "assistant"
            if role == "user":
                origin = (rec.get("origin") or {}).get("kind")
                if origin == "tool" or origin == "task-notification":
                    continue
                if rec.get("toolUseResult") is not None:
                    continue
                if is_injected_user_message(text):
                    continue
            out.append(
                {
                    "id": rec.get("uuid") or f"L{i}",
                    "role": role,
                    "text": text,
                    "tools": tools,
                    "diffs": diffs,
                    "ts": rec.get("timestamp") or "",
                }
            )
    return merge_turns(out)[-limit:]


def parse_cursor_jsonl(path: Path, limit: int = 300) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            role = rec.get("role") or (rec.get("message") or {}).get("role")
            if role not in ("user", "assistant"):
                continue
            msg = rec.get("message") or rec
            text, tools, diffs = _content_blocks(msg.get("content"))
            if text is None or (not text and not tools and not diffs):
                continue
            if role == "user" and is_injected_user_message(text):
                continue
            ts = ""
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        m = re.search(r"<timestamp>([^<]+)</timestamp>", block.get("text") or "")
                        if m:
                            ts = m.group(1).strip()
                            break
            out.append(
                {
                    "id": rec.get("id") or f"L{i}",
                    "role": role,
                    "text": text,
                    "tools": tools,
                    "diffs": diffs,
                    "ts": ts,
                }
            )
    return merge_turns(out)[-limit:]


def parse_jsonl(agent: str, path: Path, limit: int = 300) -> list[dict[str, Any]]:
    if agent == "cursor":
        return parse_cursor_jsonl(path, limit)
    return parse_claude_jsonl(path, limit)


TASK_ID_RE = re.compile(r"<task-id>([^<]+)</task-id>")
TASK_STATUS_RE = re.compile(r"<status>([^<]+)</status>")
SUBAGENT_DONE = frozenset({"completed", "killed", "failed", "stopped", "cancelled", "error"})
# Sidecar jsonl goes quiet if the child died without a task-notification.
# Parent jsonl stays idle the whole time a child runs, so only the sidecar mtime counts.
SUBAGENT_STALE_SEC = 45 * 60
_SUBAGENT_CACHE: dict[str, tuple[int, int, tuple[tuple[str, str, bool], ...]]] = {}


def _subagent_note(blob: str) -> tuple[str, str] | None:
    if "<task-id>" not in blob:
        return None
    tid = TASK_ID_RE.search(blob)
    if not tid:
        return None
    st = TASK_STATUS_RE.search(blob)
    return tid.group(1), (st.group(1).strip() if st else "")


def _scan_claude_subagents(path: Path) -> tuple[tuple[str, str, bool], ...]:
    """Last event per agentId: (id, description, running)."""
    agents: dict[str, tuple[str, str, bool]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            tur = rec.get("toolUseResult")
            if isinstance(tur, dict) and tur.get("agentId"):
                aid = str(tur["agentId"])
                desc = str(tur.get("description") or agents.get(aid, ("", "", False))[1] or "")
                st = str(tur.get("status") or "")
                agents[aid] = (aid, desc, st == "async_launched")
            typ = rec.get("type")
            msg = rec.get("message") or {}
            content = msg.get("content")
            if typ == "assistant" and isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    if block.get("name") != "SendMessage":
                        continue
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    aid = str(inp.get("to") or "").strip()
                    if not aid:
                        continue
                    prev = agents.get(aid)
                    desc = prev[1] if prev else ""
                    agents[aid] = (aid, desc, True)
            blob = ""
            if typ == "queue-operation" and isinstance(rec.get("content"), str):
                blob = rec["content"]
            elif isinstance(content, str):
                blob = content
            elif isinstance(rec.get("content"), str) and "<task-id>" in rec["content"]:
                blob = rec["content"]
            note = _subagent_note(blob)
            if note and note[1] in SUBAGENT_DONE:
                aid, _st = note
                prev = agents.get(aid)
                desc = prev[1] if prev else ""
                agents[aid] = (aid, desc, False)
    return tuple(agents.values())


def claude_subagents(path: Path) -> list[dict[str, Any]]:
    """Subagents still running according to a Claude parent jsonl.

    Launch: toolUseResult.status == async_launched.
    Resume: SendMessage to that agentId.
    Stop: task-notification / toolUseResult status completed|killed|failed.
    .meta.json has no status — do not use it.
    """
    if not path.is_file():
        return []
    try:
        st = path.stat()
    except OSError:
        return []
    key = str(path)
    stamp = (int(st.st_mtime_ns), int(st.st_size))
    hit = _SUBAGENT_CACHE.get(key)
    if hit and hit[0] == stamp[0] and hit[1] == stamp[1]:
        rows = hit[2]
    else:
        rows = _scan_claude_subagents(path)
        _SUBAGENT_CACHE[key] = (stamp[0], stamp[1], rows)
        if len(_SUBAGENT_CACHE) > 128:
            for old in list(_SUBAGENT_CACHE)[:64]:
                if old != key:
                    _SUBAGENT_CACHE.pop(old, None)
    now = time.time()
    sub_dir = path.parent / path.stem / "subagents"
    out: list[dict[str, Any]] = []
    for aid, desc, running in rows:
        if not running:
            continue
        side = sub_dir / f"agent-{aid}.jsonl"
        try:
            fresh = now - side.stat().st_mtime < SUBAGENT_STALE_SEC
        except OSError:
            fresh = now - st.st_mtime < SUBAGENT_STALE_SEC
        if not fresh:
            continue
        out.append({"id": aid, "description": desc, "running": True})
    return out


def claude_project_dir(cwd: Path, claude_home: Path) -> Path:
    encoded = str(cwd).replace("/", "-")
    return claude_home / "projects" / encoded


def cursor_project_dir(cwd: Path, cursor_home: Path) -> Path:
    encoded = str(cwd).lstrip("/").replace("/", "-")
    return cursor_home / "projects" / encoded / "agent-transcripts"


def list_transcripts(agent: str, cwd: Path, claude_home: Path, cursor_home: Path) -> list[Path]:
    if agent == "claude":
        root = claude_project_dir(cwd, claude_home)
        if not root.is_dir():
            return []
        return sorted(p for p in root.glob("*.jsonl") if p.is_file())
    if agent == "cursor":
        root = cursor_project_dir(cwd, cursor_home)
        if not root.is_dir():
            return []
        return sorted(p for p in root.glob("*/*.jsonl") if p.is_file())
    return []


def newest_transcript(agent: str, cwd: Path, claude_home: Path, cursor_home: Path) -> Path | None:
    files = list_transcripts(agent, cwd, claude_home, cursor_home)
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def opencode_db_path() -> Path:
    xdg = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    return xdg


def parse_opencode_session(db_path: Path, session_id: str, limit: int = 300) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        messages = conn.execute(
            "SELECT id, time_created, data FROM message WHERE session_id = ? "
            "ORDER BY time_created ASC, id ASC",
            (session_id,),
        ).fetchall()
        parts = conn.execute(
            "SELECT message_id, data FROM part WHERE session_id = ? "
            "ORDER BY time_created ASC, id ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()

    by_msg: dict[str, list[dict[str, Any]]] = {}
    for row in parts:
        try:
            data = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        by_msg.setdefault(row["message_id"], []).append(data)

    out: list[dict[str, Any]] = []
    for row in messages:
        try:
            info = json.loads(row["data"])
        except json.JSONDecodeError:
            continue
        role = info.get("role") or "assistant"
        texts: list[str] = []
        tools: list[str] = []
        diffs: list[dict[str, str]] = []
        for part in by_msg.get(row["id"], []):
            kind = part.get("type")
            if kind == "text" and part.get("text"):
                texts.append(part["text"])
            elif kind == "tool":
                tool = str(part.get("tool") or part.get("name") or "tool")
                st = part.get("state") if isinstance(part.get("state"), dict) else {}
                inp = st.get("input") if isinstance(st.get("input"), dict) else part.get("input")
                hunks = diffs_from_tool(tool, inp)
                if hunks:
                    diffs.extend(hunks)
                else:
                    tools.append(tool)
        if not texts and not tools and not diffs:
            continue
        out.append(
            {
                "id": row["id"],
                "role": "user" if role == "user" else "assistant",
                "text": "\n\n".join(texts),
                "tools": tools,
                "diffs": diffs,
                "ts": row["time_created"],
            }
        )
    return out[-limit:]


def is_wrap_default_title(title: str) -> bool:
    return bool(WRAP_DEFAULT_TITLE_RE.search(title or ""))


def _last_jsonl_ai_title(path: Path, tail: int = 262144) -> str | None:
    if not path.is_file():
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > tail:
        data = data[-tail:]
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1 :]
    last: str | None = None
    for line in data.decode("utf-8", "replace").splitlines():
        if "ai-title" not in line and "aiTitle" not in line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "ai-title":
            continue
        val = rec.get("aiTitle") or rec.get("title") or ""
        if isinstance(val, str) and val.strip():
            last = val.strip()
    return last


def claude_registry_names(claude_home: Path) -> dict[str, dict[str, str]]:
    """sessionId -> {name, nameSource} from ~/.claude/sessions/*.json."""
    out: dict[str, dict[str, str]] = {}
    root = claude_home / "sessions"
    if not root.is_dir():
        return out
    for p in root.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        sid = str(data.get("sessionId") or "")
        name = str(data.get("name") or "").strip()
        if not sid or not name:
            continue
        out[sid] = {
            "name": name,
            "nameSource": str(data.get("nameSource") or ""),
        }
    return out


def cursor_session_title(cursor_home: Path, transcript: Path) -> str | None:
    sid = transcript.parent.name if transcript.parent.name else transcript.stem
    chats = cursor_home / "chats"
    if not sid or not chats.is_dir():
        return None
    for meta in chats.glob(f"*/{sid}/meta.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        title = str(data.get("title") or data.get("name") or "").strip()
        if title:
            return title
    return None


def opencode_session_title(db_path: Path, session_id: str) -> str | None:
    if not session_id or not db_path.is_file():
        return None
    conn = sqlite3.connect(str(db_path), timeout=2)
    try:
        row = conn.execute("SELECT title FROM session WHERE id = ?", (session_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row:
        return None
    title = str(row[0] or "").strip()
    return title or None


def native_session_title(
    agent: str,
    *,
    transcript: Path | None = None,
    cli_session: str = "",
    oc_id: str = "",
    claude_home: Path,
    cursor_home: Path,
    registry: dict[str, dict[str, str]] | None = None,
) -> str | None:
    """Name the agent assigned to the tab/session, if we can read it."""
    if agent == "opencode":
        title = opencode_session_title(opencode_db_path(), oc_id)
        return None if title and is_wrap_default_title(title) else title
    if agent == "cursor":
        if not transcript:
            return None
        title = cursor_session_title(cursor_home, transcript)
        return None if title and is_wrap_default_title(title) else title
    if agent != "claude":
        return None
    sid = (cli_session or "").strip() or (transcript.stem if transcript else "")
    if registry is None:
        registry = claude_registry_names(claude_home)
    reg = registry.get(sid) or {}
    renamed = (reg.get("name") or "").strip()
    if renamed and reg.get("nameSource") == "user" and not is_wrap_default_title(renamed):
        return renamed
    ai = _last_jsonl_ai_title(transcript) if transcript else None
    if ai:
        return ai
    if renamed and not is_wrap_default_title(renamed):
        return renamed
    return None


def latest_opencode_session(db_path: Path, cwd: str) -> dict[str, Any] | None:
    if not db_path.is_file():
        return None
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, directory, title, time_updated FROM session "
            "WHERE directory = ? AND time_archived IS NULL "
            "ORDER BY time_updated DESC LIMIT 1",
            (cwd,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "directory": row["directory"],
        "title": row["title"],
        "time_updated": row["time_updated"],
    }


def _oc_time_sec(raw: Any) -> float:
    try:
        n = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if n > 1e12:
        return n / 1000.0
    return n


def list_opencode_sessions(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path), timeout=2)
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT id, directory, title, time_updated FROM session "
                "WHERE time_archived IS NULL"
            ).fetchall()
        except sqlite3.Error:
            rows = conn.execute(
                "SELECT id, directory, title, time_updated FROM session"
            ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": str(row["id"]),
                "directory": str(row["directory"] or ""),
                "title": str(row["title"] or "").strip(),
                "updated": _oc_time_sec(row["time_updated"]),
            }
        )
    return out


def list_native_history(
    projects: list[Path],
    *,
    claude_home: Path,
    cursor_home: Path,
    host_projects: Path,
    skip: set[tuple[str, str]] | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    """Closed CLI sessions from native stores (no wrap DB)."""
    skip = skip or set()
    root = host_projects.resolve()
    rows: list[tuple[float, dict[str, Any]]] = []

    def add(mtime: float, item: dict[str, Any]) -> None:
        key = (str(item.get("agent") or ""), str(item.get("native_id") or ""))
        if not key[1] or key in skip:
            return
        rows.append((mtime, item))

    registry = claude_registry_names(claude_home)
    for cwd in projects:
        if not cwd.is_dir():
            continue
        for agent in ("claude", "cursor"):
            for path in list_transcripts(agent, cwd, claude_home, cursor_home):
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_size < 8:
                    continue
                native = path.stem if agent == "claude" else path.parent.name
                add(
                    st.st_mtime,
                    {
                        "agent": agent,
                        "cwd": str(cwd),
                        "native_id": native,
                        "title": "",
                        "transcript": str(path),
                    },
                )

    for oc in list_opencode_sessions(opencode_db_path()):
        directory = oc["directory"]
        if not directory:
            continue
        try:
            d = Path(directory).resolve()
        except OSError:
            continue
        if d != root and root not in d.parents:
            continue
        add(
            oc["updated"],
            {
                "agent": "opencode",
                "cwd": str(d),
                "native_id": oc["id"],
                "title": oc["title"],
                "transcript": None,
            },
        )

    rows.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, Any]] = []
    for mtime, item in rows[:limit]:
        path = Path(item["transcript"]) if item.get("transcript") else None
        named = native_session_title(
            str(item["agent"]),
            transcript=path,
            cli_session=str(item["native_id"] or "") if item["agent"] == "claude" else "",
            oc_id=str(item["native_id"] or "") if item["agent"] == "opencode" else "",
            claude_home=claude_home,
            cursor_home=cursor_home,
            registry=registry,
        )
        if named:
            item["title"] = named
        elif item.get("title") and is_wrap_default_title(str(item["title"])):
            item["title"] = ""
        item["id"] = f"h:{item['agent']}:{item['native_id']}"
        item["updated"] = mtime
        item["live"] = False
        out.append(item)
    return out
