# agents

Docker environment for coding agents: **Cursor CLI**, **Pi**, **Claude Code**, **OpenCode**, and **T3 Code**.  
Also included: optional **Hermes Agent**, **Caveman**, and **CodeGraph**.  
Projects stay on the host; agents run isolated in the container and drive Docker through the host socket.

## Requirements

- macOS
- Homebrew ([brew.sh](https://brew.sh)) — for automatic Docker/Colima installation
- SSH key / login for the respective agent accounts (or API keys)

## Setup

```bash
git clone https://github.com/Laischor/agents.git
cd agents
./start.sh
```

`start.sh` checks for macOS, prepares Docker (installs `colima`, `docker`, and `docker-compose` via Homebrew if needed and starts Colima), and brings up the `agents` container.

Then adjust `.env` if you have not already:

```bash
# Absolute path to your projects (same path inside the container)
HOST_PROJECTS=/Users/YOU/Documents/projects

CURSOR_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
HERMES=0            # set to 1 to start the Hermes Agent container
COLIMA_MEMORY=4     # Colima VM RAM in GiB (when start.sh starts Colima)
AGENTS_MEM_LIMIT=3g # agents container mem_limit
```

### Shell aliases

In `~/.zshrc` (adjust the path if the repo lives elsewhere):

```bash
export AGENTS_DIR="${AGENTS_DIR:-$HOME/Documents/projects/agents}"
agents() { "$AGENTS_DIR/run.sh" "$@"; }
dagent()     { agents agent "$@"; }
dpi()        { agents pi "$@"; }
dclaude()    { agents claude "$@"; }
dopencode()  { agents opencode "$@"; }
dt3()        { agents t3 "$@"; }
dhermes()    { agents hermes "$@"; }
dcodegraph() { agents codegraph "$@"; }
agents-shell() { agents bash "$@"; }
```

```bash
source ~/.zshrc
```

## Usage

From a directory under `HOST_PROJECTS`:

```bash
cd ~/Documents/projects/my-project
dagent       # Cursor CLI
dpi          # Pi
dclaude      # Claude Code (Anthropic)
dopencode    # OpenCode
dt3          # T3 Code CLI (web UI auto-starts at :3773)
dhermes      # Hermes Agent CLI (requires HERMES=1)
dcodegraph   # CodeGraph CLI
agents-shell
```

The launcher starts the container if needed and sets the working directory 1:1 to the host path.

### T3 Code web UI

The container starts a headless **T3 Code** server (`t3 serve`) that drives the already-installed Claude, Cursor, and OpenCode CLIs. Docker publish only reaches the container's eth0, so the server binds `0.0.0.0:3773` inside; the host mapping is localhost-only (`127.0.0.1:3773:3773`).

```bash
# After ./start.sh from the host — pairing token is printed there, and:
open http://127.0.0.1:3773
dt3 pair                 # mint a new token (rewritten to localhost, saved in data/t3/pairing.txt)
dt3 project add "$PWD"   # add a repo (paths must be under HOST_PROJECTS)
dt3 --help
```

T3 does not have a long-lived deploy password. Each browser/device needs a **one-time pairing token** minted on the running server:

1. `./start.sh` waits for `:3773` and prints `Token:` plus `http://127.0.0.1:3773/pair#token=…`
2. Later (or if you missed it): `dt3 pair` — same output, also `data/t3/pairing.txt`
3. Paste the token into the pairing page, or open the **Host URL** (not the Docker `172.x` address)

Tokens expire (default via `dt3 pair`: 1 hour) and are single-use. Remote access: set `T3CODE_PUBLIC_URL` (e.g. a Tailscale HTTPS URL) so pairing links are rewritten to that host instead of localhost.

Set `AGENTS_T3_SERVE=0` in `.env` to skip auto-start, then `dt3 serve` when you want it. Recreate `agents` from the **host** after changing the compose port mapping (`./start.sh`).

Turn on Cursor and OpenCode in T3 **Settings** (they ship off by default). Provider logins stay the existing CLIs (`claude auth login`, `agent login`, `opencode auth login`) inside the container.

## GitHub CLI (`gh`)

On macOS, `gh auth login` stores the OAuth token in the **Keychain**, not in `~/.config/gh/hosts.yml`. Mounting that directory into Linux is not enough — and for Hermes it is harmful: the Docker sandbox bind-mounts a persist dir over `/root`, so a token-less host `~/.config/gh` hides any in-container login after a restart.

`./start.sh` and `run.sh` call `gh auth token` on the host and write a Linux gh config (with `oauth_token`) plus a git credential helper into `data/gh/` and `data/gitconfig` (gitignored). Those files are mounted **read-only** into `agents`, the Hermes gateway (`/opt/data/.config/gh`), and the Hermes sandbox (`/root/.config/gh` and `/root/.gitconfig`). `config.yml` includes `version: "1"` so `gh` ≥2.40 does not try to migrate (and fatal) on the `:ro` mount. They also set `GH_TOKEN` / `GITHUB_TOKEN` on the containers and in `data/hermes/.env`. `./start.sh` removes leftover Hermes sandbox containers so the new mounts apply (volume mounts are frozen at `docker run`). Prerequisites: `gh` installed and logged in on the host. Override anytime with `GH_TOKEN=` in `.env`.

Git in the container uses `/etc/gitconfig` for `safe.directory *` and `credential.helper = !gh auth git-credential`. Host `~/.gitconfig` is mounted at `/etc/gitconfig.host` (read-only) so only `user.name` / `user.email` are copied into the container — not macOS credential helpers that clear the chain or point at `/opt/homebrew/bin/gh`. On the host you can use the same pathless helper: `helper = !gh auth git-credential`.

## Screenshot paste (Claude + Cursor + OpenCode)

Docker cannot read the macOS clipboard. A small host bridge mirrors clipboard PNGs into `data/clipboard/`; stubs for `xclip` / `wl-paste` inside the container serve them to Claude Code, the Cursor CLI (`agent`), and OpenCode.

- Starts automatically with `dagent` / `dclaude` / `dopencode` (or: `agents clipboard-bridge --daemon`)
- Copy a screenshot to the clipboard, then paste with **Ctrl+V** (not Cmd+V)
- Cursor needs a dummy `DISPLAY` (set in Compose / `run.sh`) so it will call the stubs
- Status / stop: `agents clipboard-bridge --status` · `agents clipboard-bridge --stop`

## cmux notifications / sounds

Docker cannot use the host cmux control socket without opening it to every local process. Instead **cmux-bridge** delivers alerts without that:

1. `run.sh` registers the host TTY of the launching cmux pane under `data/cmux/sessions/`
2. Container `cmux notify` / agent hooks enqueue a job in `data/cmux/`
3. The host daemon writes **OSC 777** to that TTY (pane ring + desktop notification) and plays **afplay**

- Starts automatically with `dagent` / `dclaude` / `dopencode` (or: `agents cmux-bridge --daemon`)
- Container stub: `cmux` / `cmux-agent-hook` (Claude `Notification`/`Stop`, Cursor `stop` only)
- Forwards `CMUX_WORKSPACE_ID` / `CMUX_SURFACE_ID` for per-pane session routing
- Stop hooks always notify (cmux `hooks … stop` alone only updates sidebar state)
- Cursor has **no** `afterAgentResponse` hook — that plus `stop` double-fired every turn
- **No** cmux “Full open access” / `allowAll` required
- Status / stop: `agents cmux-bridge --status` · `agents cmux-bridge --stop`

Optional: copy [`cmux/config.env.example`](cmux/config.env.example) to `data/cmux/config.env` — `CMUX_BRIDGE_SOUND=0` to mute host afplay, or `CMUX_BRIDGE_SOUND_FILE=…`.

After changing the host bridge script:

```bash
agents cmux-bridge --stop && agents cmux-bridge --daemon
```

Launch agents from a **cmux** pane so the TTY can be registered. Rebuild/recreate `agents` from the **host** when the container stub changed.

## GPU bridge (Blender + Godot / Metal)

Docker on macOS cannot use Metal. Instead, opt-in **gpu-bridge** runs native Blender/Godot on the host; container `blender` / `godot` shims submit jobs via `data/gpu/`.

**Not** started automatically (unlike the clipboard bridge).

```bash
# Host: start the daemon (once per login / when you need GPU tools)
agents gpu-bridge --daemon
agents gpu-bridge --status   # shows resolved Blender/Godot paths
agents gpu-bridge --stop
```

Optional overrides — copy [`gpu/config.env.example`](gpu/config.env.example) to `data/gpu/config.env`:

```bash
BLENDER_BIN=/Applications/Blender.app/Contents/MacOS/Blender
GODOT_BIN=/Applications/Godot.app/Contents/MacOS/Godot
# or: GODOT_BIN=/opt/homebrew/bin/godot
GPU_JOB_TIMEOUT=600
```

Defaults: Blender at `/Applications/Blender.app/Contents/MacOS/Blender`; Godot probes `/Applications/Godot*.app`, Homebrew, and `/usr/local/bin/godot`.

From `agents-shell` (after rebuilding the image so the shims exist):

```bash
gpu-job status
blender --version
godot --version
# Example render (paths must stay under HOST_PROJECTS):
# blender --background "$HOST_PROJECTS/my-scene/scene.blend" -o "$HOST_PROJECTS/my-scene/out/frame" -f 1
# godot --headless --path "$HOST_PROJECTS/my-game" --quit
```

Security: only `blender` and `godot` are allowlisted; `cwd` and path-like args must stay under `HOST_PROJECTS`; one job at a time; per-job timeout. Jobs left in `data/gpu/running/` after a crash are marked error on the next daemon start.

In the Hermes sandbox (`/workspace`), shims map cwd back to `$HOST_PROJECTS/…` when `HOST_PROJECTS` is set; prefer absolute paths under `HOST_PROJECTS` for args. Override cwd with `GPU_JOB_CWD=…` if needed.

Rebuild/recreate `agents` from the **host** after this change (`./start.sh` or `docker compose build agents && docker compose up -d agents`) so the image includes the shims and the `data/gpu` mount.

## Hermes Agent

Optional [Nous Hermes Agent](https://hermes-agent.nousresearch.com/) gateway (`nousresearch/hermes-agent`). Disabled by default — only starts when `HERMES=1` in `.env` (Compose profile `hermes`).

```bash
# 1. One-time setup wizard (writes into data/hermes/)
agents hermes-setup

# 2. Enable and start
# In .env: HERMES=1
./start.sh

# 3. Interactive CLI
dhermes
```

With `HERMES=1`, Compose starts the gateway, [Open WebUI](https://github.com/open-webui/open-webui) chat, and SearxNG:

| Surface | URL | Role |
|---------|-----|------|
| **Open WebUI** | `http://127.0.0.1:3000` | Web chat frontend (first user = admin) |
| **Dashboard** | `http://127.0.0.1:9119` | Config / monitoring (`HERMES_DASHBOARD=1`) |
| Gateway API | `localhost:8642` | Agent runtime + OpenAI-compatible API |

**Architecture:** Open WebUI talks to Hermes' OpenAI-compatible API (`OPENAI_API_BASE_URL=http://hermes:8642/v1`), so tools, memory, cron, and the `agents:local` Docker sandbox stay on the gateway — Open WebUI is only the browser UI. Requires `API_SERVER_ENABLED` + `HERMES_API_SERVER_KEY` (auto-generated by `./start.sh` if empty; also used as Open WebUI's `OPENAI_API_KEY`). See [Hermes ↔ Open WebUI](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/open-webui).

Docker requires dashboard basic auth — `./start.sh` writes `HERMES_DASHBOARD_USER` / `HERMES_DASHBOARD_PASSWORD` / `HERMES_DASHBOARD_SECRET` into `.env` if missing. Hermes state lives in `data/hermes/` (`/opt/data` in the gateway); Open WebUI state in `data/open-webui/`. `HERMES_WRITE_SAFE_ROOT` is `/opt/data:${HOST_PROJECTS}` so `write_file`/`patch` can touch Hermes state and the mounted project tree. Setting `HERMES=0` and running `./start.sh` again stops hermes, open-webui, and searxng.

### Terminal sandbox (`agents:local`)

Hermes runs with `terminal.backend: docker` and image `agents:local`. Shell / file / code tools execute in a long-lived sandbox that has **Claude Code**, **Cursor CLI** (`agent`), **Pi**, **OpenCode**, **T3 Code** (`t3`), and **`gh`** on `PATH`, with the same auth mounts as the `agents` service (`data/claude`, `data/cursor`, `data/opencode`, `~/.config/gh`, …). The T3 **server** runs only in the `agents` container (not in the Hermes sandbox). The directory you launch from is mounted at `/workspace` inside the sandbox. Hermes needs the host Docker socket for this; `./start.sh` also writes `AGENTS_DIR` into `.env` so bind-mount paths resolve.

## Persistence

Configs/auth live on the host under `./data/` and survive rebuilds:

| Host              | Container            |
|-------------------|----------------------|
| `~/.gitconfig`         | `/etc/gitconfig.host` (read-only; identity only) |
| `~/.ssh/`              | `/root/.ssh` (read-only, GitHub SSH) |
| `data/gitconfig`       | `agents` / Hermes sandbox: `/root/.gitconfig`; Hermes gateway: `/opt/data/.gitconfig` (identity + `gh auth git-credential`) |
| `data/gh/`             | `agents` / Hermes sandbox: `/root/.config/gh`; Hermes gateway: `/opt/data/.config/gh` (Linux hosts.yml with token) |
| `data/cursor/`         | `/root/.cursor`         |
| `data/cursor-config/`  | `/root/.config/cursor` (login tokens) |
| `data/pi/`             | `/root/.pi`             |
| `data/claude/`         | `/root/.claude` (`CLAUDE_CONFIG_DIR`) |
| `data/opencode/`       | `/root/.local/share/opencode` (auth + sessions) |
| `data/opencode-config/`| `/root/.config/opencode` |
| `data/t3/`             | `/root/.t3` (T3 Code pairing, projects, threads) |
| `data/clipboard/`      | `/var/agents-clipboard` (host PNG bridge for Claude/Cursor/OpenCode paste) |
| `data/gpu/`            | `/var/agents-gpu` (host Blender/Godot job queue via gpu-bridge) |
| `data/cmux/`           | `/var/agents-cmux` (host cmux notify/hooks queue via cmux-bridge) |
| `data/hermes/`         | Hermes home: gateway `/opt/data` |
| `data/open-webui/`     | Open WebUI app data (`/app/backend/data`) |

`data/` and `.env` are gitignored.

## Process hygiene (long-lived container)

The `agents` container stays up for many `compose exec` / Hermes sessions. Each session can leave behind MCP children (`codegraph serve`) or zombies. Compose enables:

- **`init: true`** — Docker’s tini as PID 1 reaps zombies
- **`mem_limit`** — hard memory cap so thrash cannot freeze the whole container (default `3g` via `AGENTS_MEM_LIMIT`; Colima default `4g` via `COLIMA_MEMORY`)
- **`agents-janitor`** — background loop (started once via the entrypoint) that kills *orphaned* `codegraph` trees with no living `claude` / Cursor / OpenCode / T3 parent; under low `MemAvailable` (&lt;256 MiB) it also drops the oldest orphans first. Log: `/var/log/agents-janitor.log`

After changing these, rebuild/recreate from the **host** (`./start.sh` or `docker compose up -d --build agents`). Check: `docker compose exec agents ps -p 1 -o args=` (should show `docker-init` / tini) and `docker stats agents` (≈3 Gi limit).

## Caveman & CodeGraph

On container start the entrypoint registers (idempotent):

| Agent        | Caveman                                      | CodeGraph                                      |
|--------------|----------------------------------------------|------------------------------------------------|
| Pi           | `pi-caveman` package                         | `pi-codegraph` + CLI `codegraph`               |
| Claude Code  | Plugin `caveman@caveman` (default: **ultra**) | MCP `codegraph serve --mcp`                    |
| Cursor CLI   | —                                            | MCP in `~/.cursor/mcp.json`                    |
| OpenCode     | —                                            | MCP in `~/.config/opencode/opencode.json`      |

Default mode for Claude: `CAVEMAN_DEFAULT_MODE=ultra` in `docker-compose.yml`. Override per session with `/caveman lite|full|ultra`.

Index each project once:

```bash
cd ~/Documents/projects/my-project
dcodegraph init
```

## Docker from the agent

The container mounts `${DOCKER_SOCK:-/var/run/docker.sock}`. Agents can build/start host containers (`docker compose up`, …) as long as they work under `HOST_PROJECTS` — bind mounts in project compose files need the same host path.

## Cloning the repo elsewhere

1. Clone and create `.env` with a local `HOST_PROJECTS`  
2. Set `AGENTS_DIR` to the new repo path  
3. `docker-compose up -d --build`
