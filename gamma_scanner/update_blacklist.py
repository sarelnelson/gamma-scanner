"""
Blacklist Updater — runs weekly to identify stocks that consistently lose money
on oversold bounce trades and adds them to the blacklist.

Criteria for blacklisting:
- Stock was traded 3+ times on oversold signals
- Net P&L is negative (it loses money overall when traded this way)
- Win rate is below 30%

Criteria for UN-blacklisting (redemption):
- If a previously blacklisted stock starts bouncing reliably, it gets removed

Run weekly: python3 update_blacklist.py
"""
import json, os, sys, time
from datetime import datetime
import pandas as pd
import numpy as np
import ta
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BLACKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blacklist.json")
TRADES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_loose.json")


def update_blacklist():
    """Update blacklist based on actual trade history."""
    
    # Load trade history
    try:
        with open(TRADES_FILE) as f:
            trades = json.load(f)
    except:
        print("No trade history — can't update blacklist yet")
        return
    
    # Only look at closed/expired trades
    closed = [t for t in trades if t.get("status") in ("closed", "expired")]
    if len(closed) < 20:
        print(f"Only {len(closed)} closed trades — need at least 20 for blacklist analysis")
        return
    
    # Group by ticker
    ticker_stats = {}
    for t in closed:
        tk = t["ticker"]
        if tk not in ticker_stats:
            ticker_stats[tk] = {"trades": 0, "wins": 0, "total_pnl": 0}
        ticker_stats[tk]["trades"] += 1
        ticker_stats[tk]["total_pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            ticker_stats[tk]["wins"] += 1
    
    # Identify stocks to blacklist
    new_blacklist = []
    for tk, stats in ticker_stats.items():
        if stats["trades"] >= 3:
            wr = stats["wins"] / stats["trades"]
            if stats["total_pnl"] < 0 and wr < 0.30:
                new_blacklist.append(tk)
                print(f"  BLACKLIST: {tk} — {stats['trades']} trades, {wr*100:.0f}% WR, ${stats['total_pnl']:+.0f}")
    
    # Load existing blacklist and merge
    existing = []
    try:
        with open(BLACKLIST_FILE) as f:
            existing = json.load(f)
    except:
        pass
    
    # Check for redemption: if a previously blacklisted stock now has positive P&L, remove it
    redeemed = []
    for tk in existing:
        if tk in ticker_stats:
            stats = ticker_stats[tk]
            if stats["total_pnl"] > 0 and stats["wins"] / stats["trades"] > 0.5:
                redeemed.append(tk)
                print(f"  REDEEMED: {tk} — now profitable, removing from blacklist")
    
    # Final blacklist = existing + new - redeemed
    final = list(set(existing + new_blacklist) - set(redeemed))
    final.sort()
    
    # Save
    with open(BLACKLIST_FILE, "w") as f:
        json.dump(final, f, indent=2)
    
    print(f"\nBlacklist updated: {len(final)} stocks")
    print(f"  Added: {len(new_blacklist)}")
    print(f"  Redeemed: {len(redeemed)}")
    print(f"  Total: {final}")


if __name__ == "__main__":
    update_blacklist()
