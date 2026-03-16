# Woltspace container
# Runs the wolt server + cloudflared tunnel
# The wolt's identity/content is mounted in at runtime

# --- Stage: claude ---
# Isolated stage so the download is cached independently of app source.
# Normal build: uses cache. Update Claude: docker build --no-cache-filter=claude ...
FROM node:22-slim AS claude
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
USER node
RUN curl -fsSL https://claude.ai/install.sh | bash

# --- Stage: main ---
FROM node:22-slim

# System deps
RUN apt-get update && apt-get install -y \
    curl \
    git \
    openssh-client \
    tmux \
    python3 \
    make \
    g++ \
    procps \
    jq \
    less \
    unzip \
    vim-tiny \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast Python package manager)
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Install cloudflared
RUN curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$(dpkg --print-architecture) -o /usr/local/bin/cloudflared \
    && chmod +x /usr/local/bin/cloudflared

# Install bun (for worktui)
RUN curl -fsSL https://bun.sh/install | bash && mv /root/.bun/bin/bun /usr/local/bin/bun

# Create workspace + ssh dirs
RUN mkdir -p /workspace/wolts /home/node/.ssh \
    && chmod 700 /home/node/.ssh \
    && chown -R node:node /workspace /home/node

USER node

# Copy everything the Claude installer created (binary, launcher, shell integration)
COPY --from=claude /home/node/ /home/node/
ENV PATH="/home/node/.local/bin:${PATH}"

# Install worktui (worktree + Claude session manager)
RUN git clone https://github.com/jerpint/worktui.git /home/node/worktui \
    && cd /home/node/worktui && bun install
ENV WORKTUI_DIR="/workspace/wolts/.worktui"

# --- App source: git clone ---
# Branch is configurable via build arg (default: main)
# In --dev mode, this gets shadowed by a host mount anyway
ARG WOLTSPACE_BRANCH=main
RUN git clone --branch ${WOLTSPACE_BRANCH} --single-branch --depth 1 \
    https://github.com/jerpint/woltspace.git /workspace/woltspace

# Stamp the version into the image
RUN cd /workspace/woltspace && git rev-parse --short HEAD > /etc/woltspace-version

WORKDIR /workspace/woltspace

# Install node deps
RUN npm install && npm install ws node-pty

# Make bin scripts executable + add to PATH
RUN chmod +x container/bin/*
ENV PATH="/workspace/woltspace/container/bin:${PATH}"
RUN chmod +x container/entrypoint.sh

# Install Python deps
RUN cd container && uv sync --project bot
RUN uv sync --project server

WORKDIR /workspace

EXPOSE 7777

ENTRYPOINT ["/workspace/woltspace/container/entrypoint.sh"]
