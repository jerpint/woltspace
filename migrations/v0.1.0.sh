#!/usr/bin/env bash
# Migration: v0.0.2 → v0.1.0
# Syncs Python dependencies after code pull.
#
# Why: v0.1.0 adds python-dotenv as a dependency for both server and bot.
# Without syncing, the server crashes on reload (ImportError: dotenv).
#
# This script is idempotent — safe to run multiple times.

set -euo pipefail

echo "=== v0.1.0 migration: sync Python dependencies ==="

cd /workspace/woltspace

echo "Syncing server dependencies..."
uv sync --project server 2>&1

echo "Syncing bot dependencies..."
uv sync --project container/bot 2>&1

echo ""
echo "✓ Dependencies synced. Server and bot will auto-reload."
