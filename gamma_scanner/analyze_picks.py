#!/usr/bin/env python3
"""
analyze_picks.py — READ-ONLY report on the scanner's pick log.

Pulls gamma_pick_history.json (and gamma_briefing.json for outcomes) from the public
gist, and reports:
  1) Pick volume — total, per-day, by direction/setup, recent pace
  2) Score distribution — buckets + median
  3) Funnel — which recorded picks became trades vs were skipped (cap/capital)
  4) Pick -> outcome — for entered picks that have closed, win rate / avg P&L by score

Runs anywhere (fetches the public gist). Becomes richer as the pick log fills.
Usage: python3 analyze_picks.py [gist_id]
"""
import sys, json, urllib.request
from collections import Counter, defaultdict
from statistics import median

GID = sys.argv[1] if len(sys.argv) > 1 else "e39d7fb7b6d1b7f4fbf26d190f4aa8dd"
RAW = "https://gist.githubusercontent.com/sarelnelson/%s/raw/%s"
BUCKETS = [(0, 65), (65, 70), (70, 75), (75, 80), (80, 85), (85, 101)]


def fetch(fn):
    try:
        with urllib.request.urlopen(RAW % (GID, fn), timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def bucket_label(lo, hi):
    return "%d-%d" % (lo, hi - 1) if hi <= 100 else "%d+" % lo


def okey(ticker, strike, exp):
    try:
        s = float(strike)
    except Exception:
        s = strike
    return (str(ticker).upper(), s, str(exp))


def main():
    picks = fetch("gamma_pick_history.json")
    if not picks:
        print("No pick history yet (gamma_pick_history.json isn't on the gist).")
        print("It populates after the next SCHEDULED scan following deploy — check back in a day.")
        return
    print("Pick log: %d picks recorded\n" % len(picks))

    # ---- 1) volume ----
    days = sorted({p.get("scan_time", "")[:10] for p in picks if p.get("scan_time")})
    per_day = Counter(p.get("scan_time", "")[:10] for p in picks)
    print("=== VOLUME ===")
    if days:
        print("range: %s .. %s  (%d scan-days)" % (days[0], days[-1], len(days)))
        print("avg picks/day: %.1f" % (len(picks) / max(1, len(days))))
        recent = days[-5:]
        print("recent days: " + ", ".join("%s=%d" % (d, per_day[d]) for d in recent))
    print("by direction: " + dict_str(Counter(p.get("direction") for p in picks)))
    print("by setup: " + dict_str(Counter(p.get("setup") for p in picks)))

    # ---- 2) score distribution ----
    scores = [p["score"] for p in picks if isinstance(p.get("score"), (int, float))]
    print("\n=== SCORE DISTRIBUTION ===")
    if scores:
        print("min %d | median %.0f | max %d" % (min(scores), median(scores), max(scores)))
        for lo, hi in BUCKETS:
            n = sum(1 for s in scores if lo <= s < hi)
            bar = "#" * int(40 * n / len(scores))
            print("  %6s | %4d %s" % (bucket_label(lo, hi), n, bar))

    # ---- 3) funnel: entered vs skipped ----
    brief = fetch("gamma_briefing.json")
    taken = {}
    if brief:
        for uid, u in (brief.get("users") or {}).items():
            for t in (u.get("open_positions") or []):
                taken[okey(t.get("ticker"), t.get("strike"), t.get("expiration"))] = (uid, "open", t)
            for t in (u.get("closed_all") or []):
                taken[okey(t.get("ticker"), t.get("strike"), t.get("expiration"))] = (uid, "closed", t)
    print("\n=== FUNNEL (recorded picks that became trades) ===")
    if not brief:
        print("  (briefing unavailable — can't match picks to trades)")
    else:
        entered = [p for p in picks if okey(p.get("ticker"), p.get("strike"), p.get("expiration")) in taken]
        print("  matched to a trade: %d of %d picks (%.0f%%)" % (
            len(entered), len(picks), 100.0 * len(entered) / len(picks)))
        print("  NOTE: matches only against current open + the gist's recent closed window,")
        print("        so older entered picks may show as 'not matched'.")

        # ---- 4) pick -> outcome (entered + closed) ----
        rows = []
        for p in picks:
            m = taken.get(okey(p.get("ticker"), p.get("strike"), p.get("expiration")))
            if m and m[1] == "closed" and isinstance(m[2].get("pnl"), (int, float)) and isinstance(p.get("score"), (int, float)):
                rows.append((p["score"], m[2]["pnl"]))
        print("\n=== PICK -> OUTCOME (entered picks that have closed: %d) ===" % len(rows))
        if rows:
            hdr = "  %6s | %4s | %5s | %9s"
            print(hdr % ("score", "n", "win%", "avg P&L$"))
            for lo, hi in BUCKETS:
                b = [pnl for sc, pnl in rows if lo <= sc < hi]
                if not b:
                    continue
                wr = 100.0 * sum(1 for x in b if x > 0) / len(b)
                print(hdr % (bucket_label(lo, hi), len(b), "%.0f" % wr, "%.0f" % (sum(b) / len(b))))
        else:
            print("  none closed yet — this fills in as entered picks exit.")


def dict_str(c):
    return ", ".join("%s=%d" % (k, v) for k, v in c.items())


if __name__ == "__main__":
    main()
