"""One-shot CLI titles for wrap tabs (Haiku or the settings fallback agent)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from shutil import which
from typing import Any

import transcripts as tr

_TITLE_RE = re.compile(r"^[\s\"'`“”‘’]+|[\s\"'`“”‘’]+$")
_FALLBACK_AGENTS = ("claude", "cursor", "opencode")
_PROMPT = (
    "Name this coding session in 3 to 8 words. Same language as the user. "
    "No quotes, no trailing punctuation, no project-folder slug. "
    "If it is a question, name the topic, not an invented task. "
    "Reply with the title only. Do not use tools.\n\n"
)


def enabled() -> bool:
    raw = os.environ.get("WRAP_TITLE", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def model() -> str:
    return os.environ.get("WRAP_TITLE_MODEL", "haiku").strip() or "haiku"


def workdir() -> Path:
    path = Path("/tmp/wrap-title")
    path.mkdir(parents=True, exist_ok=True)
    return path


def snippet(messages: list[dict[str, Any]], *, limit: int = 4) -> str:
    bits: list[str] = []
    for m in messages:
        role = str(m.get("role") or "")
        text = " ".join(str(m.get("text") or "").split())
        if role not in ("user", "assistant") or not text:
            continue
        if role == "user" and tr.is_injected_user_message(text):
            continue
        bits.append(f"{role}: {text[:500]}")
        if len(bits) >= limit:
            break
    return "\n".join(bits)[:2000]


def generate(text: str, *, agent: str = "claude", model: str = "") -> str | None:
    """Return a short title, or None. Caller runs this off the request thread."""
    blob = (text or "").strip()
    if len(blob) < 8:
        return None
    agent = (agent or "claude").strip()
    if agent not in _FALLBACK_AGENTS:
        return None
    prompt = _PROMPT + f"<session>\n{blob}\n</session>"
    if agent == "claude":
        raw = _run_claude(prompt, model.strip() or model())
    elif agent == "cursor":
        raw = _run_cursor(prompt, model.strip())
    else:
        raw = _run_opencode(prompt, model.strip())
    return clean(raw or "")


def clean(title: str) -> str | None:
    t = _TITLE_RE.sub("", (title or "").replace("\n", " ")).strip()
    t = re.sub(r"\s+", " ", t)
    if t.endswith("."):
        t = t[:-1].strip()
    if len(t) < 3 or len(t) > 80:
        return None
    if tr.is_wrap_default_title(t) or tr.is_claude_derived_title(t):
        return None
    return t


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("WRAP_SESSION_ID", None)
    env["IS_SANDBOX"] = os.environ.get("IS_SANDBOX", "1")
    hook_bin = str(Path(__file__).resolve().parent / "bin")
    path = env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if hook_bin not in path.split(":"):
        path = hook_bin + ":" + path
    env["PATH"] = path
    return env


def _run(args: list[str], *, timeout: float = 35.0, stdin: int = subprocess.DEVNULL) -> str:
    try:
        r = subprocess.run(
            args,
            cwd=str(workdir()),
            env=_env(),
            stdin=stdin,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (r.stdout or b"").decode("utf-8", "replace").strip()


def _run_claude(prompt: str, model_id: str) -> str:
    binary = which("claude") or "claude"
    raw = _run(
        [
            binary,
            "-p",
            prompt,
            "--model",
            model_id or "haiku",
            "--max-turns",
            "1",
            "--permission-prompts",
            "none",
            "--output-format",
            "json",
        ]
    )
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return str(data.get("result") or data.get("title") or "").strip()
    except json.JSONDecodeError:
        pass
    return raw.splitlines()[0].strip() if raw else ""


def _run_cursor(prompt: str, model_id: str) -> str:
    binary = which("agent") or which("cursor-agent") or "agent"
    args = [
        binary,
        "-p",
        "--output-format",
        "text",
        "--mode",
        "ask",
        "--trust",
        "--workspace",
        str(workdir()),
    ]
    if model_id:
        args.extend(["--model", model_id])
    args.append(prompt)
    raw = _run(args)
    return raw.splitlines()[0].strip() if raw else ""


def _run_opencode(prompt: str, model_id: str) -> str:
    binary = which("opencode") or "opencode"
    args = [
        binary,
        "run",
        "--format",
        "json",
        "--auto",
        "--dir",
        str(workdir()),
    ]
    if model_id:
        args.extend(["--model", model_id])
    args.extend(["--", prompt])
    raw = _run(args, timeout=45.0)
    bits: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "text":
            part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
            text = str(part.get("text") or ev.get("text") or "").strip()
            if text:
                bits.append(text)
        elif ev.get("type") == "error":
            return ""
    blob = " ".join(bits).strip()
    return blob.splitlines()[0].strip() if blob else ""
