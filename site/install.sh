#!/bin/bash
# Install woltspace — clone repo to ~/.woltspace/repo/ and run init.
set -e

command -v docker >/dev/null || { echo "error: docker required — https://docs.docker.com/get-docker/"; exit 1; }
command -v git >/dev/null || { echo "error: git required"; exit 1; }

REPO_DIR="${HOME}/.woltspace/repo"

if [ -d "$REPO_DIR/.git" ]; then
  echo "  woltspace already installed — pulling latest..."
  git -C "$REPO_DIR" fetch --quiet --tags
  git -C "$REPO_DIR" merge --ff-only FETCH_HEAD || {
    echo "  warning: local changes detected — skipping pull. Run 'woltspace update' after install."
  }
elif [ -d "$REPO_DIR" ]; then
  echo "error: $REPO_DIR exists but isn't a git repo — remove it manually and re-run"
  exit 1
else
  mkdir -p "${HOME}/.woltspace"
  git clone https://github.com/jerpint/woltspace "$REPO_DIR"
fi

# Checkout latest tag
LATEST_TAG=$(cd "$REPO_DIR" && git tag --sort=-v:refname | head -1)
if [ -n "$LATEST_TAG" ]; then
  (cd "$REPO_DIR" && git checkout "$LATEST_TAG" --quiet)
  echo "  version: $LATEST_TAG"
fi

export WOLTS_DIR="${WOLTS_DIR:-$HOME/.woltspace/wolts}"

"$REPO_DIR/woltspace" init
