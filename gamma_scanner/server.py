"""
Gamma Scanner — Standalone API Server
Multi-user support: each user has their own Alpaca account, positions, and P&L.
"""
import os, sys, json, time, requests, hashlib, secrets
from datetime import datetime
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from broker_alpaca import get_option_quote, build_occ_symbol, get_account, PAPER_MODE, HEADERS as ALPACA_HEADERS, sell_to_close, find_contract, buy_to_open

app = FastAPI(title="Gamma Scanner", version="2.0")

# Security — IP allowlist
from starlette.responses import PlainTextResponse, Response

# Auth: password login only (no middleware blocking). Cookie set for convenience but not enforced.
AUTH_COOKIE_NAME = "gamma_session"
AUTH_COOKIE_MAX_AGE = 30 * 24 * 60 * 60

# ---- Whitelist gate middleware (enforced ONLY when LOGIN_GATE_ENABLED=true) ----
# Exemptions prevent the loops/lockouts that broke the previous attempt:
#  - /static, login page ("/"), auth + gate APIs stay open so the login page can load
#    and the admin can always open/lock the gate.
#  - /api/last-scan stays open so the Docker healthcheck keeps the container healthy.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "").lower() == "true"
_GATE_EXEMPT_PREFIXES = ("/static", "/api/auth", "/api/gate", "/favicon")
_GATE_EXEMPT_PATHS = {"/", "/api/last-scan"}


@app.middleware("http")
async def whitelist_gate(request: Request, call_next):
    import auth_gate
    if not auth_gate.gate_enabled():
        return await call_next(request)
    path = request.url.path
    if path in _GATE_EXEMPT_PATHS or path.startswith(_GATE_EXEMPT_PREFIXES):
        return await call_next(request)
    if auth_gate.verify_token(request.cookies.get(auth_gate.COOKIE_NAME)):
        return await call_next(request)
    from starlette.responses import JSONResponse, RedirectResponse
    if path.startswith("/api/"):
        return JSONResponse({"error": "not_whitelisted"}, status_code=401)
    # Pages: redirect to the login page (which is exempt) — never a loop back to a gated page
    return RedirectResponse(url="/", status_code=302)

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


def _apply_broker_creds(user_id):
    """Point broker_alpaca at THIS user's Alpaca account before placing/closing an order.

    The server process's broker_alpaca globals default to the paper account, so any
    manual order (close/buy) must set the per-user creds first or it hits the wrong
    account. Mirrors the override the scanner does before auto-entries. Returns True
    if creds were applied.
    """
    from user_manager import get_user_alpaca_keys, is_paper_user
    import broker_alpaca as _ba
    from broker_alpaca import PAPER_BASE, LIVE_BASE
    key, secret = get_user_alpaca_keys(user_id)
    if not key or not secret:
        return False
    _ba.API_KEY = key
    _ba.API_SECRET = secret
    _ba.HEADERS = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    _ba.HEADERS_JSON = {**_ba.HEADERS, "Content-Type": "application/json"}
    _ba.BASE_URL = PAPER_BASE if is_paper_user(user_id) else LIVE_BASE
    _ba.PAPER_MODE = is_paper_user(user_id)
    return True


# === AUTH ENDPOINTS ===

@app.post("/api/auth/login")
def login(body: dict, request: Request):
    """Password login.

    Gate OFF (legacy): the single shared password from users.json.
    Gate ON: master password always works and enrolls the device; the dashboard
    password enrolls only while the gate is OPEN. A signed whitelist cookie is set.
    """
    import auth_gate
    from starlette.responses import JSONResponse
    users_config = load_users()
    pw = body.get("password", "")
    user_list = [{"id": uid, "name": u["name"]} for uid, u in users_config.get("users", {}).items()]

    if not auth_gate.gate_enabled():
        if pw == users_config.get("password"):
            token = secrets.token_hex(16)
            _active_tokens.add(token)
            response = JSONResponse({"success": True, "token": token, "users": user_list})
            response.set_cookie(
                key=AUTH_COOKIE_NAME, value="authenticated",
                max_age=AUTH_COOKIE_MAX_AGE, httponly=True, samesite="lax",
            )
            return response
        return {"success": False}

    # Gate enabled: two-password model
    is_master = auth_gate.check_master_password(pw)
    is_dash = auth_gate.check_dashboard_password(pw)
    if is_master or (is_dash and auth_gate.gate_is_open()):
        token = auth_gate.issue_token(label=(request.client.host if request.client else ""))
        response = JSONResponse({"success": True, "users": user_list, "admin": is_master})
        response.set_cookie(
            key=auth_gate.COOKIE_NAME, value=token,
            max_age=auth_gate.COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=COOKIE_SECURE,
        )
        return response
    if is_dash and not auth_gate.gate_is_open():
        return {"success": False, "reason": "locked",
                "message": "Access is locked. Ask the admin to open the gate."}
    return {"success": False, "reason": "bad_password"}


# === SECURITY ADMIN ===

@app.post("/api/admin/logout")
def admin_logout():
    """Clear session cookie."""
    from starlette.responses import JSONResponse
    response = JSONResponse({"success": True})
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response

@app.get("/api/admin/gate-status")
def admin_gate_status():
    """Kept for dashboard compatibility — gate concept removed."""
    return {"gate_open": False, "allowed_ips": {}}


@app.post("/api/auth/logout")
def logout(token: str = ""):
    _active_tokens.discard(token)
    return {"success": True}


# ---- Gate control (master password) ----
@app.get("/api/gate/status")
def gate_status_ep():
    import auth_gate
    return {"enabled": auth_gate.gate_enabled(), **auth_gate.gate_status()}


@app.get("/api/session")
def session_check():
    """Gated route: reaching it (200) means the request is already authorized — a valid
    whitelist cookie, or the gate is off. The login page uses this to auto-forward
    returning users to the dashboard without a blind redirect loop."""
    import auth_gate
    return {"ok": True, "gate_enabled": auth_gate.gate_enabled()}


@app.post("/api/gate/open")
def gate_open_ep(body: dict):
    import auth_gate
    if not auth_gate.check_master_password(body.get("password", "")):
        return {"success": False, "error": "Master password required"}
    try:
        hours = float(body.get("hours") or auth_gate.OPEN_HOURS)
    except Exception:
        hours = auth_gate.OPEN_HOURS
    hours = max(0.1, min(72.0, hours))
    return {"success": True, **auth_gate.open_gate(hours=hours)}


@app.post("/api/gate/lock")
def gate_lock_ep(body: dict):
    import auth_gate
    if not auth_gate.check_master_password(body.get("password", "")):
        return {"success": False, "error": "Master password required"}
    return {"success": True, **auth_gate.lock_gate()}


@app.post("/api/gate/devices")
def gate_devices_ep(body: dict):
    import auth_gate
    if not auth_gate.check_master_password(body.get("password", "")):
        return {"success": False, "error": "Master password required"}
    devices = [
        {"token": tok, "label": (m.get("label") or "unknown"),
         "created": m.get("created"), "last_seen": m.get("last_seen")}
        for tok, m in auth_gate.list_tokens().items()
    ]
    devices.sort(key=lambda d: d.get("last_seen") or 0, reverse=True)
    return {"success": True, "count": len(devices), "devices": devices}


@app.post("/api/gate/revoke")
def gate_revoke_ep(body: dict):
    import auth_gate
    if not auth_gate.check_master_password(body.get("password", "")):
        return {"success": False, "error": "Master password required"}
    return {"success": auth_gate.revoke_token(body.get("token", ""))}


@app.post("/api/gate/revoke-all")
def gate_revoke_all_ep(body: dict):
    import auth_gate
    if not auth_gate.check_master_password(body.get("password", "")):
        return {"success": False, "error": "Master password required"}
    return {"success": True, "revoked": auth_gate.revoke_all()}


# === PAGES ===

@app.get("/", response_class=HTMLResponse)
def login_page():
    """Serve login page."""
    with open(os.path.join(SCANNER_DIR, "static/login.html")) as f:
        return f.read()


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """Serve the main dashboard."""
    with open(os.path.join(SCANNER_DIR, "static/dashboard_v2.html")) as f:
        return f.read()

@app.get("/dashboard-old", response_class=HTMLResponse)
def dashboard_old():
    """Original dashboard (fallback)."""
    with open(os.path.join(SCANNER_DIR, "static/index.html")) as f:
        return f.read()

def load_json(filename, default=None):
    # Check DATA_DIR first, then SCANNER_DIR
    for dir in [DATA_DIR, SCANNER_DIR]:
        path = os.path.join(dir, filename)
        try:
            with open(path) as f:
                return json.load(f)
        except:
            continue
    return default if default is not None else []




@app.get("/api/picks")
def get_picks():
    """Today's scanner picks from both strict and loose scanners."""
    return {
        "strict": load_json("picks_strict.json"),
        "loose": load_json("picks_loose.json"),
    }


@app.get("/api/users")
def list_users():
    """Accounts available for manual actions (used to populate the Buy account selector)."""
    users = load_users().get("users", {})
    return {"users": [
        {"id": uid, "name": u.get("name", uid), "paper": bool(u.get("paper", False))}
        for uid, u in users.items()
    ]}


@app.post("/api/picks/buy/{ticker}")
def buy_from_picks(ticker: str, user: str = Query(...)):
    """Manually buy one of today's picks on a chosen account, if that account can afford it.

    Funds are checked against the account's available balance, so an attempt on an
    underfunded account (e.g. yonah) fails cleanly without placing an order.
    """
    ticker = ticker.upper()
    users = load_users().get("users", {})
    if user not in users:
        return {"success": False, "error": f"Unknown account '{user}'"}

    from market_clock import is_market_open
    if not is_market_open():
        return {"success": False, "error": "Market is closed"}

    # Locate the pick (loose first, then strict)
    picks = (load_json("picks_loose.json") or []) + (load_json("picks_strict.json") or [])
    pick = next((p for p in picks if str(p.get("ticker", "")).upper() == ticker), None)
    if not pick:
        return {"success": False, "error": f"{ticker} is not in today's picks"}

    opt = pick.get("option") or {}
    strike = opt.get("strike", pick.get("option_strike"))
    exp = opt.get("expiration", pick.get("option_exp"))
    direction = pick.get("direction", "CALL")
    if not strike or not exp:
        return {"success": False, "error": f"{ticker} pick has no option contract"}

    # Point the broker at the chosen account BEFORE quoting/ordering
    if not _apply_broker_creds(user):
        return {"success": False, "error": f"No Alpaca keys configured for '{user}'"}

    from user_manager import get_user_balance, get_user_deployed, load_user_trades, save_user_trades, is_paper_user
    from broker_alpaca import get_option_quote, build_occ_symbol, buy_to_open

    # Fresh quote for a real cost estimate
    symbol = build_occ_symbol(ticker, exp, direction, strike)
    quote = get_option_quote(symbol)
    if not quote or quote.get("ask", 0) <= 0:
        return {"success": False, "error": "Can't get a current option price (no market right now)"}
    ask = quote["ask"]
    cost = round(ask * 100, 2)

    # Funds check — this is what makes an underfunded account fail cleanly
    available = get_user_balance(user) - get_user_deployed(user)
    acct_name = users[user].get("name", user)
    if cost > available:
        return {"success": False, "error": f"{acct_name} can't afford this: need ${cost:.0f}, have ${available:.0f} available"}

    # Guard against an exact-duplicate open contract (avoids accidental double-buys)
    trades = load_user_trades(user)
    for t in trades:
        if (t.get("status") == "open" and t.get("ticker") == ticker
                and t.get("option_strike") == strike and t.get("option_exp") == exp):
            return {"success": False, "error": f"{acct_name} already holds {ticker} ${strike} {exp}"}

    # Place the order on the selected account
    result = buy_to_open(ticker, exp, direction, strike)
    if not result.get("success"):
        return {"success": False, "error": f"Order failed: {result.get('status')}"}

    fill = result["fill_price"]
    trade = {
        "ticker": ticker,
        "direction": direction,
        "setup": pick.get("setup", "manual_pick"),
        "score": pick.get("score", 0),
        "entry_price": pick.get("price", 0),
        "entry_date": datetime.utcnow().strftime("%Y-%m-%d"),
        "entry_time": datetime.utcnow().isoformat(),
        "option_strike": strike,
        "option_exp": exp,
        "option_cost": round(fill, 2),
        "cost_per_contract": round(fill * 100, 2),
        "status": "open",
        "pnl": 0,
        "current_pnl": 0.0,
        "order_id": result.get("order_id"),
        "execution": "paper" if is_paper_user(user) else "live",
        "manual_entry": True,
    }
    trades.append(trade)
    save_user_trades(user, trades)
    return {
        "success": True,
        "message": f"Bought {ticker} {direction} ${strike} for {acct_name} @ ${fill:.2f} (${round(fill*100):.0f})",
        "account": user,
        "fill": fill,
        "cost": round(fill * 100, 2),
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


@app.get("/api/rescore")
def rescore_positions(user: str = Query(default="sarel")):
    """Current 'would-pick-today' score (definition A) for each open position's ticker.

    Re-runs the scanner's own evaluate + option-scoring on today's data, so the number is
    comparable to the stored entry score. 'no setup' means the scanner would not pick that
    ticker right now (e.g. it bounced out of oversold) or there's no tradeable option.
    """
    from user_manager import load_user_trades
    from scanner_loose import score_tickers_now
    trades = load_user_trades(user)
    tickers = [t.get("ticker") for t in trades if t.get("status") == "open" and t.get("ticker")]
    scored = score_tickers_now(tickers)
    return {"scores": {tk: {"score": v.get("score"), "reason": v.get("reason")} for tk, v in scored.items()}}


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
    from user_manager import get_user_balance, get_user_deployed, load_user_account, get_user_alpaca_keys, is_paper_user
    from scanner_loose import MAX_TOTAL_EXPOSURE_PCT
    from broker_alpaca import PAPER_BASE, LIVE_BASE
    
    balance = get_user_balance(user)
    deployed = get_user_deployed(user)
    max_deploy = balance * (MAX_TOTAL_EXPOSURE_PCT / 100) if balance > 0 else 0
    account = load_user_account(user)
    base = account.get("starting_balance", 0)
    deposits = sum(t["amount"] for t in account.get("transactions", []) if t["type"] == "deposit")
    withdrawals = sum(t["amount"] for t in account.get("transactions", []) if t["type"] == "withdrawal")
    cash_basis = base + deposits - withdrawals

    # Get broker info using per-user keys
    broker_equity = 0
    broker_buying_power = 0
    broker_cash = 0
    try:
        key, secret = get_user_alpaca_keys(user)
        if key:
            base_url = PAPER_BASE if is_paper_user(user) else LIVE_BASE
            headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
            resp = requests.get(f"{base_url}/v2/account", headers=headers, timeout=5)
            if resp.status_code == 200:
                b = resp.json()
                broker_equity = float(b.get("equity", 0))
                broker_buying_power = float(b.get("buying_power", 0))
                broker_cash = float(b.get("cash", 0))
    except:
        pass

    return {
        "balance": round(balance, 2),
        "deployed": round(deployed, 2),
        "available": round(max_deploy - deployed, 2),
        "cash_basis": round(cash_basis, 2),
        "broker_equity": round(broker_equity, 2),
        "broker_buying_power": round(broker_buying_power, 2),
        "broker_cash": round(broker_cash, 2),
        "transactions": account.get("transactions", []),
    }


@app.post("/api/account/deposit")
def deposit_funds(amount: float, note: str = "", user: str = Query(default="sarel")):
    """Add funds to a user's account. Validates against Alpaca's real balance."""
    if amount <= 0:
        return {"success": False, "error": "Amount must be positive"}
    
    # Validate: don't allow recording more than Alpaca actually has
    try:
        from user_manager import get_user_alpaca_keys, is_paper_user
        from broker_alpaca import PAPER_BASE, LIVE_BASE
        key, secret = get_user_alpaca_keys(user)
        if key and not is_paper_user(user):
            base = LIVE_BASE
            headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
            resp = requests.get(f"{base}/v2/account", headers=headers, timeout=5)
            if resp.status_code == 200:
                acct = resp.json()
                equity = float(acct.get("equity", 0))
                cash = float(acct.get("cash", 0))
                if amount > cash:
                    return {"success": False, "error": f"Alpaca only has ${cash:.2f} cash. Can't record ${amount:.2f} deposit."}
    except:
        pass  # If check fails, allow (don't block on network issues)
    
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


@app.get("/api/daily-move")
def daily_move(user: str = Query(default="scanner")):
    """Today's P&L move calculated from Alpaca's lastday_price (yesterday's close)."""
    try:
        from user_manager import get_user_alpaca_keys, is_paper_user
        from broker_alpaca import PAPER_BASE, LIVE_BASE
        key, secret = get_user_alpaca_keys(user)
        if not key:
            return {"total": 0, "details": [], "message": "No Alpaca keys configured"}
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        base = PAPER_BASE if is_paper_user(user) else LIVE_BASE
        resp = requests.get(f"{base}/v2/positions", headers=headers, timeout=5)
        if resp.status_code != 200:
            return {"error": "Can't fetch positions"}
        positions = resp.json()
        total = 0
        details = []
        for p in positions:
            qty = int(p["qty"])
            current = float(p["current_price"])
            lastday = float(p.get("lastday_price", current))
            multiplier = 100 if p.get("asset_class") == "us_option" else 1
            move = (current - lastday) * qty * multiplier
            total += move
            details.append({"symbol": p["symbol"][:4], "move": round(move, 0)})
        return {"total": round(total, 0), "details": details}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/alpaca-pnl")
def alpaca_pnl(user: str = Query(default="scanner")):
    """Accurate P&L directly from Alpaca account."""
    try:
        from user_manager import get_user_alpaca_keys, is_paper_user
        from broker_alpaca import PAPER_BASE, LIVE_BASE
        key, secret = get_user_alpaca_keys(user)
        if not key:
            return {"unrealized_pl": 0, "cost_basis": 0, "market_value": 0, "positions": 0, "message": "No Alpaca keys"}
        headers = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        base = PAPER_BASE if is_paper_user(user) else LIVE_BASE
        resp = requests.get(f"{base}/v2/positions", headers=headers, timeout=5)
        if resp.status_code != 200:
            return {"error": "Can't fetch positions"}
        positions = resp.json()
        unrealized = sum(float(p.get("unrealized_pl", 0)) for p in positions)
        cost_basis = sum(float(p.get("cost_basis", 0)) for p in positions)
        market_value = sum(float(p.get("market_value", 0)) for p in positions)
        return {
            "unrealized_pl": round(unrealized, 2),
            "cost_basis": round(cost_basis, 2),
            "market_value": round(market_value, 2),
            "positions": len(positions),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/spy-context")
def spy_context():
    """SPY price, daily change, and 5-day trend for market context."""
    try:
        from datetime import timedelta
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
    
    # Save scan metadata — read candidates from where scanner writes them
    candidates = load_json("candidates.json")
    scan_info = {
        "last_scan_time": datetime.utcnow().isoformat() + "Z",
        "picks_found": len(picks) if picks else 0,
        "candidates_found": len(candidates),
    }
    with open(os.path.join(DATA_DIR, "last_scan.json"), "w") as f:
        json.dump(scan_info, f)
    # Also copy candidates to DATA_DIR for consistency
    with open(os.path.join(DATA_DIR, "candidates.json"), "w") as f:
        json.dump(candidates, f)
    
    return {
        "triggered": True,
        "picks_found": len(picks) if picks else 0,
        "candidates_found": len(candidates),
        "message": f"Scan complete: {len(picks) if picks else 0} picks, {len(candidates)} candidates screened",
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
    _apply_broker_creds(user)  # ensure the sell hits THIS user's account (live vs paper)
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
    _apply_broker_creds(user)  # ensure sells hit THIS user's account (live vs paper)
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
    
    # Live execution: place real order
    if os.environ.get("LIVE_EXECUTION") == "true":
        result = buy_to_open(ticker, open_position["option_exp"], open_position["direction"], open_position["option_strike"])
        if not result["success"]:
            return {"error": f"Order failed: {result['status']}", "success": False}
        new_trade["option_cost"] = result["fill_price"]
        new_trade["cost_per_contract"] = round(result["fill_price"] * 100, 2)
        new_trade["order_id"] = result["order_id"]
        new_trade["execution"] = "live"
    else:
        new_trade["execution"] = "paper"
    
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
    _apply_broker_creds(user)  # ensure the buy hits THIS user's account (live vs paper)
    from trade_queue import load_queue, save_queue
    from user_manager import load_user_trades, save_user_trades, get_user_balance, get_user_deployed
    from broker_alpaca import get_option_quote
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
    
    # Live execution
    if os.environ.get("LIVE_EXECUTION") == "true":
        result = buy_to_open(ticker, target["option_exp"], target.get("direction", "CALL"), target["option_strike"])
        if not result["success"]:
            return {"error": f"Order failed: {result['status']}", "success": False}
        trade["option_cost"] = result["fill_price"]
        trade["cost_per_contract"] = round(result["fill_price"] * 100, 2)
        trade["order_id"] = result["order_id"]
        from user_manager import is_paper_user
        trade["execution"] = "paper" if is_paper_user(user) else "live"
    else:
        trade["execution"] = "paper"
    
    trades.append(trade)
    save_user_trades(user, trades)
    
    # Remove from queue
    queue.pop(target_idx)
    save_queue(user, queue)
    
    return {"success": True, "ticker": ticker, "cost": trade["cost_per_contract"], "message": f"Bought {ticker} at ${trade['option_cost']:.2f}"}


@app.post("/api/sync-alpaca")
def sync_from_alpaca(user: str = Query(default="sarel")):
    """
    Sync open positions from Alpaca into local trades.
    Use this to recover after a data wipe.
    Only adds positions that aren't already tracked.
    """
    from user_manager import load_user_trades, save_user_trades, get_user_alpaca_keys, load_users_config
    from broker_alpaca import PAPER_MODE
    import requests as req
    
    key, secret = get_user_alpaca_keys(user)
    if not key:
        key = ALPACA_API_KEY
        secret = ALPACA_SECRET_KEY
    
    base = "https://paper-api.alpaca.markets" if PAPER_MODE else "https://api.alpaca.markets"
    hdrs = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    
    try:
        resp = req.get(f"{base}/v2/positions", headers=hdrs, timeout=10)
        if resp.status_code != 200:
            return {"error": f"Alpaca returned {resp.status_code}", "success": False}
        
        positions = resp.json()
        option_positions = [p for p in positions if p.get("asset_class") == "us_option"]
        
        if not option_positions:
            return {"success": True, "synced": 0, "message": "No open option positions on Alpaca"}
        
        trades = load_user_trades(user)
        existing_symbols = set()
        for t in trades:
            if t.get("status") == "open":
                # Build OCC symbol for comparison
                from broker_alpaca import build_occ_symbol
                sym = build_occ_symbol(t["ticker"], t["option_exp"], t["direction"], t["option_strike"])
                existing_symbols.add(sym)
        
        synced = 0
        for pos in option_positions:
            symbol = pos.get("symbol", "")
            if symbol in existing_symbols:
                continue  # already tracking this
            
            # Parse OCC symbol: AAPL260718C00150000
            try:
                import re
                m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', symbol)
                if not m:
                    continue
                ticker = m.group(1)
                exp_raw = m.group(2)  # YYMMDD
                exp_date = f"20{exp_raw[:2]}-{exp_raw[2:4]}-{exp_raw[4:6]}"
                direction = "CALL" if m.group(3) == "C" else "PUT"
                strike = int(m.group(4)) / 1000
                
                avg_entry = float(pos.get("avg_entry_price", 0))
                qty = int(pos.get("qty", 1))
                market_val = float(pos.get("market_value", 0))
                
                trade = {
                    "ticker": ticker,
                    "direction": direction,
                    "setup": "alpaca_sync",
                    "score": 0,
                    "entry_price": 0,
                    "entry_date": datetime.utcnow().strftime("%Y-%m-%d"),
                    "entry_time": datetime.utcnow().isoformat(),
                    "option_strike": strike,
                    "option_exp": exp_date,
                    "option_cost": avg_entry,
                    "cost_per_contract": round(avg_entry * 100 * qty, 2),
                    "status": "open",
                    "pnl": 0,
                    "current_pnl": 0.0,
                    "synced_from_alpaca": True,
                    "alpaca_symbol": symbol,
                    "qty": qty,
                }
                trades.append(trade)
                synced += 1
            except:
                continue
        
        if synced > 0:
            save_user_trades(user, trades)
        
        return {
            "success": True,
            "synced": synced,
            "total_alpaca_positions": len(option_positions),
            "message": f"Synced {synced} new positions from Alpaca",
        }
    except Exception as e:
        return {"error": str(e), "success": False}


# Static files (must be last — catches all unmatched paths)
app.mount("/static", StaticFiles(directory=os.path.join(SCANNER_DIR, "static")), name="static")
