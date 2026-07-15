#!/usr/bin/env python3
"""Morning scan runner — runs both strict and loose scanners for A/B comparison."""
import sys, os, json
from datetime import datetime

sys.path.insert(0, "/workspace/stock-agent/gamma_scanner")
os.chdir("/workspace/stock-agent/gamma_scanner")

from market_clock import is_market_open
sys.path.insert(0, "/workspace/stock-agent")

def already_scanned_today():
    """Check if we already ran today by looking at trade entry dates."""
    today = datetime.now().strftime("%Y-%m-%d")
    for f in ["trades_strict.json", "trades_loose.json"]:
        try:
            trades = json.load(open(f))
            if any(t.get("entry_date") == today for t in trades):
                return True
        except:
            pass
    return False

if __name__ == "__main__":
    if already_scanned_today():
        print("Already scanned today, skipping")
    else:
        print("Running STRICT scanner...")
        import scanner_strict
        scanner_strict.run_scan()
        
        print("\nRunning LOOSE scanner...")
        import scanner_loose
        scanner_loose.run_scan()
        
        print("\nBoth scans complete.")
