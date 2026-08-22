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
    fd-find \
    git \
    jq \
    ripgrep \
    python3 \
    python3-pip \
    sqlite3 \
    sudo \
    unzip \
    wget \
  && ln -sf "$(command -v fdfind)" /usr/local/bin/fd \
  && rm -rf /var/lib/apt/lists/*

# yq (mikefarah) + ast-grep — GitHub release binaries
ARG YQ_VERSION=v4.53.3
ARG AST_GREP_VERSION=0.44.1
RUN arch="$(dpkg --print-architecture)" \
  && case "$arch" in \
       amd64) yq_arch=amd64; sg_arch=x86_64 ;; \
       arm64) yq_arch=arm64; sg_arch=aarch64 ;; \
       *) echo "unsupported arch: $arch"; exit 1 ;; \
     esac \
  && wget -qO /usr/local/bin/yq \
       "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${yq_arch}" \
  && chmod +x /usr/local/bin/yq \
  && wget -qO /tmp/ast-grep.zip \
       "https://github.com/ast-grep/ast-grep/releases/download/${AST_GREP_VERSION}/app-${sg_arch}-unknown-linux-gnu.zip" \
  && unzip -qo /tmp/ast-grep.zip -d /usr/local/bin \
  && chmod +x /usr/local/bin/ast-grep /usr/local/bin/sg \
  && rm -f /tmp/ast-grep.zip \
  && yq --version \
  && ast-grep --version

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

# OpenCode (native binary under /usr/local — Hermes docker backend overlays /root)
# Same as Claude/Cursor/Pi: track latest. GitHub /releases/latest/download avoids the API.
RUN arch="$(dpkg --print-architecture)" \
  && case "$arch" in \
       amd64) oc_arch=x64 ;; \
       arm64) oc_arch=arm64 ;; \
       *) echo "unsupported arch: $arch"; exit 1 ;; \
     esac \
  && oc_target="linux-${oc_arch}" \
  && if [ "$oc_arch" = "x64" ] && ! grep -qwi avx2 /proc/cpuinfo 2>/dev/null; then \
       oc_target="linux-x64-baseline"; \
     fi \
  && wget -qO /tmp/opencode.tar.gz \
       "https://github.com/anomalyco/opencode/releases/latest/download/opencode-${oc_target}.tar.gz" \
  && tar -xzf /tmp/opencode.tar.gz -C /tmp \
  && install -m 755 /tmp/opencode /usr/local/bin/opencode \
  && rm -f /tmp/opencode.tar.gz /tmp/opencode \
  && opencode --version

# T3 Code control surface (web UI via `t3 serve` on :3773).
# Install under /usr/local (npm -g) — Hermes overlays /root.
# node:24-bookworm already has python/make/g++ for node-pty.
RUN npm install -g t3@latest \
  && node -e "require('/usr/local/lib/node_modules/t3/node_modules/node-pty')" \
  && mv /usr/local/bin/t3 /usr/local/bin/t3-real \
  && t3-real --version

RUN apt-get update && apt-get install -y --no-install-recommends python3-pil qrencode \
  && rm -rf /var/lib/apt/lists/*

# Ensure PATH survives login shells (bash -l)
RUN printf '%s\n' 'export PATH="/root/.local/bin:$PATH"' > /etc/profile.d/agents-path.sh

# Fake clipboard tools: host clipboard-bridge.sh writes PNGs here; Claude paste reads them
ENV AGENTS_CLIPBOARD_DIR=/var/agents-clipboard \
    AGENTS_GPU_DIR=/var/agents-gpu \
    AGENTS_CMUX_DIR=/var/agents-cmux
COPY clipboard/xclip clipboard/wl-paste /usr/local/bin/
COPY bin/agents-janitor.sh /usr/local/bin/agents-janitor
COPY bin/t3-serve.sh /usr/local/bin/t3-serve
COPY bin/t3-qr.sh /usr/local/bin/t3-qr
COPY bin/t3-pair.sh /usr/local/bin/t3-pair
COPY bin/t3-wrapper.sh /usr/local/bin/t3
# Host GPU bridge shims (Blender/Godot run on macOS via gpu-bridge.sh)
COPY bin/gpu-job bin/blender bin/godot /usr/local/bin/
# Host cmux bridge stub (notifications/sounds via cmux-bridge.sh on macOS)
COPY bin/cmux bin/cmux-agent-hook /usr/local/bin/
RUN chmod +x /usr/local/bin/xclip /usr/local/bin/wl-paste \
      /usr/local/bin/agents-janitor \
      /usr/local/bin/t3-serve \
      /usr/local/bin/t3-qr \
      /usr/local/bin/t3-pair \
      /usr/local/bin/t3 \
      /usr/local/bin/gpu-job /usr/local/bin/blender /usr/local/bin/godot \
      /usr/local/bin/cmux /usr/local/bin/cmux-agent-hook \
  && mkdir -p /var/agents-clipboard /var/agents-gpu /var/agents-cmux /var/log /var/run

# Runtime: register Pi packages + Claude/Cursor MCP into mounted config dirs
COPY container-entrypoint.sh /usr/local/bin/container-entrypoint
RUN chmod +x /usr/local/bin/container-entrypoint

ENTRYPOINT ["container-entrypoint"]
CMD ["bash"]
