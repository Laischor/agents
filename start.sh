#!/usr/bin/env bash
# Bootstrap Docker on macOS (Colima) and start the agents container.
# Usage: ./start.sh

set -euo pipefail

AGENTS_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$AGENTS_DIR/docker-compose.yml"

log()  { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$*" >&2; exit 1; }

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || fail "dieses Script unterstützt nur macOS"
}

ensure_brew() {
  if command -v brew >/dev/null 2>&1; then
    return
  fi
  fail "Homebrew fehlt. Installieren: https://brew.sh"
}

docker_ready() {
  command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1
}

compose_ready() {
  docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1
}

ensure_docker_stack() {
  if docker_ready && compose_ready; then
    log "Docker ist bereit."
    return
  fi

  ensure_brew
  log "Docker nicht bereit — installiere/aktualisiere via Homebrew: colima, docker, docker-compose"
  brew install colima docker docker-compose

  if ! command -v colima >/dev/null 2>&1; then
    fail "colima konnte nicht installiert werden"
  fi

  local colima_mem="${COLIMA_MEMORY:-4}"
  if ! colima status 2>/dev/null | grep -qi 'Running'; then
    log "Starte Colima… (${colima_mem}g RAM)"
    colima start -m "$colima_mem" -c 4
  else
    log "Colima läuft bereits."
  fi

  docker_ready || fail "Docker-Daemon antwortet nicht (Colima/Docker prüfen)"
  compose_ready || fail "docker compose / docker-compose nicht gefunden"
  log "Docker ist bereit."
}

ensure_env() {
  if [[ ! -f "$AGENTS_DIR/.env" ]]; then
    if [[ -f "$AGENTS_DIR/.env.example" ]]; then
      cp "$AGENTS_DIR/.env.example" "$AGENTS_DIR/.env"
      log "`.env` aus `.env.example` angelegt — bitte HOST_PROJECTS und Keys anpassen."
    else
      fail "keine .env und keine .env.example gefunden"
    fi
  fi
}

# Absolute repo path for compose ${AGENTS_DIR} (Hermes docker_volumes → data/*).
ensure_agents_dir_env() {
  local env_file="$AGENTS_DIR/.env"
  if grep -q '^AGENTS_DIR=' "$env_file" 2>/dev/null; then
    local tmp
    tmp="$(mktemp)"
    sed "s|^AGENTS_DIR=.*|AGENTS_DIR=$AGENTS_DIR|" "$env_file" > "$tmp"
    mv "$tmp" "$env_file"
  else
    {
      printf '\n'
      printf '# Absolute path to this repo (Hermes sandbox bind mounts)\n'
      printf 'AGENTS_DIR=%s\n' "$AGENTS_DIR"
    } >> "$env_file"
    log "AGENTS_DIR in .env geschrieben."
  fi
}

load_env() {
  if [[ -f "$AGENTS_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$AGENTS_DIR/.env"
    set +a
  fi
}

hermes_enabled() {
  case "${HERMES:-0}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

t3_serve_enabled() {
  case "${AGENTS_T3_SERVE:-1}" in
    0|false|FALSE|no|NO|off|OFF) return 1 ;;
    *) return 0 ;;
  esac
}

# Mint a pairing token after deploy. t3 prints a Docker-bridge URL; run.sh
# rewrites it to T3CODE_PUBLIC_URL / http://127.0.0.1:3773.
print_t3_pairing() {
  t3_serve_enabled || return 0
  local public="${T3CODE_PUBLIC_URL:-http://127.0.0.1:3773}"
  log "T3 Code Web-UI: $public"
  local i
  for i in {1..45}; do
    if curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:3773/" 2>/dev/null; then
      log "T3 Pairing — Token in die Web-UI einfügen (oder dt3 pair):"
      "$AGENTS_DIR/run.sh" t3 pair || log "T3 pair fehlgeschlagen — später: dt3 pair"
      return 0
    fi
    sleep 1
  done
  log "T3 noch nicht erreichbar. Später: dt3 pair"
}

# shellcheck source=bin/ensure-gh-passthrough.sh
source "$AGENTS_DIR/bin/ensure-gh-passthrough.sh"

# Opt-in Hermes via compose profile when HERMES=1 in .env

ensure_data_dirs() {
  mkdir -p \
    "$AGENTS_DIR/data/cursor" \
    "$AGENTS_DIR/data/cursor-config" \
    "$AGENTS_DIR/data/pi" \
    "$AGENTS_DIR/data/claude" \
    "$AGENTS_DIR/data/opencode" \
    "$AGENTS_DIR/data/opencode-config" \
    "$AGENTS_DIR/data/t3" \
    "$AGENTS_DIR/data/clipboard" \
    "$AGENTS_DIR/data/gpu/jobs" \
    "$AGENTS_DIR/data/gpu/running" \
    "$AGENTS_DIR/data/gpu/results" \
    "$AGENTS_DIR/data/gpu/logs" \
    "$AGENTS_DIR/data/cmux/jobs" \
    "$AGENTS_DIR/data/cmux/running" \
    "$AGENTS_DIR/data/cmux/results" \
    "$AGENTS_DIR/data/cmux/logs" \
    "$AGENTS_DIR/data/cmux/sessions"
  if hermes_enabled; then
    mkdir -p "$AGENTS_DIR/data/hermes" "$AGENTS_DIR/data/open-webui"
  fi
  mkdir -p "$AGENTS_DIR/data/gh"
  [[ -f "$AGENTS_DIR/data/gitconfig" ]] || : >"$AGENTS_DIR/data/gitconfig"
  # Legacy empty file mount blocked Claude auth writes — remove if present
  if [[ -f "$AGENTS_DIR/data/claude.json" ]]; then
    rm -f "$AGENTS_DIR/data/claude.json"
  fi
}

# macOS Keychain holds the gh OAuth token; ~/.config/gh has no oauth_token.
# Export GH_TOKEN so compose can inject it into the Linux container.
# gh is optional — missing binary is silent; only warn when installed but logged out.
ensure_gh_token() {
  if [[ -n "${GH_TOKEN:-}" ]]; then
    export GH_TOKEN
    export GITHUB_TOKEN="${GITHUB_TOKEN:-$GH_TOKEN}"
    return 0
  fi
  if ! command -v gh >/dev/null 2>&1; then
    return 0
  fi
  local token
  if token="$(gh auth token 2>/dev/null)" && [[ -n "$token" ]]; then
    export GH_TOKEN="$token"
    export GITHUB_TOKEN="${GITHUB_TOKEN:-$token}"
    if [[ -z "${_AGENTS_GH_TOKEN_LOGGED:-}" ]]; then
      log "GH_TOKEN aus Host-Keychain (gh auth token) für den Container gesetzt."
      _AGENTS_GH_TOKEN_LOGGED=1
    fi
  else
    if [[ -z "${_AGENTS_GH_TOKEN_WARNED:-}" ]]; then
      log "Hinweis: gh nicht eingeloggt — 'gh auth login' auf dem Host, oder GH_TOKEN in .env."
      _AGENTS_GH_TOKEN_WARNED=1
    fi
  fi
}

# Hermes dashboard binds 0.0.0.0 in Docker and refuses to start without auth.
ensure_hermes_dashboard_auth() {
  hermes_enabled || return 0
  load_env

  local changed=0
  if [[ -z "${HERMES_DASHBOARD_USER:-}" ]]; then
    HERMES_DASHBOARD_USER=admin
    {
      printf '\n'
      printf '# Hermes dashboard basic auth — auto-generated by start.sh\n'
      printf 'HERMES_DASHBOARD_USER=%s\n' "$HERMES_DASHBOARD_USER"
    } >> "$AGENTS_DIR/.env"
    changed=1
  fi
  if [[ -z "${HERMES_DASHBOARD_PASSWORD:-}" ]]; then
    HERMES_DASHBOARD_PASSWORD="$(openssl rand -hex 16)"
    printf 'HERMES_DASHBOARD_PASSWORD=%s\n' "$HERMES_DASHBOARD_PASSWORD" >> "$AGENTS_DIR/.env"
    changed=1
  fi
  if [[ -z "${HERMES_DASHBOARD_SECRET:-}" ]]; then
    HERMES_DASHBOARD_SECRET="$(openssl rand -base64 32)"
    printf 'HERMES_DASHBOARD_SECRET=%s\n' "$HERMES_DASHBOARD_SECRET" >> "$AGENTS_DIR/.env"
    changed=1
  fi
  if [[ "$changed" -eq 1 ]]; then
    log "Hermes-Dashboard-Auth in .env geschrieben (User: $HERMES_DASHBOARD_USER)."
  fi
}

# data/hermes bind-mount UID should match the host owner (macOS ≈ 501).
ensure_hermes_uid() {
  hermes_enabled || return 0
  load_env

  local changed=0 uid gid
  uid="$(id -u)"
  gid="$(id -g)"
  if [[ -z "${HERMES_UID:-}" ]]; then
    HERMES_UID="$uid"
    {
      printf '\n'
      printf '# Hermes bind-mount UID — auto-generated by start.sh (host id -u)\n'
      printf 'HERMES_UID=%s\n' "$HERMES_UID"
    } >> "$AGENTS_DIR/.env"
    changed=1
  fi
  if [[ -z "${HERMES_GID:-}" ]]; then
    HERMES_GID="$gid"
    printf 'HERMES_GID=%s\n' "$HERMES_GID" >> "$AGENTS_DIR/.env"
    changed=1
  fi
  if [[ "$HERMES_UID" == "0" || "$HERMES_GID" == "0" ]]; then
    log "FEHLER: HERMES_UID/GID darf nicht 0 sein. Setze Host-UID in .env."
    exit 1
  fi
  if [[ "$changed" -eq 1 ]]; then
    log "Hermes UID/GID in .env geschrieben ($HERMES_UID:$HERMES_GID)."
  fi
}

# Bearer key for gateway API server (:8642) — Open WebUI uses the same key.
ensure_hermes_api_server_key() {
  hermes_enabled || return 0
  load_env

  if [[ -z "${HERMES_API_SERVER_KEY:-}" ]]; then
    HERMES_API_SERVER_KEY="$(openssl rand -hex 32)"
    {
      printf '\n'
      printf '# Hermes API server key (:8642) — Open WebUI backend; auto-generated by start.sh\n'
      printf 'HERMES_API_SERVER_KEY=%s\n' "$HERMES_API_SERVER_KEY"
    } >> "$AGENTS_DIR/.env"
    log "Hermes API-Server-Key in .env geschrieben (HERMES_API_SERVER_KEY)."
  fi
  if [[ "${#HERMES_API_SERVER_KEY}" -lt 8 ]]; then
    log "FEHLER: HERMES_API_SERVER_KEY muss mindestens 8 Zeichen haben."
    exit 1
  fi
}

start_agents() {
  ensure_env
  ensure_agents_dir_env
  load_env
  ensure_data_dirs
  ensure_gh_token
  ensure_hermes_dashboard_auth
  ensure_hermes_uid
  ensure_hermes_api_server_key
  load_env
  # Re-apply after load_env in case .env left GH_TOKEN empty
  ensure_gh_token
  ensure_gh_passthrough

  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose -f "$COMPOSE_FILE")
  else
    COMPOSE=(docker-compose -f "$COMPOSE_FILE")
  fi

  if hermes_enabled; then
    log "Starte agents + hermes (+ open-webui)…"
    log "Entferne alte Hermes-Sandboxes (gh/git-Mounts neu)…"
    reap_hermes_sandboxes
    # Drop leftover community WebUI from older stacks (no longer in compose)
    if docker ps -a --format '{{.Names}}' | grep -Eqx 'hermes-webui'; then
      log "Entferne alten hermes-webui Container…"
      docker rm -f hermes-webui >/dev/null 2>&1 || true
    fi
    "${COMPOSE[@]}" --profile hermes up -d --build
  else
    log "Starte agents…"
    # Stop hermes profile services left from a previous HERMES=1 session
    if docker ps --format '{{.Names}}' | grep -Eqx 'hermes|open-webui|searxng|hermes-webui'; then
      log "HERMES=0 — stoppe hermes / open-webui / searxng…"
      "${COMPOSE[@]}" --profile hermes stop hermes open-webui searxng >/dev/null 2>&1 || true
      docker rm -f hermes-webui >/dev/null 2>&1 || true
    fi
    "${COMPOSE[@]}" up -d --build
  fi

  log "Fertig. Beispiele: dagent | dpi | dclaude | dopencode | dt3 | agents-shell"
  print_t3_pairing
  log "Screenshot-Paste: Bridge startet mit dagent/dclaude/dopencode (Ctrl+V, nicht Cmd+V)"
  if hermes_enabled; then
    log "Open WebUI (Chat → Hermes): http://127.0.0.1:3000"
    log "Hermes Dashboard (Config): http://127.0.0.1:9119 (Login: ${HERMES_DASHBOARD_USER})"
    log "Hermes CLI: agents hermes-setup (einmalig) | agents hermes"
  fi
}

require_macos
ensure_env
load_env
ensure_docker_stack
start_agents
