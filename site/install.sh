#!/bin/bash
set -e

command -v docker >/dev/null || { echo "error: docker required — https://docs.docker.com/get-docker/"; exit 1; }
command -v git >/dev/null || { echo "error: git required"; exit 1; }

git clone https://github.com/jerpint/woltspace
export PATH="$PWD/woltspace:$PATH"

woltspace init
