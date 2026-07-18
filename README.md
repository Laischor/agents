# agents

Docker environment for coding agents: **Cursor CLI**, **Pi**, and **Claude Code**.  
Also included: **Claudex** (Claude Code + GPT-5.6 Sol via CLIProxyAPI), optional **Hermes Agent**, **Caveman**, and **CodeGraph**.  
Projects stay on the host; agents run isolated in the container and drive Docker through the host socket.

## Requirements

- macOS
- Homebrew ([brew.sh](https://brew.sh)) — for automatic Docker/Colima installation
- SSH key / login for the respective agent accounts (or API keys)
- For Claudex: ChatGPT Plus/Pro with Codex access

## Setup

```bash
git clone https://github.com/Laischor/agents.git
cd agents
./start.sh
```

`start.sh` checks for macOS, prepares Docker (installs `colima`, `docker`, and `docker-compose` via Homebrew if needed and starts Colima), and brings up `agents` + `cli-proxy-api`. If `CLIPROXY_API_KEY` is missing, it is generated and written to `.env` and `data/cliproxy/config.yaml`.

Then adjust `.env` if you have not already:

```bash
# Absolute path to your projects (same path inside the container)
HOST_PROJECTS=/Users/YOU/Documents/projects

CURSOR_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
CLIPROXY_API_KEY=   # optional — start.sh creates one
HERMES=0            # set to 1 to start the Hermes Agent container
```

### Shell aliases

In `~/.zshrc` (adjust the path if the repo lives elsewhere):

```bash
export AGENTS_DIR="${AGENTS_DIR:-$HOME/Documents/projects/agents}"
agents() { "$AGENTS_DIR/run.sh" "$@"; }
dagent()     { agents agent "$@"; }
dpi()        { agents pi "$@"; }
dclaude()    { agents claude "$@"; }
dclaudex()   { agents claudex "$@"; }
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
dclaudex     # Claude Code harness → GPT-5.6 Sol (Claudex)
dhermes      # Hermes Agent CLI (requires HERMES=1)
dcodegraph   # CodeGraph CLI
agents-shell
```

The launcher starts the container if needed and sets the working directory 1:1 to the host path.

## Screenshot paste (Claude)

Docker cannot read the macOS clipboard. A small host bridge mirrors clipboard PNGs into `data/clipboard/`; stubs for `xclip` / `wl-paste` inside the container serve them to Claude Code.

- Starts automatically with `dclaude` / `dclaudex` (or: `agents clipboard-bridge --daemon`)
- Copy a screenshot to the clipboard, then paste with **Ctrl+V** (not Cmd+V)
- Status / stop: `agents clipboard-bridge --status` · `agents clipboard-bridge --stop`

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

Gateway API listens on `localhost:8642`; web dashboard on `localhost:9119` (enabled via `HERMES_DASHBOARD=1` in Compose). Docker requires basic auth — `./start.sh` writes `HERMES_DASHBOARD_USER` / `HERMES_DASHBOARD_PASSWORD` / `HERMES_DASHBOARD_SECRET` into `.env` if missing. State lives in `data/hermes/` (`/opt/data` in the container). Setting `HERMES=0` and running `./start.sh` again stops a previously running Hermes container.

### Terminal sandbox (`agents:local`)

Hermes runs with `terminal.backend: docker` and image `agents:local`. Shell / file / code tools execute in a long-lived sandbox that has **Claude Code**, **Cursor CLI** (`agent`), and **Pi** on `PATH`, with the same auth mounts as the `agents` service (`data/claude`, `data/cursor`, …). The directory you launch from is mounted at `/workspace` inside the sandbox. Hermes needs the host Docker socket for this; `./start.sh` also writes `AGENTS_DIR` into `.env` so bind-mount paths resolve.

## Claudex (GPT-5.6 Sol)

Claude Code as the harness, inference via **CLIProxyAPI** → ChatGPT Codex (OAuth). `dclaude` stays on Anthropic unchanged; the proxy env applies only to `dclaudex`.

Log in to ChatGPT/Codex once (prints a URL; callback on `localhost:54545`):

```bash
agents cliproxy-login
```

Then:

```bash
cd ~/Documents/projects/my-project
dclaudex
# In the session: /status  → model gpt-5.6-sol, base URL proxy
```

Note: Routing subscription OAuth through a third-party proxy may violate provider terms and carries account/credential risk. Run it locally and do not share credentials.

## Persistence

Configs/auth live on the host under `./data/` and survive rebuilds:

| Host              | Container            |
|-------------------|----------------------|
| `~/.gitconfig`         | `/root/.gitconfig` (read-only) |
| `~/.ssh/`              | `/root/.ssh` (read-only, GitHub SSH) |
| `data/cursor/`         | `/root/.cursor`         |
| `data/cursor-config/`  | `/root/.config/cursor` (login tokens) |
| `data/pi/`             | `/root/.pi`             |
| `data/claude/`         | `/root/.claude` (`CLAUDE_CONFIG_DIR`) |
| `data/clipboard/`      | `/var/agents-clipboard` (host PNG bridge for Claude paste) |
| `data/cliproxy/`       | CLIProxyAPI config + OAuth (`auths/`) |
| `data/hermes/`         | Hermes Agent config / sessions (`/opt/data`) |

`data/` and `.env` are gitignored.

## Caveman & CodeGraph

On container start the entrypoint registers (idempotent):

| Agent        | Caveman                                      | CodeGraph                                      |
|--------------|----------------------------------------------|------------------------------------------------|
| Pi           | `pi-caveman` package                         | `pi-codegraph` + CLI `codegraph`               |
| Claude Code  | Plugin `caveman@caveman` (default: **ultra**) | MCP `codegraph serve --mcp`                    |
| Cursor CLI   | —                                            | MCP in `~/.cursor/mcp.json`                    |

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
