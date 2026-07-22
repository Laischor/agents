FROM docker:27-cli AS docker-cli

FROM node:24-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.local/bin:${PATH}" \
    CODEGRAPH_TELEMETRY=0

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    ripgrep \
    python3 \
    python3-pip \
    sudo \
    unzip \
    wget \
  && rm -rf /var/lib/apt/lists/*

# GitHub CLI (official apt repo — Debian packages are outdated/broken)
RUN mkdir -p -m 755 /etc/apt/keyrings \
  && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
  && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends gh \
  && rm -rf /var/lib/apt/lists/* \
  && gh --version

# Root in container, repos owned by host UID → dubious ownership without this.
# Credential helper here (not bind-mounted ~/.gitconfig): macOS gitconfig often
# clears helpers and points at /opt/homebrew/bin/gh, which does not exist here.
RUN git config --system safe.directory '*' \
  && git config --system credential.helper '!gh auth git-credential'

# Docker CLI + Compose plugin (talks to host daemon via mounted socket)
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

# Cursor CLI (provides `agent` / `cursor-agent`)
# Install under /usr/local — Hermes docker backend overlays /root with a
# sandbox volume/tmpfs, which would hide ~/.local and break symlink targets.
RUN curl https://cursor.com/install -fsS | bash \
  && mkdir -p /usr/local/lib \
  && rm -rf /usr/local/lib/cursor-agent \
  && cp -a /root/.local/share/cursor-agent /usr/local/lib/cursor-agent \
  && CURSOR_BIN="$(find /usr/local/lib/cursor-agent/versions -maxdepth 2 -type f -name cursor-agent | sort | tail -1)" \
  && test -n "$CURSOR_BIN" \
  && ln -sf "$CURSOR_BIN" /usr/local/bin/agent \
  && ln -sf "$CURSOR_BIN" /usr/local/bin/cursor-agent \
  && agent --version

# Pi coding agent
RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent \
  && pi --version

# CodeGraph CLI (MCP for Claude/Cursor; used by pi-codegraph)
RUN npm install -g @colbymchenry/codegraph@1.4.1 \
  && codegraph --version

# Claude Code
RUN npm install -g @anthropic-ai/claude-code \
  && claude --version

# Ensure PATH survives login shells (bash -l)
RUN printf '%s\n' 'export PATH="/root/.local/bin:$PATH"' > /etc/profile.d/agents-path.sh

# Fake clipboard tools: host clipboard-bridge.sh writes PNGs here; Claude paste reads them
ENV AGENTS_CLIPBOARD_DIR=/var/agents-clipboard
COPY clipboard/xclip clipboard/wl-paste /usr/local/bin/
COPY bin/claudex /usr/local/bin/claudex
RUN chmod +x /usr/local/bin/xclip /usr/local/bin/wl-paste /usr/local/bin/claudex \
  && mkdir -p /var/agents-clipboard

# Runtime: register Pi packages + Claude/Cursor MCP into mounted config dirs
COPY container-entrypoint.sh /usr/local/bin/container-entrypoint
RUN chmod +x /usr/local/bin/container-entrypoint

ENTRYPOINT ["container-entrypoint"]
CMD ["bash"]
