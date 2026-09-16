"""Project git status/diff for wrap's Diff view."""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

GIT_DIFF_MAX_FILES = 120
GIT_DIFF_MAX_BYTES = 120_000
GIT_FILE_MAX_BYTES = 80_000
GIT_TIMEOUT = 8.0
NESTED_GIT_MAX = 32
NESTED_WALK_MAX = 4000
NESTED_GIT_TIMEOUT = 4.0
NESTED_SKIP_DIRS = frozenset({
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    ".venv",
    "venv",
    "__pycache__",
    ".wrap-pastes",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "volumes",
    "tmp",
    "temp",
    "cache",
    ".cache",
    "coverage",
    "logs",
    "pgdata",
})
NESTED_MAX_DEPTH = 4
NESTED_WALK_BUDGET_S = 1.0

# Git status/walk results are re-used for a short window. Polling the diff view
# otherwise spawns ~116 git subprocesses every 2.5 s, and every one of those
# stats files over the (slow) Colima bind mount.
CACHE_TTL_S = 10.0
_nested_cache: dict[str, tuple[float, list[Path], bool]] = {}
_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_toplevel_cache: dict[str, tuple[float, Any]] = {}
_diff_cache: dict[str, tuple[float, tuple[str, bool, bool]]] = {}
_CACHE_LOCK = threading.Lock()
_STATUS_CACHE_MAX = 64
_DIFF_CACHE_MAX = 256
_nested_cache_gen = 0
_status_cache_gen = 0


def cache_gen() -> tuple[int, int]:
    """Generation stamps, bumped whenever a write may have changed a repo."""
    with _CACHE_LOCK:
        return _nested_cache_gen, _status_cache_gen


def invalidate(gen: tuple[int, int] | None = None) -> None:
    """Drop cached snapshots; with a generation stamp only if it is unchanged."""
    global _nested_cache_gen, _status_cache_gen
    with _CACHE_LOCK:
        if gen is not None and gen != (_nested_cache_gen, _status_cache_gen):
            return
        _nested_cache.clear()
        _status_cache.clear()
        _toplevel_cache.clear()
        _diff_cache.clear()
        _nested_cache_gen += 1
        _status_cache_gen += 1


def _cache_lookup(cache: dict[str, Any], key: str) -> Any:
    with _CACHE_LOCK:
        hit = cache.get(key)
    if not hit:
        return None
    if time.monotonic() - hit[0] >= CACHE_TTL_S:
        return None
    return hit[1]


def _cache_store(cache: dict[str, Any], key: str, value: Any, maxsize: int) -> Any:
    with _CACHE_LOCK:
        cache[key] = (time.monotonic(), value)
        while len(cache) > maxsize:
            cache.pop(next(iter(cache)), None)
    return value


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
    key = str(cwd)
    hit = _cache_lookup(_toplevel_cache, key)
    if hit is not None:
        return hit or None
    result: Path | None = None
    try:
        r = _git(cwd, "rev-parse", "--show-toplevel")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        r = None
    if r is not None and r.returncode == 0:
        raw = r.stdout.decode("utf-8", "replace").strip()
        if raw:
            try:
                result = Path(raw).resolve()
            except OSError:
                result = None
    # A working tree does not change during a turn, so this is cached too: the
    # nested repos otherwise cost one rev-parse each per poll.
    _cache_store(_toplevel_cache, key, result, maxsize=_STATUS_CACHE_MAX)
    return result


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


def _file_diff(
    root: Path, rel: str, untracked: bool, against: str = "HEAD"
) -> tuple[str, bool, bool]:
    key = f"{root}\x00{rel}\x00{int(bool(untracked))}\x00{against}"
    hit = _cache_lookup(_diff_cache, key)
    if hit is not None:
        return hit
    return _cache_store(
        _diff_cache, key, _file_diff_uncached(root, rel, untracked, against),
        maxsize=_DIFF_CACHE_MAX,
    )


def _file_diff_uncached(
    root: Path, rel: str, untracked: bool, against: str = "HEAD"
) -> tuple[str, bool, bool]:
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
        r = _git(root, "diff", against, "--", rel)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return "", False, True
    if b"\0" in r.stdout[:8192]:
        return "", True, False
    text = _decode(r.stdout)
    truncated = len(text.encode("utf-8")) > GIT_DIFF_MAX_BYTES
    if truncated:
        text = text[:GIT_DIFF_MAX_BYTES]
    return text, False, truncated


def _name_status_against(root: Path, against: str, timeout: float) -> list[dict[str, Any]]:
    try:
        r = _git(
            root,
            "-c",
            "core.quotepath=false",
            "diff",
            "--name-status",
            against,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if r.returncode != 0:
        return []
    out: list[dict[str, Any]] = []
    for line in _decode(r.stdout).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        letter = (parts[0][:1] or "M").upper()
        path = parts[-1].strip()
        if not path:
            continue
        out.append(
            {
                "path": path,
                "xy": f" {letter}",
                "status": letter if letter in "MADRU" else "M",
                "untracked": False,
            }
        )
    return out


def _find_nested_repos(root: Path) -> tuple[list[Path], bool]:
    """Working trees under root with their own .git (nested clones / submodules)."""
    key = str(root)
    hit = _cache_lookup(_nested_cache, key)
    if hit is not None:
        return hit
    found: list[Path] = []
    queued = deque([(root, 0)])
    walked = 0
    truncated = False
    deadline = time.monotonic() + NESTED_WALK_BUDGET_S
    while queued:
        if len(found) >= NESTED_GIT_MAX:
            truncated = True
            break
        if time.monotonic() > deadline:
            truncated = True
            break
        current, depth = queued.popleft()
        git = current / ".git"
        if current != root and (git.is_dir() or git.is_file()):
            found.append(current)
            continue
        if depth >= NESTED_MAX_DEPTH:
            continue
        walked += 1
        if walked > NESTED_WALK_MAX:
            truncated = True
            break
        try:
            with os.scandir(current) as it:
                kids = list(it)
        except OSError:
            continue
        for entry in kids:
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if entry.name in NESTED_SKIP_DIRS:
                continue
            queued.append((Path(entry.path), depth + 1))
    found.sort(key=lambda p: str(p))
    return _cache_store(_nested_cache, key, (found, truncated), maxsize=16)


def _status_snapshot(root: Path, timeout: float = GIT_TIMEOUT) -> dict[str, Any]:
    key = str(root)
    hit = _cache_lookup(_status_cache, key)
    if hit is not None:
        return _snap_copy(hit)
    try:
        r = _git(
            root,
            "-c",
            "core.quotepath=false",
            "status",
            "-sb",
            "--porcelain=v1",
            "-uall",
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "error": str(exc)}
    if r.returncode != 0:
        err = _decode(r.stderr).strip() or f"git status failed ({r.returncode})"
        return {"ok": False, "error": err}
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
        files.append(
            {
                "path": rest,
                "xy": xy,
                "status": _status_letter(xy),
                "untracked": xy == "??",
            }
        )
    snap = _snap_copy(
        {
            "ok": True,
            "branch": branch,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "files": files,
            "status_lines": max(0, len(lines) - 1),
        }
    )
    _cache_store(_status_cache, key, snap, maxsize=_STATUS_CACHE_MAX)
    return _snap_copy(snap)


def _snap_copy(snap: dict[str, Any]) -> dict[str, Any]:
    """Callers stamp _root/_rel/repo onto file entries — never hand them the cache."""
    return {**snap, "files": [dict(f) for f in (snap.get("files") or [])]}


def _attach_repo(files: list[dict[str, Any]], repo_root: Path, prefix: str) -> None:
    for item in files:
        inner = str(item["path"])
        item["_root"] = repo_root
        item["_rel"] = inner
        item["repo"] = prefix
        if prefix:
            item["path"] = f"{prefix}/{inner}"


def _norm_git_path(raw: str) -> str:
    return (raw or "").replace("\\", "/").strip().lstrip("./")


def _item_in_session(item: dict[str, Any], touched: list[str]) -> bool:
    git_path = _norm_git_path(str(item.get("path") or ""))
    if not git_path:
        return False
    needle = "/" + git_path
    for raw in touched:
        t = _norm_git_path(raw)
        if not t:
            continue
        if t == git_path or t.endswith(needle):
            return True
    return False


def git_view(
    cwd: Path, host_root: Path, only_paths: list[str] | None = None,
    with_diffs: bool = True,
) -> dict[str, Any]:
    try:
        root = git_toplevel(cwd)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "error": str(exc), "cwd": str(cwd)}
    if root is None:
        return {"ok": False, "error": "not a git repository", "cwd": str(cwd)}
    host = host_root.resolve()
    if root != host and host not in root.parents:
        return {"ok": False, "error": "repo outside HOST_PROJECTS", "cwd": str(cwd)}
    snap = _status_snapshot(root)
    if not snap.get("ok"):
        return {
            "ok": False,
            "error": snap.get("error") or "git status failed",
            "cwd": str(cwd),
            "repo": str(root),
        }
    files: list[dict[str, Any]] = list(snap["files"])
    _attach_repo(files, root, "")
    nested_meta: list[dict[str, Any]] = []
    nested_roots, nested_scan_truncated = _find_nested_repos(root)
    for nroot in nested_roots:
        try:
            ntop = git_toplevel(nroot)
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        if ntop is None:
            continue
        if ntop != host and host not in ntop.parents:
            continue
        try:
            prefix = nroot.relative_to(root).as_posix()
        except ValueError:
            continue
        nsnap = _status_snapshot(ntop, timeout=NESTED_GIT_TIMEOUT)
        if not nsnap.get("ok"):
            continue
        nfiles = list(nsnap["files"])
        against = (nsnap.get("upstream") or "").strip() or "HEAD"
        seen = {str(item["path"]) for item in nfiles}
        if against != "HEAD":
            for extra in _name_status_against(ntop, against, NESTED_GIT_TIMEOUT):
                if extra["path"] in seen:
                    continue
                nfiles.append(extra)
                seen.add(extra["path"])
        _attach_repo(nfiles, ntop, prefix)
        if against != "HEAD":
            for item in nfiles:
                if not item.get("untracked"):
                    item["_against"] = against
        files.extend(nfiles)
        if nfiles or nsnap["ahead"] or nsnap["behind"]:
            nested_meta.append(
                {
                    "path": prefix,
                    "branch": nsnap["branch"],
                    "ahead": nsnap["ahead"],
                    "behind": nsnap["behind"],
                    "files": len(nfiles),
                }
            )
    nested_dirs = {nroot.relative_to(root).as_posix() for nroot in nested_roots}
    if nested_dirs:
        files = [
            item
            for item in files
            if str(item.get("path") or "") not in nested_dirs
        ]
    dirty_total = len(files)
    scoped = bool(only_paths)
    if scoped:
        files = [item for item in files if _item_in_session(item, only_paths or [])]
        by_prefix: dict[str, int] = {}
        for item in files:
            prefix = str(item.get("repo") or "")
            if prefix:
                by_prefix[prefix] = by_prefix.get(prefix, 0) + 1
        nested_meta = [
            {**n, "files": by_prefix.get(str(n.get("path") or ""), 0)}
            for n in nested_meta
            if by_prefix.get(str(n.get("path") or ""), 0)
        ]
    # Parent repo first so nested clones cannot crowd untracked files off the cap.
    files.sort(
        key=lambda item: (
            1 if item.get("repo") else 0,
            1 if item.get("untracked") else 0,
            str(item.get("path") or ""),
        )
    )
    truncated_list = len(files) > GIT_DIFF_MAX_FILES
    files = files[:GIT_DIFF_MAX_FILES]
    if with_diffs:
        for item in files:
            repo_root = item.pop("_root", root)
            rel = str(item.pop("_rel", item["path"]))
            against = str(item.pop("_against", "HEAD") or "HEAD")
            diff, binary, truncated = _file_diff(
                repo_root, rel, bool(item["untracked"]), against=against
            )
            item["diff"] = diff
            item["binary"] = binary
            item["truncated"] = truncated
    else:
        # Listing only: the diff button needs files.length and dirty_total, and
        # one git diff per changed file per poll was the single biggest cost.
        for item in files:
            item.pop("_root", None)
            item.pop("_rel", None)
            item.pop("_against", None)
            item["diff"] = ""
            item["binary"] = False
            item["truncated"] = False
    stat = ""
    if with_diffs:
        try:
            sr = _git(root, "diff", "--shortstat", "HEAD")
            if sr.returncode == 0:
                stat = _decode(sr.stdout).strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            stat = ""
    nested_file_n = sum(n["files"] for n in nested_meta)
    if nested_file_n:
        extra = f"{nested_file_n} nested"
        stat = f"{stat} · {extra}" if stat else extra
    if scoped:
        sess_bit = f"{len(files)} session" if files else "session clean"
        stat = f"{sess_bit} · {stat}" if stat else sess_bit
    return {
        "ok": True,
        "cwd": str(cwd),
        "repo": str(root),
        "branch": snap["branch"],
        "upstream": snap["upstream"],
        "ahead": snap["ahead"],
        "behind": snap["behind"],
        "stat": stat,
        "files": files,
        "nested": nested_meta,
        "truncated_list": truncated_list or nested_scan_truncated,
        "dirty_total": dirty_total,
        "scope": "session" if scoped else "repo",
        "session_paths": len(only_paths or []),
    }
