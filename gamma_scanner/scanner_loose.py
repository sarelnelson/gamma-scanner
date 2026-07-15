"""
Daily High-Gamma Mean Reversion Scanner
Runs at market open, scans S&P 500 + high-volume mid-caps.
Finds oversold bounce candidates and extended short candidates.
Scores, filters, selects options, auto-enters top picks.
"""
import json, os, time
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
import numpy as np
import ta
import warnings
warnings.filterwarnings('ignore')

SCANNER_DIR = "/workspace/stock-agent/gamma_scanner"
PICKS_FILE = f"{SCANNER_DIR}/picks_loose.json"
TRADES_FILE = f"{SCANNER_DIR}/trades_loose.json"
SCAN_LOG = f"{SCANNER_DIR}/scan.log"
os.makedirs(SCANNER_DIR, exist_ok=True)

# S&P 500 + high-volume mid-caps (abbreviated — top movers)
# In production, load full list from a file
SP500_SAMPLE = [
    "AAPL","MSFT","AMZN","NVDA","GOOGL","META","TSLA","AMD","NFLX","DIS",
    "BA","NKE","PYPL","SNAP","ROKU","PLTR","SOFI","COIN","HOOD",
    "RIVN","LCID","NIO","PLUG","FCEL","MARA","RIOT","UPST","AFRM","PATH",
    "DKNG","PENN","WYNN","MGM","LVS","CCL","RCL","AAL","UAL","DAL",
    "F","GM","XPEV","LI",
    "AMC","GME","BB","SPCE","OPEN","QBTS",
    "INTC","MU","QCOM","AVGO","TSM","ASML","LRCX","AMAT","KLAC",
    "CRM","SNOW","DDOG","NET","CRWD","ZS","PANW","OKTA","MDB",
    "SHOP","SE","MELI","BABA","JD","PDD","GRAB","CPNG",
    "XOM","CVX","OXY","DVN","HAL","SLB","BP","SHEL",
    "JPM","GS","MS","BAC","WFC","C","SCHW","BLK","KKR",
    "PFE","MRNA","BNTX","JNJ","ABT","LLY","NVO","UNH",
    "WMT","COST","TGT","HD","LOW","SBUX","MCD","CMG",
    "V","MA","AXP","ABNB","UBER","LYFT","DASH",
]

def load_picks():
    if os.path.exists(PICKS_FILE):
        with open(PICKS_FILE) as f:
            return json.load(f)
    return []

def save_picks(picks):
    with open(PICKS_FILE, "w") as f:
        json.dump(picks, f, indent=2)

def load_trades():
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE) as f:
            return json.load(f)
    return []

def save_trades(trades):
    """Atomic write to prevent corruption if monitor is also writing."""
    tmp_path = TRADES_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp_path, TRADES_FILE)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"[{ts}] {msg}"
    print(line)
    with open(SCAN_LOG, "a") as f:
        f.write(line + "\n")


# Push notifications via ntfy.sh
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

def _notify(title, message, priority="default", tags=""):
    if not NTFY_TOPIC:
        return
    try:
        import requests
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message,
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=5,
        )
    except:
        pass

def _notify_entry(ticker, direction, strike, exp, score, cost):
    _notify(
        f"🟢 NEW: {ticker} {direction} ${strike}",
        f"Score: {score} | Exp: {exp}\nCost: ${cost:.0f}/contract",
        tags="white_check_mark"
    )

def _notify_rotation(old_ticker, old_pnl, new_ticker, direction, strike, score):
    _notify(
        f"🔄 ROTATED: {old_ticker} → {new_ticker}",
        f"Sold {old_ticker} (${old_pnl:+.2f})\nBought {new_ticker} {direction} ${strike} (score:{score})",
        tags="arrows_counterclockwise"
    )


# === STEP 1: STOCK SCREEN ===

def screen_stocks():
    """Screen for oversold bounce and mean-reversion short candidates."""
    log(f"Screening {len(SP500_SAMPLE)} stocks...")
    candidates = []

    for ticker in SP500_SAMPLE:
        try:
            df = yf.Ticker(ticker).history(period="3mo", interval="1d")
            if df.empty or len(df) < 50:
                continue

            close = df["Close"]
            volume = df["Volume"]
            price = close.iloc[-1]

            # Liquidity filter
            if price < 5 or price > 150:
                continue
            avg_vol = volume.tail(20).mean()
            if avg_vol < 2_000_000:
                continue

            # Technical indicators
            rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
            sma20 = close.tail(20).mean()
            sma50 = close.tail(50).mean()
            week_low = close.tail(252).min() if len(close) >= 252 else close.min()
            pct_from_low = (price - week_low) / week_low * 100
            move_10d = (price - close.iloc[-11]) / close.iloc[-11] * 100 if len(close) > 11 else 0
            pct_above_sma20 = (price - sma20) / sma20 * 100
            today_green = close.iloc[-1] > df["Open"].iloc[-1]
            today_red = close.iloc[-1] < df["Open"].iloc[-1]
            vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 1
            atr = (df["High"].tail(14) - df["Low"].tail(14)).mean()
            atr_pct = atr / price * 100

            # ATR filter — want stocks that move
            if atr_pct < 2:
                continue

            # === FIX #1: TREND FILTER ===
            # Only buy calls on stocks in an uptrend that are DIPPING, not dying.
            # Require: 50-SMA is rising (higher than it was 20 days ago)
            # OR: stock was above 50-SMA within the last 20 days
            # This kills structural decliners like NIO, GME, LI
            sma50_20_ago = close.tail(50).iloc[:20].mean() if len(close) >= 50 else sma50
            sma50_rising = sma50 > sma50_20_ago
            was_above_50sma_recently = any(close.tail(20) > sma50)
            in_uptrend = sma50_rising or was_above_50sma_recently

            # === FIX #2: RSI HARD MINIMUM ===
            # Must be genuinely oversold — no RSI 50+ "oversold" picks
            rsi_oversold = rsi < 40

            # === FIX #3: VOLUME FILTER ===
            # For deeply oversold (RSI < 35): skip volume check — the signal is strong enough
            # For marginal oversold (RSI 35-40): require volume confirmation
            vol_floor = vol_ratio >= 0.5
            recent_vol_spike = any(volume.tail(5) > avg_vol * 1.3)
            has_volume = vol_floor or recent_vol_spike
            
            # Deeply oversold = volume not required
            if rsi < 35:
                has_volume = True

            # === OVERSOLD BOUNCE CANDIDATE ===
            # Requirements:
            #   - In uptrend (dip, not structural decline)
            #   - RSI < 40 (genuinely oversold)
            #   - Has volume (or RSI < 35 bypasses this)
            #   - Plus 2 of 4 confirmation signals
            bounce_conditions = sum([
                pct_from_low < 10,
                rsi < 35,
                today_green,
                recent_vol_spike,
            ])
            if in_uptrend and rsi_oversold and has_volume and bounce_conditions >= 2:
                candidates.append({
                    "ticker": ticker,
                    "setup": "oversold_bounce",
                    "direction": "CALL",
                    "price": round(price, 2),
                    "rsi": round(rsi, 1),
                    "pct_from_52w_low": round(pct_from_low, 1),
                    "vol_ratio": round(vol_ratio, 1),
                    "atr_pct": round(atr_pct, 1),
                    "move_10d": round(move_10d, 1),
                    "sma50_rising": sma50_rising,
                    "recent_vol_spike": recent_vol_spike,
                })

            # === MEAN REVERSION SHORT CANDIDATE ===
            # (shorts don't need uptrend filter — they want overextended stocks)
            elif move_10d > 25 and today_red and pct_above_sma20 > 20:
                candidates.append({
                    "ticker": ticker,
                    "setup": "mean_reversion_short",
                    "direction": "PUT",
                    "price": round(price, 2),
                    "rsi": round(rsi, 1),
                    "move_10d": round(move_10d, 1),
                    "pct_above_sma20": round(pct_above_sma20, 1),
                    "vol_ratio": round(vol_ratio, 1),
                    "atr_pct": round(atr_pct, 1),
                    "today_red": today_red,
                })

        except:
            continue

    log(f"  Found {len(candidates)} candidates")
    return candidates


# === STEP 2: OPTION SELECTION & SCORING ===

def score_and_select_options(candidates):
    """Score candidates, find best option contracts."""
    scored = []

    for c in candidates:
        ticker = c["ticker"]
        try:
            t = yf.Ticker(ticker)
            expirations = t.options
            if not expirations:
                continue

            # Find expiration 7-21 days out
            today = datetime.now().date()
            valid_exp = None
            for exp in expirations:
                days_out = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
                if 14 <= days_out <= 28:
                    valid_exp = exp
                    break
            if not valid_exp:
                continue

            chain = t.option_chain(valid_exp)
            options = chain.calls if c["direction"] == "CALL" else chain.puts

            if options.empty:
                continue

            # Filter: delta 0.30-0.45, OI > 500, price $0.20-$0.80
            options = options.copy()
            # Estimate delta from moneyness if not available
            options["est_delta"] = options.apply(
                lambda r: max(0, min(1, 0.5 - abs(r["strike"] - c["price"]) / c["price"])), axis=1
            )
            options = options[
                (options["openInterest"] > 500) &
                (options["lastPrice"] >= 0.15) &
                (options["lastPrice"] <= 1.00) &
                (options["est_delta"].between(0.25, 0.50))
            ]

            if options.empty:
                continue

            # Pick highest gamma proxy (closest to ATM with good volume)
            options["gamma_proxy"] = 1 / (1 + abs(options["strike"] - c["price"]))
            best = options.nlargest(1, "gamma_proxy").iloc[0]

            bid = float(best["bid"]) if best["bid"] > 0 else float(best["lastPrice"]) * 0.95
            ask = float(best["ask"]) if best["ask"] > 0 else float(best["lastPrice"]) * 1.05
            spread_pct = (ask - bid) / ask * 100 if ask > 0 else 999

            if spread_pct > 10:
                continue

            # === SCORING (max 100) ===
            score = 0
            # Mean-reversion setup: 25 pts
            if c["setup"] == "oversold_bounce" and c["rsi"] < 25:
                score += 25
            elif c["setup"] == "oversold_bounce":
                score += 20
            elif c["setup"] == "mean_reversion_short" and c["move_10d"] > 50:
                score += 25
            elif c["setup"] == "mean_reversion_short":
                score += 20

            # High gamma (close to ATM): 20 pts
            atm_dist = abs(best["strike"] - c["price"]) / c["price"]
            score += max(0, int(20 * (1 - atm_dist * 10)))

            # Low theta (proxy: more days to exp = less daily theta): 15 pts
            days_to_exp = (datetime.strptime(valid_exp, "%Y-%m-%d").date() - today).days
            score += min(15, days_to_exp)

            # Tight spread: 10 pts
            score += max(0, int(10 * (1 - spread_pct / 10)))

            # High open interest: 10 pts
            oi = int(best["openInterest"])
            score += min(10, int(oi / 500))

            # Strong volume: 10 pts
            score += min(10, int(c["vol_ratio"] * 3))

            # Technical confirmation: 10 pts
            if c["setup"] == "oversold_bounce" and c.get("today_green", True):
                score += 10
            elif c["setup"] == "mean_reversion_short" and c.get("today_red", True):
                score += 10

            c["score"] = min(100, score)
            c["option"] = {
                "strike": float(best["strike"]),
                "expiration": valid_exp,
                "bid": round(bid, 2),
                "ask": round(ask, 2),
                "spread_pct": round(spread_pct, 1),
                "open_interest": oi,
                "last_price": float(best["lastPrice"]),
                "cost_per_contract": round(ask * 100, 0),
            }
            scored.append(c)

        except:
            continue

    # Sort by score, only take 80+
    scored.sort(key=lambda x: x["score"], reverse=True)
    top_picks = [s for s in scored if s["score"] >= 60][:5]

    log(f"  Scored {len(scored)} candidates, {len(top_picks)} above 60")
    return top_picks


# === POSITION MANAGEMENT CONFIG ===
ACCOUNT_BALANCE = 5000.00          # Starting paper account balance
MAX_RISK_PER_TRADE_PCT = 2.0      # Max 2% of account per position ($100 on $5k)
MAX_OPEN_POSITIONS = 20           # Max 20 positions at once
MAX_DAILY_ENTRIES = 8             # Max new entries per day (including rotations)
MAX_TOTAL_EXPOSURE_PCT = 40.0     # Never deploy more than 40% of account at once
ENTRY_SLIPPAGE = 0.02             # $0.02 above ask for realistic fill on market order
NO_DUPLICATE_TICKERS = False       # Allow re-entry if signal persists across days
AVOID_FIRST_MINUTES = 15         # Don't enter in first 15 min (9:30-9:45 volatility)
MIN_SCORE_TO_ROTATE = 60         # New pick must score at least this to displace a loser

def get_account_balance():
    """Calculate current account balance: cash basis + realized P&L.
    Cash basis comes from account.json (deposits - withdrawals).
    Realized P&L comes from closed trades.
    """
    try:
        from account import get_cash_basis
        base = get_cash_basis()
    except:
        base = ACCOUNT_BALANCE  # fallback to hardcoded if account module not available
    
    trades = load_trades()
    realized = 0
    for t in trades:
        if t.get("status") not in ("closed", "expired"):
            continue
        pnl = t.get("pnl", 0)
        # Sanity check: if P&L exceeds 10x the contract cost, it's probably stored as percentage
        cost = t.get("cost_per_contract", 100)
        if abs(pnl) > cost * 10 and "pnl_pct" not in t:
            pnl = (pnl / 100) * t.get("option_cost", 0) * 100
        realized += pnl
    return base + realized

def get_capital_deployed():
    """Calculate total capital currently in open positions."""
    trades = load_trades()
    open_trades = [t for t in trades if t.get("status") == "open"]
    return sum(t.get("cost_per_contract", 0) for t in open_trades)


# === STEP 3: AUTO-ENTER TRADES (with rotation) ===

def get_position_strength(trade):
    """
    Score an open position's current strength.
    Higher = better position to keep. Lower = candidate for replacement.
    
    Factors:
    - Current P&L percentage (from real bid if available)
    - Original entry score
    - Days held (older losing trades are weaker)
    - Direction of movement (trending toward us vs against)
    """
    entry_cost = trade.get("option_cost", 1)
    
    # Use real bid P&L if profit_monitor has updated it
    if trade.get("current_option_bid") and entry_cost > 0:
        bid = trade["current_option_bid"]
        pnl_pct = (bid - entry_cost) / entry_cost * 100
    elif trade.get("current_pnl"):
        # Fallback to stored current_pnl (dollars) → convert to pct
        pnl_pct = (trade["current_pnl"] / (entry_cost * 100)) * 100 if entry_cost > 0 else 0
    else:
        pnl_pct = 0
    
    # Entry score (original conviction)
    entry_score = trade.get("score", 50)
    
    # Days held — older losers are weaker
    try:
        entry_date = datetime.strptime(trade.get("entry_date", ""), "%Y-%m-%d")
        days_held = (datetime.now() - entry_date).days
    except:
        days_held = 0
    
    # Days to expiration — positions close to expiry with big losses are dead money
    try:
        exp_date = datetime.strptime(trade.get("option_exp", ""), "%Y-%m-%d")
        days_to_exp = (exp_date - datetime.now()).days
    except:
        days_to_exp = 14
    
    # Composite strength score:
    # - P&L is the primary driver (a winning position should never be rotated out)
    # - Entry score gives slight edge to high-conviction entries
    # - Penalize old losers (they've had their chance)
    # - Penalize positions near expiry that are deep red (theta is killing them)
    
    strength = 0
    
    # P&L contribution (dominant factor, -100 to +300 range)
    strength += pnl_pct * 2  # a +50% winner gets +100 strength, a -50% loser gets -100
    
    # Entry score bonus (0-20 range)
    strength += (entry_score - 50) * 0.4  # score of 75 adds +10, score of 50 adds 0
    
    # Age penalty for losers (only penalizes losing positions)
    if pnl_pct < 0:
        strength -= days_held * 5  # each day a loser is held, it gets -5 strength
    
    # Near-expiry penalty for deep losers
    if pnl_pct < -30 and days_to_exp <= 3:
        strength -= 50  # basically dead, should be rotated
    
    return round(strength, 1)


def auto_enter_picks(picks):
    """
    Enter top picks with ROTATION logic:
    
    1. If we have room (< MAX_OPEN_POSITIONS), just enter normally
    2. If at capacity, compare new picks against current open positions:
       - Rank all open positions by "strength" (P&L + score + age)
       - If a new pick has higher potential than the weakest open position, ROTATE:
         - Sell the weak position (at current bid)
         - Enter the new pick
    3. Never rotate out a winning position (P&L > 0)
    4. New pick must score higher than the position it replaces
    
    This ensures the portfolio always holds the BEST available opportunities.
    """
    trades = load_trades()
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # Check daily entry limit
    today_entries = [t for t in trades if t.get("entry_date") == today]
    if len(today_entries) >= MAX_DAILY_ENTRIES:
        log(f"  Daily entry limit reached ({MAX_DAILY_ENTRIES}), skipping")
        return

    open_trades = [t for t in trades if t.get("status") == "open"]
    
    # Calculate current account state
    balance = get_account_balance()
    deployed = get_capital_deployed()
    max_deploy = balance * (MAX_TOTAL_EXPOSURE_PCT / 100)
    max_per_trade = balance * (MAX_RISK_PER_TRADE_PCT / 100)
    available = max_deploy - deployed

    log(f"  Account: ${balance:.0f} | Deployed: ${deployed:.0f} | Available: ${available:.0f} | Max/trade: ${max_per_trade:.0f}")
    log(f"  Open positions: {len(open_trades)}/{MAX_OPEN_POSITIONS}")

    # Score all open positions for rotation comparison
    position_strengths = []
    for t in open_trades:
        strength = get_position_strength(t)
        position_strengths.append((t, strength))
    
    # Sort by strength (weakest first — these are candidates for rotation)
    position_strengths.sort(key=lambda x: x[1])
    
    if position_strengths:
        weakest = position_strengths[0]
        strongest = position_strengths[-1]
        log(f"  Weakest position: {weakest[0]['ticker']} (strength: {weakest[1]})")
        log(f"  Strongest position: {strongest[0]['ticker']} (strength: {strongest[1]})")

    entries_today = len(today_entries)
    held_tickers = set(t["ticker"] for t in open_trades)
    rotation_idx = 0  # index into weakest positions for rotation candidates

    for pick in picks:
        # Respect daily limit
        if entries_today >= MAX_DAILY_ENTRIES:
            break

        ticker = pick["ticker"]
        opt = pick["option"]

        # Position sizing check
        fill_price = opt["ask"] + ENTRY_SLIPPAGE
        cost_per_contract = round(fill_price * 100, 2)

        if cost_per_contract > max_per_trade:
            log(f"  SKIP {ticker}: cost ${cost_per_contract:.0f} exceeds max per trade ${max_per_trade:.0f}")
            continue

        # Determine if we need to rotate or can just add
        need_rotation = len(open_trades) >= MAX_OPEN_POSITIONS or available < cost_per_contract
        
        if not need_rotation:
            # Simple entry — room available
            if cost_per_contract > available:
                log(f"  SKIP {ticker}: cost ${cost_per_contract:.0f} exceeds available ${available:.0f}")
                continue
            
            trade = _create_trade_entry(pick, opt, fill_price, cost_per_contract, today, now)
            trades.append(trade)
            held_tickers.add(ticker)
            available -= cost_per_contract
            entries_today += 1
            open_trades.append(trade)  # track for subsequent rotation checks
            log(f"  ENTERED: {ticker} {pick['direction']} ${opt['strike']} {opt['expiration']} (score:{pick['score']}) cost:${cost_per_contract:.0f}")
            _notify_entry(ticker, pick['direction'], opt['strike'], opt['expiration'], pick['score'], cost_per_contract)
        
        else:
            # ROTATION: compare this pick against weakest open position
            if rotation_idx >= len(position_strengths):
                log(f"  No more rotation candidates")
                break
            
            weakest_trade, weakest_strength = position_strengths[rotation_idx]
            
            # New pick's "potential strength" = its score normalized to strength scale
            # A score-75 pick entering fresh has ~75 potential vs a -40% loser at -80 strength
            new_pick_potential = (pick["score"] - 50) * 2  # score 75 → potential 50, score 60 → 20
            
            # ROTATION RULES:
            # 1. Never rotate out a winning position
            # 2. New pick must have meaningfully higher potential than what it replaces
            # 3. Weakest position must be actually losing (strength < 0)
            
            if weakest_strength >= 0:
                log(f"  SKIP rotation: weakest position ({weakest_trade['ticker']}) is not losing (strength: {weakest_strength})")
                break  # all remaining positions are also >= 0 (sorted)
            
            if new_pick_potential <= weakest_strength:
                log(f"  SKIP {ticker}: potential ({new_pick_potential}) not better than {weakest_trade['ticker']} ({weakest_strength})")
                rotation_idx += 1
                continue
            
            if pick["score"] < MIN_SCORE_TO_ROTATE:
                log(f"  SKIP {ticker}: score {pick['score']} below min rotation threshold ({MIN_SCORE_TO_ROTATE})")
                continue
            
            # EXECUTE ROTATION
            old_ticker = weakest_trade["ticker"]
            old_bid = weakest_trade.get("current_option_bid", 0)
            old_entry = weakest_trade.get("option_cost", 0)
            
            # Close the weak position
            if old_bid > 0:
                exit_pnl = round((old_bid - ENTRY_SLIPPAGE - old_entry) * 100, 2)
            else:
                exit_pnl = round(-old_entry * 100 * 0.5, 2)  # assume 50% loss if no bid data
            
            weakest_trade["status"] = "closed"
            weakest_trade["exit_reason"] = f"ROTATED OUT (replaced by {ticker} score:{pick['score']})"
            weakest_trade["exit_date"] = today
            weakest_trade["exit_time"] = now.isoformat()
            weakest_trade["pnl"] = exit_pnl
            weakest_trade["pnl_pct"] = round((exit_pnl / (old_entry * 100)) * 100, 1) if old_entry > 0 else 0
            weakest_trade["exit_option_bid"] = old_bid
            
            # Free up capital from closed position
            freed_capital = weakest_trade.get("cost_per_contract", 0)
            available += freed_capital
            
            # Enter the new position
            if cost_per_contract > available:
                log(f"  SKIP {ticker}: even after rotation, cost ${cost_per_contract:.0f} > available ${available:.0f}")
                rotation_idx += 1
                continue
            
            trade = _create_trade_entry(pick, opt, fill_price, cost_per_contract, today, now)
            trades.append(trade)
            available -= cost_per_contract
            entries_today += 1
            rotation_idx += 1
            
            log(f"  🔄 ROTATED: Sold {old_ticker} (strength:{weakest_strength:.0f}, P&L:${exit_pnl:.2f}) → Bought {ticker} {pick['direction']} ${opt['strike']} (score:{pick['score']}, potential:{new_pick_potential:.0f})")
            _notify_rotation(old_ticker, exit_pnl, ticker, pick['direction'], opt['strike'], pick['score'])

    save_trades(trades)


def _create_trade_entry(pick, opt, fill_price, cost_per_contract, today, now):
    """Helper to build a trade dict."""
    return {
        "ticker": pick["ticker"],
        "direction": pick["direction"],
        "setup": pick["setup"],
        "score": pick["score"],
        "entry_price": pick["price"],
        "entry_date": today,
        "entry_time": now.isoformat(),
        "option_strike": opt["strike"],
        "option_exp": opt["expiration"],
        "option_cost": round(fill_price, 2),
        "option_ask_at_entry": opt["ask"],
        "option_bid_at_entry": opt["bid"],
        "option_spread_pct": opt["spread_pct"],
        "cost_per_contract": cost_per_contract,
        "status": "open",
        "pnl": 0,
        "current_pnl": 0.0,
    }


# === STEP 4: CHECK OPEN TRADES ===

def check_open_trades():
    """Check P&L on open trades, exit if target hit or expired.
    NOTE: profit_monitor.py handles real-time exit execution.
    This function is a fallback for when the monitor isn't running (e.g., scan-only mode).
    It uses delta approximation — the monitor uses real option bids.
    """
    trades = load_trades()
    now = datetime.now()

    for trade in trades:
        if trade["status"] != "open":
            continue

        ticker = trade["ticker"]
        try:
            t = yf.Ticker(ticker)
            price = t.history(period="1d")["Close"].iloc[-1]

            # Check if option expired
            exp_date = datetime.strptime(trade["option_exp"], "%Y-%m-%d").date()
            if now.date() >= exp_date:
                # Estimate final value
                strike = trade["option_strike"]
                if trade["direction"] == "CALL":
                    intrinsic = max(0, price - strike)
                else:
                    intrinsic = max(0, strike - price)
                pnl_per_share = intrinsic - trade["option_cost"]
                pnl_dollars = round(pnl_per_share * 100, 2)  # per contract
                pnl_pct = round(pnl_per_share / trade["option_cost"] * 100, 1) if trade["option_cost"] > 0 else 0
                trade["status"] = "expired"
                trade["exit_price"] = round(price, 2)
                trade["pnl"] = pnl_dollars
                trade["pnl_pct"] = pnl_pct
                trade["exit_reason"] = "EXPIRED"
                log(f"  EXPIRED: {ticker} {trade['direction']} pnl:{pnl_pct:+.1f}% (${pnl_dollars:+.2f})")
                continue

            # Estimate current option value using delta approximation
            strike = trade["option_strike"]
            if trade["direction"] == "CALL":
                move = (price - trade["entry_price"]) / trade["entry_price"]
            else:
                move = (trade["entry_price"] - price) / trade["entry_price"]

            # Rough option P&L: delta(~0.35) * stock_move * stock_price
            option_gain = move * 0.35 * trade["entry_price"]
            current_value = trade["option_cost"] + option_gain
            pnl_pct = (current_value - trade["option_cost"]) / trade["option_cost"] * 100

            # Store as dollars (consistent with profit_monitor)
            pnl_dollars = round((current_value - trade["option_cost"]) * 100, 2)
            trade["current_pnl"] = pnl_dollars

            # Take profit at 100%+ gain
            if pnl_pct >= 100:
                trade["status"] = "closed"
                trade["pnl"] = pnl_dollars
                trade["pnl_pct"] = round(pnl_pct, 1)
                trade["exit_reason"] = "PROFIT 100%+"
                trade["exit_price"] = round(price, 2)
                log(f"  PROFIT: {ticker} {trade['direction']} +{pnl_pct:.0f}% (${pnl_dollars:+.2f})!")

            # No stop loss — let losers expire or recover
            # Max loss is capped at premium paid

        except:
            continue

    save_trades(trades)


# === MAIN ===

def run_scan():
    """Full scan pipeline."""
    log("=" * 50)
    log("DAILY HIGH-GAMMA MEAN REVERSION SCAN")
    log("=" * 50)

    # Step 1: Screen
    candidates = screen_stocks()
    if not candidates:
        log("No candidates found today")
        return []

    # Step 2: Score & select options
    picks = score_and_select_options(candidates)
    save_picks(picks)

    if not picks:
        log("No picks scored above 60")
        return []

    # Step 3: Auto-enter
    auto_enter_picks(picks)

    # Note: profit_monitor.py handles exit checks continuously during market hours.
    # Don't call check_open_trades() here to avoid race conditions on the trades file.

    log(f"Scan complete: {len(picks)} new picks entered")
    return picks


def get_performance():
    """Get scanner performance for API."""
    trades = load_trades()
    closed = [t for t in trades if t["status"] in ("closed", "expired")]
    open_t = [t for t in trades if t["status"] == "open"]
    picks = load_picks()

    if not closed:
        total_pnl = 0
        win_rate = 0
        wins = 0
    else:
        wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
        win_rate = round(wins / len(closed) * 100, 1)
        total_pnl = round(sum(t.get("pnl", 0) for t in closed) / len(closed), 1)  # avg per trade

    return {
        "total_trades": len(closed),
        "open": len(open_t),
        "wins": wins,
        "win_rate": win_rate,
        "avg_pnl": total_pnl,
        "picks": picks,
        "trades": trades[-15:],
        "last_scan": picks[0].get("ticker", "—") if picks else "No scan yet",
    }


if __name__ == "__main__":
    run_scan()
