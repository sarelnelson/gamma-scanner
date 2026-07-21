"""
Briefing Module — Publishes structured system state to a GitHub Gist.

The AI assistant reads this gist on startup to understand what happened
while it was offline. Updated every monitor cycle (2 min during market hours).

SETUP:
1. Create a private gist at https://gist.github.com (any content)
2. Set GIST_ID and GITHUB_TOKEN in environment variables on EC2
3. The monitor calls publish_briefing() every cycle

The briefing contains:
- Current open positions with P&L, trailing stop status
- Trades opened/closed today
- Recent errors
- Scanner activity
- Account balance
- Alpaca position reconciliation
"""

import os
import json
import requests
from datetime import datetime, timedelta

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GIST_ID = os.getenv("BRIEFING_GIST_ID", "")
GIST_API = f"https://api.github.com/gists/{GIST_ID}"


def publish_briefing():
    """Generate and push the full system briefing to GitHub Gist."""
    if not GITHUB_TOKEN or not GIST_ID:
        return  # Silently skip if not configured
    
    try:
        briefing = generate_briefing()
        content = json.dumps(briefing, indent=2)
        
        resp = requests.patch(
            GIST_API,
            headers={
                "Authorization": f"token {GITHUB_TOKEN}",
                "Content-Type": "application/json",
            },
            json={
                "files": {
                    "gamma_briefing.json": {"content": content}
                }
            },
            timeout=10,
        )
        
        if resp.status_code != 200:
            print(f"[BRIEFING] Gist update failed: {resp.status_code}")
    except Exception as e:
        print(f"[BRIEFING] Error: {e}")


def generate_briefing() -> dict:
    """Build the full briefing snapshot."""
    from user_manager import get_active_users, load_user_trades
    
    now = datetime.utcnow()
    today_str = now.strftime("%Y-%m-%d")
    
    briefing = {
        "timestamp": now.isoformat() + "Z",
        "market_status": get_market_status(),
        "users": {},
        "errors_today": get_recent_errors(today_str),
        "scans_today": get_scan_summary(today_str),
        "alpaca_positions": get_alpaca_positions(),
    }
    
    for user_id in get_active_users():
        trades = load_user_trades(user_id)
        briefing["users"][user_id] = build_user_summary(user_id, trades, today_str)
    
    return briefing


def get_market_status() -> str:
    """Check if market is open."""
    try:
        from broker_alpaca import BASE_URL, HEADERS
        resp = requests.get(f"{BASE_URL}/v2/clock", headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            clock = resp.json()
            return "open" if clock.get("is_open") else "closed"
    except:
        pass
    return "unknown"


def build_user_summary(user_id: str, trades: list, today_str: str) -> dict:
    """Build per-user summary."""
    open_trades = [t for t in trades if t.get("status") == "open"]
    closed_today = [t for t in trades if t.get("status") == "closed" and t.get("exit_date") == today_str]
    opened_today = [t for t in trades if t.get("entry_date") == today_str and t.get("status") == "open"]
    
    # Account info
    try:
        from user_manager import get_user_data_path
        account_path = os.path.join(get_user_data_path(user_id), "account.json")
        with open(account_path) as f:
            account = json.load(f)
    except:
        account = {}
    
    # Check pause status
    try:
        from user_manager import get_user_data_path
        paused = os.path.exists(os.path.join(get_user_data_path(user_id), ".paused"))
    except:
        paused = False
    
    total_open_pnl = sum(t.get("current_pnl", 0) for t in open_trades)
    total_closed_pnl = sum(t.get("pnl", 0) for t in closed_today)
    
    return {
        "paused": paused,
        "account_balance": account.get("balance", 0),
        "open_positions": [format_position(t) for t in open_trades],
        "opened_today": [format_trade_brief(t) for t in opened_today],
        "closed_today": [format_closed_trade(t) for t in closed_today],
        "total_open_pnl": round(total_open_pnl, 2),
        "total_closed_pnl_today": round(total_closed_pnl, 2),
        "position_count": len(open_trades),
    }


def format_position(t: dict) -> dict:
    """Format an open position for the briefing."""
    pos = {
        "ticker": t["ticker"],
        "direction": t["direction"],
        "strike": t["option_strike"],
        "expiration": t["option_exp"],
        "entry_cost": t["option_cost"],
        "entry_date": t["entry_date"],
        "current_bid": t.get("current_option_bid"),
        "current_pnl_dollars": t.get("current_pnl", 0),
        "current_pnl_pct": round((t.get("current_option_bid", t["option_cost"]) - t["option_cost"]) / t["option_cost"] * 100, 1) if t.get("current_option_bid") else None,
        "high_water_pct": t.get("high_water_pct"),
        "trailing_floor_pct": t.get("trailing_floor_pct"),
        "stock_price": t.get("current_price"),
        "stock_change_pct": t.get("stock_change_pct"),
        "last_check": t.get("last_check"),
    }
    
    # Flag positions near trailing stop activation or exit
    if pos["high_water_pct"] and pos["high_water_pct"] >= 80:
        pos["note"] = f"Near trail activation (+100%). High was +{pos['high_water_pct']}%"
    if pos["trailing_floor_pct"] and pos["current_pnl_pct"]:
        cushion = pos["current_pnl_pct"] - pos["trailing_floor_pct"]
        if cushion < 15:
            pos["note"] = f"⚠️ Close to floor! P&L +{pos['current_pnl_pct']}% vs floor +{pos['trailing_floor_pct']}% (cushion: {cushion:.0f}%)"
    
    return pos


def format_trade_brief(t: dict) -> dict:
    """Brief format for new entries."""
    return {
        "ticker": t["ticker"],
        "direction": t["direction"],
        "strike": t["option_strike"],
        "score": t.get("score"),
        "entry_cost": t["option_cost"],
        "entry_time": t.get("entry_time"),
    }


def format_closed_trade(t: dict) -> dict:
    """Format a closed trade."""
    return {
        "ticker": t["ticker"],
        "direction": t["direction"],
        "strike": t["option_strike"],
        "entry_cost": t["option_cost"],
        "exit_reason": t.get("exit_reason"),
        "pnl": t.get("pnl"),
        "pnl_pct": t.get("pnl_pct"),
        "high_water_pct": t.get("high_water_pct"),
        "held_days": (datetime.strptime(t.get("exit_date", "2026-01-01"), "%Y-%m-%d") - datetime.strptime(t["entry_date"], "%Y-%m-%d")).days,
    }


def get_alpaca_positions() -> list:
    """Get actual Alpaca positions for reconciliation."""
    try:
        from broker_alpaca import BASE_URL, HEADERS
        resp = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            positions = resp.json()
            return [{
                "symbol": p["symbol"],
                "qty": int(p["qty"]),
                "avg_entry": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "unrealized_pl": float(p["unrealized_pl"]),
                "market_value": float(p["market_value"]),
            } for p in positions]
    except:
        pass
    return []


def get_recent_errors(today_str: str) -> list:
    """Pull recent errors from monitor.log."""
    errors = []
    try:
        from config import DATA_DIR
        log_path = os.path.join(DATA_DIR, "monitor.log")
        if not os.path.exists(log_path):
            log_path = "monitor.log"
        
        with open(log_path, "r") as f:
            # Read last 500 lines
            lines = f.readlines()[-500:]
        
        for line in lines:
            if today_str in line and ("[ERROR]" in line or "[WARN]" in line):
                errors.append(line.strip())
        
        # Keep last 20 errors max
        return errors[-20:]
    except:
        return []


def get_scan_summary(today_str: str) -> dict:
    """Summarize today's scan activity."""
    summary = {"scans_run": 0, "picks_found": 0, "entries_made": 0, "details": []}
    try:
        from config import DATA_DIR
        log_path = os.path.join(DATA_DIR, "scan.log")
        if not os.path.exists(log_path):
            log_path = "scan.log"
        
        with open(log_path, "r") as f:
            lines = f.readlines()
        
        current_scan = None
        for line in lines:
            if today_str not in line:
                continue
            if "DAILY SCAN" in line:
                summary["scans_run"] += 1
                current_scan = {"time": line.split("]")[0].strip("["), "picks": 0, "entries": []}
            elif "Scan complete:" in line:
                picks = int(line.split(":")[2].strip().split(" ")[0])
                summary["picks_found"] += picks
                if current_scan:
                    current_scan["picks"] = picks
                    summary["details"].append(current_scan)
            elif "ENTERED" in line:
                summary["entries_made"] += 1
                if current_scan:
                    current_scan["entries"].append(line.strip())
    except:
        pass
    return summary
