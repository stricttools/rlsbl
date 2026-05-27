#!/usr/bin/env bash
set -euo pipefail

uv run rlsbl --dump-schema

echo "Running pre-release checks..."

if [ -f pyproject.toml ]; then
  echo "Detected Python project"
  if command -v uv &>/dev/null; then
    uv run pytest
  elif command -v pytest &>/dev/null; then
    pytest
  fi
fi

echo "Pre-release checks passed."
