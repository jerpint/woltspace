#!/bin/bash
set -e

command -v docker >/dev/null || { echo "error: docker required — https://docs.docker.com/get-docker/"; exit 1; }
command -v git >/dev/null || { echo "error: git required"; exit 1; }

if [ -d "woltspace/.git" ]; then
  echo "  woltspace already cloned — pulling latest..."
  git -C woltspace fetch --quiet
  git -C woltspace merge --ff-only FETCH_HEAD || {
    echo "  warning: local changes detected — skipping pull. Run 'git -C woltspace pull' manually."
  }
elif [ -d "woltspace" ]; then
  echo "error: ./woltspace exists but isn't a git repo — remove it manually and re-run"
  exit 1
else
  git clone https://github.com/jerpint/woltspace
fi

export WOLTS_DIR="${WOLTS_DIR:-$HOME/.woltspace/wolts}"

"$PWD/woltspace/woltspace" init
