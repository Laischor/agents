#!/usr/bin/env bash
# Run wrap's unit tests (stdlib unittest — no pytest, no network, no CLIs).
#
#   ./wrap/tests/run.sh
#
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
wrap="$(dirname "$here")"
exec python3 -m unittest discover -s "$here" -t "$wrap" -v "$@"
