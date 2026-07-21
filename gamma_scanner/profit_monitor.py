"""
Gamma Scanner — Profit Monitor
Runs during market hours, checks open gamma positions every 2 minutes.
Takes profit via ratcheting trailing stop (+100% activation, +50% increments, 20% cushion).

PRODUCTION DESIGN PRINCIPLES:
- Uses REAL option bid prices from the chain (what you'd actually get filled at)
- Never uses theoretical/delta approximations for exit decisions
- Accounts for spread: exits at BID, not mid or ask
- Logs every check and every decision with full context
- Graceful failure: if we can't get a quote, we skip — never panic-sell on bad data
- Atomic file writes to prevent corruption
- Market hours only (via Alpaca clock API)
- Position tracking is append-only — closed trades never get reopened
- SINGLE INSTANCE GUARD: PID lock file prevents duplicate execution
"""
import json, os, time, sys, tempfile, atexit, signal
from datetime import datetime, timedelta

import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from market_clock import is_market_open
from config import TRADES_FILE as _TRADES_FILE, MONITOR_LOG as _MONITOR_LOG, PID_FILE as _PID_FILE, NTFY_TOPIC as _NTFY_TOPIC, NTFY_SERVER as _NTFY_SERVER

# Config (use config.py values, allow env override)
TRADES_FILE = _TRADES_FILE
LOG_FILE = _MONITOR_LOG
PID_FILE = _PID_FILE
CHECK_INTERVAL = 120  # seconds between checks (2 min)

# Exit thresholds
PROFIT_TARGET_PCT = 100  # trailing stop activates at +100% option gain
# No stop loss — per strategy, let losers expire or come back

# Slippage estimate for market orders on options
SLIPPAGE_PER_CONTRACT = 0.02  # $0.02 below bid for immediate fill estimate

# Push notifications via ntfy.sh
NTFY_TOPIC = _NTFY_TOPIC
NTFY_SERVER = _NTFY_SERVER


def notify(title, message, priority="default", tags=""):
    """Send push notification to phone via ntfy.sh"""
    if not NTFY_TOPIC:
        return
    try:
        import requests
        headers = {"Title": title, "Priority": priority}
        if tags:
            headers["Tags"] = tags
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message,
            headers=headers,
            timeout=5,
        )
    except:
        pass  # never let notification failure affect trading


# === SINGLE INSTANCE GUARD ===

def acquire_lock():
    """
    Ensure only one instance of the monitor runs at a time.
    Uses a PID file with stale-lock detection.
    Returns True if lock acquired, False if another instance is running.
    """
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if that PID is still alive
            os.kill(old_pid, 0)  # signal 0 = just check existence
            # Process exists — another instance is running
            print(f"Another monitor instance is running (PID {old_pid}). Exiting.")
            return False
        except (ProcessLookupError, ValueError):
            # Process is dead — stale lock file, safe to take over
            pass
        except PermissionError:
            # Process exists but we can't signal it — assume it's running
            print(f"Cannot verify PID in lock file. Exiting to be safe.")
            return False
    
    # Write our PID
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    
    return True


def release_lock():
    """Remove PID file on exit."""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE) as f:
                pid = int(f.read().strip())
            # Only remove if it's our PID (prevent race)
            if pid == os.getpid():
                os.remove(PID_FILE)
    except:
        pass


def handle_signal(signum, frame):
    """Clean exit on SIGTERM/SIGINT."""
    log(f"Received signal {signum}, shutting down cleanly")
    release_lock()
    sys.exit(0)


def log(msg, level="INFO"):
    """Append-only log with timestamp and level."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass  # never let logging failure stop the monitor


def load_trades():
    """Load trades with error recovery."""
    try:
        with open(TRADES_FILE) as f:
            data = json.load(f)
        if not isinstance(data, list):
            log(f"Trades file corrupted (not a list), backing up", "ERROR")
            os.rename(TRADES_FILE, TRADES_FILE + f".bak.{int(time.time())}")
            return []
        return data
    except json.JSONDecodeError:
        log(f"Trades file has invalid JSON, backing up", "ERROR")
        os.rename(TRADES_FILE, TRADES_FILE + f".bak.{int(time.time())}")
        return []
    except FileNotFoundError:
        return []


def save_trades(trades):
    """Atomic write — write to temp file then rename to prevent corruption."""
    tmp_path = TRADES_FILE + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(trades, f, indent=2)
        os.replace(tmp_path, TRADES_FILE)
    except Exception as e:
        log(f"Failed to save trades: {e}", "ERROR")
        # Don't remove tmp — it might be our only copy
        raise


def get_option_bid(ticker, strike, expiration, direction):
    """
    Get the REAL current bid price for a specific option contract via Alpaca API.
    Returns: {bid, mid, ask, last} or None on failure
    """
    try:
        from broker_alpaca import build_occ_symbol, get_option_quote
        symbol = build_occ_symbol(ticker, expiration, direction, strike)
        quote = get_option_quote(symbol)
        
        if not quote:
            return None
        
        bid = quote.get("bid", 0)
        ask = quote.get("ask", 0)
        
        if bid <= 0 and ask <= 0:
            return None
        
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else bid or ask
        
        return {
            "bid": round(bid, 2),
            "mid": round(mid, 2),
            "ask": round(ask, 2),
            "last": round(mid, 2),  # Alpaca doesn't give last, use mid
        }
    
    except Exception as e:
        log(f"  Failed to get option quote for {ticker} {strike} {expiration}: {e}", "WARN")
        return None


def get_stock_price(ticker):
    """Get current stock price via Alpaca. Returns float or None."""
    try:
        import requests
        from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
        headers = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        resp = requests.get(
            f"https://data.alpaca.markets/v2/stocks/{ticker}/snapshot",
            headers=headers, timeout=5
        )
        if resp.status_code == 200:
            snap = resp.json()
            trade = snap.get("latestTrade", {})
            price = trade.get("p", 0)
            if price:
                return round(float(price), 2)
        return None
    except Exception as e:
        log(f"  Failed to get stock price for {ticker}: {e}", "WARN")
        return None


def check_all_users():
    """Check positions for every active user."""
    try:
        from user_manager import get_active_users, load_user_trades, save_user_trades
        users = get_active_users()
    except:
        users = ["sarel"]  # fallback
    
    for user_id in users:
        try:
            from user_manager import load_user_trades, save_user_trades
            trades = load_user_trades(user_id)
            if not trades:
                continue
            open_before = sum(1 for t in trades if t.get("status") == "open")
            updated_trades = check_positions_for_trades(trades, user_id)
            open_after = sum(1 for t in updated_trades if t.get("status") == "open")
            save_user_trades(user_id, updated_trades)
            
            # If any trades closed, try to fill from queue
            if open_after < open_before:
                try:
                    from trade_queue import process_queue_on_freed_capital
                    filled = process_queue_on_freed_capital(user_id)
                    if filled:
                        log(f"  {user_id}: QUEUE FILL → {filled['ticker']} ${filled['cost_per_contract']:.0f}")
                except:
                    pass
        except Exception as e:
            log(f"Error checking {user_id}: {e}", "ERROR")


def check_positions_for_trades(trades, user_id=""):
    """
    Core monitoring — checks a list of trades for exit conditions.
    Returns the updated trades list.
    
    EXIT LOGIC:
    - Take profit when option BID >= 2x entry cost (i.e., +100% gain)
    - Using BID (not mid/last) because that's what you actually get filled at
    - Account for slippage: assume fill at bid - $0.02
    
    NO STOP LOSS by design:
    - Max loss is capped at premium paid (can't lose more than entry cost)
    - Cheap OTM contracts can swing wildly — a -50% loser can become +200% winner
    - Winners covering losers is the core thesis
    
    EXPIRATION:
    - Positions expiring today: check if ITM, close if worth exercising
    - Positions expired: mark as expired with final P&L
    """
    open_trades = [t for t in trades if t.get("status") == "open"]
    
    if not open_trades:
        return trades
    
    prefix = f"[{user_id}] " if user_id else ""
    log(f"{prefix}Checking {len(open_trades)} open positions...")
    modified = False
    
    for trade in trades:
        if trade.get("status") != "open":
            continue
        
        ticker = trade["ticker"]
        strike = trade["option_strike"]
        expiration = trade["option_exp"]
        direction = trade["direction"]
        entry_cost = trade["option_cost"]  # what we paid per share (e.g., $0.73)
        
        # Check if expired
        try:
            exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
            today = datetime.utcnow().date()
            
            if today > exp_date:
                # Past expiration — mark as expired
                stock_price = get_stock_price(ticker)
                if stock_price:
                    if direction == "CALL":
                        intrinsic = max(0, stock_price - strike)
                    else:
                        intrinsic = max(0, strike - stock_price)
                    
                    # If ITM at expiration, net value is intrinsic minus entry
                    pnl_per_share = intrinsic - entry_cost
                    pnl_dollars = round(pnl_per_share * 100, 2)  # per contract
                else:
                    # Can't get price — assume worthless (conservative)
                    pnl_per_share = -entry_cost
                    pnl_dollars = round(-entry_cost * 100, 2)
                
                trade["status"] = "expired"
                trade["exit_reason"] = "EXPIRED"
                trade["exit_date"] = today.isoformat()
                trade["pnl"] = pnl_dollars
                trade["pnl_pct"] = round(pnl_per_share / entry_cost * 100, 1)
                log(f"  EXPIRED: {ticker} {direction} ${strike} | Intrinsic: ${intrinsic if stock_price else '?'} | P&L: ${pnl_dollars}")
                notify(
                    f"⏰ {ticker} expired {'ITM' if pnl_dollars > 0 else 'worthless'}",
                    f"{direction} ${strike} | P&L: ${pnl_dollars:+.2f}",
                    priority="low",
                    tags="hourglass"
                )
                modified = True
                continue
            
            # Expiring today — check more frequently but same logic applies
            # (broker would auto-exercise if ITM by $0.01+)
            
        except ValueError:
            log(f"  Invalid expiration date format for {ticker}: {expiration}", "WARN")
            continue
        
        # Get real option bid price
        quote = get_option_bid(ticker, strike, expiration, direction)
        if quote is None:
            # Can't get quote — skip this position, try next cycle
            log(f"  {ticker}: No option quote available, skipping")
            continue
        
        bid = quote["bid"]
        mid = quote["mid"]
        last = quote["last"]
        
        # Conservative exit value: bid minus slippage
        # In production: would use a limit order at bid, may get partial fill
        exit_value = max(0, bid - SLIPPAGE_PER_CONTRACT)
        
        # Calculate P&L
        pnl_per_share = exit_value - entry_cost
        pnl_pct = (pnl_per_share / entry_cost * 100) if entry_cost > 0 else 0
        pnl_dollars = round(pnl_per_share * 100, 2)  # per contract (100 shares)
        
        # Update current P&L for dashboard display
        trade["current_pnl"] = pnl_dollars
        trade["current_option_bid"] = bid
        trade["current_option_mid"] = mid
        trade["last_check"] = datetime.utcnow().isoformat()
        
        # Track previous bid for daily P&L calculation
        # Reset prev_bid at start of each day
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        if trade.get("prev_bid_date") != today_str:
            # New day — snapshot the opening bid as today's baseline
            trade["prev_option_bid"] = trade.get("current_option_bid", bid)
            trade["prev_bid_date"] = today_str
        
        # Also get stock price for context
        stock_price = get_stock_price(ticker)
        if stock_price:
            trade["current_price"] = stock_price
            if direction == "CALL":
                stock_move = (stock_price - trade["entry_price"]) / trade["entry_price"] * 100
            else:
                stock_move = (trade["entry_price"] - stock_price) / trade["entry_price"] * 100
            trade["stock_change_pct"] = round(stock_move, 2)
        
        modified = True
        
        # === RATCHETING TRAILING STOP ===
        # Once +100%, lock in a floor. Floor ratchets up in 50% increments with 20% cushion:
        #   Hits +100% → floor = +80%
        #   Hits +150% → floor = +130%
        #   Hits +200% → floor = +180%
        #   Hits +250% → floor = +230%
        # Sells when price drops BACK to the floor. Lets runners run.
        
        # Track the highest P&L this position has reached
        prev_high = trade.get("high_water_pct", 0)
        if pnl_pct > prev_high:
            trade["high_water_pct"] = round(pnl_pct, 1)
            prev_high = pnl_pct
        
        # Calculate current floor based on high water mark
        # Floor activates at +100%, then ratchets every +50% with 20% cushion
        current_floor = trade.get("trailing_floor_pct", None)
        
        if prev_high >= 100:
            # Determine which level we've reached
            # 100 → floor 80, 150 → floor 130, 200 → floor 180, etc.
            level_reached = int(prev_high // 50) * 50  # rounds down to nearest 50
            new_floor = level_reached - 20
            
            # Only ratchet UP, never down
            if current_floor is None or new_floor > current_floor:
                old_floor = current_floor
                trade["trailing_floor_pct"] = new_floor
                current_floor = new_floor
                if old_floor != new_floor:
                    log(f"  📈 {ticker}: High water +{prev_high:.0f}% → floor ratcheted to +{new_floor}%")
                    notify(
                        f"📈 {ticker} floor locked +{new_floor}%",
                        f"High water: +{prev_high:.0f}%\nFloor: +{new_floor}% (was +{old_floor or 0}%)\nWon't sell above +{new_floor}%",
                        priority="default",
                        tags="chart_with_upwards_trend,lock"
                    )
        
        # Check if we should exit (price dropped to floor)
        should_exit = current_floor is not None and pnl_pct <= current_floor
        
        if should_exit:
            # TRAILING STOP HIT — TAKE PROFIT
            # Live execution: actually sell on Alpaca
            import os as _os
            if _os.environ.get("LIVE_EXECUTION") == "true":
                try:
                    from broker_alpaca import sell_to_close, build_occ_symbol
                    contract_symbol = trade.get("alpaca_symbol") or build_occ_symbol(ticker, expiration, direction, strike)
                    result = sell_to_close(contract_symbol, qty=1)
                    if result["success"]:
                        trade["exit_fill_price"] = result["fill_price"]
                        trade["exit_order_id"] = result["order_id"]
                        exit_value = result["fill_price"]
                        pnl_dollars = round((exit_value - entry_cost) * 100, 2)
                        log(f"  SELL ORDER FILLED: {contract_symbol} @ ${exit_value:.2f}")
                    else:
                        log(f"  SELL FAILED ({result['status']}): closing locally anyway", "WARN")
                except Exception as e:
                    log(f"  SELL ERROR: {e}", "ERROR")
            
            trade["status"] = "closed"
            trade["exit_reason"] = f"TRAILING STOP +{pnl_pct:.0f}% (floor:{current_floor}%, high:{prev_high:.0f}%)"
            trade["exit_date"] = datetime.utcnow().strftime("%Y-%m-%d")
            trade["exit_time"] = datetime.utcnow().isoformat()
            trade["exit_option_bid"] = bid
            trade["exit_fill_estimate"] = exit_value
            trade["pnl"] = pnl_dollars
            trade["pnl_pct"] = round(pnl_pct, 1)
            trade["net_proceeds"] = round(exit_value * 100, 2)
            trade["net_profit"] = round(pnl_per_share * 100, 2)
            trade["high_water_pct"] = round(prev_high, 1)
            
            log(f"  💰 TRAILING STOP EXIT: {ticker} {direction} ${strike} {expiration}")
            log(f"     High water: +{prev_high:.0f}% | Floor: +{current_floor}% | Exit: +{pnl_pct:.0f}%")
            log(f"     Entry: ${entry_cost:.2f} → Exit bid: ${bid:.2f} (fill est: ${exit_value:.2f})")
            log(f"     P&L: ${pnl_dollars:.2f} per contract")
            notify(
                f"💰 SOLD {ticker} +{pnl_pct:.0f}%",
                f"{direction} ${strike} | Entry ${entry_cost:.2f} → Exit ${bid:.2f}\nP&L: ${pnl_dollars:+.2f} per contract\nHigh: +{prev_high:.0f}% | Floor: +{current_floor}%",
                priority="high",
                tags="moneybag,chart_with_upwards_trend"
            )
        else:
            # Just log status
            floor_str = f" [floor:+{current_floor}%]" if current_floor else ""
            status_icon = "🟢" if pnl_pct > 0 else "🔴" if pnl_pct < -20 else "⚪"
            log(f"  {status_icon} {ticker} {direction} ${strike}: bid=${bid:.2f} | P&L: {pnl_pct:+.0f}%{floor_str} (${pnl_dollars:+.2f})")
    
    return trades


def run_monitor():
    """
    Main loop — runs during market hours, sleeps when closed.
    Also triggers stock scans 3x per day (10:00, 12:00, 14:00 ET = 14:00, 16:00, 18:00 UTC).
    
    CRASH DETECTION: Monitors SPY for broad market selloff.
    If SPY drops >3% in 5 days, sends warning notification.
    If SPY drops >5% in 5 days, auto-pauses scanner (no new entries).
    
    PAUSE: Check for pause file. If exists, skip scanning (but still monitor open positions).
    """
    # Single instance guard
    if not acquire_lock():
        sys.exit(1)
    
    # Register cleanup
    atexit.register(release_lock)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    PAUSE_FILE = os.path.join(os.path.dirname(TRADES_FILE), ".paused")
    CRASH_WARN_SENT_FILE = os.path.join(os.path.dirname(TRADES_FILE), ".crash_warned")

    def is_paused():
        return os.path.exists(PAUSE_FILE)
    
    def check_market_health():
        """Check SPY for crash conditions. Returns 'ok', 'warning', or 'danger'."""
        try:
            import requests as req
            from config import ALPACA_API_KEY, ALPACA_SECRET_KEY
            hdrs = {'APCA-API-KEY-ID': ALPACA_API_KEY, 'APCA-API-SECRET-KEY': ALPACA_SECRET_KEY}
            resp = req.get('https://data.alpaca.markets/v2/stocks/SPY/bars',
                headers=hdrs, params={'timeframe': '1Day', 'limit': 6, 'adjustment': 'split'}, timeout=5)
            if resp.status_code != 200:
                return 'ok'  # can't check, assume ok
            bars = resp.json().get('bars', [])
            if len(bars) < 5:
                return 'ok'
            
            # 5-day change
            price_5d_ago = bars[0]['c']
            price_now = bars[-1]['c']
            change_5d = (price_now - price_5d_ago) / price_5d_ago * 100
            
            if change_5d <= -5:
                return 'danger'
            elif change_5d <= -3:
                return 'warning'
            return 'ok'
        except:
            return 'ok'
    log("=" * 60)
    log("GAMMA PROFIT MONITOR STARTED")
    log(f"  Trailing stop: activates at +{PROFIT_TARGET_PCT}%, ratchets every +50%")
    log(f"  Check interval: {CHECK_INTERVAL}s")
    log(f"  Scan schedule: 10:00, 12:00, 14:00 ET")
    log(f"  Trades file: {TRADES_FILE}")
    log(f"  Consecutive loss watchdog: warn at 5, kill at 10")
    log("=" * 60)
    
    consecutive_errors = 0
    scans_completed_today = set()
    
    # Scan hours in UTC (ET + 4 during EDT)
    SCAN_HOURS_UTC = [14, 16, 18]  # 10:00, 12:00, 14:00 ET
    
    # Consecutive loss watchdog
    LOSS_WARN_THRESHOLD = 5
    LOSS_KILL_THRESHOLD = 10
    loss_warning_sent = False
    
    while True:
        try:
            if not is_market_open():
                # Market closed — check less frequently
                now = datetime.utcnow()
                # Reset scan tracker at midnight
                if now.hour == 0:
                    scans_completed_today = set()
                    # Clear trade queues daily
                    try:
                        from user_manager import get_active_users
                        from trade_queue import clear_daily_queue
                        for uid in get_active_users():
                            clear_daily_queue(uid)
                    except: pass
                if now.hour == 20 and now.minute < 5:
                    log("Market just closed — final position check")
                    check_all_users()
                time.sleep(300)
                continue
            
            # Check positions every cycle (always runs, even if paused)
            check_all_users()
            consecutive_errors = 0
            
            # Publish briefing for AI assistant (every cycle)
            try:
                from briefing import publish_briefing
                publish_briefing()
            except Exception as _e:
                pass  # Non-critical — don't let briefing errors break the monitor
            
            # === CRASH DETECTION (check every 10 cycles = ~20 min) ===
            if not hasattr(run_monitor, '_cycle_count'):
                run_monitor._cycle_count = 0
            run_monitor._cycle_count += 1
            
            if run_monitor._cycle_count % 10 == 0:
                health = check_market_health()
                if health == 'danger' and not os.path.exists(CRASH_WARN_SENT_FILE):
                    log("🚨 MARKET CRASH DETECTED: SPY down >5% in 5 days — AUTO-PAUSING SCANNER", "WARN")
                    notify("🚨 CRASH: Scanner auto-paused", "SPY down >5% in 5 days.\nNo new entries until you unpause.\nOpen positions still monitored.", priority="urgent", tags="rotating_light,chart_with_downwards_trend")
                    # Auto-pause
                    with open(PAUSE_FILE, 'w') as f:
                        f.write(f"Auto-paused: SPY crash detected {datetime.utcnow().isoformat()}")
                    with open(CRASH_WARN_SENT_FILE, 'w') as f:
                        f.write(datetime.utcnow().isoformat())
                elif health == 'warning' and not os.path.exists(CRASH_WARN_SENT_FILE):
                    log("⚠️ MARKET WARNING: SPY down >3% in 5 days", "WARN")
                    notify("⚠️ Market weakness warning", "SPY down >3% in 5 days.\nScanner still active but consider pausing.\nWatch for further deterioration.", priority="high", tags="warning")
                    with open(CRASH_WARN_SENT_FILE, 'w') as f:
                        f.write(datetime.utcnow().isoformat())
                elif health == 'ok' and os.path.exists(CRASH_WARN_SENT_FILE):
                    # Market recovered — clear warning flag
                    os.remove(CRASH_WARN_SENT_FILE)
            
            # === SCAN (only if not paused) ===
            if is_paused():
                if run_monitor._cycle_count % 30 == 0:  # log every ~60 min
                    log("⏸️ Scanner PAUSED — monitoring positions only, no new entries")
            else:
                # === CONSECUTIVE LOSS WATCHDOG ===
                try:
                    _trades = load_trades()
                    _recent = [t for t in _trades if t.get("status") in ("closed", "expired")][-15:]
                    if len(_recent) >= LOSS_WARN_THRESHOLD:
                        consec_losses = 0
                        for t in reversed(_recent):
                            if t.get("pnl", 0) <= 0:
                                consec_losses += 1
                            else:
                                break
                        
                        if consec_losses >= LOSS_KILL_THRESHOLD:
                            log(f"🛑 {consec_losses} CONSECUTIVE LOSSES — AUTO-PAUSING", "WARN")
                            notify(
                                f"KILLED: {consec_losses} consecutive losses",
                                f"Scanner auto-paused after {consec_losses} straight losses.\nManually unpause when conditions improve.",
                                priority="urgent", tags="stop_sign"
                            )
                            with open(PAUSE_FILE, 'w') as f:
                                f.write(f"Auto-killed: {consec_losses} consecutive losses {datetime.utcnow().isoformat()}")
                        elif consec_losses >= LOSS_WARN_THRESHOLD and not loss_warning_sent:
                            log(f"⚠️ {consec_losses} consecutive losses — WARNING", "WARN")
                            notify(
                                f"WARNING: {consec_losses} straight losses",
                                f"Scanner still active but struggling.\n{LOSS_KILL_THRESHOLD - consec_losses} more losses until auto-pause.",
                                priority="high", tags="warning"
                            )
                            loss_warning_sent = True
                        elif consec_losses < LOSS_WARN_THRESHOLD:
                            loss_warning_sent = False
                except:
                    pass
                
                # Check if it's time for a scan (only fire one per cycle to avoid flooding)
                now = datetime.utcnow()
                today_str = now.strftime("%Y-%m-%d")
                
                scan_fired_this_cycle = False
                for scan_hour in SCAN_HOURS_UTC:
                    if scan_fired_this_cycle:
                        break
                    scan_key = f"{today_str}_{scan_hour}"
                    # Fire scan if: we haven't done it today AND current time is past the scheduled hour
                    past_scan_time = now.hour > scan_hour or (now.hour == scan_hour and now.minute >= 0)
                    if scan_key not in scans_completed_today and past_scan_time:
                        # Time to scan
                        log(f"⏰ Scheduled scan triggered ({scan_hour}:00 UTC)")
                        try:
                            from scanner_loose import run_scan
                            import importlib, scanner_loose
                            importlib.reload(scanner_loose)
                            from scanner_loose import run_scan
                            picks = run_scan()
                            # Update last_scan.json for dashboard
                            try:
                                import json as _json
                                scanner_dir = os.path.dirname(TRADES_FILE)
                                # Get actual candidates count from file
                                try:
                                    with open(os.path.join(scanner_dir, "candidates.json")) as _cf:
                                        cands = _json.load(_cf)
                                    candidates_found = len(cands)
                                except:
                                    candidates_found = 0
                                scan_info = {"last_scan_time": datetime.utcnow().isoformat() + "Z", "picks_found": len(picks) if picks else 0, "candidates_found": candidates_found}
                                scan_path = os.path.join(scanner_dir, "last_scan.json")
                                with open(scan_path, "w") as _f:
                                    _json.dump(scan_info, _f)
                            except: pass
                        except Exception as e:
                            log(f"Scan error: {e}", "ERROR")
                        scans_completed_today.add(scan_key)
                        scan_fired_this_cycle = True
            
            time.sleep(CHECK_INTERVAL)
            
        except KeyboardInterrupt:
            log("Monitor stopped by user")
            break
        except Exception as e:
            consecutive_errors += 1
            log(f"Error in monitor loop: {e}", "ERROR")
            
            if consecutive_errors >= 5:
                log("5 consecutive errors — backing off to 5 min interval", "ERROR")
                time.sleep(300)
            else:
                time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    run_monitor()
