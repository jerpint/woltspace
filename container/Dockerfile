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
    gosu \
    openssh-client \
    sudo \
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

# Allow node user to install packages at runtime
RUN echo "node ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/node \
    && chmod 0440 /etc/sudoers.d/node

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
COPY --chown=node:node --from=claude /home/node/ /home/node/
ENV PATH="/home/node/.local/bin:${PATH}"

# Install worktui (worktree + Claude session manager)
RUN git clone https://github.com/jerpint/worktui.git /home/node/worktui \
    && cd /home/node/worktui && bun install
ENV WORKTUI_DIR="/workspace/wolts/.worktui"

# --- App source ---
# Two paths, same result:
#   --branch X  → git clone from github (default: main)
#   --local     → COPY from build context (local repo)
ARG USE_LOCAL=false
ARG WOLTSPACE_BRANCH=main
ARG WOLTSPACE_VERSION=
# Changing CACHE_BUST invalidates the clone layer (and everything after it)
# but keeps heavy layers above (node, claude, apt, uv, cloudflared) cached.
ARG CACHE_BUST=

# Clone from github (default path)
# --branch X    → use branch tip (no tag checkout)
# --version X   → checkout that specific tag
# --nightly     → use main branch tip (skip tags)
# --latest      → checkout latest tag
# default       → rebuild at current version, or latest tag for fresh init
RUN if [ "$USE_LOCAL" = "false" ]; then \
      git clone --branch ${WOLTSPACE_BRANCH} --single-branch \
        https://github.com/jerpint/woltspace.git /workspace/woltspace && \
      cd /workspace/woltspace && \
      git fetch --tags && \
      if [ "$WOLTSPACE_VERSION" = "HEAD" ]; then \
        echo "using branch tip (nightly): $WOLTSPACE_BRANCH"; \
      elif [ -n "$WOLTSPACE_VERSION" ]; then \
        echo "checking out version: $WOLTSPACE_VERSION" && \
        git checkout "$WOLTSPACE_VERSION"; \
      elif [ "$WOLTSPACE_BRANCH" = "main" ]; then \
        LATEST_TAG=$(git tag --sort=-v:refname | head -1) && \
        if [ -n "$LATEST_TAG" ]; then \
          echo "checking out release: $LATEST_TAG" && \
          git checkout "$LATEST_TAG"; \
        else \
          echo "no tags found — using branch tip"; \
        fi; \
      else \
        echo "using branch tip: $WOLTSPACE_BRANCH"; \
      fi; \
    fi

# Copy local source (only meaningful when --local)
COPY --chown=node:node . /tmp/local-src/
RUN if [ "$USE_LOCAL" = "true" ]; then \
      cp -r /tmp/local-src/. /workspace/woltspace/; \
    fi; rm -rf /tmp/local-src

# Stamp the version into the image (prefer tag, fall back to commit hash)
# --exact-match for release builds (HEAD on a tag), then nearest ancestor tag for
# local/branch builds, then commit hash, then "local" if no git at all.
RUN cd /workspace/woltspace && \
    (git describe --tags --exact-match 2>/dev/null || git describe --tags --abbrev=0 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo "local") > /workspace/woltspace/.version

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

# Entrypoint runs as root to fix UID, then drops to node via gosu
USER root

EXPOSE 7777

ENTRYPOINT ["/workspace/woltspace/container/entrypoint.sh"]
