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


# App directory
WORKDIR /app

# Copy package files first for better layer caching
COPY package.json ./

# Install dependencies + TUI native modules (ws + node-pty)
RUN npm install && npm install ws node-pty

# Copy server source (baked into image — not from wolt mount)
COPY server.js ./

# Copy skills (platform defaults — wolt can override via .claude/skills/)
COPY container/skills /app/skills

# Copy bot code (platform default — wolt can override via wolt/bot/)
COPY container/bot /app/bot

# Copy cron scripts
COPY container/cron /app/cron

# Copy platform UI assets (split view, etc. — not wolt content)
COPY public /app/public

# Copy entrypoint
COPY container/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Create workspace directories (will be overlaid by mounts)
RUN mkdir -p /workspace/wolts

# Create .ssh dir before mounts so it's owned by node
RUN mkdir -p /home/node/.ssh && chmod 700 /home/node/.ssh

# Set ownership to node user (non-root, required for --dangerously-skip-permissions)
RUN chown -R node:node /workspace /home/node /app

USER node

# Install claude-code (native binary → ~/.local/bin/claude)
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/node/.local/bin:${PATH}"

WORKDIR /workspace

EXPOSE 3000

ENTRYPOINT ["/app/entrypoint.sh"]
