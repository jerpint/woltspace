# Woltspace container
# Runs the wolt server + cloudflared tunnel
# The wolt's identity/content is mounted in at runtime

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

# Copy server source (baked into image — not from wolt mount)
COPY server.js ./

# Copy skills (platform defaults — wolt can override via .claude/skills/)
COPY container/skills /workspace/woltspace/container/skills

# Copy bot code (platform default — wolt can override via wolt/bot/)
COPY container/bot /workspace/woltspace/container/bot

# Copy cron scripts
COPY container/cron /workspace/woltspace/container/cron

# Copy platform UI assets (split view, etc. — not wolt content)
COPY public /workspace/woltspace/public

# Copy hooks
COPY container/hooks /workspace/woltspace/container/hooks

# Copy bin scripts
COPY container/bin /workspace/woltspace/container/bin
RUN chmod +x /workspace/woltspace/container/bin/*
ENV PATH="/workspace/woltspace/container/bin:${PATH}"

# Copy entrypoint
COPY container/entrypoint.sh /workspace/woltspace/container/entrypoint.sh
RUN chmod +x /workspace/woltspace/container/entrypoint.sh

# Create workspace directories (will be overlaid by mounts)
RUN mkdir -p /workspace/wolts

# Create .ssh dir before mounts so it's owned by node
RUN mkdir -p /home/node/.ssh && chmod 700 /home/node/.ssh

# Set ownership to node user (non-root, required for --dangerously-skip-permissions)
RUN chown -R node:node /workspace /home/node

USER node

# Install claude-code (native binary → ~/.local/bin/claude)
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/node/.local/bin:${PATH}"

# Install Python deps for bot
RUN cd /workspace/woltspace/container && uv sync --project bot/pyproject.toml

WORKDIR /workspace

EXPOSE 3000

ENTRYPOINT ["/workspace/woltspace/container/entrypoint.sh"]
