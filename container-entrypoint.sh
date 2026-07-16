#!/usr/bin/env bash
# Runtime setup for mounted agent configs (Pi packages, Claude plugins/MCP, Cursor MCP).
# Image build cannot write into volume-mounted ~/.pi / ~/.claude / ~/.cursor.

set -u

# ~/.pi is mounted from the host, so Pi packages must be registered after the
# volume is available rather than while the image is being built.
ensure_pi_package() {
  local match="$1"
  local source="$2"

  if pi list 2>/dev/null | grep -Fq "$match"; then
    return
  fi

  printf 'Installing Pi package: %s\n' "$source"
  if ! pi install "$source"; then
    printf 'warning: could not install Pi package %s\n' "$source" >&2
  fi
}

# Claude configs live under CLAUDE_CONFIG_DIR (/root/.claude, host-mounted).
ensure_claude_caveman() {
  if ! command -v claude >/dev/null 2>&1; then
    return
  fi

  mkdir -p "${CLAUDE_CONFIG_DIR:-/root/.claude}"

  if claude plugin list 2>/dev/null | grep -Fq 'caveman@caveman'; then
    return
  fi

  printf 'Installing Claude plugin: caveman@caveman\n'
  if ! claude plugin marketplace add JuliusBrussee/caveman; then
    printf 'warning: could not add caveman marketplace\n' >&2
    return
  fi
  if ! claude plugin install caveman@caveman; then
    printf 'warning: could not install caveman plugin\n' >&2
  fi
}

ensure_claude_codegraph_mcp() {
  if ! command -v claude >/dev/null 2>&1 || ! command -v codegraph >/dev/null 2>&1; then
    return
  fi

  mkdir -p "${CLAUDE_CONFIG_DIR:-/root/.claude}"

  if claude mcp list 2>/dev/null | grep -Eqi '^codegraph\b'; then
    return
  fi

  printf 'Configuring Claude MCP: codegraph\n'
  if ! claude mcp add codegraph -- codegraph serve --mcp; then
    printf 'warning: could not add codegraph MCP to Claude\n' >&2
  fi
}

# Cursor CLI config is mounted at /root/.cursor
ensure_cursor_codegraph_mcp() {
  if ! command -v codegraph >/dev/null 2>&1; then
    return
  fi

  local mcp_json="${HOME}/.cursor/mcp.json"
  mkdir -p "${HOME}/.cursor"

  if [[ -f "$mcp_json" ]] && grep -Fq '"codegraph"' "$mcp_json" 2>/dev/null; then
    return
  fi

  printf 'Configuring Cursor MCP: codegraph\n'
  if ! codegraph install --target=cursor --location=global --yes --no-permissions; then
    printf 'warning: could not configure codegraph MCP for Cursor\n' >&2
  fi
}

ensure_pi_package "v2nic/pi-caveman" \
  "git:github.com/v2nic/pi-caveman@2480692ffabddc3d1efec8eb822e664ff7e0e5ef"
ensure_pi_package "@vndv/pi-codegraph" \
  "npm:@vndv/pi-codegraph@0.1.10"

ensure_claude_caveman
ensure_claude_codegraph_mcp
ensure_cursor_codegraph_mcp

exec "$@"
