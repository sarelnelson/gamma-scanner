"""
Gamma Scanner Backtest
Simulates the scanner over 6 months of historical data to validate:
- How many picks would have been generated
- Win rate (did the option hit +100% before expiry?)
- Average P&L per trade
- How the trailing stop performs vs fixed +100% exit

Uses the SAME screening logic as scanner_loose.py but runs it day-by-day
over historical data instead of live.

NOTE: This is an approximation because:
- We use daily bars (can't see intraday spikes to +100% that came back)
- Option pricing is estimated via delta model (no historical option chain data)
- Slippage/spread is estimated, not actual
"""
import json, os, sys, time
from datetime import datetime, timedelta
import yfinance as yf
import pandas as pd
import numpy as np
import ta
import warnings
warnings.filterwarnings('ignore')

sys.path.insert(0, "/workspace/stock-agent/gamma_scanner")
sys.path.insert(0, "/workspace/stock-agent")

# Same ticker list as the live scanner
from scanner_loose import SP500_SAMPLE

# Backtest parameters
BACKTEST_DAYS = 150  # ~7.5 months of trading days (covers Jan 2026 → Aug 2026)
OPTION_DTE = 14  # assume 14 DTE options
DELTA = 0.35  # estimated delta for OTM options
ENTRY_COST_PCT = 0.03  # option costs ~3% of stock price (rough avg for 0.30-0.45 delta OTM)
SPREAD_COST = 0.02  # $0.02 entry slippage
PROFIT_TARGET = 100  # +100% trailing activation
TRAILING_CUSHION = 20  # floor = level - 20%

# Screening criteria (same as live)
RSI_MAX = 40
ATR_MIN = 2.0
VOL_RATIO_MIN = 0.8
PRICE_MIN = 5
PRICE_MAX = 150
AVG_VOL_MIN = 2_000_000

# Resolve output dir like config.py (old hardcoded /workspace path was a phantom on EC2)
_OUT_DIR = os.environ.get("GAMMA_DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else os.path.dirname(os.path.abspath(__file__)))
OUTPUT_FILE = os.path.join(_OUT_DIR, "backtest_results.json")


def run_backtest():
    print("=" * 60)
    print("  GAMMA SCANNER BACKTEST")
    print(f"  Period: {BACKTEST_DAYS} trading days")
    print(f"  Tickers: {len(SP500_SAMPLE)}")
    print(f"  Strategy: Oversold bounce, trailing stop +100%/+50% ratchet")
    print("=" * 60)
    print()

    # Download all data upfront (faster than per-ticker per-day)
    print("Downloading historical data...")
    all_data = {}
    failed = 0
    for i, ticker in enumerate(SP500_SAMPLE):
        try:
            df = yf.Ticker(ticker).history(period="12mo", interval="1d")
            if not df.empty and len(df) >= 80:
                all_data[ticker] = df
            else:
                failed += 1
        except:
            failed += 1
        if (i + 1) % 20 == 0:
            print(f"  Downloaded {i+1}/{len(SP500_SAMPLE)} ({len(all_data)} valid, {failed} failed)")
            time.sleep(1)  # rate limit

    print(f"  Done: {len(all_data)} tickers with valid data")
    print()

    # Simulate day by day
    trades = []
    daily_picks = {}

    # Get date range
    sample_df = list(all_data.values())[0]
    all_dates = sample_df.index[-BACKTEST_DAYS:]

    print(f"Simulating {len(all_dates)} trading days...")
    print(f"  From: {all_dates[0].strftime('%Y-%m-%d')}")
    print(f"  To:   {all_dates[-1].strftime('%Y-%m-%d')}")
    print()

    # Seasonal months: puts on overbought
    CAUTIOUS_MONTHS = [1, 2, 10]
    
    for day_idx, date in enumerate(all_dates):
        date_str = date.strftime("%Y-%m-%d")
        day_picks = []
        is_cautious = date.month in CAUTIOUS_MONTHS

        for ticker, df in all_data.items():
            try:
                # Get data up to this date (look-back, no forward-looking)
                mask = df.index <= date
                hist = df[mask]
                if len(hist) < 50:
                    continue

                close = hist["Close"]
                volume = hist["Volume"]
                price = close.iloc[-1]

                # Liquidity filter
                if price < PRICE_MIN or price > PRICE_MAX:
                    continue
                avg_vol = volume.tail(20).mean()
                if avg_vol < AVG_VOL_MIN:
                    continue

                # Technical indicators
                rsi = ta.momentum.RSIIndicator(close, window=14).rsi().iloc[-1]
                if pd.isna(rsi):
                    continue

                sma50 = close.tail(50).mean()
                sma50_20_ago = close.tail(50).iloc[:20].mean() if len(close) >= 50 else sma50
                sma50_rising = sma50 > sma50_20_ago
                was_above_50sma = any(close.tail(20) > sma50)
                in_uptrend = sma50_rising or was_above_50sma

                vol_ratio = volume.iloc[-1] / avg_vol if avg_vol > 0 else 0
                recent_spike = any(volume.tail(3) > avg_vol * 1.3)
                has_volume = vol_ratio >= VOL_RATIO_MIN or recent_spike

                atr = (hist["High"].tail(14) - hist["Low"].tail(14)).mean()
                atr_pct = atr / price * 100
                if atr_pct < ATR_MIN:
                    continue

                week_low = close.tail(252).min() if len(close) >= 252 else close.min()
                pct_from_low = (price - week_low) / week_low * 100
                today_green = close.iloc[-1] > hist["Open"].iloc[-1]

                # === SCREENING ===
                if is_cautious:
                    # CAUTIOUS MODE: Look for overbought stocks to buy puts
                    if rsi <= 70:
                        continue
                    pct_above_sma50 = (price - sma50) / sma50 * 100
                    if pct_above_sma50 < 15:
                        continue
                    move_10d = (float(close.iloc[-1]) - float(close.iloc[-11])) / float(close.iloc[-11]) * 100 if len(close) > 11 else 0
                    if move_10d < 10:
                        continue
                    if atr_pct < ATR_MIN:
                        continue
                    today_red = float(close.iloc[-1]) < float(hist["Open"].iloc[-1])
                    
                    score = 40
                    if rsi > 80: score += 15
                    elif rsi > 75: score += 10
                    if pct_above_sma50 > 25: score += 10
                    if move_10d > 20: score += 10
                    if today_red: score += 10
                    if atr_pct > 3: score += 5
                    
                    if score >= 60:
                        day_picks.append({
                            "ticker": ticker,
                            "date": date_str,
                            "price": round(float(price), 2),
                            "rsi": round(float(rsi), 1),
                            "score": score,
                            "atr_pct": round(float(atr_pct), 1),
                            "vol_ratio": round(float(vol_ratio), 1),
                            "direction": "PUT",
                        })
                else:
                    # NORMAL MODE: Look for oversold bounce (calls)
                    if not in_uptrend:
                        continue
                    if rsi >= RSI_MAX:
                        continue
                    if not has_volume:
                        continue

                    bounce_conditions = sum([
                        pct_from_low < 10,
                        rsi < 35,
                        today_green,
                        recent_spike,
                    ])
                    if bounce_conditions >= 2:
                        score = 40
                        if rsi < 30: score += 15
                        elif rsi < 35: score += 10
                        if pct_from_low < 5: score += 10
                        if recent_spike: score += 10
                        if today_green: score += 10
                        if atr_pct > 3: score += 5

                        if score >= 60:
                            day_picks.append({
                                "ticker": ticker,
                                "date": date_str,
                                "price": round(float(price), 2),
                                "rsi": round(float(rsi), 1),
                                "score": score,
                                "atr_pct": round(float(atr_pct), 1),
                                "vol_ratio": round(float(vol_ratio), 1),
                                "direction": "CALL",
                            })

            except:
                continue

        # Take top 5 picks for the day
        day_picks.sort(key=lambda x: x["score"], reverse=True)
        day_picks = day_picks[:5]
        if day_picks:
            daily_picks[date_str] = day_picks

        # Simulate trades for each pick
        for pick in day_picks:
            ticker = pick["ticker"]
            entry_price = pick["price"]
            entry_date = date

            # Estimate option cost (~3% of stock price for OTM option)
            option_cost = round(entry_price * ENTRY_COST_PCT + SPREAD_COST, 2)

            # Simulate forward: track stock movement for OPTION_DTE days
            future_mask = all_data[ticker].index > date
            future = all_data[ticker][future_mask].head(OPTION_DTE)

            if future.empty:
                continue

            # Track P&L day by day using delta approximation
            high_water = 0
            floor = None
            exit_day = None
            exit_pnl_pct = None
            exit_reason = None
            direction = pick.get("direction", "CALL")

            for i, (fdate, row) in enumerate(future.iterrows()):
                # For CALLS: stock up = profit. For PUTS: stock down = profit.
                if direction == "CALL":
                    stock_move = (row["Close"] - entry_price) / entry_price
                    stock_best_move = (row["High"] - entry_price) / entry_price
                else:
                    stock_move = (entry_price - row["Close"]) / entry_price
                    stock_best_move = (entry_price - row["Low"]) / entry_price
                
                # Option P&L estimate: delta * stock_move / option_cost_pct
                option_pnl_pct = (stock_move * DELTA * entry_price) / option_cost * 100
                option_high_pct = (stock_best_move * DELTA * entry_price) / option_cost * 100

                # Update high water mark (use intraday best)
                peak = max(option_pnl_pct, option_high_pct)
                if peak > high_water:
                    high_water = peak

                # Check trailing stop logic
                if high_water >= PROFIT_TARGET:
                    # Calculate current floor
                    level_reached = int(high_water // 50) * 50
                    floor = level_reached - TRAILING_CUSHION

                    # Check if current P&L dropped to floor
                    # Use close for exit (conservative)
                    if option_pnl_pct <= floor:
                        exit_day = fdate
                        exit_pnl_pct = option_pnl_pct
                        exit_reason = f"TRAILING STOP (floor:{floor}%, high:{high_water:.0f}%)"
                        break

            # If no trailing stop triggered, check expiry value
            if exit_day is None and not future.empty:
                final_price = float(future.iloc[-1]["Close"])
                if direction == "CALL":
                    stock_move = (final_price - entry_price) / entry_price
                else:
                    stock_move = (entry_price - final_price) / entry_price
                option_pnl_pct = (stock_move * DELTA * entry_price) / option_cost * 100
                exit_pnl_pct = option_pnl_pct

                if high_water >= PROFIT_TARGET:
                    # Had a trailing stop but never triggered — expired above floor
                    exit_reason = f"EXPIRED ABOVE FLOOR (high:{high_water:.0f}%)"
                else:
                    exit_reason = "EXPIRED"
                exit_day = future.index[-1]

            if exit_pnl_pct is not None:
                pnl_dollars = round(exit_pnl_pct / 100 * option_cost * 100, 2)
                trades.append({
                    "ticker": ticker,
                    "direction": direction,
                    "entry_date": date_str,
                    "exit_date": exit_day.strftime("%Y-%m-%d") if exit_day is not None else None,
                    "entry_price": entry_price,
                    "option_cost": option_cost,
                    "score": pick["score"],
                    "rsi": pick["rsi"],
                    "pnl_pct": round(exit_pnl_pct, 1),
                    "pnl_dollars": pnl_dollars,
                    "high_water": round(high_water, 1),
                    "exit_reason": exit_reason,
                    "win": exit_pnl_pct > 0,
                })

        if (day_idx + 1) % 20 == 0:
            wins = sum(1 for t in trades if t["win"])
            print(f"  Day {day_idx+1}/{len(all_dates)} | Trades: {len(trades)} | Wins: {wins} | Picks today: {len(day_picks)}")

    # === RESULTS ===
    print()
    print("=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print()

    if not trades:
        print("  No trades generated. Filters may be too restrictive for this period.")
        return

    total = len(trades)
    wins = sum(1 for t in trades if t["win"])
    losses = total - wins
    win_rate = wins / total * 100

    total_pnl = sum(t["pnl_dollars"] for t in trades)
    avg_pnl = total_pnl / total
    avg_win = sum(t["pnl_dollars"] for t in trades if t["win"]) / wins if wins else 0
    avg_loss = sum(t["pnl_dollars"] for t in trades if not t["win"]) / losses if losses else 0

    # Trailing stop stats
    trailing_exits = [t for t in trades if "TRAILING" in (t.get("exit_reason") or "")]
    expired_winners = [t for t in trades if t["win"] and "EXPIRED" in (t.get("exit_reason") or "")]

    print(f"  Total trades: {total}")
    print(f"  Win rate: {win_rate:.1f}% ({wins}W / {losses}L)")
    print(f"  Total P&L: ${total_pnl:.2f}")
    print(f"  Avg P&L per trade: ${avg_pnl:.2f}")
    print(f"  Avg winner: ${avg_win:.2f}")
    print(f"  Avg loser: ${avg_loss:.2f}")
    print(f"  Profit factor: {abs(avg_win * wins) / abs(avg_loss * losses):.2f}x" if losses > 0 else "  Profit factor: ∞")
    print()
    print(f"  Trailing stop exits: {len(trailing_exits)}")
    print(f"  Expired (above floor): {len(expired_winners)}")
    print(f"  Expired (worthless): {losses}")
    print()
    print(f"  Avg high water mark: +{sum(t['high_water'] for t in trades)/total:.0f}%")
    print(f"  Best trade: {max(trades, key=lambda t: t['pnl_dollars'])['ticker']} +${max(t['pnl_dollars'] for t in trades):.2f}")
    print(f"  Worst trade: {min(trades, key=lambda t: t['pnl_dollars'])['ticker']} ${min(t['pnl_dollars'] for t in trades):.2f}")
    print()

    # Monthly breakdown
    print("  Monthly P&L:")
    monthly = {}
    for t in trades:
        month = t["entry_date"][:7]
        if month not in monthly:
            monthly[month] = {"trades": 0, "pnl": 0, "wins": 0}
        monthly[month]["trades"] += 1
        monthly[month]["pnl"] += t["pnl_dollars"]
        if t["win"]: monthly[month]["wins"] += 1

    for month in sorted(monthly.keys()):
        m = monthly[month]
        wr = m["wins"] / m["trades"] * 100 if m["trades"] > 0 else 0
        bar = "█" * int(max(0, m["pnl"]) / 20) if m["pnl"] > 0 else "░" * int(abs(m["pnl"]) / 20)
        print(f"    {month}: {m['trades']:3} trades | {wr:5.1f}% WR | ${m['pnl']:+8.2f} {bar}")

    # Save results
    # === SCORE vs PERFORMANCE (answers: do higher-scoring picks perform better?) ===
    # NOTE: this uses backtest.py's own score formula (40 + increments), which is NOT
    # identical to the live scanner's 0-100 score. Directionally comparable only.
    print()
    print("  Score vs performance:")
    _scs = [t["score"] for t in trades]
    _bk = [(0, 55), (55, 60), (60, 65), (65, 70), (70, 75), (75, 80), (80, 9999)]
    print(f"    {'bucket':>8} | {'n':>4} | {'win%':>5} | {'avg%':>7} | {'med%':>7} | {'avg$':>7} | {'PF':>6}")
    for lo, hi in _bk:
        b = [t for t in trades if lo <= t["score"] < hi]
        if not b:
            continue
        n = len(b)
        wr = sum(1 for t in b if t["win"]) / n * 100
        pcts = sorted(t["pnl_pct"] for t in b)
        med = pcts[n // 2] if n % 2 else (pcts[n // 2 - 1] + pcts[n // 2]) / 2
        avgp = sum(t["pnl_pct"] for t in b) / n
        avgd = sum(t["pnl_dollars"] for t in b) / n
        gw = sum(t["pnl_dollars"] for t in b if t["pnl_dollars"] > 0)
        gl = -sum(t["pnl_dollars"] for t in b if t["pnl_dollars"] < 0)
        pf = ("inf" if gl == 0 else f"{gw / gl:.2f}")
        lab = f"{lo}-{hi - 1}" if hi < 9999 else f"{lo}+"
        print(f"    {lab:>8} | {n:>4} | {wr:>4.0f}% | {avgp:>6.1f}% | {med:>6.1f}% | {avgd:>6.0f} | {pf:>6}")
    # Spearman rank correlation (score vs pnl%)
    _n = len(trades)
    if _n >= 3:
        def _ranks(v):
            order = sorted(range(_n), key=lambda i: v[i]); r = [0.0] * _n; i = 0
            while i < _n:
                j = i
                while j + 1 < _n and v[order[j + 1]] == v[order[i]]:
                    j += 1
                avg = (i + j) / 2.0 + 1
                for k in range(i, j + 1):
                    r[order[k]] = avg
                i = j + 1
            return r
        _ys = [t["pnl_pct"] for t in trades]
        _rx, _ry = _ranks(_scs), _ranks(_ys)
        _mx, _my = sum(_rx) / _n, sum(_ry) / _n
        _num = sum((_rx[i] - _mx) * (_ry[i] - _my) for i in range(_n))
        _den = (sum((_rx[i] - _mx) ** 2 for i in range(_n)) * sum((_ry[i] - _my) ** 2 for i in range(_n))) ** 0.5
        print(f"    Spearman(score, pnl%) = {(_num / _den if _den else 0):+.3f}  (>0 = higher score does better)")
    print()

    results = {
        "summary": {
            "total_trades": total,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl_per_trade": round(avg_pnl, 2),
            "avg_winner": round(avg_win, 2),
            "avg_loser": round(avg_loss, 2),
            "trailing_stop_exits": len(trailing_exits),
            "period_days": BACKTEST_DAYS,
        },
        "monthly": monthly,
        "trades": trades,
    }
    with open(OUTPUT_FILE, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  Full results saved to: {OUTPUT_FILE}")


if __name__ == "__main__":
    run_backtest()
