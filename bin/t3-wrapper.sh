#!/usr/bin/env bash
# Intercept `t3 pair` so the QR encodes T3CODE_PUBLIC_URL, not 172.x.
set -euo pipefail
REAL="${T3_REAL_BIN:-/usr/local/bin/t3-real}"
if [[ "${1:-}" == "pair" ]]; then
  shift
  exec /usr/local/bin/t3-pair "$@"
fi
exec "$REAL" "$@"
