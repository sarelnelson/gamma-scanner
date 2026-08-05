"""
Gamma Scanner — DevSpaces Mirror Server

Same dashboard, queries Alpaca directly per user.
No gist dependency. Shows live data for all accounts.
"""
import os, json, time, requests
from datetime import datetime
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="Gamma Scanner (Mirror)", version="3.0")

SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))
PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"

# Load users config
def load_users():
    try:
        with open(os.path.join(SCANNER_DIR, "users.json")) as f:
            return json.load(f)
    except:
        return {"password": "", "users": {}}


def get_user_keys(user_id):
    """Get Alpaca keys and base URL for a user."""
    users = load_users()
    user = users.get("users", {}).get(user_id, {})
    key = user.get("alpaca_key", "")
    secret = user.get("alpaca_secret", "")
    is_paper = user.get("paper", True)
    base = PAPER_BASE if is_paper else LIVE_BASE
    headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    return key, secret, base, headers


def get_positions(user_id):
    """Fetch positions from Alpaca for a user."""
    key, secret, base, headers = get_user_keys(user_id)
    if not key:
        return []
    try:
        resp = requests.get(f"{base}/v2/positions", headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except:
        pass
    return []


def get_account(user_id):
    """Fetch account info from Alpaca."""
    key, secret, base, headers = get_user_keys(user_id)
    if not key:
        return {}
    try:
        resp = requests.get(f"{base}/v2/account", headers=headers, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except:
        pass
    return {}


# === Pages ===

@app.get("/", response_class=HTMLResponse)
def login_page():
    with open(os.path.join(SCANNER_DIR, "static", "login.html")) as f:
        return f.read()

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    with open(os.path.join(SCANNER_DIR, "static", "dashboard_v2.html")) as f:
        return f.read()

@app.get("/dashboard-old", response_class=HTMLResponse)
def dashboard_old():
    with open(os.path.join(SCANNER_DIR, "static", "index.html")) as f:
        return f.read()

@app.post("/api/auth/login")
def login(body: dict):
    users = load_users()
    if body.get("password") == users.get("password"):
        user_list = [{"id": uid, "name": u["name"]} for uid, u in users.get("users", {}).items()]
        return {"success": True, "token": "mirror", "users": user_list}
    return {"success": False}


# === API Endpoints ===

@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "mirror", "time": datetime.utcnow().isoformat()}


@app.get("/api/performance")
def performance(user: str = Query(default="scanner")):
    """Build performance data from Alpaca positions."""
    # Puts user: return empty — mirror can't access EC2's put trades
    if user == "puts":
        empty = {"total": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0,
                 "win_rate": 0, "total_pnl": 0, "open_pnl": 0, "avg_score": 0, "trades": []}
        return {"strict": empty, "loose": empty, "combined_pnl": 0, "total_open": 0}
    
    positions = get_positions(user)
    
    trades = []
    for p in positions:
        qty = int(p["qty"])
        entry = float(p["avg_entry_price"])
        current = float(p["current_price"])
        pl = float(p["unrealized_pl"])
        pct = float(p["unrealized_plpc"]) * 100
        symbol = p["symbol"]
        
        # Parse OCC symbol: TICKER(alpha) + YYMMDD + C/P + 8-digit strike
        # e.g., HOOD260814C00087000 = HOOD, 2026-08-14, CALL, $87
        import re
        match = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', symbol)
        if match:
            ticker = match.group(1)
            date_str = match.group(2)
            direction = "CALL" if match.group(3) == "C" else "PUT"
            strike = int(match.group(4)) / 1000
            expiration = f"20{date_str[:2]}-{date_str[2:4]}-{date_str[4:6]}"
        else:
            ticker = symbol
            direction = "CALL"
            strike = 0
            expiration = ""
        
        trades.append({
            "ticker": ticker,
            "direction": direction,
            "setup": "oversold_bounce",
            "score": 0,
            "entry_price": 0,
            "entry_date": "",
            "entry_time": "",
            "option_strike": strike,
            "option_exp": expiration,
            "option_cost": entry,
            "cost_per_contract": round(entry * 100, 2),
            "status": "open",
            "pnl": 0,
            "current_pnl": round(pl / qty, 2),
            "current_option_bid": current,
            "current_price": None,
            "stock_change_pct": None,
            "high_water_pct": None,
            "trailing_floor_pct": None,
            "last_check": datetime.utcnow().isoformat(),
            "num_contracts": qty,
        })
    
    open_t = trades
    open_pnl = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    
    return {
        "strict": {"total": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0,
                   "win_rate": 0, "total_pnl": 0, "open_pnl": 0, "avg_score": 0, "trades": []},
        "loose": {
            "total": len(trades),
            "open": len(trades),
            "closed": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "open_pnl": round(open_pnl, 2),
            "avg_score": 0,
            "trades": trades,
        },
        "combined_pnl": 0,
        "total_open": len(trades),
    }


@app.get("/api/picks")
def picks():
    return {"strict": [], "loose": []}


@app.get("/api/candidates")
def candidates():
    return {"candidates": []}


@app.get("/api/queue")
def queue(user: str = Query(default="scanner")):
    return {"queue": []}


@app.get("/api/daily-move")
def daily_move(user: str = Query(default="scanner")):
    positions = get_positions(user)
    total = 0
    for p in positions:
        qty = int(p["qty"])
        current = float(p["current_price"])
        lastday = float(p.get("lastday_price", current))
        total += (current - lastday) * qty * 100
    return {"total": round(total, 0)}


@app.get("/api/alpaca-pnl")
def alpaca_pnl(user: str = Query(default="scanner")):
    positions = get_positions(user)
    if not positions:
        return {"unrealized_pl": 0, "cost_basis": 0, "market_value": 0, "positions": 0}
    unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
    cost_basis = sum(float(p.get("cost_basis", 0)) for p in positions)
    market_value = sum(float(p.get("market_value", 0)) for p in positions)
    return {
        "unrealized_pl": round(unrealized, 2),
        "cost_basis": round(cost_basis, 2),
        "market_value": round(market_value, 2),
        "positions": len(positions),
    }


@app.get("/api/spy-context")
def spy_context():
    try:
        key, secret, _, headers = get_user_keys("scanner")
        start = (datetime.utcnow() - __import__("datetime").timedelta(days=10)).strftime("%Y-%m-%d")
        resp = requests.get(f"{DATA_BASE}/v2/stocks/SPY/bars",
            headers=headers, params={"timeframe": "1Day", "limit": 7, "adjustment": "split", "start": start}, timeout=5)
        if resp.status_code != 200:
            return {"error": "unavailable"}
        bars = resp.json().get("bars", [])
        if len(bars) < 2:
            return {"error": "insufficient data"}
        snap = requests.get(f"{DATA_BASE}/v2/stocks/SPY/snapshot", headers=headers, timeout=5)
        current = snap.json().get("latestTrade", {}).get("p", bars[-1]["c"]) if snap.status_code == 200 else bars[-1]["c"]
        prev_close = bars[-2]["c"]
        change_pct = (current - prev_close) / prev_close * 100
        result = {"price": round(current, 2), "change_pct": round(change_pct, 2)}
        if len(bars) >= 6:
            result["change_5d"] = round((current - bars[-6]["c"]) / bars[-6]["c"] * 100, 2)
        return result
    except:
        return {"error": "failed"}


@app.get("/api/last-scan")
def last_scan():
    return {"last_scan_time": None, "picks_found": 0, "candidates_found": 0}


@app.get("/api/status")
def status(user: str = Query(default="scanner")):
    return {"paused": False}


@app.get("/api/account")
def account_info(user: str = Query(default="scanner")):
    acct = get_account(user)
    return {
        "balance": 0,
        "deployed": float(acct.get("cost_basis", 0)) if acct else 0,
        "available": 0,
        "broker_equity": float(acct.get("equity", 0)) if acct else 0,
        "broker_buying_power": float(acct.get("buying_power", 0)) if acct else 0,
        "transactions": [],
    }


@app.get("/api/admin/gate-status")
def gate_status():
    return {"gate_open": False, "allowed_ips": {}}


# Write endpoints — mirror is read-only
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
@app.post("/api/admin/open-gate")
@app.post("/api/admin/close-gate")
def mirror_read_only(**kwargs):
    return {"error": "Mirror is read-only. Use EC2 dashboard for actions.", "success": False}


# Static files
app.mount("/static", StaticFiles(directory=os.path.join(SCANNER_DIR, "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
