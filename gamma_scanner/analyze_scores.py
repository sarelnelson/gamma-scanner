#!/usr/bin/env python3
"""
analyze_scores.py — READ-ONLY analysis: do higher-scoring picks perform better?

Scans DATA_DIR for every CLOSED trade that has both an entry `score` and a
realized outcome, dedupes them, buckets by entry score, and reports win rate,
average/median P&L%, average $ P&L, and profit factor per bucket. Also prints a
simple rank correlation (Spearman) between score and P&L%.

Sources (whichever exist):
  - {DATA_DIR}/position_history/_summary.json  (trades[])
  - {DATA_DIR}/user_*/trades.json              (status == closed)
  - any *.json under DATA_DIR that looks like a closed trade

Nothing is written. Safe to run against live data.

Run inside the container (data lives on the docker volume):
  CID=$(docker compose ps -q | head -1)
  docker cp analyze_scores.py "$CID":/tmp/analyze_scores.py
  docker exec "$CID" python3 /tmp/analyze_scores.py
"""
import os, json, glob, math

DATA_DIR = os.environ.get("GAMMA_DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else os.path.dirname(os.path.abspath(__file__)))

# Score buckets (lower-inclusive, upper-exclusive); last is open-ended
BUCKETS = [(0, 65), (65, 70), (70, 75), (75, 80), (80, 85), (85, 101)]


def realized_pnl_pct(t):
    """Return realized pnl_pct for a closed trade, or None if not determinable."""
    # explicit pnl_pct field
    for k in ("pnl_pct", "exit_pnl_pct"):
        v = t.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    # derive from bids/costs
    cost = t.get("option_cost") or t.get("entry_cost") or t.get("cost_per_contract")
    exit_bid = t.get("exit_option_bid") or t.get("exit_fill_price") or t.get("exit_fill_estimate")
    if isinstance(cost, (int, float)) and cost and isinstance(exit_bid, (int, float)):
        base = cost if cost < 50 else cost / 100.0  # normalize per-share vs per-contract
        eb = exit_bid if exit_bid < 50 else exit_bid / 100.0
        return (eb - base) / base * 100.0
    return None


def realized_pnl_dollars(t):
    for k in ("pnl", "pnl_dollars"):
        v = t.get(k)
        if isinstance(v, (int, float)) and v != 0:
            return float(v)
    v = t.get("pnl")
    return float(v) if isinstance(v, (int, float)) else None


def is_closed(t):
    st = str(t.get("status", "")).lower()
    if st == "closed":
        return True
    if t.get("exit") or t.get("exit_date") or t.get("exit_reason") or t.get("exit_time"):
        return True
    return False


def trade_key(t):
    return (
        t.get("ticker"),
        t.get("option_strike") or t.get("strike"),
        t.get("option_exp") or t.get("expiration"),
        t.get("entry_date"),
    )


def collect():
    seen = {}
    files = []
    sm = os.path.join(DATA_DIR, "position_history", "_summary.json")
    if os.path.exists(sm):
        files.append(sm)
    files += glob.glob(os.path.join(DATA_DIR, "user_*", "trades.json"))
    files += glob.glob(os.path.join(DATA_DIR, "*trades*.json"))

    def consider(t, src):
        if not isinstance(t, dict):
            return
        score = t.get("score")
        if not isinstance(score, (int, float)):
            return
        # _summary trades already represent closed trades; others must be closed
        if src.endswith("_summary.json") or is_closed(t):
            pct = realized_pnl_pct(t)
            if pct is None:
                return
            k = trade_key(t)
            # prefer the record with the most fields
            if k not in seen:
                seen[k] = {
                    "ticker": t.get("ticker"),
                    "score": float(score),
                    "pnl_pct": pct,
                    "pnl_dollars": realized_pnl_dollars(t),
                    "high_water": t.get("high_water") or t.get("high_water_pct"),
                    "days_held": t.get("days_held"),
                    "src": os.path.relpath(src, DATA_DIR),
                }

    for fp in files:
        try:
            with open(fp) as f:
                data = json.load(f)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("trades"), list):
            for t in data["trades"]:
                consider(t, fp)
        elif isinstance(data, list):
            for t in data:
                consider(t, fp)
    return list(seen.values())


def spearman(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    def ranks(v):
        order = sorted(range(n), key=lambda i: v[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    den = math.sqrt(sum((rx[i] - mx) ** 2 for i in range(n)) * sum((ry[i] - my) ** 2 for i in range(n)))
    return num / den if den else None


def stats(rows):
    n = len(rows)
    if not n:
        return None
    wins = [r for r in rows if r["pnl_pct"] > 0]
    pcts = sorted(r["pnl_pct"] for r in rows)
    med = pcts[n // 2] if n % 2 else (pcts[n // 2 - 1] + pcts[n // 2]) / 2
    dollars = [r["pnl_dollars"] for r in rows if isinstance(r["pnl_dollars"], (int, float))]
    gross_win = sum(d for d in dollars if d > 0)
    gross_loss = -sum(d for d in dollars if d < 0)
    pf = (gross_win / gross_loss) if gross_loss else float("inf")
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "avg_pct": sum(r["pnl_pct"] for r in rows) / n,
        "med_pct": med,
        "avg_dollars": (sum(dollars) / len(dollars)) if dollars else None,
        "profit_factor": pf,
    }


def main():
    rows = collect()
    print(f"DATA_DIR = {DATA_DIR}")
    print(f"Closed trades with score + outcome: {len(rows)}\n")
    if not rows:
        print("No closed scored trades found yet. (Need trades to close with a recorded score.)")
        return

    hdr = f"{'bucket':>10} | {'n':>3} | {'win%':>6} | {'avg%':>8} | {'med%':>7} | {'avg$':>8} | {'PF':>6}"
    print(hdr); print("-" * len(hdr))
    for lo, hi in BUCKETS:
        b = [r for r in rows if lo <= r["score"] < hi]
        s = stats(b)
        label = f"{lo}-{hi-1}" if hi <= 100 else f"{lo}+"
        if not s:
            print(f"{label:>10} | {0:>3} |   —    |    —     |   —    |    —     |   —")
            continue
        avgd = f"{s['avg_dollars']:>8.0f}" if s['avg_dollars'] is not None else "     —"
        pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        print(f"{label:>10} | {s['n']:>3} | {s['win_rate']:>5.0f}% | {s['avg_pct']:>7.1f}% | {s['med_pct']:>6.1f}% | {avgd} | {pf:>6}")

    alls = stats(rows)
    pf = "inf" if alls["profit_factor"] == float("inf") else f"{alls['profit_factor']:.2f}"
    print("-" * len(hdr))
    print(f"{'ALL':>10} | {alls['n']:>3} | {alls['win_rate']:>5.0f}% | {alls['avg_pct']:>7.1f}% | {alls['med_pct']:>6.1f}% | "
          + (f"{alls['avg_dollars']:>8.0f}" if alls['avg_dollars'] is not None else "     —") + f" | {pf:>6}")

    rho = spearman([r["score"] for r in rows], [r["pnl_pct"] for r in rows])
    print(f"\nSpearman rank corr (score vs pnl%): " + ("n/a (need >=3)" if rho is None else f"{rho:+.3f}"))
    print("  >0 means higher score -> better outcome. Near 0 means score doesn't predict.")
    print(f"\nSample size caveat: {len(rows)} trades is "
          + ("very small — treat as directional only." if len(rows) < 30 else "modest — still preliminary."))


if __name__ == "__main__":
    main()
