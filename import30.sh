#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

if [[ -f ".venv/bin/activate" ]]; then
  # shellcheck disable=SC1091
  source ".venv/bin/activate"
fi

if [[ "${1:-}" == "dry" ]]; then
  shift
  python3 scripts/import_simulated_distribution_30d.py --dry-run "$@"
else
  python3 scripts/import_simulated_distribution_30d.py "$@"
fi
