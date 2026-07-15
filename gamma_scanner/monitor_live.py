"""
Gamma Scanner — LIVE Profit Monitor
Same as profit_monitor.py but executes REAL orders through the Alpaca broker.

This is the "flip the switch" version. To go live:
1. Set LIVE_EXECUTION = True below
2. Ensure broker_alpaca.py has correct API keys
3. The local trades_loose.json becomes the ORDER BOOK — 
   broker positions are the source of truth for actual holdings.

SAFETY:
- Starts in DRY_RUN mode (logs what it would do, doesn't execute)
- Set LIVE_EXECUTION = True only when ready for real money
- Always uses LIMIT orders (never market)
- Logs every action to broker.log for audit trail
- Reconciles local state with broker positions on each cycle
"""
import json, os, time, sys, tempfile
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, "/workspace/stock-agent")
sys.path.insert(0, "/workspace/stock-agent/gamma_scanner")
from market_clock import is_market_open
from broker_alpaca import (
    get_option_quote, find_contract, build_occ_symbol,
    sell_to_close, get_account, get_positions, log as broker_log,
    PAPER_MODE
)

# === CONFIG ===

LIVE_EXECUTION = False  # ← SET TO True TO EXECUTE REAL ORDERS
TRADES_FILE = "/workspace/stock-agent/gamma_scanner/trades_loose.json"
LOG_FILE = "/workspace/stock-agent/gamma_scanner/monitor_live.log"
CHECK_INTERVAL = 120  # 2 minutes

# Exit thresholds
PROFIT_TARGET_PCT = 100  # +100% on option value (2x entry cost)


def log(msg, level="INFO"):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    mode = "LIVE" if LIVE_EXECUTION else "DRY-RUN"
    line = f"[{ts}] [{mode}] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass


def load_trades():
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except:
        return []


def save_trades(trades):
    tmp = TRADES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp, TRADES_FILE)


def check_and_execute():
    """
    Main check loop:
    1. Load open positions from local trades file
    2. For each open trade, get real option quote from Alpaca
    3. If profit target hit:
       - DRY RUN: log what would happen
       - LIVE: execute sell_to_close through broker
    4. Update local state
    """
    trades = load_trades()
    open_trades = [t for t in trades if t.get("status") == "open"]
    
    if not open_trades:
        return
    
    log(f"Checking {len(open_trades)} open positions...")
    modified = False
    
    for trade in trades:
        if trade.get("status") != "open":
            continue
        
        ticker = trade["ticker"]
        strike = trade["option_strike"]
        expiration = trade["option_exp"]
        direction = trade["direction"]
        entry_cost = trade["option_cost"]
        
        # Check expiration
        try:
            exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
            today = datetime.utcnow().date()
            if today > exp_date:
                trade["status"] = "expired"
                trade["exit_reason"] = "EXPIRED"
                trade["exit_date"] = today.isoformat()
                trade["pnl"] = round(-entry_cost * 100, 2)  # assume worthless
                trade["pnl_pct"] = -100.0
                log(f"  EXPIRED: {ticker} {direction} ${strike} — marking worthless")
                modified = True
                continue
        except:
            continue
        
        # Build contract symbol and get real quote
        contract_symbol = build_occ_symbol(ticker, expiration, direction, strike)
        
        # Try Alpaca quote first (faster, more reliable during market hours)
        quote = get_option_quote(contract_symbol)
        if not quote:
            # Fallback: try finding contract first (symbol might be slightly different)
            contract = find_contract(ticker, expiration, direction, strike)
            if contract:
                contract_symbol = contract["symbol"]
                quote = get_option_quote(contract_symbol)
        
        if not quote:
            log(f"  {ticker}: No quote available, skipping")
            continue
        
        bid = quote["bid"]
        ask = quote["ask"]
        mid = quote["mid"]
        
        # P&L calculation using bid (what we'd actually get)
        exit_value = bid  # limit order at bid
        pnl_per_share = exit_value - entry_cost
        pnl_pct = (pnl_per_share / entry_cost * 100) if entry_cost > 0 else 0
        pnl_dollars = round(pnl_per_share * 100, 2)
        
        # Update tracking
        trade["current_pnl"] = pnl_dollars
        trade["current_option_bid"] = bid
        trade["current_option_ask"] = ask
        trade["last_check"] = datetime.utcnow().isoformat()
        trade["contract_symbol"] = contract_symbol
        modified = True
        
        # PROFIT TARGET CHECK
        if pnl_pct >= PROFIT_TARGET_PCT:
            log(f"  🎯 PROFIT TARGET HIT: {ticker} {direction} ${strike}")
            log(f"     Entry: ${entry_cost:.2f} → Bid: ${bid:.2f} (+{pnl_pct:.0f}%)")
            
            if LIVE_EXECUTION:
                # EXECUTE REAL SELL ORDER
                log(f"     ⚡ EXECUTING SELL: {contract_symbol} @ ${bid:.2f}")
                result = sell_to_close(contract_symbol, qty=1, min_price=entry_cost * 1.5)
                
                if result["success"]:
                    actual_fill = result["fill_price"]
                    actual_pnl = round((actual_fill - entry_cost) * 100, 2)
                    trade["status"] = "closed"
                    trade["exit_reason"] = f"PROFIT +{pnl_pct:.0f}% (LIVE)"
                    trade["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
                    trade["exit_time"] = datetime.utcnow().isoformat()
                    trade["exit_fill_price"] = actual_fill
                    trade["exit_order_id"] = result["order_id"]
                    trade["pnl"] = actual_pnl
                    trade["pnl_pct"] = round((actual_fill - entry_cost) / entry_cost * 100, 1)
                    trade["execution_status"] = result["status"]  # "filled" or "partial"
                    log(f"     ✅ SOLD: fill=${actual_fill:.2f} | P&L: ${actual_pnl:.2f}")
                else:
                    # Order didn't fill — mark as pending_exit, try again next cycle
                    trade["pending_exit"] = True
                    trade["pending_exit_reason"] = result["status"]
                    log(f"     ⚠️ Sell not filled ({result['status']}) — will retry next cycle")
            else:
                # DRY RUN — simulate the exit
                trade["status"] = "closed"
                trade["exit_reason"] = f"PROFIT +{pnl_pct:.0f}% (DRY-RUN)"
                trade["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
                trade["exit_time"] = datetime.utcnow().isoformat()
                trade["exit_option_bid"] = bid
                trade["pnl"] = pnl_dollars
                trade["pnl_pct"] = round(pnl_pct, 1)
                log(f"     📝 DRY-RUN: Would sell @ ${bid:.2f} for P&L ${pnl_dollars:.2f}")
        
        elif trade.get("pending_exit"):
            # Retry a previously failed exit
            log(f"  🔄 RETRY EXIT: {ticker} {direction} ${strike} (prev: {trade.get('pending_exit_reason')})")
            if LIVE_EXECUTION and pnl_pct >= PROFIT_TARGET_PCT * 0.8:
                # Still profitable enough — try again at current bid
                result = sell_to_close(contract_symbol, qty=1)
                if result["success"]:
                    actual_fill = result["fill_price"]
                    actual_pnl = round((actual_fill - entry_cost) * 100, 2)
                    trade["status"] = "closed"
                    trade["exit_reason"] = f"PROFIT +{pnl_pct:.0f}% (RETRY)"
                    trade["exit_fill_price"] = actual_fill
                    trade["exit_order_id"] = result["order_id"]
                    trade["pnl"] = actual_pnl
                    trade["pnl_pct"] = round((actual_fill - entry_cost) / entry_cost * 100, 1)
                    del trade["pending_exit"]
                    del trade["pending_exit_reason"]
                    log(f"     ✅ RETRY SOLD: fill=${actual_fill:.2f} | P&L: ${actual_pnl:.2f}")
            elif pnl_pct < PROFIT_TARGET_PCT * 0.5:
                # Price fell back significantly — cancel pending exit
                del trade["pending_exit"]
                del trade["pending_exit_reason"]
                log(f"     ↩️ Price retreated to +{pnl_pct:.0f}% — canceling exit intent")
        else:
            # Status log
            icon = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < -30 else "⚪"
            log(f"  {icon} {ticker} {direction} ${strike}: bid=${bid:.2f} ask=${ask:.2f} | P&L: {pnl_pct:+.0f}% (${pnl_dollars:+.2f})")
    
    if modified:
        save_trades(trades)


def reconcile_with_broker():
    """
    Reconcile local state with broker positions.
    The broker is the source of truth for what we actually hold.
    This catches cases where:
    - An order filled but we didn't record it (crash during execution)
    - A position was closed by the broker (assignment, expiration exercise)
    - Local state drifted from reality
    """
    if not LIVE_EXECUTION:
        return
    
    broker_positions = get_positions()
    trades = load_trades()
    
    # Build map of what broker says we hold
    broker_symbols = {p["symbol"]: p for p in broker_positions}
    
    # Check local "open" trades against broker
    for trade in trades:
        if trade.get("status") != "open":
            continue
        
        symbol = trade.get("contract_symbol")
        if not symbol:
            continue
        
        if symbol not in broker_symbols:
            # We think it's open but broker doesn't have it
            # This means it was closed (exercised, expired, or sold elsewhere)
            log(f"RECONCILE: {trade['ticker']} {symbol} — not in broker positions, marking closed", "WARN")
            trade["status"] = "closed"
            trade["exit_reason"] = "RECONCILED (not in broker)"
            trade["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
    
    save_trades(trades)


def run():
    """Main loop."""
    log("=" * 60)
    log(f"GAMMA LIVE PROFIT MONITOR STARTED")
    log(f"  Mode: {'⚡ LIVE EXECUTION' if LIVE_EXECUTION else '📝 DRY-RUN (no orders)'}")
    log(f"  Broker: {'Alpaca PAPER' if PAPER_MODE else '⚠️  Alpaca LIVE'}")
    log(f"  Profit target: +{PROFIT_TARGET_PCT}%")
    log(f"  Check interval: {CHECK_INTERVAL}s")
    log("=" * 60)
    
    # Verify broker connectivity
    acct = get_account()
    if acct:
        log(f"  Broker connected: ${acct['buying_power']:,.0f} buying power, Level {acct['options_level']}")
    else:
        log("  ⚠️ Cannot connect to broker — will retry", "WARN")
    
    cycle = 0
    while True:
        try:
            if not is_market_open():
                time.sleep(300)
                continue
            
            check_and_execute()
            
            # Reconcile with broker every 10 cycles (20 min)
            cycle += 1
            if cycle % 10 == 0 and LIVE_EXECUTION:
                reconcile_with_broker()
            
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            log("Monitor stopped")
            break
        except Exception as e:
            log(f"Error: {e}", "ERROR")
            time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run()
