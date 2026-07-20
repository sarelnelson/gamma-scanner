"""
Trade Queue — manages pending trades that couldn't enter due to insufficient funds.

Rules:
- Trades that score well but can't enter → queue
- Manual "buy more" requests → front of queue (priority)
- Re-evaluated every scan: price moved up >20% → removed, otherwise enter if funds available
- When a trade closes → immediately try to fill top queued trade
- Queue clears at end of each trading day
"""
import os, json
from datetime import datetime
from user_manager import get_user_dir, load_user_trades, save_user_trades, get_user_balance, get_user_deployed


def get_queue_file(user_id):
    return os.path.join(get_user_dir(user_id), "queue.json")


def load_queue(user_id):
    path = get_queue_file(user_id)
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return []


def save_queue(user_id, queue):
    path = get_queue_file(user_id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(queue, f, indent=2)
    os.replace(tmp, path)


def add_to_queue(user_id, trade_info, priority=False):
    """
    Add a trade to the queue.
    trade_info: {ticker, direction, option_strike, option_exp, option_cost, score, entry_price, reason}
    priority=True puts it at front (manual buy requests)
    """
    queue = load_queue(user_id)
    
    trade_info["queued_at"] = datetime.utcnow().isoformat()
    trade_info["queued_price"] = trade_info.get("option_cost", 0)  # price when queued
    
    if priority:
        queue.insert(0, trade_info)
    else:
        queue.append(trade_info)
    
    save_queue(user_id, queue)
    return queue


def clear_daily_queue(user_id):
    """Clear queue at end of day."""
    save_queue(user_id, [])


def process_queue_on_freed_capital(user_id):
    """
    Called when a trade closes. Try to fill the top queued trade.
    Re-verifies price before entering.
    Returns: filled trade info or None
    """
    queue = load_queue(user_id)
    if not queue:
        return None
    
    # Check available funds
    balance = get_user_balance(user_id)
    deployed = get_user_deployed(user_id)
    available = balance - deployed
    
    if available <= 0:
        return None
    
    # Try to fill from top of queue
    from data_alpaca import get_option_quote
    from broker_alpaca import build_occ_symbol
    
    for i, item in enumerate(queue):
        ticker = item.get("ticker")
        strike = item.get("option_strike")
        exp = item.get("option_exp")
        direction = item.get("direction", "CALL")
        queued_price = item.get("queued_price", 0)
        
        if not ticker or not strike or not exp:
            continue
        
        # Get fresh quote
        symbol = build_occ_symbol(ticker, exp, direction, strike)
        quote = get_option_quote(symbol)
        
        if not quote or quote.get("ask", 0) <= 0:
            continue
        
        current_ask = quote["ask"]
        
        # Check: has price moved up >20%? If so, missed it — remove
        if queued_price > 0 and current_ask > queued_price * 1.20:
            queue.pop(i)
            save_queue(user_id, queue)
            return None  # don't try next item, just clean this one
        
        # Check: can we afford it?
        cost = round(current_ask * 100, 2)
        if cost > available:
            continue  # can't afford this one, try next
        
        # Enter the trade
        trades = load_user_trades(user_id)
        now = datetime.now()
        trade = {
            "ticker": ticker,
            "direction": direction,
            "setup": item.get("setup", "queued"),
            "score": item.get("score", 0),
            "entry_price": item.get("entry_price", 0),
            "entry_date": now.strftime("%Y-%m-%d"),
            "entry_time": now.isoformat(),
            "option_strike": strike,
            "option_exp": exp,
            "option_cost": round(current_ask, 2),
            "cost_per_contract": cost,
            "status": "open",
            "pnl": 0,
            "current_pnl": 0.0,
            "from_queue": True,
        }
        trades.append(trade)
        save_user_trades(user_id, trades)
        
        # Remove from queue
        queue.pop(i)
        save_queue(user_id, queue)
        
        return trade
    
    return None


def process_queue_on_scan(user_id):
    """
    Called during each scan. Re-evaluate all queued trades:
    - Price up >20% → remove
    - Price down or stable + funds available → enter
    - Otherwise leave in queue
    Returns list of entered trades.
    """
    queue = load_queue(user_id)
    if not queue:
        return []
    
    balance = get_user_balance(user_id)
    deployed = get_user_deployed(user_id)
    available = balance - deployed
    
    entered = []
    new_queue = []
    
    from data_alpaca import get_option_quote
    from broker_alpaca import build_occ_symbol
    
    for item in queue:
        ticker = item.get("ticker")
        strike = item.get("option_strike")
        exp = item.get("option_exp")
        direction = item.get("direction", "CALL")
        queued_price = item.get("queued_price", 0)
        
        if not ticker or not strike or not exp:
            continue
        
        # Check if expired
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            if datetime.now().date() >= exp_date:
                continue  # drop expired items silently
        except:
            pass
        
        # Get fresh quote
        symbol = build_occ_symbol(ticker, exp, direction, strike)
        quote = get_option_quote(symbol)
        
        if not quote or quote.get("ask", 0) <= 0:
            new_queue.append(item)  # keep in queue, can't verify
            continue
        
        current_ask = quote["ask"]
        
        # Price moved up >20%? Remove — missed the entry
        if queued_price > 0 and current_ask > queued_price * 1.20:
            continue  # drop from queue
        
        # Update queued price to current (if it went down, that's a better entry)
        item["current_price"] = current_ask
        
        # Can we afford it?
        cost = round(current_ask * 100, 2)
        if cost <= available:
            # Enter
            trades = load_user_trades(user_id)
            now = datetime.now()
            trade = {
                "ticker": ticker,
                "direction": direction,
                "setup": item.get("setup", "queued"),
                "score": item.get("score", 0),
                "entry_price": item.get("entry_price", 0),
                "entry_date": now.strftime("%Y-%m-%d"),
                "entry_time": now.isoformat(),
                "option_strike": strike,
                "option_exp": exp,
                "option_cost": round(current_ask, 2),
                "cost_per_contract": cost,
                "status": "open",
                "pnl": 0,
                "current_pnl": 0.0,
                "from_queue": True,
            }
            trades.append(trade)
            save_user_trades(user_id, trades)
            available -= cost
            entered.append(trade)
        else:
            new_queue.append(item)  # keep waiting
    
    save_queue(user_id, new_queue)
    return entered
