"""Parse native Claude / Cursor JSONL transcripts."""

from __future__ import annotations

import json
import re
import time
from difflib import unified_diff
from pathlib import Path
from typing import Any

USER_QUERY_RE = re.compile(r"<user_query>\s*([\s\S]*?)\s*</user_query>")
TIMESTAMP_RE = re.compile(r"<timestamp>[\s\S]*?</timestamp>\s*")
COMMAND_NAME_RE = re.compile(r"<command-name>([^<]+)</command-name>")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
WRAP_DEFAULT_TITLE_RE = re.compile(r" · (claude|cursor|opencode|hermes)( · |$)", re.I)
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
            _extend_parts(merged[-1].setdefault("parts", []), msg.get("parts") or [])
            continue
        merged.append({**msg, "parts": [dict(p) for p in (msg.get("parts") or [])]})
    return merged


def _extend_parts(dst: list[dict[str, Any]], src: list[dict[str, Any]]) -> None:
    for raw in src:
        part = dict(raw)
        if part.get("type") == "text" and dst and dst[-1].get("type") == "text":
            prev = dst[-1].get("text") or ""
            nxt = part.get("text") or ""
            dst[-1]["text"] = f"{prev}\n\n{nxt}".strip() if prev else nxt
            continue
        dst.append(part)


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


def _content_parts(content: Any, *, as_user: bool = False) -> list[dict[str, Any]] | None:
    """Ordered text / diff / tool parts. None means skip the record."""
    if content is None:
        return []
    if isinstance(content, str):
        if "local-command-caveat" in content:
            return None
        text = extract_user_query(content) if as_user else content.strip()
        return [{"type": "text", "text": text}] if text else []
    if not isinstance(content, list):
        text = str(content)
        return [{"type": "text", "text": text}] if text else []
    parts: list[dict[str, Any]] = []
    saw_tool_result = False
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            t = block.get("text") or ""
            if as_user:
                t = extract_user_query(t)
            t = (t or "").strip()
            if t:
                parts.append({"type": "text", "text": t})
        elif kind == "tool_use":
            name = str(block.get("name") or "tool")
            hunks = diffs_from_tool(name, block.get("input"))
            if hunks:
                for h in hunks:
                    parts.append({"type": "diff", **h})
            else:
                parts.append({"type": "tool", "name": name})
        elif kind == "tool_result":
            saw_tool_result = True
        elif kind == "thinking":
            continue
    if saw_tool_result and not parts:
        return None
    return parts


def _parts_text(parts: list[dict[str, Any]]) -> str:
    return "\n\n".join(p.get("text") or "" for p in parts if p.get("type") == "text").strip()


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
            if typ == "queue-operation":
                _apply_queue_operation(out, rec, i)
                continue
            if typ not in ("user", "assistant"):
                continue
            msg = rec.get("message") or {}
            role = "user" if typ == "user" else "assistant"
            parts = _content_parts(msg.get("content"), as_user=(role == "user"))
            if parts is None:
                continue
            if not parts:
                continue
            text = _parts_text(parts)
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
                    "parts": parts,
                    "ts": rec.get("timestamp") or "",
                }
            )
    return merge_turns(out)[-limit:]


def _apply_queue_operation(out: list[dict[str, Any]], rec: dict[str, Any], i: int) -> None:
    """Surface Claude's mid-turn queue as user bubbles (enqueue, then absorb)."""
    op = str(rec.get("operation") or "")
    text = str(rec.get("content") or "").strip()
    if op == "enqueue":
        if not text or is_injected_user_message(text):
            return
        out.append(
            {
                "id": rec.get("uuid") or f"q{i}",
                "role": "user",
                "text": text,
                "parts": [{"type": "text", "text": text}],
                "ts": rec.get("timestamp") or "",
                "pending": True,
            }
        )
        return
    if op in ("remove", "dequeue"):
        for msg in reversed(out):
            if msg.get("role") != "user" or not msg.get("pending"):
                continue
            if text and (msg.get("text") or "") != text:
                continue
            msg["pending"] = False
            break


def _jsonl_tail_records(path: Path, max_bytes: int = 262144) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
    except OSError:
        return []
    recs: list[dict[str, Any]] = []
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
            fh.readline()
        for raw in fh:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                recs.append(rec)
    return recs


def claude_turn_open(path: Path) -> bool:
    """True while Claude still owes work after the last real user prompt.

    A text-only assistant message closes the turn. Tool calls, tool-result
    user rows, or a prompt with no assistant yet keep it open. Claude's Stop
    hook fires after every response — including mid-task — so pane scraping
    alone is not enough.
    """
    if not path or not path.is_file():
        return False
    in_turn = False
    for rec in _jsonl_tail_records(path):
        if rec.get("isSidechain") or rec.get("isMeta"):
            continue
        typ = rec.get("type")
        msg = rec.get("message") or {}
        content = msg.get("content")
        if typ == "assistant":
            has_tools = False
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        has_tools = True
                        break
            in_turn = has_tools
            continue
        if typ != "user":
            continue
        origin = (rec.get("origin") or {}).get("kind")
        if rec.get("toolUseResult") is not None or origin in ("tool", "task-notification"):
            in_turn = True
            continue
        text = ""
        if isinstance(content, str):
            text = extract_user_query(content)
        elif isinstance(content, list):
            bits = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    bits.append(extract_user_query(block.get("text") or ""))
            text = "\n".join(b for b in bits if b)
        else:
            text = extract_user_query(str(content or ""))
        if is_injected_user_message(text):
            continue
        in_turn = True
    return in_turn


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
            parts = _content_parts(msg.get("content"), as_user=(role == "user"))
            if parts is None or not parts:
                continue
            text = _parts_text(parts)
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
                    "parts": parts,
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


ASK_TOOLS = frozenset({"AskUserQuestion", "AskQuestion"})
_CHOICE_CACHE: dict[str, tuple[int, int, dict[str, Any] | None]] = {}


def _choice_from_tool(block: dict[str, Any]) -> dict[str, Any] | None:
    if block.get("type") != "tool_use" or block.get("name") not in ASK_TOOLS:
        return None
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    questions: list[dict[str, Any]] = []
    for raw in inp.get("questions") or []:
        if not isinstance(raw, dict):
            continue
        options: list[dict[str, str]] = []
        for opt in raw.get("options") or []:
            if not isinstance(opt, dict):
                continue
            label = str(opt.get("label") or opt.get("id") or "").strip()
            if not label:
                continue
            options.append(
                {
                    "id": str(opt.get("id") or ""),
                    "label": label,
                    "description": str(opt.get("description") or "").strip(),
                }
            )
        if len(options) < 2:
            continue
        questions.append(
            {
                "id": str(raw.get("id") or ""),
                "header": str(raw.get("header") or "").strip(),
                "prompt": str(raw.get("question") or raw.get("prompt") or "").strip(),
                "multi": bool(raw.get("multiSelect") or raw.get("allow_multiple")),
                "options": options,
            }
        )
    if not questions:
        return None
    return {
        "id": str(block.get("id") or block.get("name") or "ask"),
        "title": str(inp.get("title") or "").strip(),
        "questions": questions,
    }


def _scan_pending_choice(agent: str, path: Path) -> dict[str, Any] | None:
    pending: dict[str, Any] | None = None
    open_ids: set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            typ = rec.get("type")
            role = rec.get("role") or (rec.get("message") or {}).get("role")
            msg = rec.get("message") or {}
            content = msg.get("content") if isinstance(msg, dict) else None
            if typ == "assistant" or role == "assistant":
                found = False
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        choice = _choice_from_tool(block)
                        if not choice:
                            continue
                        found = True
                        pending = choice
                        open_ids.add(choice["id"])
                if agent == "cursor" and pending and not found:
                    pending = None
                    open_ids.clear()
            if typ == "user" or role == "user":
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict) or block.get("type") != "tool_result":
                            continue
                        tid = str(block.get("tool_use_id") or "")
                        if tid and tid in open_ids:
                            open_ids.discard(tid)
                            if pending and pending.get("id") == tid:
                                pending = None
                if agent == "cursor":
                    blob = content if isinstance(content, str) else ""
                    if isinstance(content, list):
                        blob = "\n".join(
                            str(b.get("text") or "") for b in content if isinstance(b, dict)
                        )
                    if USER_QUERY_RE.search(blob or "") and not is_injected_user_message(
                        extract_user_query(blob)
                    ):
                        pending = None
                        open_ids.clear()
    return pending


def pending_choice(agent: str, path: Path) -> dict[str, Any] | None:
    """Unanswered AskUserQuestion / AskQuestion in a parent jsonl."""
    if agent not in ("claude", "cursor") or not path.is_file():
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    key = f"{agent}:{path}"
    stamp = (int(st.st_mtime_ns), int(st.st_size))
    hit = _CHOICE_CACHE.get(key)
    if hit and hit[0] == stamp[0] and hit[1] == stamp[1]:
        return hit[2]
    found = _scan_pending_choice(agent, path)
    _CHOICE_CACHE[key] = (stamp[0], stamp[1], found)
    if len(_CHOICE_CACHE) > 128:
        for old in list(_CHOICE_CACHE)[:64]:
            if old != key:
                _CHOICE_CACHE.pop(old, None)
    return found


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


def native_session_title(
    agent: str,
    *,
    transcript: Path | None = None,
    cli_session: str = "",
    claude_home: Path,
    cursor_home: Path,
    registry: dict[str, dict[str, str]] | None = None,
) -> str | None:
    """Name the agent assigned to the tab/session, if we can read it."""
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


def _snippet_around(text: str, query: str, max_snippet: int) -> str:
    low = text.lower()
    i = low.find(query)
    if i < 0:
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_snippet]
    start = max(0, i - 40)
    end = min(len(text), i + len(query) + 80)
    chunk = re.sub(r"\s+", " ", text[start:end]).strip()
    if start:
        chunk = "…" + chunk
    if end < len(text):
        chunk += "…"
    return chunk[:max_snippet]


def _record_search_text(rec: dict[str, Any]) -> str:
    msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return extract_user_query(content) or content
    if isinstance(content, list):
        bits: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = str(block.get("text") or "")
                bits.append(extract_user_query(t) or t)
        return "\n".join(bits)
    text = rec.get("text") if isinstance(rec.get("text"), str) else ""
    return text or ""


def scan_transcript(path: Path, query: str, max_snippet: int = 140) -> tuple[bool, str]:
    """Case-insensitive scan of a JSONL transcript. Returns (hit, snippet)."""
    q = (query or "").lower()
    if len(q) < 2 or not path.is_file():
        return False, ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                low = line.lower()
                if q not in low:
                    continue
                snippet = ""
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    rec = None
                if isinstance(rec, dict):
                    blob = _record_search_text(rec)
                    if blob and q in blob.lower():
                        snippet = _snippet_around(blob, q, max_snippet)
                if not snippet:
                    i = low.find(q)
                    start = max(0, i - 48)
                    end = min(len(line), i + len(q) + 96)
                    chunk = re.sub(r"\\[ntr]|[{}\[\]\"]", " ", line[start:end])
                    snippet = re.sub(r"\s+", " ", chunk).strip()
                    if start:
                        snippet = "…" + snippet
                    if end < len(line):
                        snippet += "…"
                    snippet = snippet[:max_snippet]
                return True, snippet
    except OSError:
        return False, ""
    return False, ""


def list_native_history(
    projects: list[Path],
    *,
    claude_home: Path,
    cursor_home: Path,
    host_projects: Path,
    skip: set[tuple[str, str]] | None = None,
    keep: set[tuple[str, str]] | None = None,
    limit: int = 80,
    titles: bool = True,
) -> list[dict[str, Any]]:
    """Closed CLI sessions from native stores (no wrap DB). OpenCode history is listed via HTTP."""
    skip = skip or set()
    keep = keep or set()
    _ = host_projects
    rows: list[tuple[float, dict[str, Any]]] = []

    def add(mtime: float, item: dict[str, Any]) -> None:
        key = (str(item.get("agent") or ""), str(item.get("native_id") or ""))
        if not key[1] or key in skip:
            return
        rows.append((mtime, item))

    registry = claude_registry_names(claude_home) if titles else {}
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

    rows.sort(key=lambda item: item[0], reverse=True)
    kept = [(mtime, item) for mtime, item in rows if (str(item.get("agent") or ""), str(item.get("native_id") or "")) in keep]
    rest = [(mtime, item) for mtime, item in rows if (str(item.get("agent") or ""), str(item.get("native_id") or "")) not in keep]
    out: list[dict[str, Any]] = []
    for mtime, item in kept + rest[:limit]:
        path = Path(item["transcript"]) if item.get("transcript") else None
        if titles:
            named = native_session_title(
                str(item["agent"]),
                transcript=path,
                cli_session=str(item["native_id"] or "") if item["agent"] == "claude" else "",
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
