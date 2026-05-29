#!/usr/bin/env bash
# Sarvam AI Voice Agent — quick start
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Load .env if present
if [ -f .env ]; then
    set -a; source .env; set +a
fi

# Install deps if needed
if ! python3 -c "import sarvamai" 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r requirements.txt
fi

echo "Starting Sarvam AI Voice Agent on port ${SERVER_PORT:-19200}..."
exec python3 server.py
