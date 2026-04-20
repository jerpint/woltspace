#!/bin/bash
# Root wrapper: fix UID/GID to match host, then drop to node.
# The woltspace CLI passes HOST_UID/HOST_GID from the host user.
# Defaults to 1000 (standard first user on Linux) if not set.
set -e

HOST_UID="${HOST_UID:-1000}"
HOST_GID="${HOST_GID:-1000}"

groupmod -o -g "$HOST_GID" node
usermod -o -u "$HOST_UID" -g "$HOST_GID" node
chown -R node:node /home/node /workspace/woltspace

exec gosu node /workspace/woltspace/container/start.sh
