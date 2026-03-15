#!/bin/bash
set -e

command -v docker >/dev/null || { echo "error: docker required — https://docs.docker.com/get-docker/"; exit 1; }
command -v git >/dev/null || { echo "error: git required"; exit 1; }

if [ -d "woltspace/.git" ]; then
  echo "  woltspace already cloned — pulling latest..."
  git -C woltspace pull --ff-only
elif [ -d "woltspace" ]; then
  echo "  woltspace directory exists but isn't a git repo — removing and re-cloning..."
  rm -rf woltspace
  git clone https://github.com/jerpint/woltspace
else
  git clone https://github.com/jerpint/woltspace
fi

export PATH="$PWD/woltspace:$PATH"
export WOLTS_DIR="${WOLTS_DIR:-$PWD}"

woltspace init
