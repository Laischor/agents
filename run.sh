#!/usr/bin/env bash
# Launch an agent CLI inside the agents container.
# Usage: run.sh <agent|pi|claude|opencode|t3|hermes|hermes-setup|cursor-agent|codegraph|clipboard-bridge|gpu-bridge|cmux-bridge|bash> [args...]

set -euo pipefail

AGENTS_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$AGENTS_DIR/docker-compose.yml"
SERVICE="agents"

# Keep AGENTS_DIR in .env so compose can interpolate Hermes docker_volumes
if [[ -f "$AGENTS_DIR/.env" ]]; then
  if grep -q '^AGENTS_DIR=' "$AGENTS_DIR/.env" 2>/dev/null; then
    tmp="$(mktemp)"
    sed "s|^AGENTS_DIR=.*|AGENTS_DIR=$AGENTS_DIR|" "$AGENTS_DIR/.env" > "$tmp"
    mv "$tmp" "$AGENTS_DIR/.env"
  else
    {
      printf '\n'
      printf '# Absolute path to this repo (Hermes sandbox bind mounts)\n'
      printf 'AGENTS_DIR=%s\n' "$AGENTS_DIR"
    } >> "$AGENTS_DIR/.env"
  fi
fi

# Load local .env (HOST_PROJECTS, API keys for compose)
if [[ -f "$AGENTS_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$AGENTS_DIR/.env"
  set +a
fi
# Script path wins over a stale .env value
export AGENTS_DIR

# shellcheck source=bin/ensure-gh-passthrough.sh
source "$AGENTS_DIR/bin/ensure-gh-passthrough.sh"

hermes_enabled() {
  case "${HERMES:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

# macOS: gh OAuth lives in Keychain, not in ~/.config/gh — inject for compose.
if [[ -z "${GH_TOKEN:-}" ]] && command -v gh >/dev/null 2>&1; then
  if token="$(gh auth token 2>/dev/null)" && [[ -n "$token" ]]; then
    export GH_TOKEN="$token"
  fi
fi
if [[ -n "${GH_TOKEN:-}" ]]; then
  export GITHUB_TOKEN="${GITHUB_TOKEN:-$GH_TOKEN}"
fi
ensure_gh_passthrough

# Projects root on the host (== path inside container)
HOST_PROJECTS="${HOST_PROJECTS:-$HOME/Documents/projects}"
HOST_PROJECTS="${HOST_PROJECTS/#\~/$HOME}"

# Prefer standalone docker-compose if the compose plugin is missing
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose -f "$COMPOSE_FILE")
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose -f "$COMPOSE_FILE")
else
  echo "docker compose / docker-compose not found" >&2
  exit 1
fi

ensure_services() {
  mkdir -p "$AGENTS_DIR/data/clipboard" "$AGENTS_DIR/data/gpu/jobs" \
    "$AGENTS_DIR/data/gpu/running" "$AGENTS_DIR/data/gpu/results" \
    "$AGENTS_DIR/data/gpu/logs" \
    "$AGENTS_DIR/data/cmux/jobs" "$AGENTS_DIR/data/cmux/running" \
    "$AGENTS_DIR/data/cmux/results" "$AGENTS_DIR/data/cmux/logs" \
    "$AGENTS_DIR/data/cmux/sessions" \
    "$AGENTS_DIR/data/t3"
  if hermes_enabled; then
    mkdir -p "$AGENTS_DIR/data/hermes" "$AGENTS_DIR/data/open-webui"
    ensure_gh_passthrough
    reap_hermes_sandboxes
    "${COMPOSE[@]}" --profile hermes up -d --quiet-pull
  else
    "${COMPOSE[@]}" up -d --quiet-pull
  fi
}

# Host-side: mirror macOS clipboard PNGs into data/clipboard for container stubs.
ensure_clipboard_bridge() {
  [[ "$(uname -s)" == "Darwin" ]] || return 0
  mkdir -p "$AGENTS_DIR/data/clipboard"
  "$AGENTS_DIR/clipboard-bridge.sh" --daemon
}

# Host-side: forward container cmux CLI → host cmux (notifications/sounds).
ensure_cmux_bridge() {
  [[ "$(uname -s)" == "Darwin" ]] || return 0
  mkdir -p "$AGENTS_DIR/data/cmux/jobs" "$AGENTS_DIR/data/cmux/running" \
    "$AGENTS_DIR/data/cmux/results" "$AGENTS_DIR/data/cmux/logs" \
    "$AGENTS_DIR/data/cmux/sessions"
  register_cmux_session
  "$AGENTS_DIR/cmux-bridge.sh" --daemon
}

# Record the host TTY for this cmux pane so the bridge can emit OSC 777
# without needing the cmux control socket (no allowAll / Full open access).
register_cmux_session() {
  [[ "$(uname -s)" == "Darwin" ]] || return 0
  local sessions="$AGENTS_DIR/data/cmux/sessions"
  local tty_path
  tty_path="$(tty 2>/dev/null || true)"
  [[ -n "$tty_path" && -e "$tty_path" ]] || return 0
  mkdir -p "$sessions"
  CMUX_SESSION_TTY="$tty_path" \
  CMUX_SESSION_SURFACE="${CMUX_SURFACE_ID:-}" \
  CMUX_SESSION_WORKSPACE="${CMUX_WORKSPACE_ID:-}" \
  CMUX_SESSION_TAB="${CMUX_TAB_ID:-}" \
  CMUX_SESSIONS_DIR="$sessions" \
  python3 - <<'PY'
import json, os, re, time
sessions = os.environ["CMUX_SESSIONS_DIR"]
tty = os.environ["CMUX_SESSION_TTY"]
surface = (os.environ.get("CMUX_SESSION_SURFACE") or "").strip()
meta = {
    "tty": tty,
    "surface_id": surface or None,
    "workspace_id": (os.environ.get("CMUX_SESSION_WORKSPACE") or "").strip() or None,
    "tab_id": (os.environ.get("CMUX_SESSION_TAB") or "").strip() or None,
    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
latest = os.path.join(sessions, "latest.json")
tmp = latest + ".tmp"
with open(tmp, "w") as fh:
    json.dump(meta, fh)
    fh.write("\n")
os.replace(tmp, latest)
if surface:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", surface)[:120]
    dest = os.path.join(sessions, f"{safe}.json")
    tmp = dest + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(meta, fh)
        fh.write("\n")
    os.replace(tmp, dest)
PY
}

# Forward cmux pane routing env into the container (empty if unset is fine;
# the cmux stub only forwards non-empty values to the host bridge).
CMUX_DOCKER_ENV=(
  -e "CMUX_WORKSPACE_ID=${CMUX_WORKSPACE_ID:-}"
  -e "CMUX_SURFACE_ID=${CMUX_SURFACE_ID:-}"
  -e "CMUX_TAB_ID=${CMUX_TAB_ID:-}"
  -e "CMUX_WINDOW_ID=${CMUX_WINDOW_ID:-}"
)

cmd="${1:-}"
if [[ -z "$cmd" ]]; then
  echo "usage: $(basename "$0") <agent|pi|claude|opencode|t3|hermes|hermes-setup|codegraph|clipboard-bridge|gpu-bridge|cmux-bridge|bash> [args...]" >&2
  exit 1
fi
shift

case "$cmd" in
  agent|cursor-agent|pi|claude|opencode|t3|hermes|hermes-setup|codegraph|clipboard-bridge|gpu-bridge|cmux-bridge|bash) ;;
  *)
    echo "unknown command: $cmd (expected agent, pi, claude, opencode, t3, hermes, hermes-setup, codegraph, clipboard-bridge, gpu-bridge, cmux-bridge, or bash)" >&2
    exit 1
    ;;
esac

# Host clipboard bridge (not inside the container)
if [[ "$cmd" == "clipboard-bridge" ]]; then
  exec "$AGENTS_DIR/clipboard-bridge.sh" "$@"
fi

# Host GPU bridge — Blender/Godot with Metal (opt-in; not auto-started)
if [[ "$cmd" == "gpu-bridge" ]]; then
  mkdir -p "$AGENTS_DIR/data/gpu/jobs" "$AGENTS_DIR/data/gpu/running" \
    "$AGENTS_DIR/data/gpu/results" "$AGENTS_DIR/data/gpu/logs"
  exec "$AGENTS_DIR/gpu-bridge.sh" "$@"
fi

# Host cmux bridge — notifications/sounds (auto-started with agent/claude/opencode)
if [[ "$cmd" == "cmux-bridge" ]]; then
  mkdir -p "$AGENTS_DIR/data/cmux/jobs" "$AGENTS_DIR/data/cmux/running" \
    "$AGENTS_DIR/data/cmux/results" "$AGENTS_DIR/data/cmux/logs"
  exec "$AGENTS_DIR/cmux-bridge.sh" "$@"
fi

# Same-path mount: host cwd under HOST_PROJECTS is identical in container
if [[ "$PWD" == "$HOST_PROJECTS" || "$PWD" == "$HOST_PROJECTS"/* ]]; then
  workdir="$PWD"
else
  workdir="$HOST_PROJECTS"
  echo "note: cwd is outside $HOST_PROJECTS → starting in $HOST_PROJECTS" >&2
fi

# Hermes one-time setup wizard (does not require HERMES=1 / running gateway)
if [[ "$cmd" == "hermes-setup" ]]; then
  mkdir -p "$AGENTS_DIR/data/hermes"
  exec docker run -it --rm \
    -v "$AGENTS_DIR/data/hermes:/opt/data" \
    -v "${HOST_PROJECTS}:${HOST_PROJECTS}" \
    -e ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}" \
    -e OPENAI_API_KEY="${OPENAI_API_KEY:-}" \
    -e GOOGLE_API_KEY="${GOOGLE_API_KEY:-}" \
    -e GH_TOKEN="${GH_TOKEN:-}" \
    -e GITHUB_TOKEN="${GITHUB_TOKEN:-}" \
    -w "$workdir" \
    nousresearch/hermes-agent:latest setup "$@"
fi

# Interactive Hermes CLI against the running gateway container
if [[ "$cmd" == "hermes" ]]; then
  if ! hermes_enabled; then
    echo "error: Hermes is disabled — set HERMES=1 in .env and run ./start.sh" >&2
    exit 1
  fi
  ensure_services
  if ! docker ps --format '{{.Names}}' | grep -qx hermes; then
    echo "error: hermes container is not running — check HERMES=1 and ./start.sh" >&2
    exit 1
  fi
  exec "${COMPOSE[@]}" --profile hermes exec -it -w "$workdir" hermes hermes "$@"
fi

# Start container if needed
if ! docker ps --format '{{.Names}}' | grep -qx agents; then
  ensure_services
fi

# Screenshot paste: host bridge → xclip/wl-paste stubs (Claude + Cursor CLI + OpenCode).
# cmux notifications: host cmux-bridge → container `cmux` stub.
# DISPLAY=:0 is a no-op for Claude; Cursor only probes clipboard when DISPLAY is set.
# Keep the TUI path on "$@" — empty-array expansion breaks `set -u` on macOS bash 3.2.
case "$cmd" in
  claude|agent|cursor-agent|opencode)
    ensure_clipboard_bridge
    ensure_cmux_bridge
    exec "${COMPOSE[@]}" exec -it -w "$workdir" \
      -e "DISPLAY=${DISPLAY:-:0}" \
      "${CMUX_DOCKER_ENV[@]}" \
      "$SERVICE" "$cmd" "$@"
    ;;
  t3)
    # Pairing tokens are minted on demand. t3 prints the container IP
    # (172.x); rewrite to the host publish URL so the token is usable.
    if [[ "${1:-}" == "pair" ]]; then
      shift
      t3_has_base=0
      t3_has_ttl=0
      for t3_a in "$@"; do
        case "$t3_a" in
          --base-dir|--base-dir=*) t3_has_base=1 ;;
          --ttl|--ttl=*) t3_has_ttl=1 ;;
        esac
      done
      t3_pair_args=()
      [[ "$t3_has_base" -eq 1 ]] || t3_pair_args+=(--base-dir /root/.t3)
      [[ "$t3_has_ttl" -eq 1 ]] || t3_pair_args+=(--ttl 1h)
      t3_pair_args+=("$@")
      t3_out=""
      t3_ec=0
      t3_out="$("${COMPOSE[@]}" exec -T "$SERVICE" t3 pair "${t3_pair_args[@]}")" || t3_ec=$?
      t3_public="${T3CODE_PUBLIC_URL:-http://127.0.0.1:3773}"
      t3_public="${t3_public%/}"
      t3_rewritten="$(printf '%s\n' "$t3_out" | sed -E "s#https?://[0-9]+(\\.[0-9]+){3}(:[0-9]+)?#${t3_public}#g")"
      printf '%s\n' "$t3_rewritten"
      t3_token="$(printf '%s\n' "$t3_rewritten" | awk '/^Token:/{print $2; exit}')"
      if [[ -n "$t3_token" ]]; then
        mkdir -p "$AGENTS_DIR/data/t3"
        {
          printf 'url=%s/pair#token=%s\n' "$t3_public" "$t3_token"
          printf 'token=%s\n' "$t3_token"
        } > "$AGENTS_DIR/data/t3/pairing.txt"
        printf '\nPaste this token into the T3 pairing page.\n'
        printf 'Token: %s\n' "$t3_token"
        printf 'Host URL: %s/pair#token=%s\n' "$t3_public" "$t3_token"
        printf 'Also written to %s\n' "$AGENTS_DIR/data/t3/pairing.txt"
      fi
      exit "$t3_ec"
    fi
    # Default `t3 serve` binds 0.0.0.0:3773 so the compose publish reaches it.
    # If the entrypoint already started the UI, just print the URL.
    if [[ $# -eq 0 || "${1:-}" == "serve" ]]; then
      if curl -sf -o /dev/null --max-time 2 "http://127.0.0.1:3773/" 2>/dev/null; then
        printf 'T3 Code web UI already running: http://127.0.0.1:3773\n'
        printf 'Pairing / projects: dt3 pair | dt3 project add PATH\n'
        exit 0
      fi
      if [[ "${1:-}" == "serve" ]]; then
        shift
      fi
      t3_has_host=0
      t3_has_port=0
      for t3_a in "$@"; do
        case "$t3_a" in
          --host|--host=*) t3_has_host=1 ;;
          --port|--port=*) t3_has_port=1 ;;
        esac
      done
      t3_args=(serve)
      [[ "$t3_has_host" -eq 1 ]] || t3_args+=(--host 0.0.0.0)
      [[ "$t3_has_port" -eq 1 ]] || t3_args+=(--port 3773)
      t3_args+=("$@")
      exec "${COMPOSE[@]}" exec -it -w "$workdir" \
        "$SERVICE" t3 "${t3_args[@]}"
    fi
    exec "${COMPOSE[@]}" exec -it -w "$workdir" "$SERVICE" t3 "$@"
    ;;
esac

exec "${COMPOSE[@]}" exec -it -w "$workdir" "${CMUX_DOCKER_ENV[@]}" "$SERVICE" "$cmd" "$@"
