"""
Gamma Scanner — DevSpaces Mirror Server

Serves the same dashboard UI but reads live data from the GitHub Gist briefing
instead of local files. This keeps DevSpaces in sync with EC2 without needing
direct network access.

Data flow: EC2 monitor → Gist → This server → Dashboard UI
"""
import os, json, time, requests
from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Gamma Scanner (Mirror)", version="2.1")

# Gist config
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GIST_ID = os.getenv("BRIEFING_GIST_ID", "e39d7fb7b6d1b7f4fbf26d190f4aa8dd")
GIST_API = f"https://api.github.com/gists/{GIST_ID}"

# Cache to avoid hammering GitHub API
_cache = {"data": None, "fetched_at": 0}
CACHE_TTL = 30  # seconds

SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))


def get_briefing() -> dict:
    """Fetch briefing from gist with caching."""
    now = time.time()
    if _cache["data"] and (now - _cache["fetched_at"]) < CACHE_TTL:
        return _cache["data"]
    
    try:
        resp = requests.get(
            GIST_API,
            headers={"Authorization": f"token {GITHUB_TOKEN}"},
            timeout=10,
        )
        if resp.status_code == 200:
            gist = resp.json()
            content = gist["files"]["gamma_briefing.json"]["content"]
            _cache["data"] = json.loads(content)
            _cache["fetched_at"] = now
            return _cache["data"]
    except Exception as e:
        print(f"[MIRROR] Gist fetch error: {e}")
    
    return _cache["data"] or {"error": "No data available", "users": {}}


def positions_to_trades(user_data: dict) -> list:
    """Convert briefing positions to the trades format the dashboard expects."""
    trades = []
    
    for pos in user_data.get("open_positions", []):
        trades.append({
            "ticker": pos["ticker"],
            "direction": pos["direction"],
            "setup": "oversold_bounce",
            "score": 0,
            "entry_price": pos.get("stock_price", 0),
            "entry_date": pos["entry_date"],
            "entry_time": pos.get("entry_time") or pos["entry_date"] + "T10:00:00",
            "option_strike": pos["strike"],
            "option_exp": pos["expiration"],
            "option_cost": pos["entry_cost"],
            "cost_per_contract": round(pos["entry_cost"] * 100, 2),
            "status": "open",
            "pnl": 0,
            "current_pnl": pos.get("current_pnl_dollars", 0),
            "current_option_bid": pos.get("current_bid"),
            "current_option_mid": pos.get("current_bid"),
            "current_price": pos.get("stock_price"),
            "stock_change_pct": pos.get("stock_change_pct"),
            "high_water_pct": pos.get("high_water_pct"),
            "trailing_floor_pct": pos.get("trailing_floor_pct"),
            "last_check": pos.get("last_check"),
            "prev_option_bid": pos.get("current_bid"),
            "prev_bid_date": pos["entry_date"],
        })
    
    for t in user_data.get("closed_today", []):
        trades.append({
            "ticker": t["ticker"],
            "direction": t["direction"],
            "setup": "oversold_bounce",
            "score": 0,
            "entry_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "option_strike": t["strike"],
            "option_exp": "",
            "option_cost": t["entry_cost"],
            "cost_per_contract": round(t["entry_cost"] * 100, 2),
            "status": "closed",
            "pnl": t.get("pnl", 0),
            "pnl_pct": t.get("pnl_pct"),
            "exit_reason": t.get("exit_reason"),
            "high_water_pct": t.get("high_water_pct"),
            "exit_date": datetime.utcnow().strftime("%Y-%m-%d"),
        })
    
    return trades


# === Dashboard UI ===

@app.get("/", response_class=HTMLResponse)
def login_page():
    with open(os.path.join(SCANNER_DIR, "static", "login.html")) as f:
        return f.read()

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    with open(os.path.join(SCANNER_DIR, "static", "index.html")) as f:
        return f.read()

@app.post("/api/login")
def login():
    return {"success": True, "token": "mirror-mode"}


# === API Endpoints matching original server format ===

@app.get("/api/health")
def health():
    briefing = get_briefing()
    return {
        "status": "ok",
        "mode": "mirror",
        "source": "gist",
        "market_status": briefing.get("market_status", "unknown"),
        "last_update": briefing.get("timestamp", "never"),
    }


@app.get("/api/trades")
def get_trades(user: str = Query(default="sarel")):
    briefing = get_briefing()
    user_data = briefing.get("users", {}).get(user, {})
    trades = positions_to_trades(user_data)
    return {
        "strict": [],
        "loose": trades,
    }


@app.get("/api/picks")
def get_picks():
    return {
        "strict": [],
        "loose": [],
    }


@app.get("/api/performance")
def get_performance(user: str = Query(default="sarel")):
    briefing = get_briefing()
    user_data = briefing.get("users", {}).get(user, {})
    trades = positions_to_trades(user_data)
    
    open_t = [t for t in trades if t["status"] == "open"]
    closed_t = [t for t in trades if t["status"] == "closed"]
    wins = sum(1 for t in closed_t if t.get("pnl", 0) > 0)
    losses = len(closed_t) - wins
    total_pnl = sum(t.get("pnl", 0) for t in closed_t)
    open_pnl = sum(t.get("current_pnl", 0) for t in open_t)
    
    return {
        "strict": {"total": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0,
                   "win_rate": 0, "total_pnl": 0, "open_pnl": 0, "avg_score": 0, "trades": []},
        "loose": {
            "total": len(trades),
            "open": len(open_t),
            "closed": len(closed_t),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(closed_t) * 100, 1) if closed_t else 0,
            "total_pnl": round(total_pnl, 2),
            "open_pnl": round(open_pnl, 2),
            "avg_score": 0,
            "trades": trades,
        },
        "combined_pnl": round(total_pnl, 2),
        "total_open": len(open_t),
    }


@app.get("/api/candidates")
def get_candidates():
    briefing = get_briefing()
    scans = briefing.get("scans_today", {})
    # Return empty candidates list — scan details aren't in briefing yet
    return {"candidates": []}


@app.get("/api/queue")
def get_queue(user: str = Query(default="sarel")):
    return {"queue": []}


@app.get("/api/last-scan")
def last_scan():
    briefing = get_briefing()
    scans = briefing.get("scans_today", {})
    return {
        "last_scan_time": briefing.get("timestamp"),
        "picks_found": scans.get("picks_found", 0),
        "candidates_found": 0,
    }


@app.get("/api/spy-context")
def spy_context():
    """SPY price, daily change, and 5-day trend."""
    try:
        from datetime import datetime, timedelta
        ALPACA_DATA_URL = "https://data.alpaca.markets/v2"
        STOCK_HEADERS = {
            "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", "PKOMKRLONHFRTJIPY3OTSRQYDP"),
            "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", "85eucWnKfY5DmBxCiWP3uTefYMbLdwn7D7fjTSpbNGx4"),
        }
        start = (datetime.utcnow() - timedelta(days=10)).strftime("%Y-%m-%d")
        resp = requests.get(
            f"{ALPACA_DATA_URL}/stocks/SPY/bars",
            headers=STOCK_HEADERS,
            params={"timeframe": "1Day", "limit": 7, "adjustment": "split", "start": start},
            timeout=5,
        )
        if resp.status_code != 200:
            return {"error": "SPY data unavailable"}
        bars = resp.json().get("bars", [])
        if len(bars) < 2:
            return {"error": "Insufficient data"}
        
        # Use snapshot for current intraday price
        snap_resp = requests.get(
            f"{ALPACA_DATA_URL}/stocks/SPY/snapshot",
            headers=STOCK_HEADERS,
            timeout=5,
        )
        if snap_resp.status_code == 200:
            snap = snap_resp.json()
            current = snap.get("latestTrade", {}).get("p", bars[-1]["c"])
        else:
            current = bars[-1]["c"]
        
        prev_close = bars[-2]["c"] if len(bars) >= 2 else bars[-1]["o"]
        change_pct = (current - prev_close) / prev_close * 100
        
        result = {"price": round(current, 2), "change_pct": round(change_pct, 2)}
        
        if len(bars) >= 6:
            price_5d_ago = bars[-6]["c"]
            change_5d = (current - price_5d_ago) / price_5d_ago * 100
            result["change_5d"] = round(change_5d, 2)
        
        return result
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/status")
def get_status(user: str = Query(default="sarel")):
    briefing = get_briefing()
    user_data = briefing.get("users", {}).get(user, {})
    return {"paused": user_data.get("paused", False)}


@app.get("/api/account")
def account_info(user: str = Query(default="sarel")):
    briefing = get_briefing()
    user_data = briefing.get("users", {}).get(user, {})
    alpaca = briefing.get("alpaca_positions", [])
    
    broker_equity = sum(p.get("market_value", 0) for p in alpaca)
    
    return {
        "balance": user_data.get("account_balance", 0),
        "deployed": sum(pos["entry_cost"] * 100 for pos in user_data.get("open_positions", [])),
        "available": 0,
        "max_deploy": 0,
        "cash_basis": 0,
        "broker_equity": broker_equity,
        "broker_buying_power": 0,
        "transactions": [],
    }


@app.get("/api/alpaca-positions")
def get_alpaca_positions():
    briefing = get_briefing()
    return briefing.get("alpaca_positions", [])


@app.get("/api/briefing")
def get_full_briefing():
    return get_briefing()


# Write endpoints — return "mirror mode" error for any action
@app.post("/api/scan")
@app.post("/api/pause")
@app.post("/api/unpause")
@app.post("/api/close/{ticker}")
@app.post("/api/close-all")
@app.post("/api/add-contract/{ticker}")
@app.post("/api/queue/buy/{ticker}")
@app.post("/api/sync-alpaca")
@app.post("/api/account/deposit")
@app.post("/api/account/withdraw")
def mirror_write_blocked(**kwargs):
    return {"error": "Mirror mode — actions must be done on EC2 dashboard", "success": False}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
