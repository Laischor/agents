#!/usr/bin/env bash
# Write a Linux gh config + git credential helper into data/ so containers
# keep GitHub auth across restarts. macOS stores the OAuth token in Keychain;
# mounting ~/.config/gh is not enough.
#
# Source from start.sh / run.sh after AGENTS_DIR is set.
#   source "$AGENTS_DIR/bin/ensure-gh-passthrough.sh"
#   ensure_gh_passthrough

ensure_gh_passthrough() {
  local gh_dir="$AGENTS_DIR/data/gh"
  local gitconfig_file="$AGENTS_DIR/data/gitconfig"
  local hosts_yml="$gh_dir/hosts.yml"
  local name email user

  mkdir -p "$gh_dir"
  chmod 700 "$gh_dir" 2>/dev/null || true

  name="$(git config --global --get user.name 2>/dev/null || true)"
  email="$(git config --global --get user.email 2>/dev/null || true)"
  if [[ -f "${HOME}/.gitconfig" ]]; then
    [[ -n "$name" ]] || name="$(git config -f "${HOME}/.gitconfig" --get user.name 2>/dev/null || true)"
    [[ -n "$email" ]] || email="$(git config -f "${HOME}/.gitconfig" --get user.email 2>/dev/null || true)"
  fi

  # File must exist before Compose bind-mounts it (else Docker creates a dir).
  : >"$gitconfig_file"
  git config -f "$gitconfig_file" credential.helper '!gh auth git-credential'
  if [[ -n "$name" ]]; then
    git config -f "$gitconfig_file" user.name "$name"
  fi
  if [[ -n "$email" ]]; then
    git config -f "$gitconfig_file" user.email "$email"
  fi

  if [[ -z "${GH_TOKEN:-}" ]]; then
    return 0
  fi
  export GITHUB_TOKEN="${GITHUB_TOKEN:-$GH_TOKEN}"

  user=""
  if command -v gh >/dev/null 2>&1; then
    user="$(gh api user --jq .login 2>/dev/null || true)"
  fi
  [[ -n "$user" ]] || user="github-user"

  GH_USER="$user" python3 - "$hosts_yml" <<'PY'
import json, os, pathlib, sys
dest = pathlib.Path(sys.argv[1])
token = os.environ["GH_TOKEN"]
user = os.environ["GH_USER"]
# Match post-migration hosts.yml (unquoted user) so gh does not rewrite under :ro.
dest.write_text(
    "github.com:\n"
    "    git_protocol: https\n"
    "    users:\n"
    f"        {json.dumps(user)}:\n"
    f"            oauth_token: {json.dumps(token)}\n"
    f"    user: {user}\n"
    f"    oauth_token: {json.dumps(token)}\n"
)
PY
  chmod 600 "$hosts_yml"
  # gh ≥2.40 migrates config.yml (adds version) and fatals if the mount is :ro.
  # Pre-seed version so credential-helper / any gh call skips migration.
  # See: https://github.com/cli/cli/issues/8462
  printf '%s\n' 'git_protocol: https' 'version: "1"' >"$gh_dir/config.yml"

  if command -v hermes_enabled >/dev/null 2>&1 && hermes_enabled; then
    _upsert_dotenv_key "$AGENTS_DIR/data/hermes/.env" GH_TOKEN "$GH_TOKEN"
    _upsert_dotenv_key "$AGENTS_DIR/data/hermes/.env" GITHUB_TOKEN "$GITHUB_TOKEN"
  fi
}

_upsert_dotenv_key() {
  local file="$1" key="$2" value="$3"
  local tmp
  mkdir -p "$(dirname "$file")"
  [[ -f "$file" ]] || : >"$file"
  tmp="$(mktemp)"
  grep -vE "^(export[[:space:]]+)?${key}=" "$file" >"$tmp" || true
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  mv "$tmp" "$file"
  chmod 600 "$file" 2>/dev/null || true
}
