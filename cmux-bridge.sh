#!/usr/bin/env bash
# Forward container `cmux` CLI calls to the host cmux app (notifications/sounds).
# Jobs land in data/cmux/jobs/; results in data/cmux/results/.
# Usage: ./cmux-bridge.sh           # foreground
#        ./cmux-bridge.sh --daemon  # background (PID in data/cmux/bridge.pid)

set -euo pipefail

AGENTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CMUX_DIR="${AGENTS_CMUX_DIR:-$AGENTS_DIR/data/cmux}"
PIDFILE="$CMUX_DIR/bridge.pid"
LOGFILE="$CMUX_DIR/bridge.log"
HEARTBEAT="$CMUX_DIR/bridge.heartbeat"
JOBS_DIR="$CMUX_DIR/jobs"
RUNNING_DIR="$CMUX_DIR/running"
RESULTS_DIR="$CMUX_DIR/results"
LOGS_DIR="$CMUX_DIR/logs"
CONFIG_ENV="$CMUX_DIR/config.env"

log() { printf '%s\n' "$*" >&2; }

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || {
    log "error: cmux-bridge requires macOS (host cmux app)"
    exit 1
  }
}

load_config() {
  if [[ -f "$AGENTS_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$AGENTS_DIR/.env"
    set +a
  fi
  if [[ -f "$CONFIG_ENV" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$CONFIG_ENV"
    set +a
  fi
  POLL="${CMUX_BRIDGE_POLL:-0.25}"
  DEFAULT_TIMEOUT="${CMUX_JOB_TIMEOUT:-15}"
}

resolve_cmux() {
  if [[ -n "${CMUX_BIN:-}" && -x "$CMUX_BIN" ]]; then
    printf '%s\n' "$CMUX_BIN"
    return 0
  fi
  local c
  for c in \
    "/Applications/cmux.app/Contents/MacOS/cmux" \
    "$HOME/Applications/cmux.app/Contents/MacOS/cmux"; do
    if [[ -x "$c" ]]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  if command -v cmux >/dev/null 2>&1; then
    command -v cmux
    return 0
  fi
  return 1
}

ensure_dirs() {
  mkdir -p "$JOBS_DIR" "$RUNNING_DIR" "$RESULTS_DIR" "$LOGS_DIR"
}

fail_stale_running() {
  local f id
  shopt -s nullglob
  for f in "$RUNNING_DIR"/*.json; do
    id="$(basename "$f" .json)"
    log "marking interrupted job $id as error"
    python3 - "$f" "$RESULTS_DIR/$id.json" <<'PY'
import json, os, sys, time
src, dest = sys.argv[1:3]
with open(src) as fh:
    job = json.load(fh)
jid = job.get("id") or os.path.splitext(os.path.basename(src))[0]
result = {
    "id": jid,
    "status": "error",
    "exit_code": 1,
    "stdout": "",
    "stderr": "interrupted: job left in running/ after bridge restart",
    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
os.makedirs(os.path.dirname(dest), exist_ok=True)
tmp = dest + ".tmp"
with open(tmp, "w") as fh:
    json.dump(result, fh)
    fh.write("\n")
os.replace(tmp, dest)
os.remove(src)
PY
  done
  shopt -u nullglob
}

is_running() {
  local pid
  [[ -f "$PIDFILE" ]] || return 1
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

pick_oldest_job() {
  python3 - "$JOBS_DIR" <<'PY'
import os, sys
jobs_dir = sys.argv[1]
entries = []
for name in os.listdir(jobs_dir):
    if not name.endswith(".json"):
        continue
    path = os.path.join(jobs_dir, name)
    if os.path.isfile(path):
        entries.append((os.path.getmtime(path), path))
if not entries:
    sys.exit(0)
entries.sort()
print(entries[0][1])
PY
}

run_job() {
  local job_file="$1"
  local cmux_bin="$2"

  python3 - "$job_file" "$cmux_bin" "$DEFAULT_TIMEOUT" "$RESULTS_DIR" <<'PY'
import base64, json, os, subprocess, sys, time

job_file, cmux_bin, default_timeout, results_dir = sys.argv[1:5]

with open(job_file) as fh:
    job = json.load(fh)

jid = job.get("id") or ""
argv = job.get("argv") or []
stdin_b64 = job.get("stdin_b64") or ""
env_extra = job.get("env") or {}
timeout_sec = float(job.get("timeout_sec") or default_timeout)

ALLOW = {
    "notify", "claude-hook", "cursor-hook", "hooks", "ping",
    "list-notifications", "clear-notifications",
    "set-status", "clear-status", "list-status",
    "set-progress", "clear-progress",
    "log", "clear-log", "list-log", "sidebar-state",
    "identify", "capabilities", "version",
    "--version", "-V", "-h", "--help", "help",
}

def write_result(status, code, stdout="", stderr="", error=None):
    result = {
        "id": jid,
        "status": status,
        "exit_code": int(code),
        "stdout": stdout,
        "stderr": stderr,
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if error:
        result["error"] = error
    dest = os.path.join(results_dir, f"{jid}.json")
    tmp = dest + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(result, fh)
        fh.write("\n")
    os.replace(tmp, dest)
    try:
        os.remove(job_file)
    except OSError:
        pass

if not jid:
    write_result("error", 1, stderr="missing job id")
    sys.exit(0)
if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
    write_result("error", 1, stderr="argv must be a list of strings")
    sys.exit(0)
if not argv:
    write_result("error", 2, stderr="empty argv")
    sys.exit(0)

head = argv[0]
if head.startswith("-"):
    # flags-only forms like --version
    if head not in ALLOW and not any(a in ALLOW for a in argv):
        write_result("error", 2, stderr=f"command not allowlisted: {head!r}")
        sys.exit(0)
elif head not in ALLOW:
    write_result("error", 2, stderr=f"command not allowlisted: {head!r}")
    sys.exit(0)

if not cmux_bin or cmux_bin == "MISSING" or not os.path.isfile(cmux_bin) or not os.access(cmux_bin, os.X_OK):
    write_result(
        "error",
        127,
        stderr="cmux binary not found (install cmux.app or set CMUX_BIN in data/cmux/config.env)",
    )
    sys.exit(0)

try:
    stdin_data = base64.b64decode(stdin_b64) if stdin_b64 else b""
except Exception as e:
    write_result("error", 1, stderr=f"invalid stdin_b64: {e}")
    sys.exit(0)

child_env = os.environ.copy()
# Prefer job-provided routing ids from the launching cmux pane.
for key, val in env_extra.items():
    if not isinstance(key, str) or not isinstance(val, str):
        continue
    if key.startswith("CMUX_") or key in ("TERM_PROGRAM", "TERM"):
        child_env[key] = val

# Detached bridge is not a cmux child — allowAll so notify/hooks reach the socket.
child_env.setdefault("CMUX_SOCKET_MODE", "allowAll")

status = "ok"
exit_code = 0
stdout = ""
stderr = ""
error_msg = ""
try:
    proc = subprocess.run(
        [cmux_bin, *argv],
        input=stdin_data,
        capture_output=True,
        timeout=timeout_sec,
        env=child_env,
    )
    exit_code = int(proc.returncode)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    if exit_code != 0:
        status = "error"
except subprocess.TimeoutExpired as e:
    status = "timeout"
    exit_code = 124
    error_msg = f"timeout after {timeout_sec}s"
    stdout = (e.stdout or b"").decode("utf-8", errors="replace")
    stderr = (e.stderr or b"").decode("utf-8", errors="replace")
except OSError as e:
    status = "error"
    exit_code = 127
    error_msg = str(e)

write_result(status, exit_code, stdout=stdout, stderr=stderr, error=error_msg or None)
print(f"job {jid} finished status={status} exit={exit_code}", file=sys.stderr)
PY
}

process_one() {
  local cmux_bin="$1"
  local pending claimed id
  pending="$(pick_oldest_job)"
  [[ -n "$pending" ]] || return 0
  id="$(basename "$pending" .json)"
  claimed="$RUNNING_DIR/$id.json"
  if ! mv "$pending" "$claimed" 2>/dev/null; then
    return 0
  fi
  log "claimed job $id"
  run_job "$claimed" "$cmux_bin" || true
}

run_loop() {
  ensure_dirs
  fail_stale_running
  # shellcheck disable=SC2064
  trap 'rm -f "$HEARTBEAT"; [[ -f "$PIDFILE" ]] && [[ "$(cat "$PIDFILE" 2>/dev/null)" == "$$" ]] && rm -f "$PIDFILE"' EXIT
  echo $$ >"$PIDFILE"

  local cmux_bin
  cmux_bin="$(resolve_cmux 2>/dev/null || true)"
  [[ -n "$cmux_bin" ]] || cmux_bin="MISSING"

  log "cmux-bridge watching $JOBS_DIR (poll ${POLL}s)"
  log "  cmux=${cmux_bin}"
  log "  default_timeout=${DEFAULT_TIMEOUT}s"
  log "  tip: detached bridge sets CMUX_SOCKET_MODE=allowAll for host cmux calls"

  while true; do
    touch "$HEARTBEAT"
    process_one "$cmux_bin"
    sleep "$POLL"
  done
}

status_report() {
  load_config
  ensure_dirs
  local cmux_bin cstate
  if cmux_bin="$(resolve_cmux 2>/dev/null)"; then
    cstate="ok:$cmux_bin"
  else
    cstate="missing"
  fi
  if is_running; then
    printf 'running pid=%s cmux=%s queue=%s\n' \
      "$(cat "$PIDFILE")" "$cstate" "$CMUX_DIR"
    exit 0
  fi
  printf 'stopped cmux=%s queue=%s\n' "$cstate" "$CMUX_DIR"
  exit 1
}

cmd="${1:-}"
require_macos
load_config
ensure_dirs

case "$cmd" in
  --daemon|daemon)
    if is_running; then
      log "cmux-bridge already running (pid $(cat "$PIDFILE"))"
      exit 0
    fi
    nohup "$0" >>"$LOGFILE" 2>&1 &
    sleep 0.3
    if is_running; then
      log "cmux-bridge started (pid $(cat "$PIDFILE"))"
      exit 0
    fi
    log "error: cmux-bridge failed to start — see $LOGFILE"
    exit 1
    ;;
  --stop|stop)
    if ! is_running; then
      rm -f "$PIDFILE" "$HEARTBEAT"
      log "cmux-bridge not running"
      exit 0
    fi
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE" "$HEARTBEAT"
    log "cmux-bridge stopped"
    exit 0
    ;;
  --status|status)
    status_report
    ;;
  ""|--foreground|foreground)
    if is_running && [[ "$(cat "$PIDFILE")" != "$$" ]]; then
      log "error: already running (pid $(cat "$PIDFILE")); use --stop first"
      exit 1
    fi
    run_loop
    ;;
  -h|--help)
    cat <<EOF
Usage: $(basename "$0") [--daemon|--stop|--status|--foreground]

Host daemon: runs allowlisted cmux CLI jobs from:
  $JOBS_DIR

Results:
  $RESULTS_DIR

Config (optional): $CONFIG_ENV
  CMUX_BIN=  CMUX_BRIDGE_POLL=0.25  CMUX_JOB_TIMEOUT=15

Start from the host:  agents cmux-bridge --daemon

Socket access: the bridge sets CMUX_SOCKET_MODE=allowAll for child calls.
If notifies still fail, enable allow-all in cmux Settings or export
CMUX_SOCKET_MODE=allowAll in the cmux app environment.
EOF
    exit 0
    ;;
  *)
    log "unknown option: $cmd (try --help)"
    exit 1
    ;;
esac
