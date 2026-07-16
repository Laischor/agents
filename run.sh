#!/usr/bin/env bash
# Launch an agent CLI inside the agents container.
# Usage: run.sh <agent|pi|claude|cursor-agent|bash> [args...]

set -euo pipefail

AGENTS_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPOSE_FILE="$AGENTS_DIR/docker-compose.yml"
SERVICE="agents"

# Load local .env (HOST_PROJECTS, API keys for compose)
if [[ -f "$AGENTS_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$AGENTS_DIR/.env"
  set +a
fi

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

cmd="${1:-}"
if [[ -z "$cmd" ]]; then
  echo "usage: $(basename "$0") <agent|pi|claude|bash> [args...]" >&2
  exit 1
fi
shift

case "$cmd" in
  agent|cursor-agent|pi|claude|bash) ;;
  *)
    echo "unknown command: $cmd (expected agent, pi, claude, or bash)" >&2
    exit 1
    ;;
esac

# Same-path mount: host cwd under HOST_PROJECTS is identical in container
if [[ "$PWD" == "$HOST_PROJECTS" || "$PWD" == "$HOST_PROJECTS"/* ]]; then
  workdir="$PWD"
else
  workdir="$HOST_PROJECTS"
  echo "note: cwd is outside $HOST_PROJECTS → starting in $HOST_PROJECTS" >&2
fi

# Start container if needed
if ! docker ps --format '{{.Names}}' | grep -qx agents; then
  "${COMPOSE[@]}" up -d --quiet-pull
fi

exec "${COMPOSE[@]}" exec -it -w "$workdir" "$SERVICE" "$cmd" "$@"
