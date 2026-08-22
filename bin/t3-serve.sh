#!/usr/bin/env bash
# Headless T3 Code web server for the long-lived agents container.
# Started once from container-entrypoint when AGENTS_T3_SERVE=1.
#
# `t3 serve` is already headless (no browser, no cwd auto-bootstrap).
# Keep compose working_dir (HOST_PROJECTS) so provider sessions start there.

set -u

PID_FILE="${AGENTS_T3_PID_FILE:-/var/run/t3-serve.pid}"
LOG_FILE="${AGENTS_T3_LOG:-/var/log/t3-serve.log}"
HOST="${T3CODE_HOST:-0.0.0.0}"
PORT="${T3CODE_PORT:-3773}"
BASE_DIR="${T3CODE_HOME:-/root/.t3}"

log() {
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s %s\n' "$ts" "$*" | tee -a "$LOG_FILE" >/dev/null
}

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")" "$BASE_DIR/userdata"

if [[ -f "$PID_FILE" ]]; then
  old="$(tr -d ' \n' <"$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old" && "$old" != "$$" ]] && kill -0 "$old" 2>/dev/null; then
    exit 0
  fi
  rm -f "$PID_FILE"
fi

if pgrep -f '[t]3 serve' >/dev/null 2>&1 \
  || pgrep -f '[b]in.mjs serve' >/dev/null 2>&1; then
  exit 0
fi

if ! command -v t3 >/dev/null 2>&1; then
  log "t3 binary missing; skip serve"
  exit 0
fi

export T3CODE_HOST="$HOST"
export T3CODE_PORT="$PORT"
export T3CODE_HOME="$BASE_DIR"
export T3CODE_NO_BROWSER=true
export T3CODE_MODE="${T3CODE_MODE:-web}"
export T3CODE_AUTO_BOOTSTRAP_PROJECT_FROM_CWD=false

log "t3 serve start host=$HOST port=$PORT base=$BASE_DIR cwd=$(pwd)"

# Same PID after exec; leave the file in place (stale PIDs are dropped on next start).
printf '%s\n' "$$" >"$PID_FILE"
exec t3 serve --host "$HOST" --port "$PORT" --base-dir "$BASE_DIR"
