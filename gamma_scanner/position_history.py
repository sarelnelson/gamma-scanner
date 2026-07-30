"""
Position History Recorder — Captures the full lifecycle of every trade.

Stores time-series snapshots for each open position every monitor cycle.
This data enables:
- Real theta decay analysis (compare actual vs model)
- Conversion rate tracking (+70% → +100% live hit rate)
- Timing pattern analysis (when do bounces happen?)
- Score vs performance correlation
- Market regime impact on win rates

Data stored in: /app/data/position_history/
  - {ticker}_{strike}_{exp}_{entry_date}.json per trade
  - Each file contains the full snapshot history + milestone events

Also maintains a summary file for quick analysis:
  - /app/data/position_history/_summary.json
"""

import os
import json
from datetime import datetime

# Use DATA_DIR for persistence across container restarts
try:
    from config import DATA_DIR
except:
    DATA_DIR = os.environ.get("GAMMA_DATA_DIR", "/app/data")

HISTORY_DIR = os.path.join(DATA_DIR, "position_history")
SUMMARY_FILE = os.path.join(HISTORY_DIR, "_summary.json")

# Milestone thresholds to track
MILESTONES = [25, 50, 70, 80, 90, 100, 150, 200]


def ensure_dir():
    os.makedirs(HISTORY_DIR, exist_ok=True)


def get_trade_key(trade: dict) -> str:
    """Generate unique key for a trade."""
    ticker = trade.get("ticker", "UNK")
    strike = trade.get("option_strike", 0)
    exp = trade.get("option_exp", "").replace("-", "")
    entry = trade.get("entry_date", "").replace("-", "")
    return f"{ticker}_{strike}_{exp}_{entry}"


def get_trade_file(trade: dict) -> str:
    """Get the history file path for a trade."""
    return os.path.join(HISTORY_DIR, f"{get_trade_key(trade)}.json")


def record_snapshot(trade: dict, spy_price: float = None):
    """
    Record a point-in-time snapshot for an open position.
    Called every monitor cycle (2 min during market hours).
    """
    ensure_dir()
    
    if trade.get("status") != "open":
        return
    
    filepath = get_trade_file(trade)
    
    # Load existing history or create new
    if os.path.exists(filepath):
        with open(filepath) as f:
            history = json.load(f)
    else:
        history = {
            "trade_key": get_trade_key(trade),
            "ticker": trade.get("ticker"),
            "direction": trade.get("direction"),
            "strike": trade.get("option_strike"),
            "expiration": trade.get("option_exp"),
            "entry_date": trade.get("entry_date"),
            "entry_time": trade.get("entry_time"),
            "entry_cost": trade.get("option_cost"),
            "entry_stock_price": trade.get("entry_price"),
            "score": trade.get("score"),
            "setup": trade.get("setup"),
            "milestones": {},  # {threshold: first_hit_time}
            "pullbacks": [],   # [{from_pct, to_pct, from_time, to_time}]
            "snapshots": [],
        }
    
    now = datetime.utcnow()
    bid = trade.get("current_option_bid")
    entry_cost = trade.get("option_cost", 0)
    
    if not bid or not entry_cost:
        return
    
    pnl_pct = (bid - entry_cost) / entry_cost * 100 if entry_cost > 0 else 0
    high_water = trade.get("high_water_pct", 0)
    
    # Create snapshot (compact — only essential fields)
    snapshot = {
        "t": now.strftime("%Y-%m-%dT%H:%M"),  # minute precision is enough
        "bid": round(bid, 2),
        "pnl": round(pnl_pct, 1),
        "hw": round(high_water, 1) if high_water else None,
        "stock": trade.get("current_price"),
    }
    
    if spy_price:
        snapshot["spy"] = round(spy_price, 2)
    
    # Don't store duplicate snapshots (same bid as last one)
    if history["snapshots"]:
        last = history["snapshots"][-1]
        if last.get("bid") == snapshot["bid"] and last.get("stock") == snapshot["stock"]:
            return  # No change, skip
    
    history["snapshots"].append(snapshot)
    
    # Check milestones
    for threshold in MILESTONES:
        key = f"+{threshold}%"
        if key not in history["milestones"] and pnl_pct >= threshold:
            history["milestones"][key] = {
                "time": now.isoformat(),
                "day": (now - datetime.strptime(trade["entry_date"], "%Y-%m-%d")).days,
                "bid": round(bid, 2),
                "stock": trade.get("current_price"),
            }
    
    # Track pullbacks (dropped >15% from a recent high in this session)
    if history["snapshots"] and len(history["snapshots"]) > 5:
        recent = history["snapshots"][-20:]
        recent_high = max(s["pnl"] for s in recent)
        if recent_high > 30 and pnl_pct < recent_high - 15:
            # Check if this is a new pullback or continuation
            if not history["pullbacks"] or history["pullbacks"][-1].get("recovered", False):
                history["pullbacks"].append({
                    "from_pct": round(recent_high, 1),
                    "to_pct": round(pnl_pct, 1),
                    "from_time": now.isoformat(),
                    "recovered": False,
                })
            else:
                # Update existing pullback low
                current_pb = history["pullbacks"][-1]
                if pnl_pct < current_pb["to_pct"]:
                    current_pb["to_pct"] = round(pnl_pct, 1)
        elif history["pullbacks"] and not history["pullbacks"][-1].get("recovered", False):
            # Check if we recovered from the pullback
            current_pb = history["pullbacks"][-1]
            if pnl_pct > current_pb["from_pct"] - 5:
                current_pb["recovered"] = True
                current_pb["recovery_time"] = now.isoformat()
    
    # Save (atomic write)
    tmp = filepath + ".tmp"
    with open(tmp, "w") as f:
        json.dump(history, f)
    os.replace(tmp, filepath)


def record_close(trade: dict):
    """Record the final state when a trade closes."""
    ensure_dir()
    filepath = get_trade_file(trade)
    
    if not os.path.exists(filepath):
        return
    
    with open(filepath) as f:
        history = json.load(f)
    
    history["exit"] = {
        "time": trade.get("exit_time"),
        "date": trade.get("exit_date"),
        "reason": trade.get("exit_reason"),
        "pnl": trade.get("pnl"),
        "pnl_pct": trade.get("pnl_pct"),
        "high_water_pct": trade.get("high_water_pct"),
        "exit_bid": trade.get("exit_option_bid"),
        "fill_price": trade.get("exit_fill_price") or trade.get("exit_fill_estimate"),
        "days_held": (datetime.strptime(trade.get("exit_date", "2026-01-01"), "%Y-%m-%d") - 
                      datetime.strptime(trade["entry_date"], "%Y-%m-%d")).days,
    }
    
    with open(filepath, "w") as f:
        json.dump(history, f)
    
    # Update summary
    update_summary(history)


def record_market_context(spy_price: float, spy_change_5d: float = None):
    """Store daily market context for regime analysis."""
    ensure_dir()
    context_file = os.path.join(HISTORY_DIR, "_market_context.json")
    
    if os.path.exists(context_file):
        with open(context_file) as f:
            context = json.load(f)
    else:
        context = {"daily": {}}
    
    today = datetime.utcnow().strftime("%Y-%m-%d")
    context["daily"][today] = {
        "spy": spy_price,
        "spy_5d": spy_change_5d,
        "time": datetime.utcnow().isoformat(),
    }
    
    # Keep last 90 days
    dates = sorted(context["daily"].keys())
    if len(dates) > 90:
        for d in dates[:-90]:
            del context["daily"][d]
    
    with open(context_file, "w") as f:
        json.dump(context, f)


def update_summary(history: dict):
    """Update the running summary with completed trade stats."""
    ensure_dir()
    
    if os.path.exists(SUMMARY_FILE):
        with open(SUMMARY_FILE) as f:
            summary = json.load(f)
    else:
        summary = {
            "total_closed": 0,
            "wins": 0,
            "losses": 0,
            "milestone_conversion": {},  # "+70%" → {"hit": N, "converted_to_100": M}
            "avg_days_to_milestones": {},
            "pullback_stats": {"total": 0, "recovered": 0},
            "trades": [],
        }
    
    exit_data = history.get("exit", {})
    pnl = exit_data.get("pnl", 0)
    
    summary["total_closed"] += 1
    if pnl > 0:
        summary["wins"] += 1
    else:
        summary["losses"] += 1
    
    # Track milestone conversions
    milestones = history.get("milestones", {})
    for threshold in [50, 70, 80, 90]:
        key = f"+{threshold}%"
        if key not in summary["milestone_conversion"]:
            summary["milestone_conversion"][key] = {"hit": 0, "converted_to_100": 0}
        if key in milestones:
            summary["milestone_conversion"][key]["hit"] += 1
            if "+100%" in milestones:
                summary["milestone_conversion"][key]["converted_to_100"] += 1
    
    # Pullback stats
    for pb in history.get("pullbacks", []):
        summary["pullback_stats"]["total"] += 1
        if pb.get("recovered"):
            summary["pullback_stats"]["recovered"] += 1
    
    # Compact trade record
    summary["trades"].append({
        "key": history.get("trade_key"),
        "ticker": history.get("ticker"),
        "score": history.get("score"),
        "pnl": pnl,
        "pnl_pct": exit_data.get("pnl_pct"),
        "high_water": exit_data.get("high_water_pct"),
        "days_held": exit_data.get("days_held"),
        "milestones_hit": list(milestones.keys()),
        "pullbacks": len(history.get("pullbacks", [])),
        "entry_date": history.get("entry_date"),
        "exit_date": exit_data.get("date"),
    })
    
    with open(SUMMARY_FILE, "w") as f:
        json.dump(summary, f, indent=2)


def get_live_stats() -> dict:
    """Get current conversion rates and stats for the briefing/dashboard."""
    if not os.path.exists(SUMMARY_FILE):
        return {"message": "No completed trades with history yet"}
    
    with open(SUMMARY_FILE) as f:
        summary = json.load(f)
    
    result = {
        "total_closed": summary["total_closed"],
        "win_rate": round(summary["wins"] / summary["total_closed"] * 100, 1) if summary["total_closed"] else 0,
        "milestone_conversion": {},
        "pullback_recovery_rate": None,
    }
    
    for key, data in summary.get("milestone_conversion", {}).items():
        if data["hit"] > 0:
            result["milestone_conversion"][key] = {
                "hit": data["hit"],
                "converted": data["converted_to_100"],
                "rate": round(data["converted_to_100"] / data["hit"] * 100, 1),
            }
    
    pb = summary.get("pullback_stats", {})
    if pb.get("total", 0) > 0:
        result["pullback_recovery_rate"] = round(pb["recovered"] / pb["total"] * 100, 1)
    
    return result
