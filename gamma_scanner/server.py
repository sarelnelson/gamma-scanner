"""
Gamma Scanner — Standalone API Server
Multi-user support: each user has their own Alpaca account, positions, and P&L.
"""
import os, sys, json, time, requests, hashlib, secrets
from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from broker_alpaca import get_option_quote, build_occ_symbol, get_account, PAPER_MODE, HEADERS as ALPACA_HEADERS, sell_to_close, find_contract

app = FastAPI(title="Gamma Scanner", version="2.0")

# Config
from config import SCANNER_DIR, DATA_DIR, ALPACA_API_KEY, ALPACA_SECRET_KEY
ALPACA_DATA_URL = "https://data.alpaca.markets/v2"
STOCK_HEADERS = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}

# Users config
USERS_FILE = os.path.join(SCANNER_DIR, "users.json")
_active_tokens = set()

def load_users():
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except:
        return {"password": "gamma2026", "users": {"sarel": {"name": "Sarel"}}}

def get_user_data_dir(user_id):
    """Each user gets their own data directory."""
    d = os.path.join(DATA_DIR, f"user_{user_id}")
    os.makedirs(d, exist_ok=True)
    # Init empty files if needed
    for f in ["trades.json", "picks.json", "account.json"]:
        path = os.path.join(d, f)
        if not os.path.exists(path):
            if f == "account.json":
                users = load_users()
                bal = users.get("users", {}).get(user_id, {}).get("starting_balance", 0)
                json.dump({"starting_balance": bal, "transactions": []}, open(path, "w"), indent=2)
            else:
                json.dump([], open(path, "w"))
    return d

def load_user_json(user_id, filename, default=None):
    path = os.path.join(get_user_data_dir(user_id), filename)
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default if default is not None else []

def save_user_json(user_id, filename, data):
    path = os.path.join(get_user_data_dir(user_id), filename)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# === AUTH ENDPOINTS ===

@app.post("/api/auth/login")
def login(body: dict):
    """Verify password and return token + user list."""
    users_config = load_users()
    if body.get("password") == users_config.get("password"):
        token = secrets.token_hex(16)
        _active_tokens.add(token)
        user_list = [{"id": uid, "name": u["name"]} for uid, u in users_config.get("users", {}).items()]
        return {"success": True, "token": token, "users": user_list}
    return {"success": False}


@app.post("/api/auth/logout")
def logout(token: str = ""):
    _active_tokens.discard(token)
    return {"success": True}


# === PAGES ===

@app.get("/", response_class=HTMLResponse)
def login_page():
    """Serve login page."""
    with open(os.path.join(SCANNER_DIR, "static/login.html")) as f:
        return f.read()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Serve the main dashboard."""
    with open(os.path.join(SCANNER_DIR, "static/index.html")) as f:
        return f.read()
def load_json(filename, default=None):
    path = os.path.join(DATA_DIR, filename)
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default if default is not None else []




@app.get("/api/picks")
def get_picks():
    """Today's scanner picks from both strict and loose scanners."""
    return {
        "strict": load_json("picks_strict.json"),
        "loose": load_json("picks_loose.json"),
    }


@app.get("/api/candidates")
def get_candidates():
    """Today's candidates (stocks that passed screening but may not have been traded)."""
    return {"candidates": load_json("candidates.json")}


@app.get("/api/trades")
def get_trades():
    """All trades — open and closed."""
    return {
        "strict": load_json("trades_strict.json"),
        "loose": load_json("trades_loose.json"),
    }


@app.get("/api/performance")
def get_performance():
    """Performance stats with live P&L from real option bids."""
    strict_trades = load_json("trades_strict.json")
    loose_trades = load_json("trades_loose.json")

    def calc_stats(trades):
        if not trades:
            return {"total": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0,
                    "win_rate": 0, "total_pnl": 0, "open_pnl": 0, "avg_score": 0, "trades": []}

        open_t = [t for t in trades if t.get("status") == "open"]
        closed_t = [t for t in trades if t.get("status") in ("closed", "expired")]
        wins = sum(1 for t in closed_t if t.get("pnl", 0) > 0)
        losses = len(closed_t) - wins
        total_pnl = sum(t.get("pnl", 0) for t in closed_t)
        avg_score = sum(t.get("score", 0) for t in trades) / len(trades) if trades else 0

        # Use profit_monitor's cached bid data if available, otherwise get stock price
        for t in open_t:
            if t.get("current_option_bid") and t.get("last_check"):
                continue  # already has fresh data from monitor
            try:
                ticker = t["ticker"]
                resp = requests.get(f"{ALPACA_DATA_URL}/stocks/{ticker}/snapshot",
                                    headers=STOCK_HEADERS, timeout=3)
                if resp.status_code == 200:
                    snap = resp.json()
                    trade_data = snap.get("latestTrade", {})
                    price = trade_data.get("p", 0)
                    if price and t.get("entry_price"):
                        if t["direction"] == "CALL":
                            move = (price - t["entry_price"]) / t["entry_price"]
                        else:
                            move = (t["entry_price"] - price) / t["entry_price"]
                        option_pnl = round((move * 0.35 * t["entry_price"]) * 100, 2)
                        t["current_price"] = round(price, 2)
                        t["stock_change_pct"] = round(move * 100, 2)
                        if "current_pnl" not in t or t["current_pnl"] == 0:
                            t["current_pnl"] = option_pnl
            except:
                pass

        open_pnl = sum(t.get("current_pnl", 0) for t in open_t)
        return {
            "total": len(trades),
            "open": len(open_t),
            "closed": len(closed_t),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(closed_t) * 100, 1) if closed_t else 0,
            "total_pnl": round(total_pnl, 2),
            "open_pnl": round(open_pnl, 2),
            "avg_score": round(avg_score, 1),
            "trades": trades,
        }

    strict_stats = calc_stats(strict_trades)
    loose_stats = calc_stats(loose_trades)

    return {
        "strict": strict_stats,
        "loose": loose_stats,
        "combined_pnl": round(strict_stats["total_pnl"] + loose_stats["total_pnl"], 2),
        "total_open": strict_stats["open"] + loose_stats["open"],
    }


@app.get("/api/account")
def account_info():
    """Account balance, exposure, and broker status."""
    from scanner_loose import get_account_balance, get_capital_deployed, MAX_TOTAL_EXPOSURE_PCT
    from account import get_account_summary, get_cash_basis
    
    balance = get_account_balance()
    deployed = get_capital_deployed()
    max_deploy = balance * (MAX_TOTAL_EXPOSURE_PCT / 100)
    cash_basis = get_cash_basis()
    summary = get_account_summary()

    broker = get_account()
    return {
        "account": {
            "cash_basis": round(cash_basis, 2),
            "current_balance": round(balance, 2),
            "total_return": round(balance - cash_basis, 2),
            "total_return_pct": round((balance - cash_basis) / cash_basis * 100, 1) if cash_basis > 0 else 0,
            "capital_deployed": round(deployed, 2),
            "available_for_trades": round(max_deploy - deployed, 2),
            "exposure_pct": round(deployed / balance * 100, 1) if balance > 0 else 0,
        },
        "funding": summary,
        "broker": broker if broker else {"status": "disconnected"},
        "broker_mode": "PAPER" if PAPER_MODE else "LIVE",
    }


@app.post("/api/account/deposit")
def deposit_funds(amount: float, note: str = ""):
    """Add funds to the trading account."""
    from account import deposit
    return deposit(amount, note)


@app.post("/api/account/withdraw")
def withdraw_funds(amount: float, note: str = ""):
    """Withdraw funds from the trading account."""
    from account import withdraw
    return withdraw(amount, note)


@app.post("/api/account/set-balance")
def set_balance(amount: float):
    """Set the starting balance (first-time setup or reset)."""
    from account import set_starting_balance
    return set_starting_balance(amount)


@app.get("/api/health")
def health():
    """Health check — monitor status, last scan, process info."""
    # Check if profit monitor is alive (look at last_check timestamps)
    trades = load_json("trades_loose.json")
    open_trades = [t for t in trades if t.get("status") == "open"]

    monitor_alive = False
    last_monitor_check = None
    for t in open_trades:
        lc = t.get("last_check")
        if lc:
            monitor_alive = True
            if not last_monitor_check or lc > last_monitor_check:
                last_monitor_check = lc

    # Check last scan time from log
    last_scan = None
    try:
        with open(os.path.join(SCANNER_DIR, "scan.log")) as f:
            lines = f.readlines()
            for line in reversed(lines):
                if "Scan complete" in line or "ENTERED" in line:
                    last_scan = line.split("]")[0].strip("[")
                    break
    except:
        pass

    from market_clock import is_market_open
    return {
        "status": "ok",
        "market_open": is_market_open(),
        "profit_monitor_alive": monitor_alive,
        "last_monitor_check": last_monitor_check,
        "last_scan": last_scan,
        "open_positions": len(open_trades),
        "server_time": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/api/scan")
def trigger_scan(user: str = Query(default="sarel")):
    """Manually trigger a scan."""
    from market_clock import is_market_open
    user_dir = get_user_data_dir(user)
    pause_file = os.path.join(user_dir, ".paused")
    if os.path.exists(pause_file):
        return {"error": "Scanner is PAUSED. Unpause first.", "triggered": False}
    if not is_market_open():
        return {"error": "Market is closed", "triggered": False}

    from scanner_loose import run_scan
    picks = run_scan()
    
    # Save scan metadata
    scan_info = {
        "last_scan_time": datetime.utcnow().isoformat() + "Z",
        "picks_found": len(picks),
        "candidates_found": len(load_json("candidates.json")),
    }
    with open(os.path.join(DATA_DIR, "last_scan.json"), "w") as f:
        json.dump(scan_info, f)
    
    return {
        "triggered": True,
        "picks_found": len(picks),
        "picks": picks,
    }


@app.get("/api/last-scan")
def last_scan_info():
    """Get info about the most recent scan."""
    return load_json("last_scan.json", default={"last_scan_time": None, "picks_found": 0, "candidates_found": 0})


@app.post("/api/pause")
def pause_scanner(user: str = Query(default="sarel")):
    """Pause scanner for a specific user."""
    pause_file = os.path.join(get_user_data_dir(user), ".paused")
    with open(pause_file, 'w') as f:
        f.write(f"Manually paused at {datetime.utcnow().isoformat()}")
    return {"paused": True, "user": user, "message": f"{user}'s scanner paused. Open positions still monitored."}


@app.post("/api/unpause")
def unpause_scanner(user: str = Query(default="sarel")):
    """Resume scanner for a specific user."""
    pause_file = os.path.join(get_user_data_dir(user), ".paused")
    crash_file = os.path.join(get_user_data_dir(user), ".crash_warned")
    if os.path.exists(pause_file):
        os.remove(pause_file)
    if os.path.exists(crash_file):
        os.remove(crash_file)
    return {"paused": False, "user": user, "message": f"{user}'s scanner resumed."}


@app.get("/api/status")
def get_status(user: str = Query(default="sarel")):
    """Get pause/crash status for a specific user."""
    user_dir = get_user_data_dir(user)
    pause_file = os.path.join(user_dir, ".paused")
    crash_file = os.path.join(user_dir, ".crash_warned")
    paused = os.path.exists(pause_file)
    crash_warned = os.path.exists(crash_file)
    pause_reason = ""
    if paused:
        try:
            with open(pause_file) as f:
                pause_reason = f.read()
        except:
            pass
    return {
        "paused": paused,
        "crash_warning": crash_warned,
        "pause_reason": pause_reason,
        "user": user,
    }


@app.post("/api/close/{ticker}")
def close_position(ticker: str):
    """Manually close an open position by ticker. Submits real sell order via broker."""
    ticker = ticker.upper()
    trades = load_json("trades_loose.json")
    
    closed_count = 0
    total_pnl = 0
    results = []
    
    for t in trades:
        if t.get("status") == "open" and t.get("ticker") == ticker:
            entry = t.get("option_cost", 0)
            contract_symbol = t.get("contract_symbol")
            
            # Try to get/build contract symbol
            if not contract_symbol:
                contract_symbol = build_occ_symbol(ticker, t["option_exp"], t["direction"], t["option_strike"])
            
            # Execute real sell order
            from broker_alpaca import sell_to_close, get_option_quote, find_contract, PAPER_MODE
            
            # Verify contract
            if not contract_symbol:
                contract = find_contract(ticker, t["option_exp"], t["direction"], t["option_strike"])
                if contract:
                    contract_symbol = contract["symbol"]
            
            if contract_symbol:
                result = sell_to_close(contract_symbol, qty=1)
                
                if result["success"]:
                    fill_price = result["fill_price"]
                    pnl_dollars = round((fill_price - entry) * 100, 2)
                    pnl_pct = round((fill_price - entry) / entry * 100, 1) if entry > 0 else 0
                    
                    t["status"] = "closed"
                    t["exit_reason"] = "MANUAL CLOSE"
                    t["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
                    t["exit_time"] = datetime.utcnow().isoformat()
                    t["exit_fill_price"] = fill_price
                    t["exit_order_id"] = result["order_id"]
                    t["exit_option_bid"] = fill_price
                    t["pnl"] = pnl_dollars
                    t["pnl_pct"] = pnl_pct
                    t["execution_status"] = result["status"]
                    closed_count += 1
                    total_pnl += pnl_dollars
                    results.append({"ticker": ticker, "fill": fill_price, "pnl": pnl_dollars, "status": "filled"})
                else:
                    # Order didn't fill — fall back to marking closed at bid
                    bid = t.get("current_option_bid", 0)
                    pnl_dollars = round((bid - entry) * 100, 2) if bid else 0
                    pnl_pct = round((bid - entry) / entry * 100, 1) if bid and entry > 0 else 0
                    
                    t["status"] = "closed"
                    t["exit_reason"] = f"MANUAL CLOSE (order:{result['status']})"
                    t["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
                    t["exit_time"] = datetime.utcnow().isoformat()
                    t["exit_option_bid"] = bid
                    t["pnl"] = pnl_dollars
                    t["pnl_pct"] = pnl_pct
                    closed_count += 1
                    total_pnl += pnl_dollars
                    results.append({"ticker": ticker, "status": result["status"], "pnl": pnl_dollars})
            else:
                # No contract symbol — close at recorded bid
                bid = t.get("current_option_bid", 0)
                pnl_dollars = round((bid - entry) * 100, 2) if bid else 0
                t["status"] = "closed"
                t["exit_reason"] = "MANUAL CLOSE (no contract)"
                t["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
                t["exit_time"] = datetime.utcnow().isoformat()
                t["exit_option_bid"] = bid
                t["pnl"] = pnl_dollars
                t["pnl_pct"] = round((bid - entry) / entry * 100, 1) if bid and entry > 0 else 0
                closed_count += 1
                total_pnl += pnl_dollars
                results.append({"ticker": ticker, "status": "no_contract", "pnl": pnl_dollars})
    
    if closed_count > 0:
        tmp = os.path.join(SCANNER_DIR, "trades_loose.json.tmp")
        with open(tmp, "w") as f:
            json.dump(trades, f, indent=2)
        os.replace(tmp, os.path.join(SCANNER_DIR, "trades_loose.json"))
    
    return {
        "ticker": ticker,
        "closed": closed_count,
        "total_pnl": round(total_pnl, 2),
        "results": results,
        "message": f"Closed {closed_count} {ticker} position(s) for ${total_pnl:+.2f}" if closed_count > 0 else f"No open positions found for {ticker}",
    }


@app.post("/api/close-all")
def close_all_positions():
    """Manually close ALL open positions via broker."""
    trades = load_json("trades_loose.json")
    from broker_alpaca import sell_to_close, find_contract, PAPER_MODE
    
    closed_count = 0
    total_pnl = 0
    results = []
    
    for t in trades:
        if t.get("status") != "open":
            continue
            
        ticker = t["ticker"]
        entry = t.get("option_cost", 0)
        contract_symbol = t.get("contract_symbol")
        
        if not contract_symbol:
            contract_symbol = build_occ_symbol(ticker, t["option_exp"], t["direction"], t["option_strike"])
        
        if contract_symbol:
            result = sell_to_close(contract_symbol, qty=1)
            
            if result["success"]:
                fill_price = result["fill_price"]
                pnl_dollars = round((fill_price - entry) * 100, 2)
                pnl_pct = round((fill_price - entry) / entry * 100, 1) if entry > 0 else 0
                t["exit_fill_price"] = fill_price
                t["exit_order_id"] = result["order_id"]
                t["execution_status"] = result["status"]
            else:
                bid = t.get("current_option_bid", 0)
                fill_price = bid
                pnl_dollars = round((bid - entry) * 100, 2) if bid else 0
                pnl_pct = round((bid - entry) / entry * 100, 1) if bid and entry > 0 else 0
        else:
            bid = t.get("current_option_bid", 0)
            fill_price = bid
            pnl_dollars = round((bid - entry) * 100, 2) if bid else 0
            pnl_pct = round((bid - entry) / entry * 100, 1) if bid and entry > 0 else 0
        
        t["status"] = "closed"
        t["exit_reason"] = "MANUAL CLOSE ALL"
        t["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
        t["exit_time"] = datetime.utcnow().isoformat()
        t["exit_option_bid"] = fill_price
        t["pnl"] = pnl_dollars
        t["pnl_pct"] = pnl_pct
        closed_count += 1
        total_pnl += pnl_dollars
        results.append({"ticker": ticker, "pnl": pnl_dollars})
    
    if closed_count > 0:
        tmp = os.path.join(SCANNER_DIR, "trades_loose.json.tmp")
        with open(tmp, "w") as f:
            json.dump(trades, f, indent=2)
        os.replace(tmp, os.path.join(SCANNER_DIR, "trades_loose.json"))
    
    return {
        "closed": closed_count,
        "total_pnl": round(total_pnl, 2),
        "results": results,
        "message": f"Closed {closed_count} positions for ${total_pnl:+.2f} total",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
