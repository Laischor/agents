#!/usr/bin/env bash
# Mint a T3 pairing token and print a QR for the *host* URL.
# t3 itself advertises the container eth0 address (172.x); phones cannot use that.
set -euo pipefail

REAL="${T3_REAL_BIN:-}"
if [[ -z "$REAL" ]]; then
  if [[ -x /usr/local/bin/t3-real ]]; then
    REAL=/usr/local/bin/t3-real
  else
    REAL="$(command -v t3)"
  fi
fi

PUBLIC="${T3CODE_PUBLIC_URL:-http://127.0.0.1:3773}"
PUBLIC="${PUBLIC%/}"

has_base=0
has_ttl=0
for a in "$@"; do
  case "$a" in
    --base-dir|--base-dir=*) has_base=1 ;;
    --ttl|--ttl=*) has_ttl=1 ;;
  esac
done

args=()
[[ "$has_base" -eq 1 ]] || args+=(--base-dir "${T3CODE_HOME:-/root/.t3}")
[[ "$has_ttl" -eq 1 ]] || args+=(--ttl 1h)
args+=("$@")

out=""
ec=0
out="$("$REAL" pair "${args[@]}")" || ec=$?
if [[ "$ec" -ne 0 ]]; then
  printf '%s\n' "$out" >&2
  exit "$ec"
fi

token="$(printf '%s\n' "$out" | awk '/^Token:/{print $2; exit}')"
expires="$(printf '%s\n' "$out" | awk '/^Expires:/{print $2; exit}')"
if [[ -z "$token" ]]; then
  printf '%s\n' "$out" >&2
  echo "error: t3 pair printed no Token:" >&2
  exit 1
fi

url="${PUBLIC}/pair#token=${token}"
home="${T3CODE_HOME:-/root/.t3}"
mkdir -p "$home"
{
  printf 'url=%s\n' "$url"
  printf 'token=%s\n' "$token"
} > "$home/pairing.txt"

printf 'Token: %s\n' "$token"
[[ -n "$expires" ]] && printf 'Expires: %s\n' "$expires"
printf 'Pairing URL: %s\n\n' "$url"

here="$(cd "$(dirname "$0")" && pwd)"
if [[ -x /usr/local/bin/t3-qr ]]; then
  /usr/local/bin/t3-qr "$url"
elif [[ -x "$here/t3-qr.sh" ]]; then
  "$here/t3-qr.sh" "$url"
fi

printf '\nScan the QR or paste the token into %s\n' "$PUBLIC"
printf 'Saved %s/pairing.txt\n' "$home"
