#!/usr/bin/env bash
# Native-session web wrap (Claude/Cursor via tmux, OpenCode via its HTTP API).
# Started once from container-entrypoint when AGENTS_WRAP_SERVE=1.

set -u

PID_FILE="${AGENTS_WRAP_PID_FILE:-/var/run/wrap-serve.pid}"
LOG_FILE="${AGENTS_WRAP_LOG:-/var/log/wrap-serve.log}"
HOST="${WRAP_HOST:-0.0.0.0}"
PORT="${WRAP_PORT:-3780}"
WRAP_ROOT="${WRAP_ROOT:-/usr/local/share/wrap}"

log() {
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s %s\n' "$ts" "$*" | tee -a "$LOG_FILE" >/dev/null
}

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

if [[ -f "$PID_FILE" ]]; then
  old="$(tr -d ' \n' <"$PID_FILE" 2>/dev/null || true)"
  if [[ -n "$old" && "$old" != "$$" ]] && kill -0 "$old" 2>/dev/null; then
    exit 0
  fi
  rm -f "$PID_FILE"
fi

# Match the python server only. Do not match wrap-serve itself, and do not
# match unrelated shells whose argv happens to mention the source path.
if pgrep -f "python3 ${WRAP_ROOT}/server.py" >/dev/null 2>&1; then
  log "wrap already running (server.py)"
  exit 0
fi

if [[ ! -f "$WRAP_ROOT/server.py" ]]; then
  log "wrap server missing at $WRAP_ROOT; skip"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  log "python3 missing; skip wrap"
  exit 0
fi

export WRAP_HOST="$HOST"
export WRAP_PORT="$PORT"
export WRAP_ROOT
# Leave default OpenCode serve on :4096.
export WRAP_OPENCODE_PORT="${WRAP_OPENCODE_PORT:-4097}"
export PYTHONUNBUFFERED=1

log "wrap start host=$HOST port=$PORT root=$WRAP_ROOT cwd=$(pwd)"
printf '%s\n' "$$" >"$PID_FILE"
exec python3 "$WRAP_ROOT/server.py"
