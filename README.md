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

## GitHub CLI (`gh`)

On macOS, `gh auth login` stores the OAuth token in the **Keychain**, not in `~/.config/gh/hosts.yml`. Mounting that directory alone is not enough for the Linux container.

`./start.sh` and `run.sh` call `gh auth token` on the host and pass the result as `GH_TOKEN` into the container (and Hermes sandbox). Prerequisites: `gh` installed and logged in on the host. Override anytime with `GH_TOKEN=` in `.env`.

Git in the container uses `/etc/gitconfig` for `safe.directory *` and `credential.helper = !gh auth git-credential`. Host `~/.gitconfig` is mounted at `/etc/gitconfig.host` (read-only) so only `user.name` / `user.email` are copied into the container — not macOS credential helpers that clear the chain or point at `/opt/homebrew/bin/gh`. On the host you can use the same pathless helper: `helper = !gh auth git-credential`.

## Screenshot paste (Claude + Cursor)

Docker cannot read the macOS clipboard. A small host bridge mirrors clipboard PNGs into `data/clipboard/`; stubs for `xclip` / `wl-paste` inside the container serve them to Claude Code and the Cursor CLI (`agent`).

- Starts automatically with `dagent` / `dclaude` / `dclaudex` (or: `agents clipboard-bridge --daemon`)
- Copy a screenshot to the clipboard, then paste with **Ctrl+V** (not Cmd+V)
- Cursor needs a dummy `DISPLAY` (set in Compose / `run.sh`) so it will call the stubs
- Status / stop: `agents clipboard-bridge --status` · `agents clipboard-bridge --stop`

## cmux notifications / sounds

Docker cannot use the host cmux control socket without opening it to every local process. Instead **cmux-bridge** delivers alerts without that:

1. `run.sh` registers the host TTY of the launching cmux pane under `data/cmux/sessions/`
2. Container `cmux notify` / agent hooks enqueue a job in `data/cmux/`
3. The host daemon writes **OSC 777** to that TTY (pane ring + desktop notification) and plays **afplay**

- Starts automatically with `dagent` / `dclaude` / `dclaudex` (or: `agents cmux-bridge --daemon`)
- Container stub: `cmux` / `cmux-agent-hook` (Claude `Notification`/`Stop`, Cursor `stop`/`afterAgentResponse`)
- Forwards `CMUX_WORKSPACE_ID` / `CMUX_SURFACE_ID` for per-pane session routing
- Stop hooks always notify (cmux `hooks … stop` alone only updates sidebar state)
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

Gateway API listens on `localhost:8642`; web dashboard on `localhost:9119` (enabled via `HERMES_DASHBOARD=1` in Compose). Docker requires basic auth — `./start.sh` writes `HERMES_DASHBOARD_USER` / `HERMES_DASHBOARD_PASSWORD` / `HERMES_DASHBOARD_SECRET` into `.env` if missing. State lives in `data/hermes/` (`/opt/data` in the container). `HERMES_WRITE_SAFE_ROOT` is `/opt/data:${HOST_PROJECTS}` so `write_file`/`patch` can touch Hermes state and the mounted project tree. Setting `HERMES=0` and running `./start.sh` again stops a previously running Hermes container.

### Terminal sandbox (`agents:local`)

Hermes runs with `terminal.backend: docker` and image `agents:local`. Shell / file / code tools execute in a long-lived sandbox that has **Claude Code**, **Cursor CLI** (`agent`), **Pi**, **`claudex`**, and **`gh`** on `PATH`, with the same auth mounts as the `agents` service (`data/claude`, `data/cursor`, `~/.config/gh`, …). The directory you launch from is mounted at `/workspace` inside the sandbox. Hermes needs the host Docker socket for this; `./start.sh` also writes `AGENTS_DIR` into `.env` so bind-mount paths resolve.

`claudex` is a shim in the image (`/usr/local/bin/claudex`): Claude Code → CLIProxyAPI → GPT-5.6 Sol. It picks `cli-proxy-api:8317` on the Compose network, otherwise `host.docker.internal:8317` (Hermes sandbox). Needs `CLIPROXY_API_KEY` and a logged-in proxy (`agents cliproxy-login`).

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
| `~/.gitconfig`         | `/etc/gitconfig.host` (read-only; identity only) |
| `~/.ssh/`              | `/root/.ssh` (read-only, GitHub SSH) |
| `~/.config/gh/`        | `/root/.config/gh` (read-only; config only — token via `GH_TOKEN`) |
| `data/cursor/`         | `/root/.cursor`         |
| `data/cursor-config/`  | `/root/.config/cursor` (login tokens) |
| `data/pi/`             | `/root/.pi`             |
| `data/claude/`         | `/root/.claude` (`CLAUDE_CONFIG_DIR`) |
| `data/clipboard/`      | `/var/agents-clipboard` (host PNG bridge for Claude/Cursor paste) |
| `data/gpu/`            | `/var/agents-gpu` (host Blender/Godot job queue via gpu-bridge) |
| `data/cmux/`           | `/var/agents-cmux` (host cmux notify/hooks queue via cmux-bridge) |
| `data/cliproxy/`       | CLIProxyAPI config + OAuth (`auths/`) |
| `data/hermes/`         | Hermes Agent config / sessions (`/opt/data`) |

`data/` and `.env` are gitignored.

## Process hygiene (long-lived container)

The `agents` container stays up for many `compose exec` / Hermes sessions. Each session can leave behind MCP children (`codegraph serve`) or zombies. Compose enables:

- **`init: true`** — Docker’s tini as PID 1 reaps zombies
- **`mem_limit: 3g`** — hard memory cap so thrash cannot freeze the whole container (Colima defaults to 4g)
- **`agents-janitor`** — background loop (started once via the entrypoint) that kills *orphaned* `codegraph` trees with no living `claude` / Cursor agent parent; under low `MemAvailable` (&lt;256 MiB) it also drops the oldest orphans first. Log: `/var/log/agents-janitor.log`

After changing these, rebuild/recreate from the **host** (`./start.sh` or `docker compose up -d --build agents`). Check: `docker compose exec agents ps -p 1 -o args=` (should show `docker-init` / tini) and `docker stats agents` (≈3 Gi limit).

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
