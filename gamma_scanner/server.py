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
def get_trades(user: str = Query(default="sarel")):
    """All trades for a specific user."""
    from user_manager import load_user_trades
    trades = load_user_trades(user)
    return {
        "strict": [],
        "loose": trades,
    }


@app.get("/api/performance")
def get_performance(user: str = Query(default="sarel")):
    """Performance stats for a specific user."""
    from user_manager import load_user_trades
    strict_trades = []
    loose_trades = load_user_trades(user)

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
def account_info(user: str = Query(default="sarel")):
    """Account balance for a specific user."""
    from user_manager import get_user_balance, get_user_deployed, load_user_account
    from scanner_loose import MAX_TOTAL_EXPOSURE_PCT
    
    balance = get_user_balance(user)
    deployed = get_user_deployed(user)
    max_deploy = balance * (MAX_TOTAL_EXPOSURE_PCT / 100) if balance > 0 else 0
    account = load_user_account(user)
    base = account.get("starting_balance", 0)
    deposits = sum(t["amount"] for t in account.get("transactions", []) if t["type"] == "deposit")
    withdrawals = sum(t["amount"] for t in account.get("transactions", []) if t["type"] == "withdrawal")
    cash_basis = base + deposits - withdrawals

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
        "funding": account,
        "broker": broker if broker else {"status": "disconnected"},
        "broker_mode": "PAPER" if PAPER_MODE else "LIVE",
    }


@app.post("/api/account/deposit")
def deposit_funds(amount: float, note: str = "", user: str = Query(default="sarel")):
    """Add funds to a user's account."""
    from user_manager import load_user_account, save_user_account
    account = load_user_account(user)
    account["transactions"].append({"type": "deposit", "amount": round(amount, 2), "date": datetime.utcnow().isoformat(), "note": note or f"Deposit ${amount:.2f}"})
    save_user_account(user, account)
    from user_manager import get_user_balance
    return {"success": True, "deposited": amount, "new_balance": round(get_user_balance(user), 2)}


@app.post("/api/account/withdraw")
def withdraw_funds(amount: float, note: str = "", user: str = Query(default="sarel")):
    """Withdraw funds from a user's account."""
    from user_manager import load_user_account, save_user_account, get_user_balance
    account = load_user_account(user)
    account["transactions"].append({"type": "withdrawal", "amount": round(amount, 2), "date": datetime.utcnow().isoformat(), "note": note or f"Withdrawal ${amount:.2f}"})
    save_user_account(user, account)
    return {"success": True, "withdrawn": amount, "new_balance": round(get_user_balance(user), 2)}


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
def close_position(ticker: str, user: str = Query(default="sarel")):
    """Manually close an open position for a specific user."""
    ticker = ticker.upper()
    from user_manager import load_user_trades, save_user_trades
    trades = load_user_trades(user)
    
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
        save_user_trades(user, trades)






    
    return {
        "ticker": ticker,
        "closed": closed_count,
        "total_pnl": round(total_pnl, 2),
        "results": results,
        "message": f"Closed {closed_count} {ticker} position(s) for ${total_pnl:+.2f}" if closed_count > 0 else f"No open positions found for {ticker}",
    }


@app.post("/api/close-all")
def close_all_positions(user: str = Query(default="sarel")):
    """Manually close ALL open positions for a user."""
    from user_manager import load_user_trades, save_user_trades
    trades = load_user_trades(user)
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
        save_user_trades(user, trades)
    
    return {
        "closed": closed_count,
        "total_pnl": round(total_pnl, 2),
        "results": results,
        "message": f"Closed {closed_count} positions for ${total_pnl:+.2f} total",
    }


@app.post("/api/add-contract/{ticker}")
def add_contract(ticker: str, user: str = Query(default="sarel")):
    """Buy another contract of an existing open position."""
    ticker = ticker.upper()
    from user_manager import load_user_trades, save_user_trades, get_user_balance, get_user_deployed
    trades = load_user_trades(user)
    
    # Find the open position for this ticker
    open_position = None
    for t in trades:
        if t.get("status") == "open" and t.get("ticker") == ticker:
            open_position = t
            break
    
    if not open_position:
        return {"error": f"No open position found for {ticker}", "success": False}
    
    # Check if user can afford another contract
    balance = get_user_balance(user)
    deployed = get_user_deployed(user)
    available = balance - deployed
    cost = open_position.get("cost_per_contract", 0)
    
    if cost > available:
        # Can't afford — add to front of queue (priority)
        from trade_queue import add_to_queue
        add_to_queue(user, {
            "ticker": ticker,
            "direction": open_position["direction"],
            "setup": "manual_add",
            "score": open_position.get("score", 0),
            "entry_price": open_position.get("current_price", open_position["entry_price"]),
            "option_strike": open_position["option_strike"],
            "option_exp": open_position["option_exp"],
            "option_cost": open_position.get("current_option_bid", open_position["option_cost"]),
        }, priority=True)
        return {"success": True, "queued": True, "message": f"{ticker} added to queue (need ${cost:.0f}, have ${available:.0f}). Will fill when funds available."}
    
    # Create a new trade entry duplicating the position
    from datetime import datetime
    now = datetime.now()
    new_trade = {
        "ticker": ticker,
        "direction": open_position["direction"],
        "setup": open_position.get("setup", "manual_add"),
        "score": open_position.get("score", 0),
        "entry_price": open_position.get("current_price", open_position["entry_price"]),
        "entry_date": now.strftime("%Y-%m-%d"),
        "entry_time": now.isoformat(),
        "option_strike": open_position["option_strike"],
        "option_exp": open_position["option_exp"],
        "option_cost": open_position.get("current_option_bid", open_position["option_cost"]),
        "cost_per_contract": round(open_position.get("current_option_bid", open_position["option_cost"]) * 100, 2),
        "status": "open",
        "pnl": 0,
        "current_pnl": 0.0,
        "added_to_position": True,
    }
    trades.append(new_trade)
    save_user_trades(user, trades)
    
    return {
        "success": True,
        "ticker": ticker,
        "cost": new_trade["cost_per_contract"],
        "message": f"Added 1 contract of {ticker} at ${new_trade['option_cost']:.2f}",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)


@app.get("/api/queue")
def get_queue(user: str = Query(default="sarel")):
    """Get user's trade queue."""
    from trade_queue import load_queue
    return {"queue": load_queue(user)}


@app.post("/api/queue/buy/{ticker}")
def buy_from_queue(ticker: str, user: str = Query(default="sarel")):
    """Manually buy a queued trade."""
    ticker = ticker.upper()
    from trade_queue import load_queue, save_queue
    from user_manager import load_user_trades, save_user_trades, get_user_balance, get_user_deployed
    from data_alpaca import get_option_quote
    from broker_alpaca import build_occ_symbol
    
    queue = load_queue(user)
    target = None
    target_idx = None
    for i, item in enumerate(queue):
        if item.get("ticker") == ticker:
            target = item
            target_idx = i
            break
    
    if not target:
        return {"error": f"{ticker} not in queue", "success": False}
    
    # Get fresh quote
    symbol = build_occ_symbol(ticker, target["option_exp"], target.get("direction", "CALL"), target["option_strike"])
    quote = get_option_quote(symbol)
    
    if not quote or quote.get("ask", 0) <= 0:
        return {"error": "Can't get current price", "success": False}
    
    current_ask = quote["ask"]
    cost = round(current_ask * 100, 2)
    
    # Check funds
    balance = get_user_balance(user)
    deployed = get_user_deployed(user)
    available = balance - deployed
    
    if cost > available:
        return {"error": f"Need ${cost:.0f}, have ${available:.0f} available", "success": False}
    
    # Enter trade
    from datetime import datetime
    trades = load_user_trades(user)
    trade = {
        "ticker": ticker,
        "direction": target.get("direction", "CALL"),
        "setup": target.get("setup", "queued"),
        "score": target.get("score", 0),
        "entry_price": target.get("entry_price", 0),
        "entry_date": datetime.now().strftime("%Y-%m-%d"),
        "entry_time": datetime.now().isoformat(),
        "option_strike": target["option_strike"],
        "option_exp": target["option_exp"],
        "option_cost": round(current_ask, 2),
        "cost_per_contract": cost,
        "status": "open",
        "pnl": 0,
        "current_pnl": 0.0,
        "from_queue": True,
    }
    trades.append(trade)
    save_user_trades(user, trades)
    
    # Remove from queue
    queue.pop(target_idx)
    save_queue(user, queue)
    
    return {"success": True, "ticker": ticker, "cost": cost, "message": f"Bought {ticker} at ${current_ask:.2f}"}
