"""
Reversal Put — Quick put trades after trailing stop exits.

Strategy:
- When the main scanner sells a winning call via trailing stop
- Immediately buy an ATM put on the same ticker
- Target: +30% profit (sell as soon as hit)
- Stop: -30% or 3 days (whichever comes first)
- Only on "big" exits (call high water > 150%)

This runs on the paper account but tracks separately in user_puts/trades.json.
"""

import os
import json
from datetime import datetime

try:
    from config import DATA_DIR
except:
    DATA_DIR = os.environ.get("GAMMA_DATA_DIR", "/app/data")

from user_manager import get_user_dir

PUT_USER = "puts"
PROFIT_TARGET_PCT = 30
STOP_LOSS_PCT = -30
MAX_HOLD_DAYS = 3
MIN_CALL_HW = 150  # Only enter put if call high water was > 150%


def should_enter_put(trade: dict) -> bool:
    """Decide if this call exit qualifies for a reversal put."""
    # Only on trailing stop exits (not expirations)
    if "TRAILING" not in (trade.get("exit_reason") or ""):
        return False
    
    # Only if the call had a big spike
    hw = trade.get("high_water_pct", 0)
    if hw < MIN_CALL_HW:
        return False
    
    # Must be a CALL that won (we're reversing the direction)
    if trade.get("direction") != "CALL":
        return False
    
    return True


def enter_reversal_put(trade: dict, log_fn=None):
    """
    Enter a put trade on the same ticker after a winning call exits.
    Uses the paper Alpaca account.
    
    Args:
        trade: The closed call trade dict
        log_fn: Optional logging function
    """
    def log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(f"[PUTS] {msg}")
    
    ticker = trade["ticker"]
    strike = trade["option_strike"]
    expiration = trade.get("option_exp")
    
    if not expiration:
        log(f"  No expiration for {ticker} put — skipping")
        return
    
    log(f"  🔄 REVERSAL PUT: {ticker} — call exited at +{trade.get('pnl_pct', 0):.0f}% (hw: +{trade.get('high_water_pct', 0):.0f}%)")
    
    # Place the put order using paper account
    import os as _os
    if _os.environ.get("LIVE_EXECUTION") == "true":
        try:
            import broker_alpaca as _ba
            from user_manager import get_user_alpaca_keys, is_paper_user
            from broker_alpaca import buy_to_open, PAPER_BASE
            from data_alpaca import get_option_expirations, get_option_chain
            
            # Use paper keys for puts
            key, secret = get_user_alpaca_keys(PUT_USER)
            if key:
                _ba.API_KEY = key
                _ba.API_SECRET = secret
                _ba.HEADERS = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
                _ba.HEADERS_JSON = {**_ba.HEADERS, "Content-Type": "application/json"}
                _ba.BASE_URL = PAPER_BASE
            
            # Get current stock price for ATM strike
            import requests as _req
            snap = _req.get(f"https://data.alpaca.markets/v2/stocks/{ticker}/snapshot",
                headers={"APCA-API-KEY-ID": _ba.API_KEY, "APCA-API-SECRET-KEY": _ba.API_SECRET}, timeout=5)
            if snap.status_code != 200:
                log(f"  PUT FAILED: Can't get stock price for {ticker}")
                return
            current_price = float(snap.json().get("latestTrade", {}).get("p", 0))
            if not current_price:
                log(f"  PUT FAILED: No price for {ticker}")
                return
            
            # Find ATM strike with 14-28 DTE expiration
            put_strike = round(current_price)  # nearest dollar = ATM
            expirations = get_option_expirations(ticker, min_days=14, max_days=28)
            if not expirations:
                # Fallback: try 7-35 day window
                expirations = get_option_expirations(ticker, min_days=7, max_days=35)
            if not expirations:
                log(f"  PUT FAILED: No valid expirations for {ticker}")
                return
            put_exp = expirations[0]  # nearest valid expiration
            
            log(f"  PUT: {ticker} ATM ${put_strike} exp {put_exp} (stock at ${current_price:.2f})")
            result = buy_to_open(ticker, put_exp, "PUT", put_strike)
            
            if not result["success"]:
                log(f"  PUT ORDER FAILED: {ticker} — {result['status']}")
                return
            
            fill_price = result["fill_price"]
            log(f"  ✅ PUT FILLED: {ticker} PUT ${put_strike} @ ${fill_price:.2f}")
            
            # Record the put trade
            put_trade = {
                "ticker": ticker,
                "direction": "PUT",
                "setup": "reversal_put",
                "score": 0,
                "entry_price": current_price,
                "entry_date": datetime.utcnow().strftime("%Y-%m-%d"),
                "entry_time": datetime.utcnow().isoformat(),
                "option_strike": put_strike,
                "option_exp": put_exp,
                "option_cost": fill_price,
                "cost_per_contract": round(fill_price * 100, 2),
                "status": "open",
                "pnl": 0,
                "current_pnl": 0.0,
                "order_id": result["order_id"],
                "execution": "live",
                "profit_target_pct": PROFIT_TARGET_PCT,
                "stop_loss_pct": STOP_LOSS_PCT,
                "max_hold_days": MAX_HOLD_DAYS,
                "source_call_hw": trade.get("high_water_pct", 0),
                "source_call_pnl": trade.get("pnl_pct", 0),
            }
            
            # Save to puts user trades
            put_trades = load_put_trades()
            put_trades.append(put_trade)
            save_put_trades(put_trades)
            
        except Exception as e:
            log(f"  PUT ERROR: {e}")
    else:
        # Paper tracking only (no Alpaca order)
        put_trade = {
            "ticker": ticker,
            "direction": "PUT",
            "setup": "reversal_put",
            "entry_price": trade.get("current_price", trade.get("entry_price", 0)),
            "entry_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "entry_time": datetime.utcnow().isoformat(),
            "option_strike": strike,
            "option_exp": expiration,
            "option_cost": trade.get("exit_option_bid", 3.0),  # estimate
            "cost_per_contract": round(trade.get("exit_option_bid", 3.0) * 100, 2),
            "status": "open",
            "pnl": 0,
            "current_pnl": 0.0,
            "execution": "paper",
            "profit_target_pct": PROFIT_TARGET_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "max_hold_days": MAX_HOLD_DAYS,
            "source_call_hw": trade.get("high_water_pct", 0),
            "source_call_pnl": trade.get("pnl_pct", 0),
        }
        put_trades = load_put_trades()
        put_trades.append(put_trade)
        save_put_trades(put_trades)
        log(f"  📝 PUT RECORDED (paper): {ticker} PUT ${strike}")


def check_put_exits(log_fn=None):
    """
    Check open put positions for exit conditions:
    - Hit +30% profit target → SELL
    - Hit -30% stop loss → SELL
    - Held 3+ days → SELL
    """
    def log(msg):
        if log_fn:
            log_fn(msg)
    
    # Set broker to paper account for puts
    try:
        import broker_alpaca as _ba
        from user_manager import get_user_alpaca_keys
        from broker_alpaca import PAPER_BASE
        key, secret = get_user_alpaca_keys(PUT_USER)
        if key:
            _ba.API_KEY = key
            _ba.API_SECRET = secret
            _ba.HEADERS = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
            _ba.HEADERS_JSON = {**_ba.HEADERS, "Content-Type": "application/json"}
            _ba.BASE_URL = PAPER_BASE
    except:
        pass
    
    put_trades = load_put_trades()
    modified = False
    
    for trade in put_trades:
        if trade.get("status") != "open":
            continue
        
        ticker = trade["ticker"]
        entry_cost = trade["option_cost"]
        
        # Fetch current bid
        try:
            from broker_alpaca import get_option_quote, build_occ_symbol
            symbol = build_occ_symbol(ticker, trade["option_exp"], "PUT", trade["option_strike"])
            quote = get_option_quote(symbol)
            if quote:
                bid = quote["bid"]
                trade["current_option_bid"] = bid
                trade["last_check"] = datetime.utcnow().isoformat()
            else:
                bid = trade.get("current_option_bid")
        except:
            bid = trade.get("current_option_bid")
        
        if not bid or not entry_cost:
            continue
        
        pnl_pct = (bid - entry_cost) / entry_cost * 100 if entry_cost > 0 else 0
        trade["current_pnl"] = round((bid - entry_cost) * 100, 2)
        
        # Check days held
        try:
            entry_date = datetime.strptime(trade["entry_date"], "%Y-%m-%d").date()
            days_held = (datetime.utcnow().date() - entry_date).days
        except:
            days_held = 0
        
        should_exit = False
        exit_reason = ""
        
        # Profit target
        if pnl_pct >= PROFIT_TARGET_PCT:
            should_exit = True
            exit_reason = f"TARGET HIT +{pnl_pct:.0f}% (target: +{PROFIT_TARGET_PCT}%)"
        
        # Stop loss
        elif pnl_pct <= STOP_LOSS_PCT:
            should_exit = True
            exit_reason = f"STOP LOSS {pnl_pct:.0f}% (limit: {STOP_LOSS_PCT}%)"
        
        # Time stop
        elif days_held >= MAX_HOLD_DAYS:
            should_exit = True
            exit_reason = f"TIME STOP day {days_held} at {pnl_pct:+.0f}%"
        
        if should_exit:
            # Execute sell
            import os as _os
            if _os.environ.get("LIVE_EXECUTION") == "true":
                try:
                    import broker_alpaca as _ba
                    from user_manager import get_user_alpaca_keys
                    from broker_alpaca import sell_to_close, build_occ_symbol, PAPER_BASE
                    
                    key, secret = get_user_alpaca_keys(PUT_USER)
                    if key:
                        _ba.API_KEY = key
                        _ba.API_SECRET = secret
                        _ba.HEADERS = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
                        _ba.HEADERS_JSON = {**_ba.HEADERS, "Content-Type": "application/json"}
                        _ba.BASE_URL = PAPER_BASE
                    
                    symbol = build_occ_symbol(ticker, trade["option_exp"], "PUT", trade["option_strike"])
                    result = sell_to_close(symbol, qty=1)
                    if result["success"]:
                        bid = result["fill_price"]
                except Exception as e:
                    log(f"  PUT SELL ERROR {ticker}: {e}")
            
            pnl_dollars = round((bid - entry_cost) * 100, 2)
            trade["status"] = "closed"
            trade["exit_reason"] = exit_reason
            trade["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
            trade["exit_time"] = datetime.utcnow().isoformat()
            trade["exit_option_bid"] = bid
            trade["pnl"] = pnl_dollars
            trade["pnl_pct"] = round(pnl_pct, 1)
            trade["days_held"] = days_held
            modified = True
            log(f"  🔄 PUT EXIT: {ticker} | {exit_reason} | P&L: ${pnl_dollars:+.0f}")
    
    if modified:
        save_put_trades(put_trades)


def load_put_trades() -> list:
    """Load put trades from the puts user directory."""
    path = os.path.join(get_user_dir(PUT_USER), "trades.json")
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []


def save_put_trades(trades: list):
    """Save put trades."""
    path = os.path.join(get_user_dir(PUT_USER), "trades.json")
    with open(path, "w") as f:
        json.dump(trades, f, indent=2)
