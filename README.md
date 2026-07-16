# agents

Docker-Umgebung für Coding-Agents: **Cursor CLI**, **Pi** und **Claude Code**.  
Projekte bleiben auf dem Host; Agents laufen isoliert im Container und steuern Docker über den Host-Socket.

## Voraussetzungen

- macOS
- Homebrew ([brew.sh](https://brew.sh)) — für automatische Docker/Colima-Installation
- SSH-Key / Login für die jeweiligen Agent-Accounts (oder API-Keys)

## Setup

```bash
git clone https://github.com/Laischor/agents.git
cd agents
./start.sh
```

`start.sh` prüft macOS, stellt Docker bereit (installiert bei Bedarf per Homebrew `colima`, `docker`, `docker-compose` und startet Colima) und bringt den Agents-Container hoch.

Danach `.env` anpassen falls noch nicht geschehen:

```bash
# Absoluter Pfad zu deinen Projekten (gleicher Pfad im Container)
HOST_PROJECTS=/Users/YOU/Documents/projects

CURSOR_API_KEY=
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
GOOGLE_API_KEY=
```

### Shell-Aliases

In `~/.zshrc` (Pfad anpassen, falls das Repo woanders liegt):

```bash
export AGENTS_DIR="${AGENTS_DIR:-$HOME/Documents/projects/agents}"
agents() { "$AGENTS_DIR/run.sh" "$@"; }
dagent()  { agents agent "$@"; }
dpi()     { agents pi "$@"; }
dclaude() { agents claude "$@"; }
agents-shell() { agents bash "$@"; }
```

```bash
source ~/.zshrc
```

## Nutzung

Aus einem Ordner unter `HOST_PROJECTS`:

```bash
cd ~/Documents/projects/mein-projekt
dagent      # Cursor CLI
dpi         # Pi
dclaude     # Claude Code
agents-shell
```

Der Launcher startet den Container bei Bedarf und setzt das Working Directory 1:1 zum Host-Pfad.

## Persistenz

Configs/Auth liegen auf dem Host unter `./data/` und überleben Rebuilds:

| Host              | Container            |
|-------------------|----------------------|
| `data/cursor/`    | `/root/.cursor`      |
| `data/pi/`        | `/root/.pi`          |
| `data/claude/`    | `/root/.claude`      |
| `data/claude.json`| `/root/.claude.json` |

`data/` und `.env` sind gitignored.

## Docker vom Agent aus

Der Container mountet `/var/run/docker.sock`. Agents können damit Host-Container bauen/starten (`docker compose up`, …), solange sie unter `HOST_PROJECTS` arbeiten — Bind-Mounts in Projekt-Compose-Dateien brauchen denselben Host-Pfad.

## Repo woanders klonen

1. Klonen und `.env` mit lokalem `HOST_PROJECTS` anlegen  
2. `AGENTS_DIR` auf den neuen Repo-Pfad setzen  
3. `docker-compose up -d --build`
