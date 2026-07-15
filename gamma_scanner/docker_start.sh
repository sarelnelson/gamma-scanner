#!/bin/bash
# Docker startup script — runs the profit monitor and API server together.
# The monitor runs in the background, the API server runs in foreground (Docker needs one foreground process).

set -e

echo "=== Gamma Scanner Container Starting ==="
echo "  API Port: ${PORT:-8081}"
echo "  Alpaca Key: ${ALPACA_API_KEY:0:8}..."
echo "  Mode: $([ -z '$LIVE_EXECUTION' ] && echo 'PAPER' || echo 'LIVE')"
echo ""

# Symlink data directory so trades persist across restarts (mount /app/data as a volume)
if [ -d "/app/data" ]; then
    # Use mounted volume for persistent data
    [ -f "/app/data/trades_loose.json" ] || echo "[]" > /app/data/trades_loose.json
    [ -f "/app/data/trades_strict.json" ] || echo "[]" > /app/data/trades_strict.json
    [ -f "/app/data/picks_loose.json" ] || echo "[]" > /app/data/picks_loose.json
    [ -f "/app/data/picks_strict.json" ] || echo "[]" > /app/data/picks_strict.json
    
    # Symlink scanner data files to the persistent volume
    ln -sf /app/data/trades_loose.json /app/gamma_scanner/trades_loose.json
    ln -sf /app/data/trades_strict.json /app/gamma_scanner/trades_strict.json
    ln -sf /app/data/picks_loose.json /app/gamma_scanner/picks_loose.json
    ln -sf /app/data/picks_strict.json /app/gamma_scanner/picks_strict.json
fi

# Start profit monitor in background
echo "Starting profit monitor..."
cd /app && python -u gamma_scanner/profit_monitor.py > /app/data/monitor.log 2>&1 &
MONITOR_PID=$!
echo "  Monitor PID: $MONITOR_PID"

# Health check: verify monitor started
sleep 2
if kill -0 $MONITOR_PID 2>/dev/null; then
    echo "  Monitor: ✅ running"
else
    echo "  Monitor: ❌ failed to start"
    cat /app/data/monitor.log
    exit 1
fi

# Trap signals to cleanly stop both processes
cleanup() {
    echo "Shutting down..."
    kill $MONITOR_PID 2>/dev/null
    wait $MONITOR_PID 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

# Start API server in foreground (keeps container alive)
echo "Starting API server on port ${PORT:-8081}..."
cd /app/gamma_scanner && exec python -m uvicorn server:app --host 0.0.0.0 --port ${PORT:-8081}
