#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# DMARC Pipeline — local development runner
# For production (Render), use start.sh instead.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
export DMARC_REPORTS_DIR="${SCRIPT_DIR}/reports"
export DMARC_LOG_LEVEL="${DMARC_LOG_LEVEL:-INFO}"

# ── Virtual environment ──────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi
source .venv/bin/activate

pip install -q -r requirements.txt

# ── Directories ──────────────────────────────────────────────────────────────
mkdir -p "$DMARC_REPORTS_DIR" quarantine reports

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  DMARC Pipeline"
echo "═══════════════════════════════════════════════════════════"
echo "  Dashboard   : http://localhost:${DMARC_PORT:-8000}"
echo "  API docs    : http://localhost:${DMARC_PORT:-8000}/docs"
echo "  Drop folder : ${DMARC_REPORTS_DIR}"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Initialise DB ────────────────────────────────────────────────────────────
python -m cli init-db

# ── Start folder watcher (background) ────────────────────────────────────────
python -m cli watch --directory "$DMARC_REPORTS_DIR" &
WATCHER_PID=$!

# ── Start auto-sync scheduler (background) ───────────────────────────────────
SCHEDULER_PID=""
if [ "${ENABLE_AUTO_SYNC:-true}" = "true" ]; then
  echo "Starting auto-sync scheduler (every ${AUTO_SYNC_INTERVAL:-300}s)..."
  python -m services.scheduler &
  SCHEDULER_PID=$!
fi

cleanup() {
  echo "Shutting down..."
  kill "$WATCHER_PID" 2>/dev/null || true
  [ -n "$SCHEDULER_PID" ] && kill "$SCHEDULER_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

python -m cli serve --host "${DMARC_HOST:-0.0.0.0}" --port "${DMARC_PORT:-8000}"
