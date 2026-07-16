"""
Daily High-Gamma Mean Reversion Scanner
Runs at market open, scans S&P 500 + high-volume mid-caps.
Finds oversold bounce candidates and extended short candidates.
Scores, filters, selects options, auto-enters top picks.
"""
import json, os, time, sys
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import ta
import warnings
warnings.filterwarnings('ignore')

# Use Alpaca data (works on cloud/EC2), fall back to yfinance (works locally)
try:
    from data_alpaca import get_daily_bars, get_option_expirations, get_option_chain
    USE_ALPACA_DATA = True
except ImportError:
    USE_ALPACA_DATA = False

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False


def get_stock_history(ticker, days=90):
    """Get daily OHLCV data from best available source."""
    if USE_ALPACA_DATA:
        df = get_daily_bars(ticker, days)
        if not df.empty:
            return df
    if HAS_YFINANCE:
        try:
            df = yf.Ticker(ticker).history(period="3mo", interval="1d")
            if not df.empty:
                return df
        except:
            pass
    return pd.DataFrame()

SCANNER_DIR = "/workspace/stock-agent/gamma_scanner"
PICKS_FILE = f"{SCANNER_DIR}/picks_loose.json"
TRADES_FILE = f"{SCANNER_DIR}/trades_loose.json"
SCAN_LOG = f"{SCANNER_DIR}/scan.log"
os.makedirs(SCANNER_DIR, exist_ok=True)

# Load full ticker universe from file (4500+ liquid US stocks)
# Generated from Alpaca assets API — NYSE, NASDAQ, ARCA, tradeable + shortable
import json as _json
_TICKERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "all_tickers.json")
_BLACKLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blacklist.json")
try:
    with open(_TICKERS_FILE) as _f:
        SP500_SAMPLE = _json.load(_f)
except:
    # Fallback if file doesn't exist
    SP500_SAMPLE = [
        "AAPL","MSFT","AMZN","NVDA","GOOGL","META","TSLA","AMD","NFLX","DIS",
        "BA","NKE","PYPL","SNAP","ROKU","PLTR","SOFI","COIN","HOOD",
        "INTC","MU","QCOM","AVGO","CRM","NET","CRWD","PANW",
        "XOM","CVX","OXY","DVN","HAL","JPM","GS","BAC","WFC",
        "PFE","MRNA","JNJ","LLY","UNH","WMT","COST","TGT","HD",
        "V","MA","ABNB","UBER","LYFT","DASH","F","GM","NIO","XPEV","LI",
        "BABA","JD","DKNG","LVS","CCL","AAL","DAL",
    ]

# Load and apply blacklist (stocks that historically never bounce profitably)
try:
    with open(_BLACKLIST_FILE) as _f:
        _BLACKLIST = set(_json.load(_f))
    SP500_SAMPLE = [t for t in SP500_SAMPLE if t not in _BLACKLIST]
except:
    pass  # no blacklist file = scan everything

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
    """
    Two-stage screen for oversold bounce candidates.
    Stage 1: Bulk fetch 20 days of bars for ALL tickers (few API calls) → compute RSI → pre-filter
    Stage 2: Deep scan only the ~20-50 that pass pre-filter (full 90-day history)
    
    This scans 4500+ stocks in ~30 seconds instead of hours.
    """
    log(f"Screening {len(SP500_SAMPLE)} stocks...")
    
    # === STAGE 1: BULK PRE-FILTER ===
    # Fetch 20 days of data for ALL stocks in batches of 200 (one API call each)
    if USE_ALPACA_DATA:
        from data_alpaca import get_bulk_daily_bars
        all_bars = get_bulk_daily_bars(SP500_SAMPLE, days=20)
        log(f"  Bulk fetch: got data for {len(all_bars)} stocks")
    else:
        # Fallback: fetch one by one (slow)
        all_bars = {}
        for ticker in SP500_SAMPLE:
            df = get_stock_history(ticker, days=20)
            if not df.empty:
                all_bars[ticker] = df
    
    # Quick RSI + price filter on bulk data
    pre_filtered = []
    for ticker, df in all_bars.items():
        try:
            if len(df) < 14:
                continue
            
            close = df["Close"]
            price = float(close.iloc[-1])
            
            # Basic filters (fast, eliminates 90%+)
            if price < 5 or price > 150:
                continue
            
            # Quick RSI check
            rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
            if pd.isna(rsi) or rsi >= 40:
                continue
            
            # Made it past pre-filter — worth a deeper look
            pre_filtered.append(ticker)
        except:
            continue
    
    log(f"  Pre-filter: {len(pre_filtered)} stocks with RSI < 40")
    
    if not pre_filtered:
        log(f"  Found 0 candidates")
        return []
    
    # === STAGE 2: DEEP SCAN (only on pre-filtered stocks) ===
    # Now fetch full 90-day history for just the candidates
    candidates = []
    
    if USE_ALPACA_DATA:
        from data_alpaca import get_bulk_daily_bars
        deep_bars = get_bulk_daily_bars(pre_filtered, days=90)
    else:
        deep_bars = {}
        for ticker in pre_filtered:
            df = get_stock_history(ticker, days=90)
            if not df.empty:
                deep_bars[ticker] = df
    
    for ticker in pre_filtered:
        try:
            df = deep_bars.get(ticker)
            if df is None or len(df) < 50:
                continue

            close = df["Close"]
            volume = df["Volume"]
            price = float(close.iloc[-1])

            # Liquidity filter
            avg_vol = volume.tail(20).mean()
            if avg_vol < 2_000_000:
                continue

            # Technical indicators
            rsi = float(ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1])
            sma50 = float(close.tail(50).mean())
            sma50_20_ago = float(close.tail(50).iloc[:20].mean())
            week_low = float(close.tail(252).min() if len(close) >= 252 else close.min())
            pct_from_low = (price - week_low) / week_low * 100
            move_10d = (price - float(close.iloc[-11])) / float(close.iloc[-11]) * 100 if len(close) > 11 else 0
            pct_above_sma20 = (price - float(close.tail(20).mean())) / float(close.tail(20).mean()) * 100
            today_green = float(close.iloc[-1]) > float(df["Open"].iloc[-1])
            today_red = float(close.iloc[-1]) < float(df["Open"].iloc[-1])
            vol_ratio = float(volume.iloc[-1] / avg_vol) if avg_vol > 0 else 1
            atr = float((df["High"].tail(14) - df["Low"].tail(14)).mean())
            atr_pct = atr / price * 100

            # ATR filter — want stocks that move
            if atr_pct < 2:
                continue

            # === TREND FILTER ===
            sma50_rising = sma50 > sma50_20_ago
            was_above_50sma_recently = any(close.tail(20) > sma50)
            in_uptrend = sma50_rising or was_above_50sma_recently

            # === RSI CHECK ===
            rsi_oversold = rsi < 40

            # === VOLUME FILTER ===
            vol_floor = vol_ratio >= 0.5
            recent_vol_spike = any(volume.tail(5) > avg_vol * 1.3)
            has_volume = vol_floor or recent_vol_spike
            if rsi < 35:
                has_volume = True  # deeply oversold bypasses volume check

            # === BOUNCE CONDITIONS ===
            bounce_conditions = sum([
                pct_from_low < 10,
                rsi < 35,
                today_green,
                recent_vol_spike,
            ])
            if in_uptrend and rsi_oversold and has_volume and bounce_conditions >= 2:
                # === QUALITY SCORE ===
                # Measures how likely this stock is to bounce HARD (not just pass filters)
                quality = 0
                
                # 1. Volume strength (higher avg volume = institutional, stronger bounces)
                #    2M = baseline (0pts), 10M = good (+10), 50M+ = excellent (+20)
                if avg_vol >= 50_000_000: quality += 20
                elif avg_vol >= 20_000_000: quality += 15
                elif avg_vol >= 10_000_000: quality += 10
                elif avg_vol >= 5_000_000: quality += 5
                # below 5M = 0 bonus (small cap, weaker bounces)
                
                # 2. Selloff speed (sharp drops bounce harder than slow grinds)
                #    -10% in 5 days = sharp, -3% in 10 days = slow grind
                move_5d = (price - float(close.iloc[-6])) / float(close.iloc[-6]) * 100 if len(close) > 6 else 0
                if move_5d <= -8: quality += 15  # sharp drop
                elif move_5d <= -5: quality += 10
                elif move_5d <= -3: quality += 5
                # slow grind = 0 (less likely to snap back)
                
                # 3. Distance from 50-SMA (mild dip = better, extreme = might be broken)
                pct_below_sma50 = (sma50 - price) / sma50 * 100
                if 2 <= pct_below_sma50 <= 8: quality += 10  # healthy dip
                elif pct_below_sma50 < 2: quality += 5  # barely dipped
                # >8% below = 0 (might be structurally broken)
                
                # 4. Bounce history (has this stock bounced from oversold before?)
                #    Check if RSI went <35 and then price was higher 10 days later in recent history
                try:
                    rsi_series = ta.momentum.RSIIndicator(close, window=14).rsi()
                    past_oversold = rsi_series[rsi_series < 35]
                    if len(past_oversold) >= 2:
                        quality += 10  # has been oversold before and survived
                except:
                    pass
                
                # 5. SMA50 trending strongly (rising SMA = stronger support)
                if sma50_rising: quality += 5
                
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
                    "recent_vol_spike": bool(recent_vol_spike),
                    "quality": quality,
                })

            # === MEAN REVERSION SHORT ===
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
    # Sort by quality score — best bounce candidates first
    candidates.sort(key=lambda x: x.get("quality", 0), reverse=True)
    return candidates


def score_and_select_options(candidates):
    """Score candidates, find best option contracts."""
    scored = []

    for c in candidates:
        ticker = c["ticker"]
        try:
            # Get option expirations
            if USE_ALPACA_DATA:
                expirations = get_option_expirations(ticker)
            elif HAS_YFINANCE:
                t = yf.Ticker(ticker)
                expirations = t.options if t.options else []
            else:
                continue
                
            if not expirations:
                continue

            # Find expiration 14-28 days out
            today = datetime.now().date()
            valid_exp = None
            for exp in expirations:
                days_out = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
                if 14 <= days_out <= 28:
                    valid_exp = exp
                    break
            if not valid_exp:
                continue

            # Get option chain
            if USE_ALPACA_DATA:
                chain_data = get_option_chain(ticker, valid_exp)
                if not chain_data:
                    continue
                options = chain_data["calls"] if c["direction"] == "CALL" else chain_data["puts"]
            elif HAS_YFINANCE:
                chain = yf.Ticker(ticker).option_chain(valid_exp)
                options = chain.calls if c["direction"] == "CALL" else chain.puts
            else:
                continue

            if options.empty:
                continue

            # Filter: delta 0.30-0.45, OI > 500, price $0.20-$0.80
            options = options.copy()
            # Estimate delta from moneyness if not available
            options["est_delta"] = options.apply(
                lambda r: max(0, min(1, 0.5 - abs(r["strike"] - c["price"]) / c["price"])), axis=1
            )
            # Option selection: ATM has priority (better win rate)
            # Only go OTM if ATM options exceed position sizing limit
            max_cost_per_contract = 110  # $110 max risk per trade (~2% of $5k)
            min_option_price = 0.10
            min_oi = 50 if USE_ALPACA_DATA else 500
            
            # First try: ATM options (delta 0.30-0.55, no price cap)
            atm_options = options[
                (options["openInterest"] > min_oi) &
                (options["lastPrice"] >= min_option_price) &
                (options["est_delta"].between(0.30, 0.55))
            ]
            
            # Pick closest to ATM
            if not atm_options.empty:
                atm_options = atm_options.copy()
                atm_options["atm_dist"] = abs(atm_options["strike"] - c["price"])
                best_atm = atm_options.nsmallest(1, "atm_dist").iloc[0]
                
                # Check if it fits position sizing
                atm_ask = float(best_atm["ask"]) if best_atm["ask"] > 0 else float(best_atm["lastPrice"]) * 1.05
                if atm_ask * 100 <= max_cost_per_contract:
                    # ATM fits — use it
                    best = best_atm
                else:
                    # ATM too expensive — go OTM (cheaper strikes)
                    otm_options = options[
                        (options["openInterest"] > min_oi) &
                        (options["lastPrice"] >= min_option_price) &
                        (options["lastPrice"] <= max_cost_per_contract / 100) &
                        (options["est_delta"].between(0.10, 0.55))
                    ]
                    if otm_options.empty:
                        continue
                    # Pick closest to ATM that still fits budget
                    otm_options = otm_options.copy()
                    otm_options["atm_dist"] = abs(otm_options["strike"] - c["price"])
                    best = otm_options.nsmallest(1, "atm_dist").iloc[0]
            else:
                # No ATM options available — try OTM
                otm_options = options[
                    (options["openInterest"] > min_oi) &
                    (options["lastPrice"] >= min_option_price) &
                    (options["lastPrice"] <= max_cost_per_contract / 100) &
                    (options["est_delta"].between(0.10, 0.55))
                ]
                if otm_options.empty:
                    continue
                otm_options = otm_options.copy()
                otm_options["atm_dist"] = abs(otm_options["strike"] - c["price"])
                best = otm_options.nsmallest(1, "atm_dist").iloc[0]

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

            c["score"] = min(100, score + c.get("quality", 0) // 3)  # quality adds up to ~20 pts
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

    # Sort by score
    scored.sort(key=lambda x: x["score"], reverse=True)
    
    # Dynamic threshold: adapts to market conditions AND recent performance
    min_score = 60  # default
    
    # Factor 1: Market health (SPY vs 20-SMA)
    try:
        if USE_ALPACA_DATA:
            from data_alpaca import get_daily_bars
            spy = get_daily_bars("SPY", days=25)
        else:
            spy = get_stock_history("SPY", days=25)
        if not spy.empty and len(spy) >= 20:
            spy_price = float(spy["Close"].iloc[-1])
            spy_sma20 = float(spy["Close"].tail(20).mean())
            spy_pct = (spy_price - spy_sma20) / spy_sma20 * 100
            
            if spy_pct < -3:
                min_score = max(min_score, 75)
            elif spy_pct < 0:
                min_score = max(min_score, 68)
    except:
        pass
    
    # Factor 2: Recent win rate (if losing streak, be more selective)
    try:
        recent_trades = load_trades()
        recent_closed = [t for t in recent_trades if t.get("status") in ("closed", "expired")][-15:]
        if len(recent_closed) >= 8:
            recent_wins = sum(1 for t in recent_closed if t.get("pnl", 0) > 0)
            recent_wr = recent_wins / len(recent_closed)
            if recent_wr < 0.35:
                # Losing streak — raise threshold significantly
                min_score = max(min_score, 78)
                log(f"  ⚠️ Recent WR {recent_wr:.0%} — tightening threshold to {min_score}")
            elif recent_wr < 0.45:
                min_score = max(min_score, 70)
                log(f"  Recent WR {recent_wr:.0%} — threshold raised to {min_score}")
    except:
        pass
    
    log(f"  Market threshold: {min_score}")
    top_picks = [s for s in scored if s["score"] >= min_score][:5]

    log(f"  Scored {len(scored)} candidates, {len(top_picks)} above {min_score}")
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
            df = get_stock_history(ticker, days=5)
            if df.empty:
                continue
            price = float(df["Close"].iloc[-1])

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

# === SEASONAL MODES ===
# Jan, Feb, Oct: cautious mode (puts on overbought stocks)
# All other months: normal mode (calls on oversold stocks)
CAUTIOUS_MONTHS = [1, 2, 10]


def get_seasonal_mode():
    """Determine current trading mode based on month."""
    month = datetime.now().month
    if month in CAUTIOUS_MONTHS:
        return "cautious"
    return "normal"


def screen_overbought():
    """
    Cautious-mode scanner: find OVERBOUGHT stocks for put trades.
    Opposite of the normal strategy — look for stocks that ran up too far and are due for pullback.
    Criteria:
    - RSI > 70 (overbought)
    - Price > 20% above 50-SMA (overextended)
    - Had a big run-up in last 10 days (>15%)
    - Still in overall uptrend (so it's overextended, not just strong)
    """
    log("  CAUTIOUS MODE: Scanning for overbought puts...")
    
    if USE_ALPACA_DATA:
        from data_alpaca import get_bulk_daily_bars
        all_bars = get_bulk_daily_bars(SP500_SAMPLE, days=20)
    else:
        all_bars = {}
        for ticker in SP500_SAMPLE[:500]:
            df = get_stock_history(ticker, days=20)
            if not df.empty: all_bars[ticker] = df
    
    # Pre-filter: RSI > 65
    pre_filtered = []
    for ticker, df in all_bars.items():
        try:
            if len(df) < 14: continue
            close = df["Close"]
            price = float(close.iloc[-1])
            if price < 10 or price > 150: continue
            rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
            if pd.isna(rsi) or rsi <= 65: continue
            pre_filtered.append(ticker)
        except: continue
    
    log(f"  Pre-filter: {len(pre_filtered)} stocks with RSI > 65")
    if not pre_filtered: return []
    
    # Deep scan
    if USE_ALPACA_DATA:
        from data_alpaca import get_bulk_daily_bars
        deep_bars = get_bulk_daily_bars(pre_filtered, days=90)
    else:
        deep_bars = {}
        for ticker in pre_filtered:
            df = get_stock_history(ticker, days=90)
            if not df.empty: deep_bars[ticker] = df
    
    candidates = []
    for ticker in pre_filtered:
        try:
            df = deep_bars.get(ticker)
            if df is None or len(df) < 50: continue
            close = df["Close"]
            volume = df["Volume"]
            price = float(close.iloc[-1])
            avg_vol = volume.tail(20).mean()
            if avg_vol < 2_000_000: continue
            
            rsi = float(ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1])
            if rsi <= 70: continue
            
            sma50 = float(close.tail(50).mean())
            pct_above_sma50 = (price - sma50) / sma50 * 100
            if pct_above_sma50 < 15: continue  # needs to be well extended
            
            move_10d = (price - float(close.iloc[-11])) / float(close.iloc[-11]) * 100 if len(close) > 11 else 0
            if move_10d < 10: continue  # needs a recent big run
            
            atr = float((df["High"].tail(14) - df["Low"].tail(14)).mean())
            atr_pct = atr / price * 100
            if atr_pct < 2: continue
            
            today_red = float(close.iloc[-1]) < float(df["Open"].iloc[-1])
            
            candidates.append({
                "ticker": ticker,
                "setup": "overbought_short",
                "direction": "PUT",
                "price": round(price, 2),
                "rsi": round(rsi, 1),
                "pct_above_sma50": round(pct_above_sma50, 1),
                "move_10d": round(move_10d, 1),
                "atr_pct": round(atr_pct, 1),
                "today_red": today_red,
                "vol_ratio": round(float(volume.iloc[-1] / avg_vol), 1) if avg_vol > 0 else 0,
            })
        except: continue
    
    log(f"  Found {len(candidates)} overbought candidates")
    return candidates


def run_scan():
    """Full scan pipeline with seasonal mode."""
    mode = get_seasonal_mode()
    log("=" * 50)
    log(f"DAILY SCAN — MODE: {mode.upper()}")
    log("=" * 50)

    if mode == "cautious":
        # Cautious months: scan for overbought puts
        candidates = screen_overbought()
    else:
        # Normal months: scan for oversold calls
        candidates = screen_stocks()
    
    if not candidates:
        log("No candidates found today")
        return []

    # Step 2: Score & select options
    picks = score_and_select_options(candidates)
    save_picks(picks)

    if not picks:
        log("No picks scored above threshold")
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
