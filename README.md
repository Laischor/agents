# agents

Docker-Umgebung für Coding-Agents: **Cursor CLI**, **Pi** und **Claude Code**.  
Zusätzlich: **Claudex** (Claude Code + GPT-5.6 Sol via CLIProxyAPI), **Caveman** und **CodeGraph**.  
Projekte bleiben auf dem Host; Agents laufen isoliert im Container und steuern Docker über den Host-Socket.

## Voraussetzungen

- macOS
- Homebrew ([brew.sh](https://brew.sh)) — für automatische Docker/Colima-Installation
- SSH-Key / Login für die jeweiligen Agent-Accounts (oder API-Keys)
- Für Claudex: ChatGPT Plus/Pro mit Codex-Zugang

## Setup

```bash
git clone https://github.com/Laischor/agents.git
cd agents
./start.sh
```

`start.sh` prüft macOS, stellt Docker bereit (installiert bei Bedarf per Homebrew `colima`, `docker`, `docker-compose` und startet Colima) und bringt `agents` + `cli-proxy-api` hoch. Fehlt `CLIPROXY_API_KEY`, wird er generiert und in `.env` sowie `data/cliproxy/config.yaml` geschrieben.

Danach `.env` anpassen falls noch nicht geschehen:

```bash
# Absoluter Pfad zu deinen Projekten (gleicher Pfad im Container)
HOST_PROJECTS=/Users/YOU/Documents/projects

CURSOR_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
CLIPROXY_API_KEY=   # optional — start.sh legt einen an
```

### Shell-Aliases

In `~/.zshrc` (Pfad anpassen, falls das Repo woanders liegt):

```bash
export AGENTS_DIR="${AGENTS_DIR:-$HOME/Documents/projects/agents}"
agents() { "$AGENTS_DIR/run.sh" "$@"; }
dagent()     { agents agent "$@"; }
dpi()        { agents pi "$@"; }
dclaude()    { agents claude "$@"; }
dclaudex()   { agents claudex "$@"; }
dcodegraph() { agents codegraph "$@"; }
agents-shell() { agents bash "$@"; }
```

```bash
source ~/.zshrc
```

## Nutzung

Aus einem Ordner unter `HOST_PROJECTS`:

```bash
cd ~/Documents/projects/mein-projekt
dagent       # Cursor CLI
dpi          # Pi
dclaude      # Claude Code (Anthropic)
dclaudex     # Claude Code Harness → GPT-5.6 Sol (Claudex)
dcodegraph   # CodeGraph CLI
agents-shell
```

Der Launcher startet den Container bei Bedarf und setzt das Working Directory 1:1 zum Host-Pfad.

## Claudex (GPT-5.6 Sol)

Claude Code als Harness, Inference über **CLIProxyAPI** → ChatGPT Codex (OAuth). `dclaude` bleibt unverändert auf Anthropic; die Proxy-Env gilt nur für `dclaudex`.

Einmalig ChatGPT/Codex einloggen (öffnet eine URL; Callback auf `localhost:54545`):

```bash
agents cliproxy-login
```

Danach:

```bash
cd ~/Documents/projects/mein-projekt
dclaudex
# In der Session: /status  → Model gpt-5.6-sol, Base-URL Proxy
```

Hinweis: Subscription-OAuth über einen Dritt-Proxy kann gegen Anbieter-Nutzungsbedingungen verstoßen und birgt Account-/Credential-Risiko. Lokal betreiben, Credentials nicht teilen.

## Persistenz

Configs/Auth liegen auf dem Host unter `./data/` und überleben Rebuilds:

| Host              | Container            |
|-------------------|----------------------|
| `~/.gitconfig`         | `/root/.gitconfig` (read-only) |
| `~/.ssh/`              | `/root/.ssh` (read-only, GitHub SSH) |
| `data/cursor/`         | `/root/.cursor`         |
| `data/cursor-config/`  | `/root/.config/cursor` (Login-Tokens) |
| `data/pi/`             | `/root/.pi`             |
| `data/claude/`         | `/root/.claude` (`CLAUDE_CONFIG_DIR`) |
| `data/cliproxy/`       | CLIProxyAPI Config + OAuth (`auths/`) |

`data/` und `.env` sind gitignored.

## Caveman & CodeGraph

Beim Container-Start registriert der Entrypoint (idempotent):

| Agent        | Caveman                                      | CodeGraph                                      |
|--------------|----------------------------------------------|------------------------------------------------|
| Pi           | `pi-caveman` Package                         | `pi-codegraph` + CLI `codegraph`               |
| Claude Code  | Plugin `caveman@caveman` (Default: **ultra**) | MCP `codegraph serve --mcp`                    |
| Cursor CLI   | —                                            | MCP in `~/.cursor/mcp.json`                    |

Default-Mode für Claude: `CAVEMAN_DEFAULT_MODE=ultra` in `docker-compose.yml`. Pro Session überschreibbar mit `/caveman lite|full|ultra`.

Pro Projekt einmal indexieren:

```bash
cd ~/Documents/projects/mein-projekt
dcodegraph init
```

## Docker vom Agent aus

Der Container mountet `${DOCKER_SOCK:-/var/run/docker.sock}`. Agents können damit Host-Container bauen/starten (`docker compose up`, …), solange sie unter `HOST_PROJECTS` arbeiten — Bind-Mounts in Projekt-Compose-Dateien brauchen denselben Host-Pfad.

## Repo woanders klonen

1. Klonen und `.env` mit lokalem `HOST_PROJECTS` anlegen  
2. `AGENTS_DIR` auf den neuen Repo-Pfad setzen  
3. `docker-compose up -d --build`
