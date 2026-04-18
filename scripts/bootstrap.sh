#!/usr/bin/env bash
# One-shot bootstrap for super-q on a fresh machine.
#
# Creates a venv, installs super-q, verifies tool presence, and prints
# next steps. Safe to re-run — idempotent.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${SUPERQ_VENV:-$HERE/.venv}"

if ! command -v python3 >/dev/null; then
  echo "bootstrap: python3 is required" >&2
  exit 2
fi

if [ ! -d "$VENV" ]; then
  echo "Creating venv at $VENV"
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1090
. "$VENV/bin/activate"

pip install -U pip
pip install -e "$HERE[all]" || pip install -e "$HERE"

echo
echo "=== super-q environment ==="
superq info || true

cat <<EOF

Next steps:
  1. Install Quartus Lite if you want local builds:
       bash $HERE/scripts/install-quartus.sh --version=24.1 --accept-eula

  2. Or use the Docker backend:
       bash $HERE/docker/README.md

  3. Run a smoke test:
       superq verify <path-to-a-pocket-core>
       superq sweep <path-to-a-pocket-core> --min=1 --max=4

  4. Expose to Claude Code:
       Add to your ~/.claude/config.json:
         { "mcpServers": { "super-q": { "command": "super-q-mcp" } } }

EOF
