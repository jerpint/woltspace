# Woltspace container — environment provisioning + isolation, nothing more.
#
# The image does NOT bake the source tree any more. It installs the same two
# published artifacts a native install uses:
#
#   PyPI  woltspace[connectors]==<WOLTSPACE_PYPI_VERSION>   → the control plane,
#         whose wheel carries the whole runtime in its `_bundle` (server,
#         container/lib, container/bin, skills, templates)
#   npm   @woltspace/tui@<WOLTSPACE_TUI_VERSION>            → woltspace-tui and
#         the woltspace-tui-service pty bridge the control plane supervises
#
# So what is left here is what a container is actually for: the OS, the harness
# CLIs, tmux, the tunnel binary, a non-root user, and one mount point. The
# supervisor that runs inside is byte-for-byte the one a native user runs.
#
# Dev builds: --build-arg USE_LOCAL=true installs a wheel BUILT FROM THE COPIED
# TREE (and the local tui/), never runs from the raw checkout — one code path.

# --- Stage: claude ---
# Isolated stage so the download is cached independently of app source.
# Normal build: uses cache. Update Claude: docker build --no-cache-filter=claude ...
FROM node:22-slim AS claude
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
USER node
RUN curl -fsSL https://claude.ai/install.sh | bash

# --- Stage: codex ---
# Same pattern. Update Codex: docker build --no-cache-filter=codex ...
FROM node:22-slim AS codex
RUN npm install -g @openai/codex

# --- Stage: opencode ---
# Same pattern. Update opencode: docker build --no-cache-filter=opencode ...
# UNVERIFIED: npm package name `opencode-ai` (the bare `opencode` name is taken
# by an unrelated package — confirmed via opencode.ai/docs install guide). Unlike
# codex (pure JS), opencode-ai pulls a prebuilt native binary via a per-platform
# optional dependency at install time, so we install into an isolated --prefix
# and copy the whole tree (binary + relative bin symlink) rather than cherry-
# picking one package dir. Alternative if npm proves unreliable in-image:
# `curl -fsSL https://opencode.ai/install | bash` (self-contained binary).
FROM node:22-slim AS opencode
RUN npm install -g --prefix /opt/opencode opencode-ai

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

# Install bun (for worktui — the tui itself now comes from npm)
RUN curl -fsSL https://bun.sh/install | bash && mv /root/.bun/bin/bun /usr/local/bin/bun

# Create workspace + ssh dirs
RUN mkdir -p /workspace/wolts /home/node/.ssh \
    && chmod 700 /home/node/.ssh

# Copy everything the Claude installer created (binary, launcher, shell integration)
COPY --from=claude /home/node/ /home/node/
ENV PATH="/home/node/.local/bin:${PATH}"

# Copy the Codex CLI (npm global package + bin symlink)
COPY --from=codex /usr/local/lib/node_modules/@openai/codex /usr/local/lib/node_modules/@openai/codex
RUN ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
# Default CODEX_HOME is the shared seed in the mount — durable across
# rebuilds, and where `codex login` lands. wcodex overrides this per-wolt
# at session launch. (Dir is created at boot; mount shadows build-time mkdir.)
ENV CODEX_HOME=/workspace/wolts/.codex

# Copy the opencode install (native binary + relative bin symlink) as one tree.
COPY --from=opencode /opt/opencode /opt/opencode
ENV PATH="/opt/opencode/bin:${PATH}"
# No global XDG/data ENV: wopencode sets HOME + XDG_DATA_HOME/XDG_CONFIG_HOME
# per-wolt at launch. The shared auth seed lives at
# /workspace/wolts/.local/share/opencode/auth.json (in the mount, durable across
# rebuilds; created by `opencode auth login` or copied in). wopencode self-heals
# a per-wolt copy from it, mirroring the codex/claude seed pattern.

# Install worktui (worktree + Claude session manager)
RUN git clone https://github.com/jerpint/worktui.git /home/node/worktui \
    && cd /home/node/worktui && bun install
ENV WORKTUI_DIR="/workspace/wolts/.worktui"

# --- The woltspace runtime, from the registries ---
ARG WOLTSPACE_PYPI_VERSION=0.5.0rc5
ARG WOLTSPACE_TUI_VERSION=0.5.0-rc.1
# --build-arg USE_LOCAL=true installs a wheel built from the build context
# instead of PyPI (and the context's tui/ instead of npm). Same install shape,
# same code path — only the source of the artifact differs.
ARG USE_LOCAL=false
# Changing CACHE_BUST invalidates the install layer (and everything after it)
# but keeps the heavy layers above (node, claude, apt, uv, cloudflared) cached.
ARG CACHE_BUST=

# uv's tool store lives somewhere predictable and node-owned, and the console
# scripts land straight on PATH.
# UV_NO_CACHE: an installed-once image has nothing to reinstall, and the wheel
# cache would otherwise be a second copy of every dependency in the layer.
ENV UV_TOOL_DIR=/opt/woltspace/tools \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

# Boot is a long-lived python process writing to docker's pipe, which is not a
# tty: block buffering would hold "scaffolded new wolt", "setup complete" and
# the tunnel URL until the container exits. Set for every python in the image,
# the entrypoint and its children alike.
ENV PYTHONUNBUFFERED=1

# Local source (only read when USE_LOCAL=true). Last COPY in the file so the
# layers above stay cached.
COPY . /tmp/local-src/

# One RUN, on purpose. The wheel's bundle is the install root — `server/`,
# `container/lib`, `container/bin`, skills and templates all live inside it —
# and its path carries the interpreter's version, so it is resolved here through
# the CLI's own answer and pinned behind the stable /workspace/woltspace name
# that every skill, wolt CLAUDE.md and bin script in the colony already knows.
# Splitting that off into its own layer (or adding a `chown -R`) copies the
# whole venv a second time: a quarter of a gigabyte for a symlink.
#
# The local path packs the tui tarball first: `npm install -g <dir>` only links
# the directory, and this build deletes it.
RUN set -eux; \
    if [ "$USE_LOCAL" = "true" ]; then \
      echo "installing woltspace from the local tree"; \
      uv tool install --python "$(command -v python3)" "/tmp/local-src[connectors]"; \
      (cd /tmp/local-src/tui && npm pack --pack-destination /tmp); \
      npm install -g /tmp/woltspace-tui-*.tgz; \
      rm -f /tmp/woltspace-tui-*.tgz; \
    else \
      echo "installing woltspace==${WOLTSPACE_PYPI_VERSION} + @woltspace/tui@${WOLTSPACE_TUI_VERSION}"; \
      uv tool install --python "$(command -v python3)" "woltspace[connectors]==${WOLTSPACE_PYPI_VERSION}"; \
      npm install -g "@woltspace/tui@${WOLTSPACE_TUI_VERSION}"; \
    fi; \
    npm cache clean --force; \
    rm -rf /tmp/local-src /root/.npm; \
    BUNDLE="$(/usr/local/bin/woltspace paths --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["install_root"])')"; \
    test -d "$BUNDLE/container/lib"; \
    ln -s "$BUNDLE" /workspace/woltspace; \
    ln -s "${UV_TOOL_DIR}/woltspace/bin/python" /usr/local/bin/woltspace-python; \
    chmod +x "$BUNDLE"/container/bin/*; \
    /usr/local/bin/woltspace --version | awk '{print $2}' > "$BUNDLE/.version"; \
    ln -sf /workspace/woltspace/container/config/tmux.conf /etc/tmux.conf

# container/bin ahead of the installed console scripts, exactly as before: inside
# the container `woltspace` is the thin HTTP client (session list/send/spawn) and
# the packaged CLI is reached by absolute path from the entrypoint. ENV covers
# processes that inherit it; a login shell (`bash -lc`, which is how codex runs
# model commands) rebuilds PATH from /etc/profile, hence profile.d as well.
ENV PATH="/workspace/woltspace/container/bin:${PATH}"
RUN echo 'export PATH="/workspace/woltspace/container/bin:$PATH"' > /etc/profile.d/woltspace-path.sh

WORKDIR /workspace

EXPOSE 7777

# Boot is a subcommand of the installed CLI — the wheel ships no bash. The
# command runs twice: once as root (uid/gid fixup) and once as node, which it
# re-execs itself as through gosu.
ENTRYPOINT ["/usr/local/bin/woltspace", "container-entrypoint"]
