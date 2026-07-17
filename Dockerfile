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

# Docker CLI + Compose plugin (talks to host daemon via mounted socket)
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins

# Cursor CLI (provides `agent` / `cursor-agent`)
RUN curl https://cursor.com/install -fsS | bash \
  && ln -sf /root/.local/bin/agent /usr/local/bin/agent \
  && ln -sf /root/.local/bin/cursor-agent /usr/local/bin/cursor-agent \
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
RUN chmod +x /usr/local/bin/xclip /usr/local/bin/wl-paste \
  && mkdir -p /var/agents-clipboard

# Runtime: register Pi packages + Claude/Cursor MCP into mounted config dirs
COPY container-entrypoint.sh /usr/local/bin/container-entrypoint
RUN chmod +x /usr/local/bin/container-entrypoint

ENTRYPOINT ["container-entrypoint"]
CMD ["bash"]
