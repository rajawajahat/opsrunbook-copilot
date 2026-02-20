#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if ! command -v ruff &>/dev/null; then
  echo "ruff not found. Install: pip install ruff"
  exit 1
fi

echo "=== Ruff check ==="
ruff check services/ packages/ tests/ --fix --show-fixes
echo ""
echo "=== Ruff format check ==="
ruff format --check services/ packages/ tests/ || {
  echo ""
  echo "Format issues found. Run: ruff format services/ packages/ tests/"
  exit 1
}
echo ""
echo "=== Lint passed ==="
