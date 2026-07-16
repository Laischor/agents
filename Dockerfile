FROM docker:27-cli AS docker-cli

FROM node:24-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PATH="/root/.local/bin:${PATH}"

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

# Claude Code
RUN npm install -g @anthropic-ai/claude-code \
  && claude --version

# Ensure PATH survives login shells (bash -l)
RUN printf '%s\n' 'export PATH="/root/.local/bin:$PATH"' > /etc/profile.d/agents-path.sh

CMD ["bash"]
