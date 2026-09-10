#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# DMARC Pipeline — local development runner
# For production, use the Dockerfile directly.
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

# ── Schedule daily Outlook sync (cron-friendly entry point) ──────────────────
# Uncomment the line below if you want a daily scheduled sync via cron instead
# of relying on the continuous scheduler above:
# 0 6 * * * cd /path/to/dmarc_pipeline && python -m automation.daily_outlook --notify
#
# For run.sh users: set ENABLE_DAILY_OUTLOOK=true to schedule a daily sync at 6 AM
DAILY_OUTLOOK_PID=""
if [ "${ENABLE_DAILY_OUTLOOK:-false}" = "true" ]; then
  echo "Scheduling daily Outlook sync at 6:00 AM..."
  (
    # Simple daily scheduler — sleep until 6 AM then run every 24h
    while true; do
      NOW=$(date +%s)
      TARGET=$(date -j -f "%H:%M" "06:00" +%s 2>/dev/null || echo $((NOW + 21600)))
      if [ "$TARGET" -le "$NOW" ]; then
        TARGET=$((TARGET + 86400))
      fi
      SLEEP_SECS=$((TARGET - NOW))
      sleep "$SLEEP_SECS"
      echo "[daily-outlook] Running scheduled sync..."
      python -m automation.daily_outlook --notify || true
    done
  ) &
  DAILY_OUTLOOK_PID=$!
fi

cleanup() {
  echo "Shutting down..."
  kill "$WATCHER_PID" 2>/dev/null || true
  [ -n "$SCHEDULER_PID" ] && kill "$SCHEDULER_PID" 2>/dev/null || true
  [ -n "$DAILY_OUTLOOK_PID" ] && kill "$DAILY_OUTLOOK_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

python -m cli serve --host "${DMARC_HOST:-0.0.0.0}" --port "${DMARC_PORT:-8000}"
