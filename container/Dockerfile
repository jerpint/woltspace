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


# App directory — matches repo structure so --dev can mount directly
WORKDIR /workspace/woltspace

# Copy package files first for better layer caching
COPY package.json ./

# Install dependencies + TUI native modules (ws + node-pty)
RUN npm install && npm install ws node-pty

# Create workspace directories (will be overlaid by mounts)
RUN mkdir -p /workspace/wolts

# Create .ssh dir before mounts so it's owned by node
RUN mkdir -p /home/node/.ssh && chmod 700 /home/node/.ssh

# Set ownership to node user (non-root, required for --dangerously-skip-permissions)
RUN chown -R node:node /workspace /home/node

USER node

# Copy everything the Claude installer created (binary, launcher, shell integration)
COPY --from=claude /home/node/ /home/node/
ENV PATH="/home/node/.local/bin:${PATH}"

# --- App source (changes often — everything below here rebuilds on edits) ---

# Copy server source (baked into image — not from wolt mount)
COPY --chown=node:node server.js ./
COPY --chown=node:node server/ ./server/

# Copy skills (platform defaults — wolt can override via .claude/skills/)
COPY --chown=node:node container/skills /workspace/woltspace/container/skills

# Copy bot code (platform default — wolt can override via wolt/bot/)
COPY --chown=node:node container/bot /workspace/woltspace/container/bot

# Copy cron scripts
COPY --chown=node:node container/cron /workspace/woltspace/container/cron

# Copy platform UI assets (split view, etc. — not wolt content)
COPY --chown=node:node public /workspace/woltspace/public

# Copy hooks
COPY --chown=node:node container/hooks /workspace/woltspace/container/hooks

# Copy bin scripts
COPY --chown=node:node container/bin /workspace/woltspace/container/bin
RUN chmod +x /workspace/woltspace/container/bin/*
ENV PATH="/workspace/woltspace/container/bin:${PATH}"

# Copy entrypoint
COPY --chown=node:node container/entrypoint.sh /workspace/woltspace/container/entrypoint.sh
RUN chmod +x /workspace/woltspace/container/entrypoint.sh

# Install Python deps for bot
RUN cd /workspace/woltspace/container && uv sync --project bot/pyproject.toml

# Install Python server dependencies (FastAPI, uvicorn, etc.)
RUN cd /workspace/woltspace && uv sync --project server

WORKDIR /workspace

EXPOSE 3000

ENTRYPOINT ["/workspace/woltspace/container/entrypoint.sh"]
