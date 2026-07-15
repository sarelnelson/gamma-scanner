"""
Gamma Scanner — LIVE Entry Execution
Wraps scanner_loose.py scan logic with real Alpaca order execution.

USAGE:
  DRY RUN (default):  python3 scanner_live.py
  LIVE EXECUTION:     python3 scanner_live.py --live

FLOW:
1. Run the same scan as scanner_loose.py (screen → score → select options)
2. For each pick that passes filters:
   - Verify contract exists on Alpaca
   - Get real-time bid/ask from Alpaca (not yfinance)
   - Check spread is acceptable (<10%)
   - Submit limit buy order at ask price
   - Wait for fill (30s timeout, then cancel/replace)
   - Record actual fill price (not theoretical ask)
3. Only live-enter if the Alpaca quote confirms the yfinance pricing
   (protects against stale/bad data triggering entries)
"""
import sys, json, os, time
from datetime import datetime

sys.path.insert(0, "/workspace/stock-agent")
sys.path.insert(0, "/workspace/stock-agent/gamma_scanner")

from scanner_loose import (
    screen_stocks, score_and_select_options, load_trades, save_trades,
    load_picks, save_picks, log, TRADES_FILE,
    MAX_RISK_PER_TRADE_PCT, MAX_OPEN_POSITIONS, MAX_DAILY_ENTRIES,
    MAX_TOTAL_EXPOSURE_PCT, ACCOUNT_BALANCE, get_account_balance,
    get_capital_deployed,
)
from broker_alpaca import (
    find_contract, get_option_quote, buy_to_open, get_account,
    PAPER_MODE,
)
from market_clock import is_market_open

# === CONFIG ===
LIVE_EXECUTION = "--live" in sys.argv
MAX_PRICE_DEVIATION_PCT = 20  # reject if Alpaca ask is >20% different from yfinance ask


def run_live_scan():
    """Full scan with live execution."""
    log("=" * 60)
    log(f"GAMMA LIVE SCANNER — {'⚡ LIVE' if LIVE_EXECUTION else '📝 DRY-RUN'}")
    log(f"Broker: {'Alpaca PAPER' if PAPER_MODE else '⚠️ Alpaca LIVE'}")
    log("=" * 60)
    
    # Verify market is open
    if not is_market_open():
        log("Market is closed — not scanning")
        return []
    
    # Verify broker connectivity
    if LIVE_EXECUTION:
        acct = get_account()
        if not acct:
            log("❌ Cannot connect to broker — aborting", "ERROR")
            return []
        log(f"Broker OK: ${acct['buying_power']:,.0f} buying power")
    
    # Step 1: Screen stocks (same as scanner_loose)
    candidates = screen_stocks()
    if not candidates:
        log("No candidates found today")
        return []
    
    # Step 2: Score and select options (same as scanner_loose)
    picks = score_and_select_options(candidates)
    save_picks(picks)
    
    if not picks:
        log("No picks scored above threshold")
        return []
    
    # Step 3: Live-validated entry
    live_enter_picks(picks)
    return picks


def live_enter_picks(picks):
    """Enter picks with broker execution validation."""
    trades = load_trades()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    
    # Position sizing checks (same as scanner_loose)
    today_entries = [t for t in trades if t.get("entry_date") == today]
    if len(today_entries) >= MAX_DAILY_ENTRIES:
        log(f"Daily entry limit reached ({MAX_DAILY_ENTRIES})")
        return
    
    open_trades = [t for t in trades if t.get("status") == "open"]
    if len(open_trades) >= MAX_OPEN_POSITIONS:
        log(f"Max open positions reached ({MAX_OPEN_POSITIONS})")
        return
    
    balance = get_account_balance()
    deployed = get_capital_deployed()
    max_deploy = balance * (MAX_TOTAL_EXPOSURE_PCT / 100)
    max_per_trade = balance * (MAX_RISK_PER_TRADE_PCT / 100)
    available = max_deploy - deployed
    
    log(f"Account: ${balance:.0f} | Deployed: ${deployed:.0f} | Available: ${available:.0f}")
    
    entries_today = len(today_entries)
    
    for pick in picks:
        if entries_today >= MAX_DAILY_ENTRIES:
            break
        if available <= 0:
            log("Exposure cap reached")
            break
        
        ticker = pick["ticker"]
        direction = pick["direction"]
        strike = pick["option"]["strike"]
        expiration = pick["option"]["expiration"]
        yfinance_ask = pick["option"]["ask"]
        
        log(f"\n  Evaluating: {ticker} {direction} ${strike} {expiration} (score:{pick['score']})")
        
        # Step 3a: Verify contract exists on Alpaca
        contract = find_contract(ticker, expiration, direction, strike)
        if not contract:
            log(f"  SKIP {ticker}: Contract not found on Alpaca")
            continue
        
        symbol = contract["symbol"]
        
        # Step 3b: Get real-time quote from Alpaca
        quote = get_option_quote(symbol)
        if not quote:
            log(f"  SKIP {ticker}: No Alpaca quote available")
            continue
        
        alpaca_bid = quote["bid"]
        alpaca_ask = quote["ask"]
        spread_pct = quote["spread_pct"]
        
        log(f"  Alpaca quote: bid=${alpaca_bid:.2f} ask=${alpaca_ask:.2f} spread={spread_pct:.1f}%")
        log(f"  yfinance ask: ${yfinance_ask:.2f}")
        
        # Step 3c: Validate pricing (reject if Alpaca vs yfinance diverge too much)
        if yfinance_ask > 0:
            deviation = abs(alpaca_ask - yfinance_ask) / yfinance_ask * 100
            if deviation > MAX_PRICE_DEVIATION_PCT:
                log(f"  SKIP {ticker}: Price deviation {deviation:.0f}% between sources (max {MAX_PRICE_DEVIATION_PCT}%)")
                continue
        
        # Step 3d: Reject wide spreads
        if spread_pct > 12:
            log(f"  SKIP {ticker}: Spread too wide ({spread_pct:.1f}%) — illiquid")
            continue
        
        # Step 3e: Position sizing with REAL Alpaca ask price
        cost_per_contract = round(alpaca_ask * 100, 2)
        if cost_per_contract > max_per_trade:
            log(f"  SKIP {ticker}: Cost ${cost_per_contract:.0f} > max ${max_per_trade:.0f}")
            continue
        if cost_per_contract > available:
            log(f"  SKIP {ticker}: Cost ${cost_per_contract:.0f} > available ${available:.0f}")
            continue
        
        # Step 3f: Execute or simulate
        if LIVE_EXECUTION:
            log(f"  ⚡ EXECUTING BUY: {symbol} @ limit ${alpaca_ask:.2f}")
            result = buy_to_open(ticker, expiration, direction, strike, max_price=alpaca_ask + 0.03)
            
            if result["success"]:
                fill_price = result["fill_price"]
                filled_qty = result["filled_qty"]
                actual_cost = round(fill_price * filled_qty * 100, 2)
                
                trade = {
                    "ticker": ticker,
                    "direction": direction,
                    "setup": pick["setup"],
                    "score": pick["score"],
                    "entry_price": pick["price"],
                    "entry_date": today,
                    "entry_time": now.isoformat(),
                    "option_strike": strike,
                    "option_exp": expiration,
                    "option_cost": round(fill_price, 2),
                    "option_ask_at_entry": alpaca_ask,
                    "option_bid_at_entry": alpaca_bid,
                    "option_spread_pct": spread_pct,
                    "cost_per_contract": actual_cost,
                    "contract_symbol": result["contract_symbol"],
                    "order_id": result["order_id"],
                    "filled_qty": filled_qty,
                    "execution_status": result["status"],
                    "status": "open",
                    "pnl": 0,
                    "current_pnl": 0.0,
                }
                trades.append(trade)
                available -= actual_cost
                entries_today += 1
                log(f"  ✅ FILLED: {filled_qty}x @ ${fill_price:.2f} (cost: ${actual_cost:.2f})")
            else:
                log(f"  ❌ NOT FILLED: {result['status']} — skipping {ticker}")
        else:
            # DRY RUN — record with simulated fill at ask
            fill_price = alpaca_ask
            trade = {
                "ticker": ticker,
                "direction": direction,
                "setup": pick["setup"],
                "score": pick["score"],
                "entry_price": pick["price"],
                "entry_date": today,
                "entry_time": now.isoformat(),
                "option_strike": strike,
                "option_exp": expiration,
                "option_cost": round(fill_price, 2),
                "option_ask_at_entry": alpaca_ask,
                "option_bid_at_entry": alpaca_bid,
                "option_spread_pct": spread_pct,
                "cost_per_contract": cost_per_contract,
                "contract_symbol": symbol,
                "execution_status": "dry_run",
                "status": "open",
                "pnl": 0,
                "current_pnl": 0.0,
            }
            trades.append(trade)
            available -= cost_per_contract
            entries_today += 1
            log(f"  📝 DRY-RUN ENTRY: {ticker} {direction} ${strike} @ ${fill_price:.2f} (cost: ${cost_per_contract:.0f})")
    
    save_trades(trades)
    log(f"\nScan complete: {entries_today - len(today_entries)} new entries")


if __name__ == "__main__":
    run_live_scan()
