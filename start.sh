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

  if ! colima status 2>/dev/null | grep -qi 'Running'; then
    log "Starte Colima…"
    colima start
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

ensure_data_dirs() {
  mkdir -p \
    "$AGENTS_DIR/data/cursor" \
    "$AGENTS_DIR/data/cursor-config" \
    "$AGENTS_DIR/data/pi" \
    "$AGENTS_DIR/data/claude"
  # Legacy empty file mount blocked Claude auth writes — remove if present
  if [[ -f "$AGENTS_DIR/data/claude.json" ]]; then
    rm -f "$AGENTS_DIR/data/claude.json"
  fi
}

start_agents() {
  ensure_env
  ensure_data_dirs

  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose -f "$COMPOSE_FILE")
  else
    COMPOSE=(docker-compose -f "$COMPOSE_FILE")
  fi

  log "Starte agents-Container…"
  "${COMPOSE[@]}" up -d --build
  log "Fertig. Beispiele: dagent | dpi | dclaude | agents-shell"
}

require_macos
ensure_docker_stack
start_agents
