#!/usr/bin/env bash
# Reap orphaned CodeGraph MCP trees left behind after agent sessions die.
# Safe: never kills claude/cursor-agent/opencode/t3; only codegraph (+ its watchdogs) without
# a living agent ancestor. Also emergency-kills oldest orphans when RAM is low.
#
# Started once from container-entrypoint (PID file /var/run/agents-janitor.pid).

set -u

INTERVAL_SEC="${AGENTS_JANITOR_INTERVAL:-60}"
MEM_FLOOR_KB="${AGENTS_JANITOR_MEM_FLOOR_KB:-262144}" # 256 MiB
LOG_FILE="${AGENTS_JANITOR_LOG:-/var/log/agents-janitor.log}"
PID_FILE="${AGENTS_JANITOR_PID_FILE:-/var/run/agents-janitor.pid}"
LOCK_FILE="${AGENTS_JANITOR_LOCK_FILE:-/var/run/agents-janitor.lock}"
LOG_MAX_BYTES=1048576 # 1 MiB

log() {
  local ts
  ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  printf '%s %s\n' "$ts" "$*" >>"$LOG_FILE" 2>/dev/null || true
}

rotate_log() {
  [[ -f "$LOG_FILE" ]] || return 0
  local size
  size="$(wc -c <"$LOG_FILE" 2>/dev/null || echo 0)"
  if [[ "${size// /}" -gt "$LOG_MAX_BYTES" ]]; then
    : >"$LOG_FILE"
    log "log truncated (exceeded ${LOG_MAX_BYTES} bytes)"
  fi
}

mem_available_kb() {
  awk '/^MemAvailable:/ {print $2; exit}' /proc/meminfo 2>/dev/null || echo 0
}

# True if PID looks like a coding-agent session parent.
is_agent_proc() {
  local cmd="$1"
  case "$cmd" in
    *'/bin/claude'*|*' claude '*|*/claude)
      return 0
      ;;
    *cursor-agent*|*'/bin/agent '*|*'/usr/local/bin/agent'*|*index.js*resume*|*index.js*)
      # Cursor agent entrypoints
      if [[ "$cmd" == *cursor-agent* || "$cmd" == *'/bin/agent'* || "$cmd" == *'/usr/local/bin/agent'* ]]; then
        return 0
      fi
      if [[ "$cmd" == *'cursor-agent/versions'* && "$cmd" == *index.js* ]]; then
        return 0
      fi
      return 1
      ;;
    *'/usr/local/bin/opencode'*|*'/bin/opencode'*|*' opencode '*|*/opencode)
      return 0
      ;;
    *'/usr/local/bin/t3'*|*' t3 serve'*|*'bin.mjs serve'*|*/t3-serve)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

# True if PID is a CodeGraph MCP server or its watchdog helper.
is_codegraph_proc() {
  local cmd="$1"
  case "$cmd" in
    *'codegraph serve'*|*'codegraph.js serve'*)
      return 0
      ;;
    *'CodeGraph] Main thread unresponsive'*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

cmdline_of() {
  local pid="$1"
  tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true
}

ppid_of() {
  local pid="$1"
  awk '/^PPid:/ {print $2; exit}' "/proc/$pid/status" 2>/dev/null || echo 0
}

# Walk parents; return 0 if any ancestor is a living agent.
has_agent_ancestor() {
  local pid="$1"
  local guard=0
  local ppid cmd
  while [[ "$pid" -gt 1 && "$guard" -lt 64 ]]; do
    ppid="$(ppid_of "$pid")"
    [[ -n "$ppid" && "$ppid" -gt 0 ]] || return 1
    [[ "$ppid" -eq 1 ]] && return 1
    cmd="$(cmdline_of "$ppid")"
    if is_agent_proc "$cmd"; then
      return 0
    fi
    pid="$ppid"
    guard=$((guard + 1))
  done
  return 1
}

# List orphan codegraph PIDs (oldest first by starttime).
list_orphan_codegraphs() {
  local pid cmd st
  # starttime (field 22) ascending ≈ oldest first
  for pid in /proc/[0-9]*; do
    pid="${pid#/proc/}"
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    cmd="$(cmdline_of "$pid")"
    [[ -n "$cmd" ]] || continue
    is_codegraph_proc "$cmd" || continue
    if has_agent_ancestor "$pid"; then
      continue
    fi
    st="$(awk '{print $22}' "/proc/$pid/stat" 2>/dev/null || echo 0)"
    printf '%s %s\n' "$st" "$pid"
  done | sort -n | awk '{print $2}'
}

kill_tree_soft() {
  local pid="$1"
  local reason="$2"
  [[ -d "/proc/$pid" ]] || return 0
  log "kill orphan pid=$pid reason=$reason cmd=$(cmdline_of "$pid" | cut -c1-160)"
  kill -TERM "$pid" 2>/dev/null || true
  sleep 2
  if [[ -d "/proc/$pid" ]]; then
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

sweep_orphans() {
  local pid count=0
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill_tree_soft "$pid" "no-agent-ancestor"
    count=$((count + 1))
  done < <(list_orphan_codegraphs)
  [[ "$count" -gt 0 ]] && log "swept $count orphan codegraph process(es)"
}

emergency_low_mem() {
  local avail
  avail="$(mem_available_kb)"
  [[ "$avail" -lt "$MEM_FLOOR_KB" ]] || return 0

  log "low mem: MemAvailable=${avail}kB < floor=${MEM_FLOOR_KB}kB — killing oldest orphans"
  local pid killed=0
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill_tree_soft "$pid" "low-mem"
    killed=$((killed + 1))
    # Re-check; stop once we're above the floor
    avail="$(mem_available_kb)"
    [[ "$avail" -ge "$MEM_FLOOR_KB" ]] && break
  done < <(list_orphan_codegraphs)

  [[ "$killed" -eq 0 ]] && log "low mem but no orphan codegraphs to kill"
}

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")" "$(dirname "$LOCK_FILE")"

# Exclusive lock — second instance exits quietly (entrypoint may race; manual runs too).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

printf '%s\n' "$$" >"$PID_FILE"
log "janitor start pid=$$ interval=${INTERVAL_SEC}s mem_floor=${MEM_FLOOR_KB}kB"

trap 'log "janitor stop"; rm -f "$PID_FILE"; exit 0' TERM INT

while true; do
  rotate_log
  sweep_orphans
  emergency_low_mem
  sleep "$INTERVAL_SEC" || sleep 60
done
