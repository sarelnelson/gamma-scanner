"""
pick_log.py — persistent record of every pick the scanner makes.

Appends each pick to DATA_DIR/pick_history.json (kept in full) and mirrors the recent
history to the briefing gist as gamma_pick_history.json (stored on GitHub).

Only SCHEDULED/auto scans are recorded — run_scan(record=True). Manual scans
(/api/scan) call run_scan() with record=False, so they are NOT logged.
"""
import os
import json
import requests
from datetime import datetime

try:
    from config import DATA_DIR
except Exception:
    DATA_DIR = os.environ.get("GAMMA_DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else os.path.dirname(os.path.abspath(__file__)))

HISTORY_FILE = os.path.join(DATA_DIR, "pick_history.json")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GIST_ID = os.getenv("BRIEFING_GIST_ID", "")
GIST_MAX = 5000  # mirror only the last N to the gist; local file keeps everything


def _load():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, HISTORY_FILE)


def record_picks(picks, mode="auto"):
    """Append every pick from a scan to the persistent history + push to the gist."""
    if not picks:
        return
    hist = _load()
    now = datetime.utcnow().isoformat() + "Z"
    for p in picks:
        opt = p.get("option") or {}
        hist.append({
            "scan_time": now,
            "scan_mode": mode,
            "ticker": p.get("ticker"),
            "direction": p.get("direction"),
            "setup": p.get("setup"),
            "score": p.get("score"),
            "entry_price": p.get("price"),
            "rsi": p.get("rsi"),
            "quality": p.get("quality"),
            "strike": opt.get("strike"),
            "expiration": opt.get("expiration"),
            "option_bid": opt.get("bid"),
            "option_ask": opt.get("ask"),
            "cost_per_contract": opt.get("cost_per_contract"),
            "spread_pct": opt.get("spread_pct"),
            "open_interest": opt.get("open_interest"),
        })
    _save(hist)
    _push_gist(hist)


def _push_gist(hist):
    if not GITHUB_TOKEN or not GIST_ID:
        return
    try:
        content = json.dumps(hist[-GIST_MAX:], indent=1)
        requests.patch(
            "https://api.github.com/gists/" + GIST_ID,
            headers={"Authorization": "token " + GITHUB_TOKEN, "Content-Type": "application/json"},
            json={"files": {"gamma_pick_history.json": {"content": content}}},
            timeout=10,
        )
    except Exception:
        pass
