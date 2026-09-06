#!/bin/bash
# Root wrapper: fix UID/GID to match host, then drop to node.
# The woltspace CLI passes HOST_UID/HOST_GID from the host user.
# Defaults to 1000 (standard first user on Linux) if not set.
set -e

HOST_UID="${HOST_UID:-1000}"
HOST_GID="${HOST_GID:-1000}"

groupmod -o -g "$HOST_GID" node
usermod -o -u "$HOST_UID" -g "$HOST_GID" node
chown node:node /workspace
chown -R node:node /home/node
# The platform is an installed package now, so this is its wheel bundle rather
# than a checkout — a few MB, not the venv. It still has to be re-owned here:
# `usermod` above just moved node to the host's uid, and boot writes a derived
# skill into it (container/skills/woltspace-worktui).
chown -R node:node "$(readlink -f /workspace/woltspace)"

exec gosu node /workspace/woltspace/container/start.sh
