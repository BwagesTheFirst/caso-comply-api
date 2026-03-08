#!/usr/bin/env bash
# CASO Comply -- API Service launcher
# Creates venv, installs dependencies, and starts uvicorn on port 8787.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/venv"

echo "=== CASO Comply API Service ==="

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/3] Creating Python virtual environment..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/3] Virtual environment already exists."
fi

# Activate and install
echo "[2/3] Installing dependencies..."
source "$VENV_DIR/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$SCRIPT_DIR/requirements.txt"

# Run
echo "[3/3] Starting uvicorn on port 8787..."
echo "       http://localhost:8787"
echo "       http://localhost:8787/docs  (Swagger UI)"
echo ""
exec uvicorn main:app --host 0.0.0.0 --port 8787 --reload --app-dir "$SCRIPT_DIR"
