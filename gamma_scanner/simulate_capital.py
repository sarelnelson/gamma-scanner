#!/usr/bin/env python3
"""
simulate_capital.py — READ-ONLY what-if replay.

Question: with NO position cap and a limited starting balance, how would the trades
we ACTUALLY took have played out? Replays real closed trades (real entry cost, real
realized P&L, real timestamps) as a cash-constrained event simulation.

IMPORTANT SCOPE / CAVEAT:
  This uses only trades that were actually ENTERED. Picks the 20-position cap turned
  away were never taken and have no recorded outcome, so they are NOT here. Therefore:
    - The "infinite balance" row ~= what actually happened (you took every pick you could
      within the 20 cap), and its MAX CONCURRENT tells you whether the 20 cap ever bound.
    - If max concurrent stayed <= 20, the cap never limited this account, so "no cap"
      would not have changed anything on the trades we have.
    - The smaller-balance rows show how starting capital alone would have gated entries.

Usage (inside the container, where /app/data lives):
  CID=$(docker compose ps -q | head -1)
  docker cp simulate_capital.py "$CID":/tmp/simulate_capital.py
  docker exec "$CID" python3 /tmp/simulate_capital.py            # default: scanner
  docker exec "$CID" python3 /tmp/simulate_capital.py sarel      # a specific account
"""
import os, sys, json, glob
from datetime import datetime

DATA_DIR = os.environ.get("GAMMA_DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else os.path.dirname(os.path.abspath(__file__)))
BALANCES = [1000.0, 2000.0, 5000.0, None]  # None = infinite


def parse_ts(t, fallback_date):
    for v in (t, fallback_date):
        if not v:
            continue
        s = str(v)
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:26] if "." in s else s[:19] if "T" in s else s[:10], fmt)
            except Exception:
                continue
    return None


def load_closed(user):
    fp = os.path.join(DATA_DIR, "user_%s" % user, "trades.json")
    try:
        trades = json.load(open(fp))
    except Exception as e:
        print("could not read %s: %s" % (fp, e))
        return []
    out = []
    for t in trades if isinstance(trades, list) else []:
        if t.get("status") not in ("closed", "expired"):
            continue
        cost = t.get("cost_per_contract")
        if not cost:
            oc = t.get("option_cost") or 0
            cost = oc * 100 if oc and oc < 50 else oc
        ncon = t.get("num_contracts") or 1
        deployed = float(cost or 0) * ncon
        pnl = t.get("pnl")
        if not isinstance(pnl, (int, float)) or deployed <= 0:
            continue
        entry = parse_ts(t.get("entry_time"), t.get("entry_date"))
        exit_ = parse_ts(t.get("exit_time"), t.get("exit_date"))
        if entry is None:
            continue
        if exit_ is None:
            exit_ = entry
        out.append({"ticker": t.get("ticker"), "entry": entry, "exit": exit_,
                    "deployed": deployed, "pnl": float(pnl)})
    out.sort(key=lambda x: x["entry"])
    return out


def simulate(trades, start_balance):
    """Event-driven cash replay, NO position cap. start_balance None = infinite."""
    events = []
    for i, t in enumerate(trades):
        events.append((t["entry"], 0, i))   # 0 = entry (process entries first at a tie)
        events.append((t["exit"], 1, i))    # 1 = exit
    events.sort(key=lambda e: (e[0], e[1]))
    inf = start_balance is None
    cash = float("inf") if inf else start_balance
    taken = set()
    concurrent = 0
    max_concurrent = 0
    entered = skipped = 0
    realized = 0.0
    for when, kind, i in events:
        t = trades[i]
        if kind == 0:  # entry
            if inf or cash >= t["deployed"]:
                if not inf:
                    cash -= t["deployed"]
                taken.add(i)
                entered += 1
                concurrent += 1
                max_concurrent = max(max_concurrent, concurrent)
            else:
                skipped += 1
        else:  # exit
            if i in taken:
                if not inf:
                    cash += t["deployed"] + t["pnl"]
                realized += t["pnl"]
                concurrent -= 1
    end_equity = None if inf else cash
    return {"entered": entered, "skipped": skipped, "max_concurrent": max_concurrent,
            "realized": realized, "end_equity": end_equity}


def main():
    user = sys.argv[1] if len(sys.argv) > 1 else "scanner"
    trades = load_closed(user)
    print("DATA_DIR=%s  account=%s  closed trades=%d" % (DATA_DIR, user, len(trades)))
    if not trades:
        print("No closed trades with cost+pnl+timestamps for this account.")
        return
    span = "%s .. %s" % (trades[0]["entry"].date(), max(t["exit"] for t in trades).date())
    print("period: %s\n" % span)
    hdr = "%-10s | %7s | %7s | %13s | %11s | %11s | %8s" % (
        "balance", "entered", "skipped", "maxConcurrent", "realized$", "endEquity", "return%")
    print(hdr); print("-" * len(hdr))
    for b in BALANCES:
        r = simulate(trades, b)
        label = "inf" if b is None else "$%d" % int(b)
        ee = "-" if r["end_equity"] is None else "$%.0f" % r["end_equity"]
        ret = "-" if b is None else ("%+.0f%%" % ((r["end_equity"] - b) / b * 100))
        print("%-10s | %7d | %7d | %13d | %10.0f | %11s | %8s" % (
            label, r["entered"], r["skipped"], r["max_concurrent"], r["realized"], ee, ret))
    inf_r = simulate(trades, None)
    print("\nMax positions held at once (no cap, unlimited cash): %d" % inf_r["max_concurrent"])
    if inf_r["max_concurrent"] <= 20:
        print("=> The 20-position cap NEVER bound for this account's taken trades — removing it")
        print("   would not have changed these results. (Cap-rejected picks aren't in the data.)")
    else:
        print("=> The account wanted MORE than 20 concurrent — but note trades the cap actually")
        print("   rejected were never recorded, so real 'no cap' upside is UNDERSTATED here.")


if __name__ == "__main__":
    main()
