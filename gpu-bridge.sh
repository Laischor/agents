#!/usr/bin/env bash
# Run Blender/Godot jobs on macOS (Metal) for agents inside Docker.
# Jobs land in data/gpu/jobs/; results in data/gpu/results/.
# Usage: ./gpu-bridge.sh           # foreground
#        ./gpu-bridge.sh --daemon  # background (PID in data/gpu/bridge.pid)

set -euo pipefail

AGENTS_DIR="$(cd "$(dirname "$0")" && pwd)"
GPU_DIR="${AGENTS_GPU_DIR:-$AGENTS_DIR/data/gpu}"
PIDFILE="$GPU_DIR/bridge.pid"
LOGFILE="$GPU_DIR/bridge.log"
HEARTBEAT="$GPU_DIR/bridge.heartbeat"
JOBS_DIR="$GPU_DIR/jobs"
RUNNING_DIR="$GPU_DIR/running"
RESULTS_DIR="$GPU_DIR/results"
LOGS_DIR="$GPU_DIR/logs"
CONFIG_ENV="$GPU_DIR/config.env"

log() { printf '%s\n' "$*" >&2; }

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || {
    log "error: gpu-bridge requires macOS (host Metal / native Blender & Godot)"
    exit 1
  }
}

load_config() {
  # HOST_PROJECTS from repo .env (same as run.sh)
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
  HOST_PROJECTS="${HOST_PROJECTS:-$HOME/Documents/projects}"
  HOST_PROJECTS="${HOST_PROJECTS/#\~/$HOME}"
  POLL="${GPU_BRIDGE_POLL:-0.5}"
  DEFAULT_TIMEOUT="${GPU_JOB_TIMEOUT:-600}"
}

resolve_blender() {
  if [[ -n "${BLENDER_BIN:-}" && -x "$BLENDER_BIN" ]]; then
    printf '%s\n' "$BLENDER_BIN"
    return 0
  fi
  local candidate="/Applications/Blender.app/Contents/MacOS/Blender"
  if [[ -x "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi
  return 1
}

resolve_godot() {
  if [[ -n "${GODOT_BIN:-}" && -x "$GODOT_BIN" ]]; then
    printf '%s\n' "$GODOT_BIN"
    return 0
  fi
  local c
  for c in \
    "/Applications/Godot.app/Contents/MacOS/Godot" \
    "/Applications/Godot_mono.app/Contents/MacOS/Godot" \
    "/opt/homebrew/bin/godot" \
    "/usr/local/bin/godot"; do
    if [[ -x "$c" ]]; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  # Newest Godot_*.app in /Applications (versioned installs)
  local app
  app="$(ls -1d /Applications/Godot*.app 2>/dev/null | sort -V | tail -1 || true)"
  if [[ -n "$app" && -x "$app/Contents/MacOS/Godot" ]]; then
    printf '%s\n' "$app/Contents/MacOS/Godot"
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
    python3 - "$f" "$RESULTS_DIR/$id.json" "$LOGS_DIR" <<'PY'
import json, sys, os, time
src, dest, logs = sys.argv[1:4]
with open(src) as fh:
    job = json.load(fh)
jid = job.get("id") or os.path.splitext(os.path.basename(src))[0]
result = {
    "id": jid,
    "status": "error",
    "exit_code": 1,
    "stdout_path": os.path.join(logs, f"{jid}.stdout"),
    "stderr_path": os.path.join(logs, f"{jid}.stderr"),
    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "error": "interrupted: job left in running/ after bridge restart",
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

validate_and_run() {
  local job_file="$1"
  local blender_bin="$2"
  local godot_bin="$3"

  python3 - "$job_file" "$HOST_PROJECTS" "$DEFAULT_TIMEOUT" \
    "$blender_bin" "$godot_bin" "$LOGS_DIR" "$RESULTS_DIR" <<'PY'
import json, os, sys, time, signal, subprocess

job_file, host_projects, default_timeout, blender_bin, godot_bin, logs_dir, results_dir = sys.argv[1:8]
host_projects = os.path.realpath(host_projects)

with open(job_file) as fh:
    job = json.load(fh)

jid = job.get("id") or ""
tool = job.get("tool") or ""
args = job.get("args") or []
cwd = job.get("cwd") or host_projects
timeout_sec = int(job.get("timeout_sec") or default_timeout)

def fail(msg, code=1, status="error"):
    result = {
        "id": jid,
        "status": status,
        "exit_code": code,
        "stdout_path": os.path.join(logs_dir, f"{jid}.stdout"),
        "stderr_path": os.path.join(logs_dir, f"{jid}.stderr"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error": msg,
    }
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
    print(msg, file=sys.stderr)
    sys.exit(0)

if not jid:
    fail("missing job id")
if tool not in ("blender", "godot"):
    fail(f"tool not allowlisted: {tool!r}")
if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
    fail("args must be a list of strings")

cwd_real = os.path.realpath(cwd)
if cwd_real != host_projects and not cwd_real.startswith(host_projects + os.sep):
    fail(f"cwd outside HOST_PROJECTS: {cwd}")
if not os.path.isdir(cwd_real):
    fail(f"cwd is not a directory: {cwd}")

def path_allowed(arg: str) -> bool:
    # Flags / bare tokens that are not path-like
    if arg.startswith("-") and not arg.startswith("-/"):
        # allow -o etc.; values come as separate argv entries
        if "=" in arg:
            # e.g. --path=/foo — check value side
            _, _, val = arg.partition("=")
            if val:
                return path_allowed(val)
        return True
    if "/" not in arg and arg not in (".", "..") and not arg.startswith(".."):
        return True
    candidate = arg if arg.startswith("/") else os.path.join(cwd_real, arg)
    cur = candidate
    while cur and not os.path.exists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    real = os.path.realpath(cur if os.path.exists(cur) else os.path.dirname(candidate) or candidate)
    return real == host_projects or real.startswith(host_projects + os.sep)

for a in args:
    if not path_allowed(a):
        fail(f"arg path outside HOST_PROJECTS: {a!r}")

if tool == "blender":
    binary = blender_bin
elif tool == "godot":
    binary = godot_bin
else:
    fail(f"unknown tool: {tool}")

if not binary or binary == "MISSING" or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
    fail(f"{tool} binary not found or not executable (set BLENDER_BIN / GODOT_BIN in data/gpu/config.env)")

stdout_path = os.path.join(logs_dir, f"{jid}.stdout")
stderr_path = os.path.join(logs_dir, f"{jid}.stderr")
os.makedirs(logs_dir, exist_ok=True)

status = "ok"
exit_code = 0
error_msg = ""
with open(stdout_path, "wb") as fo, open(stderr_path, "wb") as fe:
    try:
        proc = subprocess.Popen(
            [binary] + args,
            cwd=cwd_real,
            stdout=fo,
            stderr=fe,
            start_new_session=True,
        )
        try:
            exit_code = proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            status = "timeout"
            exit_code = 124
            error_msg = f"timeout after {timeout_sec}s"
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
    except OSError as e:
        status = "error"
        exit_code = 127
        error_msg = str(e)

if exit_code != 0 and status == "ok":
    status = "error"

result = {
    "id": jid,
    "status": status,
    "exit_code": int(exit_code),
    "stdout_path": stdout_path,
    "stderr_path": stderr_path,
    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
if error_msg:
    result["error"] = error_msg
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
print(f"job {jid} finished status={status} exit={exit_code}", file=sys.stderr)
PY
}

pick_oldest_job() {
  # Print path of oldest pending job, or empty
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

process_one() {
  local blender_bin="$1"
  local godot_bin="$2"
  local pending claimed id
  pending="$(pick_oldest_job)"
  [[ -n "$pending" ]] || return 0
  id="$(basename "$pending" .json)"
  claimed="$RUNNING_DIR/$id.json"
  if ! mv "$pending" "$claimed" 2>/dev/null; then
    return 0
  fi
  log "claimed job $id"
  validate_and_run "$claimed" "$blender_bin" "$godot_bin" || true
}

run_loop() {
  ensure_dirs
  fail_stale_running
  # shellcheck disable=SC2064
  trap 'rm -f "$HEARTBEAT"; [[ -f "$PIDFILE" ]] && [[ "$(cat "$PIDFILE" 2>/dev/null)" == "$$" ]] && rm -f "$PIDFILE"' EXIT
  echo $$ >"$PIDFILE"

  local blender_bin godot_bin
  blender_bin="$(resolve_blender 2>/dev/null || true)"
  godot_bin="$(resolve_godot 2>/dev/null || true)"
  [[ -n "$blender_bin" ]] || blender_bin="MISSING"
  [[ -n "$godot_bin" ]] || godot_bin="MISSING"

  log "gpu-bridge watching $JOBS_DIR (poll ${POLL}s)"
  log "  HOST_PROJECTS=$HOST_PROJECTS"
  log "  blender=${blender_bin}"
  log "  godot=${godot_bin}"
  log "  default_timeout=${DEFAULT_TIMEOUT}s"

  while true; do
    touch "$HEARTBEAT"
    process_one "$blender_bin" "$godot_bin"
    sleep "$POLL"
  done
}

status_report() {
  load_config
  ensure_dirs
  local blender_bin godot_bin bstate gstate
  if blender_bin="$(resolve_blender 2>/dev/null)"; then
    bstate="ok:$blender_bin"
  else
    bstate="missing"
  fi
  if godot_bin="$(resolve_godot 2>/dev/null)"; then
    gstate="ok:$godot_bin"
  else
    gstate="missing"
  fi
  if is_running; then
    printf 'running pid=%s blender=%s godot=%s queue=%s\n' \
      "$(cat "$PIDFILE")" "$bstate" "$gstate" "$GPU_DIR"
    exit 0
  fi
  printf 'stopped blender=%s godot=%s queue=%s\n' "$bstate" "$gstate" "$GPU_DIR"
  exit 1
}

cmd="${1:-}"
require_macos
load_config
ensure_dirs

case "$cmd" in
  --daemon|daemon)
    if is_running; then
      log "gpu-bridge already running (pid $(cat "$PIDFILE"))"
      exit 0
    fi
    nohup "$0" >>"$LOGFILE" 2>&1 &
    sleep 0.3
    if is_running; then
      log "gpu-bridge started (pid $(cat "$PIDFILE"))"
      exit 0
    fi
    log "error: gpu-bridge failed to start — see $LOGFILE"
    exit 1
    ;;
  --stop|stop)
    if ! is_running; then
      rm -f "$PIDFILE" "$HEARTBEAT"
      log "gpu-bridge not running"
      exit 0
    fi
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE" "$HEARTBEAT"
    log "gpu-bridge stopped"
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

Opt-in host daemon: runs allowlisted Blender/Godot jobs from:
  $JOBS_DIR

Results:
  $RESULTS_DIR

Config (optional): $CONFIG_ENV
  BLENDER_BIN=  GODOT_BIN=  GPU_BRIDGE_POLL=0.5  GPU_JOB_TIMEOUT=600

Start from the host:  agents gpu-bridge --daemon
EOF
    exit 0
    ;;
  *)
    log "unknown option: $cmd (try --help)"
    exit 1
    ;;
esac
