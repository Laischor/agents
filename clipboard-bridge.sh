#!/usr/bin/env bash
# Mirror macOS clipboard images into data/clipboard/image.png for the agents container.
# Claude Code / Cursor CLI inside Docker read them via the xclip/wl-paste stubs.
# Usage: ./clipboard-bridge.sh           # foreground
#        ./clipboard-bridge.sh --daemon  # background (PID in data/clipboard/bridge.pid)

set -euo pipefail

AGENTS_DIR="$(cd "$(dirname "$0")" && pwd)"
CLIP_DIR="${AGENTS_CLIPBOARD_DIR:-$AGENTS_DIR/data/clipboard}"
IMG="$CLIP_DIR/image.png"
TMP="$CLIP_DIR/image.png.tmp"
PIDFILE="$CLIP_DIR/bridge.pid"
LOGFILE="$CLIP_DIR/bridge.log"
INTERVAL="${AGENTS_CLIPBOARD_INTERVAL:-0.4}"

log() { printf '%s\n' "$*" >&2; }

require_macos() {
  [[ "$(uname -s)" == "Darwin" ]] || {
    log "error: clipboard-bridge requires macOS (host clipboard)"
    exit 1
  }
}

write_png_from_clipboard() {
  # Write clipboard PNG to $1. Exit 0 on success, 1 if clipboard has no PNG.
  local out="$1"
  osascript >/dev/null 2>&1 <<APPLESCRIPT
try
  set png_data to (the clipboard as «class PNGf»)
on error
  error "no-png"
end try
set fRef to open for access (POSIX file "$out") with write permission
try
  set eof fRef to 0
  write png_data to fRef
end try
close access fRef
APPLESCRIPT
}

sync_once() {
  if write_png_from_clipboard "$TMP"; then
    mv -f "$TMP" "$IMG"
  else
    rm -f "$TMP" "$IMG"
  fi
}

run_loop() {
  mkdir -p "$CLIP_DIR"
  # shellcheck disable=SC2064
  trap 'rm -f "$TMP"; [[ -f "$PIDFILE" ]] && [[ "$(cat "$PIDFILE" 2>/dev/null)" == "$$" ]] && rm -f "$PIDFILE"' EXIT
  echo $$ >"$PIDFILE"
  log "clipboard-bridge watching macOS clipboard → $IMG (interval ${INTERVAL}s)"
  while true; do
    sync_once || true
    sleep "$INTERVAL"
  done
}

is_running() {
  local pid
  [[ -f "$PIDFILE" ]] || return 1
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [[ -n "${pid:-}" ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

cmd="${1:-}"
require_macos
mkdir -p "$CLIP_DIR"

case "$cmd" in
  --daemon|daemon)
    if is_running; then
      log "clipboard-bridge already running (pid $(cat "$PIDFILE"))"
      exit 0
    fi
    nohup "$0" >>"$LOGFILE" 2>&1 &
    # child writes pidfile; give it a moment
    sleep 0.2
    if is_running; then
      log "clipboard-bridge started (pid $(cat "$PIDFILE"))"
      exit 0
    fi
    log "error: clipboard-bridge failed to start — see $LOGFILE"
    exit 1
    ;;
  --stop|stop)
    if ! is_running; then
      rm -f "$PIDFILE"
      log "clipboard-bridge not running"
      exit 0
    fi
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"
    log "clipboard-bridge stopped"
    exit 0
    ;;
  --status|status)
    if is_running; then
      printf 'running pid=%s image=%s\n' "$(cat "$PIDFILE")" "$([[ -s $IMG ]] && echo yes || echo no)"
      exit 0
    fi
    printf 'stopped\n'
    exit 1
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

Mirrors macOS clipboard PNGs into:
  $IMG

The agents container exposes this via xclip/wl-paste stubs so Claude Code
and the Cursor CLI can Ctrl+V screenshots (Cmd+V does not work in the TUI).
EOF
    exit 0
    ;;
  *)
    log "unknown option: $cmd (try --help)"
    exit 1
    ;;
esac
