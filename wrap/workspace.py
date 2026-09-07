"""Project git status/diff for wrap's Diff view."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

GIT_DIFF_MAX_FILES = 80
GIT_DIFF_MAX_BYTES = 120_000
GIT_FILE_MAX_BYTES = 80_000
GIT_TIMEOUT = 8.0

AHEAD_RE = re.compile(r"ahead (\d+)")
BEHIND_RE = re.compile(r"behind (\d+)")


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def git_toplevel(cwd: Path) -> Path | None:
    try:
        r = _git(cwd, "rev-parse", "--show-toplevel")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    raw = r.stdout.decode("utf-8", "replace").strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except OSError:
        return None


def _decode(raw: bytes) -> str:
    return raw.decode("utf-8", "replace")


def _is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return True
    return b"\0" in chunk


def _status_letter(xy: str) -> str:
    x, y = (xy + "  ")[:2]
    if "?" in xy:
        return "?"
    if "U" in xy:
        return "U"
    if "D" in xy:
        return "D"
    if "A" in xy or "C" in xy:
        return "A"
    if "R" in xy:
        return "R"
    if "M" in xy:
        return "M"
    return (y if y not in " " else x).strip() or "M"


def _file_diff(root: Path, rel: str, untracked: bool) -> tuple[str, bool, bool]:
    path = root / rel
    if untracked:
        try:
            if path.is_file() and path.stat().st_size > GIT_FILE_MAX_BYTES:
                return "", False, True
        except OSError:
            return "", False, True
        if _is_binary(path):
            return "", True, False
        try:
            r = _git(root, "diff", "--no-index", "--", "/dev/null", rel)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return "", False, True
        text = _decode(r.stdout)
        truncated = len(text.encode("utf-8")) > GIT_DIFF_MAX_BYTES
        if truncated:
            text = text[:GIT_DIFF_MAX_BYTES]
        return text, False, truncated
    try:
        r = _git(root, "diff", "HEAD", "--", rel)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "", False, True
    if b"\0" in r.stdout[:8192]:
        return "", True, False
    text = _decode(r.stdout)
    truncated = len(text.encode("utf-8")) > GIT_DIFF_MAX_BYTES
    if truncated:
        text = text[:GIT_DIFF_MAX_BYTES]
    return text, False, truncated


def git_view(cwd: Path, host_root: Path) -> dict[str, Any]:
    try:
        root = git_toplevel(cwd)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "error": str(exc), "cwd": str(cwd)}
    if root is None:
        return {"ok": False, "error": "not a git repository", "cwd": str(cwd)}
    host = host_root.resolve()
    if root != host and host not in root.parents:
        return {"ok": False, "error": "repo outside HOST_PROJECTS", "cwd": str(cwd)}
    try:
        r = _git(root, "-c", "core.quotepath=false", "status", "-sb", "--porcelain=v1", "-uall")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "error": str(exc), "cwd": str(cwd), "repo": str(root)}
    if r.returncode != 0:
        err = _decode(r.stderr).strip() or f"git status failed ({r.returncode})"
        return {"ok": False, "error": err, "cwd": str(cwd), "repo": str(root)}
    lines = _decode(r.stdout).splitlines()
    branch = ""
    upstream = ""
    ahead = 0
    behind = 0
    files: list[dict[str, Any]] = []
    for line in lines:
        if line.startswith("## "):
            head = line[3:]
            m_ahead = AHEAD_RE.search(head)
            m_behind = BEHIND_RE.search(head)
            if m_ahead:
                ahead = int(m_ahead.group(1))
            if m_behind:
                behind = int(m_behind.group(1))
            left = head.split(" [", 1)[0]
            if "..." in left:
                branch, upstream = left.split("...", 1)
            else:
                branch = left
            branch = branch.strip()
            upstream = upstream.strip()
            continue
        if len(line) < 4:
            continue
        xy = line[:2]
        rest = line[3:]
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rel = rest
        files.append(
            {
                "path": rel,
                "xy": xy,
                "status": _status_letter(xy),
                "untracked": xy == "??",
            }
        )
    files = files[:GIT_DIFF_MAX_FILES]
    for item in files:
        diff, binary, truncated = _file_diff(root, str(item["path"]), bool(item["untracked"]))
        item["diff"] = diff
        item["binary"] = binary
        item["truncated"] = truncated
    stat = ""
    try:
        sr = _git(root, "diff", "--shortstat", "HEAD")
        if sr.returncode == 0:
            stat = _decode(sr.stdout).strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        stat = ""
    return {
        "ok": True,
        "cwd": str(cwd),
        "repo": str(root),
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "stat": stat,
        "files": files,
        "truncated_list": len(lines) - 1 > GIT_DIFF_MAX_FILES if lines else False,
    }
