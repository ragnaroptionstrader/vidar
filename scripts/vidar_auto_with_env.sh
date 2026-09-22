#!/usr/bin/env bash
# vidar_auto_with_env.sh — cron wrapper for VIDAR orchestrator.
#
# Sets venv + dotenv + paths before invoking vidar_auto.py.
# Mirrors dez_auto_with_env.sh structure for cron parity.

set -euo pipefail

# Resolve repo + venv
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PY="/home/freya/RAGNAR/verticals-bot/.venv/bin/python"

# Load .env (operator-provided; do not commit secrets)
if [ -f "/home/freya/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "/home/freya/.env"
  set +a
fi

# Honor PIPELINE selector — only demo runs paper-trade on first deploy
PIPELINE="${PIPELINE:-demo}"
export TIGER_ACCOUNT_TYPE="paper"
export TIGER_ACCOUNT_ID="21224823943487560"
export TIGER_PRIVATE_KEY_PATH="/home/freya/RAGNAR/tiger_openapi_demo.properties"

# Cron marker (mirrors dez auto)
echo "/tmp/expected_account_type = $TIGER_ACCOUNT_TYPE" > /tmp/expected_account_type_vidar 2>/dev/null || true

exec "$VENV_PY" "$REPO_ROOT/ragnar_scripts/vidar_auto.py" "$@"
