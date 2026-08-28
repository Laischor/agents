#!/usr/bin/env bash
# Runtime setup for mounted agent configs (Pi packages, Claude plugins/MCP, Cursor/OpenCode MCP, wrap serve).
# Image build cannot write into volume-mounted ~/.pi / ~/.claude / ~/.cursor / ~/.config/opencode.

set -u

# Host ~/.gitconfig is mounted read-only at /etc/gitconfig.host (not /root/.gitconfig).
# macOS configs often reset credential helpers and hardcode Homebrew paths; system
# /etc/gitconfig supplies safe.directory + `gh auth git-credential` instead.
ensure_git_identity() {
  local host_cfg="/etc/gitconfig.host"
  [[ -f "$host_cfg" ]] || return

  local name email
  name="$(git config -f "$host_cfg" --get user.name 2>/dev/null || true)"
  email="$(git config -f "$host_cfg" --get user.email 2>/dev/null || true)"

  if [[ -n "$name" ]]; then
    git config --global user.name "$name"
  fi
  if [[ -n "$email" ]]; then
    git config --global user.email "$email"
  fi
}

# ~/.pi is mounted from the host, so Pi packages must be registered after the
# volume is available rather than while the image is being built.
ensure_pi_package() {
  local match="$1"
  local source="$2"

  if pi list 2>/dev/null | grep -Fq "$match"; then
    return
  fi

  printf 'Installing Pi package: %s\n' "$source"
  if ! pi install "$source"; then
    printf 'warning: could not install Pi package %s\n' "$source" >&2
  fi
}

# Claude configs live under CLAUDE_CONFIG_DIR (/root/.claude, host-mounted).
# Plugin install alone is not enough: must be enabled, and statusline helps
# confirm activation. Do not also wire SessionStart into settings.json — that
# would double-fire with the plugin manifest hooks.
ensure_claude_caveman() {
  if ! command -v claude >/dev/null 2>&1; then
    return
  fi

  local claude_dir="${CLAUDE_CONFIG_DIR:-/root/.claude}"
  mkdir -p "$claude_dir"

  local list
  list="$(claude plugin list 2>/dev/null || true)"

  if ! printf '%s\n' "$list" | grep -Fq 'caveman@caveman'; then
    printf 'Installing Claude plugin: caveman@caveman\n'
    if ! claude plugin marketplace add JuliusBrussee/caveman; then
      printf 'warning: could not add caveman marketplace\n' >&2
      return
    fi
    if ! claude plugin install caveman@caveman; then
      printf 'warning: could not install caveman plugin\n' >&2
      return
    fi
    list="$(claude plugin list 2>/dev/null || true)"
  fi

  # Installed but disabled → enable (entrypoint used to return early here)
  if printf '%s\n' "$list" | grep -F 'caveman@caveman' | grep -Eqi 'disabled|✘|✗'; then
    printf 'Enabling Claude plugin: caveman@caveman\n'
    if ! claude plugin enable caveman@caveman; then
      printf 'warning: could not enable caveman plugin\n' >&2
    fi
  fi

  ensure_claude_caveman_statusline "$claude_dir"
}

# Pin statusline to the installed plugin cache so the TUI shows [CAVEMAN].
ensure_claude_caveman_statusline() {
  local claude_dir="$1"
  local settings="${claude_dir}/settings.json"
  local plugin_root=""

  plugin_root="$(
    find "${claude_dir}/plugins/cache/caveman/caveman" -mindepth 1 -maxdepth 1 -type d 2>/dev/null \
      | sort | tail -n 1
  )"
  if [[ -z "$plugin_root" || ! -x "${plugin_root}/src/hooks/caveman-statusline.sh" ]]; then
    return
  fi

  local sl_cmd="bash \"${plugin_root}/src/hooks/caveman-statusline.sh\""
  if [[ -f "$settings" ]] && grep -Fq 'caveman-statusline.sh' "$settings" 2>/dev/null; then
    return
  fi

  printf 'Configuring Claude statusline: caveman\n'
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'warning: python3 missing; skip caveman statusline\n' >&2
    return
  fi

  CLAUDE_SETTINGS="$settings" CAVEMAN_STATUSLINE_CMD="$sl_cmd" python3 - <<'PY'
import json, os
from pathlib import Path

path = Path(os.environ["CLAUDE_SETTINGS"])
cmd = os.environ["CAVEMAN_STATUSLINE_CMD"]
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        data = {}
data["statusLine"] = {"type": "command", "command": cmd}
path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

ensure_claude_codegraph_mcp() {
  if ! command -v claude >/dev/null 2>&1 || ! command -v codegraph >/dev/null 2>&1; then
    return
  fi

  mkdir -p "${CLAUDE_CONFIG_DIR:-/root/.claude}"

  # Must be user scope: default `claude mcp add` is local to the entrypoint cwd
  # (HOST_PROJECTS), so child projects like ~/…/game never saw codegraph.
  local scope_info=""
  scope_info="$(claude mcp get codegraph 2>/dev/null || true)"
  if printf '%s\n' "$scope_info" | grep -Eqi 'Scope:[[:space:]]*User'; then
    return
  fi

  # Drop stale project-local registration from older entrypoints (ignore errors)
  claude mcp remove codegraph -s local >/dev/null 2>&1 || true

  printf 'Configuring Claude MCP (user scope): codegraph\n'
  if ! claude mcp add -s user codegraph -- codegraph serve --mcp; then
    printf 'warning: could not add codegraph MCP to Claude\n' >&2
  fi
}

# OpenCode global config is mounted at /root/.config/opencode
ensure_opencode_codegraph_mcp() {
  if ! command -v opencode >/dev/null 2>&1 || ! command -v codegraph >/dev/null 2>&1; then
    return
  fi

  local cfg_dir="${HOME}/.config/opencode"
  local cfg="${cfg_dir}/opencode.json"
  mkdir -p "$cfg_dir"

  if [[ -f "$cfg" ]] && grep -Fq '"codegraph"' "$cfg" 2>/dev/null; then
    return
  fi

  printf 'Configuring OpenCode MCP: codegraph\n'
  if ! command -v python3 >/dev/null 2>&1; then
    printf 'warning: python3 missing; skip OpenCode codegraph MCP\n' >&2
    return
  fi

  OPENCODE_CFG="$cfg" python3 - <<'PY'
import json, os
from pathlib import Path

path = Path(os.environ["OPENCODE_CFG"])
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        data = {}
data.setdefault("$schema", "https://opencode.ai/config.json")
data.setdefault("autoupdate", False)
mcp = data.setdefault("mcp", {})
if "codegraph" not in mcp:
    mcp["codegraph"] = {
        "type": "local",
        "command": ["codegraph", "serve", "--mcp"],
        "enabled": True,
    }
path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

# Cursor CLI config is mounted at /root/.cursor
ensure_cursor_codegraph_mcp() {
  if ! command -v codegraph >/dev/null 2>&1; then
    return
  fi

  local mcp_json="${HOME}/.cursor/mcp.json"
  mkdir -p "${HOME}/.cursor"

  if [[ -f "$mcp_json" ]] && grep -Fq '"codegraph"' "$mcp_json" 2>/dev/null; then
    return
  fi

  printf 'Configuring Cursor MCP: codegraph\n'
  if ! codegraph install --target=cursor --location=global --yes --no-permissions; then
    printf 'warning: could not configure codegraph MCP for Cursor\n' >&2
  fi
}

# Wire Claude Notification/Stop → cmux stub (host cmux-bridge for sounds/rings).
# Idempotent: skips when our marker command is already present.
ensure_claude_cmux_hooks() {
  local claude_dir="${CLAUDE_CONFIG_DIR:-/root/.claude}"
  local settings="${claude_dir}/settings.json"
  mkdir -p "$claude_dir"

  if [[ -f "$settings" ]] && grep -Fq 'cmux-agent-hook' "$settings" 2>/dev/null; then
    return
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    printf 'warning: python3 missing; skip cmux Claude hooks\n' >&2
    return
  fi

  printf 'Configuring Claude hooks: cmux notifications\n'
  CLAUDE_SETTINGS="$settings" python3 - <<'PY'
import json, os
from pathlib import Path

path = Path(os.environ["CLAUDE_SETTINGS"])
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        data = {}

hooks = data.setdefault("hooks", {})
marker = "cmux-agent-hook"

def has_marker(event: str) -> bool:
    for group in hooks.get(event) or []:
        for h in (group.get("hooks") or []):
            if marker in str(h.get("command") or ""):
                return True
    return False

def add(event: str, cmd: str):
    if has_marker(event):
        return
    entry = {
        "matcher": "",
        "hooks": [{"type": "command", "command": cmd, "timeout": 10}],
    }
    hooks.setdefault(event, []).append(entry)

add("Notification", "cmux-agent-hook claude notification")
add("Stop", "cmux-agent-hook claude stop")
path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

# Wire Cursor stop → cmux stub (same “done” signal as Claude Stop).
# Do NOT also hook afterAgentResponse — Cursor fires both per turn, which
# double-notifies (body snippet + "completed") and feels like 3–4 rings with OSC+sound.
ensure_cursor_cmux_hooks() {
  local hooks_json="${HOME}/.cursor/hooks.json"
  mkdir -p "${HOME}/.cursor"

  if ! command -v python3 >/dev/null 2>&1; then
    printf 'warning: python3 missing; skip cmux Cursor hooks\n' >&2
    return
  fi

  # Always reconcile: add stop if missing, drop afterAgentResponse duplicates.
  CURSOR_HOOKS="$hooks_json" python3 - <<'PY'
import json, os
from pathlib import Path

path = Path(os.environ["CURSOR_HOOKS"])
data = {}
if path.exists():
    try:
        data = json.loads(path.read_text() or "{}")
    except json.JSONDecodeError:
        data = {}

if "version" not in data:
    data["version"] = 1

hooks = data.setdefault("hooks", {})
marker = "cmux-agent-hook"
stop_cmd = "cmux-agent-hook cursor stop"

def entry_cmd(entry) -> str:
    if isinstance(entry, dict):
        return str(entry.get("command") or "")
    return str(entry or "")

def has_marker(event: str) -> bool:
    return any(marker in entry_cmd(e) for e in (hooks.get(event) or []))

# Remove legacy afterAgentResponse hooks we installed (caused multi-fire).
aar = hooks.get("afterAgentResponse") or []
if aar:
    pruned = [e for e in aar if marker not in entry_cmd(e)]
    if pruned:
        hooks["afterAgentResponse"] = pruned
    else:
        hooks.pop("afterAgentResponse", None)

if not has_marker("stop"):
    print("Configuring Cursor hooks: cmux notifications (stop only)", flush=True)
    hooks.setdefault("stop", []).append({"command": stop_cmd})

path.write_text(json.dumps(data, indent=2) + "\n")
PY
}

# Single background janitor: kill orphaned codegraph MCP trees; reap via init:true.
ensure_agents_janitor() {
  local pid_file="/var/run/agents-janitor.pid"
  local log_file="/var/log/agents-janitor.log"
  local janitor="/usr/local/bin/agents-janitor"

  [[ -x "$janitor" ]] || return 0

  mkdir -p /var/run /var/log

  if [[ -f "$pid_file" ]]; then
    local old
    old="$(tr -d ' \n' <"$pid_file" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
      return 0
    fi
    rm -f "$pid_file"
  fi

  # Belts: any live janitor process (pidfile may be stale after OOM)
  if pgrep -f '^bash /usr/local/bin/agents-janitor$' >/dev/null 2>&1 \
    || pgrep -f '^/usr/local/bin/agents-janitor$' >/dev/null 2>&1; then
    return 0
  fi

  # Disown from this exec session so the janitor outlives interactive shells.
  # Script itself flock-locks so a race still yields a single instance.
  nohup "$janitor" >>"$log_file" 2>&1 &
  printf '%s\n' "$!" >"$pid_file"
}

# Native-session wrap (tmux Claude/Cursor, OpenCode HTTP). Only the
# long-lived agents service sets AGENTS_WRAP_SERVE=1.
ensure_wrap_serve() {
  case "${AGENTS_WRAP_SERVE:-0}" in
    1|true|TRUE|yes|YES|on|ON) ;;
    *) return 0 ;;
  esac

  local pid_file="/var/run/wrap-serve.pid"
  local log_file="/var/log/wrap-serve.log"
  local serve="/usr/local/bin/wrap-serve"

  [[ -x "$serve" ]] || return 0
  command -v python3 >/dev/null 2>&1 || return 0

  mkdir -p /var/run /var/log /usr/local/share/wrap

  if [[ -f "$pid_file" ]]; then
    local old
    old="$(tr -d ' \n' <"$pid_file" 2>/dev/null || true)"
    if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
      return 0
    fi
    rm -f "$pid_file"
  fi

  # python server.py is the running daemon; wrap-serve exec's into it.
  if pgrep -f "python3 ${WRAP_ROOT:-/usr/local/share/wrap}/server.py" >/dev/null 2>&1; then
    return 0
  fi

  printf 'Starting wrap (native sessions) on %s:%s\n' \
    "${WRAP_HOST:-0.0.0.0}" "${WRAP_PORT:-3780}"
  nohup "$serve" >>"$log_file" 2>&1 &
}

ensure_git_identity

ensure_pi_package "v2nic/pi-caveman" \
  "git:github.com/v2nic/pi-caveman@2480692ffabddc3d1efec8eb822e664ff7e0e5ef"
ensure_pi_package "@vndv/pi-codegraph" \
  "npm:@vndv/pi-codegraph@0.1.10"

ensure_claude_caveman
ensure_claude_codegraph_mcp
ensure_claude_cmux_hooks
ensure_cursor_codegraph_mcp
ensure_cursor_cmux_hooks
ensure_opencode_codegraph_mcp
ensure_agents_janitor
ensure_wrap_serve

exec "$@"
