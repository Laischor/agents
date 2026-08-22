#!/usr/bin/env bash
# Print a UTF-8 QR for the pairing URL (not the Docker-bridge address).
set -euo pipefail

url="${1:-}"
if [[ -z "$url" ]]; then
  echo "usage: t3-qr URL" >&2
  exit 1
fi

if command -v qrencode >/dev/null 2>&1; then
  qrencode -t ansiutf8 -m 1 "$url"
  exit 0
fi

if command -v qrcode-terminal >/dev/null 2>&1; then
  qrcode-terminal "$url"
  exit 0
fi

if command -v python3 >/dev/null 2>&1 && python3 -c 'import qrcode' 2>/dev/null; then
  T3_QR_URL="$url" python3 - <<'PY'
import os
import qrcode

q = qrcode.QRCode(border=1)
q.add_data(os.environ["T3_QR_URL"])
q.print_ascii(invert=True)
PY
  exit 0
fi

printf '(qrencode missing — open the Pairing URL or paste the token)\n' >&2
exit 0
