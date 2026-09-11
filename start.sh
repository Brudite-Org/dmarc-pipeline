#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# DMARC Pipeline — Render production start command
#
# Runs the auto-sync scheduler and the web server as a supervised pair:
#   - SIGTERM/SIGINT (e.g. a Render deploy/restart) is forwarded to both
#     child processes for a clean shutdown.
#   - If either process exits on its own (crash), the other is stopped and
#     this script exits with that process's exit code — so the container
#     dies and Render restarts the whole service, instead of (for example)
#     the scheduler dying silently while the web server keeps serving
#     traffic and looking healthy on Render's /health check.
#
# Render start command:
#   bash start.sh
# ═══════════════════════════════════════════════════════════════════════════

set -u

scheduler_pid=""
web_pid=""

forward_signal() {
    sig="$1"
    [ -n "$scheduler_pid" ] && kill -s "$sig" "$scheduler_pid" 2>/dev/null
    [ -n "$web_pid" ] && kill -s "$sig" "$web_pid" 2>/dev/null
}

on_signal() {
    echo "[start.sh] received signal, forwarding to children and exiting"
    forward_signal TERM
    wait "$scheduler_pid" 2>/dev/null
    wait "$web_pid" 2>/dev/null
    exit 0
}

trap on_signal TERM INT

python -m services.scheduler &
scheduler_pid=$!
echo "[start.sh] scheduler started (pid $scheduler_pid)"

gunicorn wsgi:app -w 2 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:"${PORT:-8000}" &
web_pid=$!
echo "[start.sh] web server started (pid $web_pid)"

exit_code=0
while true; do
    if ! kill -0 "$scheduler_pid" 2>/dev/null; then
        wait "$scheduler_pid"
        exit_code=$?
        echo "[start.sh] scheduler exited (code $exit_code) - stopping web server"
        break
    fi
    if ! kill -0 "$web_pid" 2>/dev/null; then
        wait "$web_pid"
        exit_code=$?
        echo "[start.sh] web server exited (code $exit_code) - stopping scheduler"
        break
    fi
    sleep 2
done

forward_signal TERM
wait "$scheduler_pid" 2>/dev/null
wait "$web_pid" 2>/dev/null
exit "$exit_code"
