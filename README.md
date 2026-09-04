# agents

Docker environment for coding agents: **Cursor CLI**, **Pi**, **Claude Code**, and **OpenCode**.  
Also included: optional **Hermes Agent** and **Caveman**.  
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
HERMES=0            # set to 1 to start Hermes + Firecrawl (Molx)
FIRECRAWL=0         # set to 1 to start Molx without Hermes (:3002)
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
dwrap()      { agents wrap "$@"; }
dhermes()    { agents hermes "$@"; }
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
dwrap        # wrap URL (native CLI sessions at :3000)
dhermes      # Hermes Agent CLI (requires HERMES=1)
agents-shell
```

The launcher starts the container if needed and sets the working directory 1:1 to the host path.

### Wrap (native sessions)

**Wrap** is a thin web UI over the **same native CLI process** (no extra harness):

| Agent | How wrap talks to it | Extra model calls |
|---|---|---|
| Claude Code | tmux session + `~/.claude/projects/…/*.jsonl` | none |
| Cursor CLI | tmux session + `~/.cursor/projects/…/agent-transcripts/` | none |
| OpenCode | `opencode serve` HTTP API (same backend as the TUI) | none |
| Hermes | Gateway API on `:8642` (only when `HERMES=1`) | none |

The container starts wrap on `0.0.0.0:3000` (host: `http://127.0.0.1:3000`). Pick a project, choose model/effort, then **New** — several sessions per project and agent can run in parallel. A message is pasted into that session's live TUI (or posted to OpenCode / the Hermes gateway). Chat bubbles come from the CLI's own transcript, not a second agent.

Pin a session with the thumbtack in the sidebar — pinned sessions stay at the top of the list after wrap restarts, even when they are closed.

```bash
open http://127.0.0.1:3000
agents wrap                 # print URL if already up
```

TUI permission prompts show **Yes** / **No** in the chat when they appear (or open the **TUI** pane). **Stop** kills that tmux process only. OpenCode and Hermes sessions stay in their own history (Stop does not delete them).

Set `AGENTS_WRAP_SERVE=0` in `.env` to skip auto-start. Recreate `agents` from the **host** after this change (`./start.sh`) so the image has `tmux` and the port mapping.

## GitHub CLI (`gh`)

On macOS, `gh auth login` stores the OAuth token in the **Keychain**, not in `~/.config/gh/hosts.yml`. Mounting that directory into Linux is not enough.

`./start.sh` and `run.sh` call `gh auth token` on the host and write a Linux gh config (with `oauth_token`) plus a git credential helper into `data/gh/` and `data/gitconfig` (gitignored). Those files are mounted **read-only** into `agents` and the Hermes gateway (`/opt/data/.config/gh`, `/opt/data/.gitconfig`). `config.yml` includes `version: "1"` so `gh` ≥2.40 does not try to migrate (and fatal) on the `:ro` mount. They also set `GH_TOKEN` / `GITHUB_TOKEN` on the containers and in `data/hermes/.env`. Prerequisites: `gh` installed and logged in on the host. Override anytime with `GH_TOKEN=` in `.env`.

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

Prefer absolute paths under `HOST_PROJECTS` for args. Override cwd with `GPU_JOB_CWD=…` if needed.

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

With `HERMES=1`, Compose starts the gateway and [Molx](https://github.com/ioi-labs/molx) as the Firecrawl-compatible `web_search` / `web_extract` backend:

| Surface | URL | Role |
|---------|-----|------|
| **Dashboard** | `http://127.0.0.1:9119` | Chat / config / monitoring (`HERMES_DASHBOARD=1`) |
| Gateway API | `localhost:8642` | Agent runtime + OpenAI-compatible API |
| **Firecrawl (Molx)** | `http://127.0.0.1:3002` | Search + extract (`FIRECRAWL_API_URL`) |

**Architecture:** Hermes runs **its own model** with a **local** terminal (shell/file/`execute_code` inside the `hermes` container). Chat is the dashboard, `dhermes` CLI, and wrap (Agent dropdown when `HERMES=1`). It does **not** spawn `agents:local` or hand work off to Cursor / Claude Code / Pi / OpenCode. Requires `API_SERVER_ENABLED` + `HERMES_API_SERVER_KEY` (auto-generated by `./start.sh` if empty).

Docker requires dashboard basic auth — `./start.sh` writes `HERMES_DASHBOARD_USER` / `HERMES_DASHBOARD_PASSWORD` / `HERMES_DASHBOARD_SECRET` into `.env` if missing. Hermes state lives in `data/hermes/` (`/opt/data` in the gateway). `HERMES_WRITE_SAFE_ROOT` is `/opt/data:${HOST_PROJECTS}` so `write_file`/`patch` can touch Hermes state and the mounted project tree. Setting `HERMES=0` and running `./start.sh` again stops hermes and firecrawl (unless `FIRECRAWL=1`).

## Firecrawl

Self-hosted scrape/search API. Official [Firecrawl OSS](https://docs.firecrawl.dev/contributing/self-host) is a five-container stack (API, Playwright, Redis, RabbitMQ, Postgres). This repo runs a **single** [Molx](https://github.com/ioi-labs/molx) container instead — Firecrawl-shaped `/v2/scrape` plus native search, no queue/browser sidecars.

Starts automatically with `HERMES=1` (Hermes web tools). Without Hermes, set `FIRECRAWL=1`:

```bash
# In .env: FIRECRAWL=1
./start.sh
```

The API is localhost-only (`127.0.0.1:3002`). Auth is off unless you set `FIRECRAWL_API_KEY`. Hermes is pinned to this instance (`FIRECRAWL_API_URL=http://firecrawl:3002`, `web.backend: firecrawl` in `data/hermes/config.yaml`).

| Surface | URL | Role |
|---------|-----|------|
| **API** | `http://127.0.0.1:3002` | Host / browser / curl |
| **From agents / Hermes** | `http://firecrawl:3002` | Compose-network URL (`FIRECRAWL_API_URL`) |

```bash
curl -sf http://127.0.0.1:3002/health
curl -sS -X POST http://127.0.0.1:3002/v2/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","formats":["markdown"]}'
```

Search is built into Molx. Optional LLM extract uses `OPENAI_API_KEY` + `FIRECRAWL_MODEL_NAME`. Setting `FIRECRAWL=0` with `HERMES=0` and running `./start.sh` again stops the container.

### Terminal (local)

Hermes uses `terminal.backend: local` (`TERMINAL_ENV=local`). Shell, file, and `execute_code` tools run **inside the `hermes` container** — not in `agents:local`, and without Cursor CLI / Claude Code on `PATH`. Coding agents stay in the separate `agents` service (`dagent` / `dclaude` / …).

`HOST_PROJECTS` is bind-mounted so `write_file` / `patch` can touch the project tree (`HERMES_WRITE_SAFE_ROOT=/opt/data:/tmp:${HOST_PROJECTS}`). There is no Docker socket on the gateway.

## Persistence

Configs/auth live on the host under `./data/` and survive rebuilds:

| Host              | Container            |
|-------------------|----------------------|
| `~/.gitconfig`         | `/etc/gitconfig.host` (read-only; identity only) |
| `~/.ssh/`              | `/root/.ssh` (read-only, GitHub SSH) |
| `data/gitconfig`       | Hermes gateway: `/opt/data/.gitconfig` (identity + `gh auth git-credential`) |
| `data/gh/`             | `agents`: `/root/.config/gh`; Hermes gateway: `/opt/data/.config/gh` (Linux hosts.yml with token) |
| `data/cursor/`         | `/root/.cursor`         |
| `data/cursor-config/`  | `/root/.config/cursor` (login tokens) |
| `data/pi/`             | `/root/.pi`             |
| `data/claude/`         | `/root/.claude` (`CLAUDE_CONFIG_DIR`) |
| `data/opencode/`       | `/root/.local/share/opencode` (auth + sessions) |
| `data/opencode-config/`| `/root/.config/opencode` |
| `data/clipboard/`      | `/var/agents-clipboard` (host PNG bridge for Claude/Cursor/OpenCode paste) |
| `data/gpu/`            | `/var/agents-gpu` (host Blender/Godot job queue via gpu-bridge) |
| `data/cmux/`           | `/var/agents-cmux` (host cmux notify/hooks queue via cmux-bridge) |
| `data/hermes/`         | Hermes home: gateway `/opt/data` |

`data/` and `.env` are gitignored.

## Process hygiene (long-lived container)

The `agents` container stays up for many `compose exec` sessions. Each session can leave behind child processes or zombies. Compose enables:

- **`init: true`** — Docker’s tini as PID 1 reaps zombies
- **`mem_limit`** — hard memory cap so thrash cannot freeze the whole container (default `3g` via `AGENTS_MEM_LIMIT`; Colima default `4g` via `COLIMA_MEMORY`)

After changing these, rebuild/recreate from the **host** (`./start.sh` or `docker compose up -d --build agents`). Check: `docker compose exec agents ps -p 1 -o args=` (should show `docker-init` / tini) and `docker stats agents` (≈3 Gi limit).

## Caveman

On container start the entrypoint registers (idempotent):

| Agent        | Caveman                                      |
|--------------|----------------------------------------------|
| Pi           | `pi-caveman` package                         |
| Claude Code  | Plugin `caveman@caveman` (default: **ultra**) |

Default mode for Claude: `CAVEMAN_DEFAULT_MODE=ultra` in `docker-compose.yml`. Override per session with `/caveman lite|full|ultra`.

## Docker from the agent

The container mounts `${DOCKER_SOCK:-/var/run/docker.sock}`. Agents can build/start host containers (`docker compose up`, …) as long as they work under `HOST_PROJECTS` — bind mounts in project compose files need the same host path.

## Cloning the repo elsewhere

1. Clone and create `.env` with a local `HOST_PROJECTS`  
2. Set `AGENTS_DIR` to the new repo path  
3. `docker-compose up -d --build`
