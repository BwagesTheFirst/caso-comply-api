#!/bin/bash
# CASO Comply -- Container startup script
# Cleans up stale files and launches the API server.

set -e

echo "[startup] Cleaning files older than 1 hour from uploads/ and output/..."
find /app/uploads -type f -mmin +60 -delete 2>/dev/null || true
find /app/output  -type f -mmin +60 -delete 2>/dev/null || true
echo "[startup] Cleanup complete."

echo "[startup] Starting uvicorn..."
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-10000}" --workers 2
