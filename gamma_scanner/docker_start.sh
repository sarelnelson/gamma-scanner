#!/bin/bash
# Docker startup script — runs the profit monitor and API server together.

set -e

echo "=== Gamma Scanner Container Starting ==="
echo "  API Port: ${PORT:-8081}"
echo "  Alpaca Key: ${ALPACA_API_KEY:0:8}..."
echo "  Mode: $([ -z '$LIVE_EXECUTION' ] && echo 'PAPER' || echo 'LIVE')"
echo ""

# Set up persistent data directory structure
if [ -d "/app/data" ]; then
    # Shared scanner files (candidates, picks, scan log)
    [ -f "/app/data/candidates.json" ] || echo "[]" > /app/data/candidates.json
    [ -f "/app/data/picks_loose.json" ] || echo "[]" > /app/data/picks_loose.json
    [ -f "/app/data/last_scan.json" ] || echo '{"last_scan_time":null,"picks_found":0,"candidates_found":0}' > /app/data/last_scan.json
    
    # Symlink shared files to scanner dir
    ln -sf /app/data/candidates.json /app/gamma_scanner/candidates.json
    ln -sf /app/data/picks_loose.json /app/gamma_scanner/picks_loose.json
    ln -sf /app/data/last_scan.json /app/gamma_scanner/last_scan.json
    
    # Per-user directories — preserve existing data
    # Load users from config to create directories for each
    python3 -c "
import json, os, sys
try:
    with open('/app/gamma_scanner/users.json') as f:
        config = json.load(f)
    for uid in config.get('users', {}).keys():
        user_dir = f'/app/data/user_{uid}'
        os.makedirs(user_dir, exist_ok=True)
        # Init files only if they don't exist (don't overwrite existing data)
        for fname, default in [('trades.json', '[]'), ('picks.json', '[]'), ('queue.json', '[]')]:
            path = os.path.join(user_dir, fname)
            if not os.path.exists(path):
                with open(path, 'w') as f:
                    f.write(default)
        # Account file
        acc_path = os.path.join(user_dir, 'account.json')
        if not os.path.exists(acc_path):
            bal = config.get('users', {}).get(uid, {}).get('starting_balance', 0)
            with open(acc_path, 'w') as f:
                json.dump({'starting_balance': bal, 'transactions': []}, f)
        print(f'  User {uid}: data at {user_dir}')
        # Symlink user data dir into scanner dir for relative path access
        link = f'/app/gamma_scanner/user_{uid}'
        # Remove existing directory/link if it exists
        if os.path.isdir(link) and not os.path.islink(link):
            import shutil
            shutil.rmtree(link)
        elif os.path.islink(link):
            os.remove(link)
        os.symlink(user_dir, link)
except Exception as e:
    print(f'  Warning: {e}', file=sys.stderr)
"
    echo "  Data directories ready"
fi

# Start profit monitor in background
echo "Starting profit monitor..."
cd /app && python -u gamma_scanner/profit_monitor.py > /app/data/monitor.log 2>&1 &
MONITOR_PID=$!
echo "  Monitor PID: $MONITOR_PID"

sleep 2
if kill -0 $MONITOR_PID 2>/dev/null; then
    echo "  Monitor: ✅ running"
else
    echo "  Monitor: ❌ failed to start"
    cat /app/data/monitor.log
    exit 1
fi

cleanup() {
    echo "Shutting down..."
    kill $MONITOR_PID 2>/dev/null
    wait $MONITOR_PID 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

echo "Starting API server on port ${PORT:-8081}..."
cd /app/gamma_scanner && exec python -m uvicorn server:app --host 0.0.0.0 --port ${PORT:-8081}
