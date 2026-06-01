"""
filtered_retest.py
==================
Backtests the retest entry strategy on gainers.db, restricted to
high-quality signals:  is_breakout = 1  AND  volume_ratio >= 2.0

Entry methods tested:
  M1  Enter at close of Day 3 after appearance
  M2  Enter at close of Day 5 after appearance
  M3  Enter when price drops -10% from appearance close
  M4  Enter when price drops -15% from appearance close
  M5  Enter when price drops -20% from appearance close

Additional filter combinations also tested.

Output: printed tables + filtered_retest.txt
"""

import sqlite3
import os
import sys
from collections import defaultdict

DB_PATH     = "gainers.db"
OUTPUT_FILE = "filtered_retest.txt"
MAX_DAYS    = 20   # max forward trading days tracked per appearance

# ── stats helpers ─────────────────────────────────────────────────────────────

def mean(lst):
    return sum(lst) / len(lst) if lst else None

def win_rate(lst):
    return sum(1 for x in lst if x > 0) / len(lst) * 100 if lst else None

def profit_factor(lst):
    gains  = sum(x for x in lst if x > 0)
    losses = sum(abs(x) for x in lst if x < 0)
    return gains / losses if losses > 0 else (float("inf") if gains > 0 else None)

def max_dd_from_entry(daily):
    """Worst intra-path drawdown measured from the entry close (day 0 = 0%)."""
    worst = 0.0
    for d in daily:
        if d["chg_pct"] < worst:
            worst = d["chg_pct"]
    return worst

def fp(v, d=1):
    if v is None: return "  N/A"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{d}f}%"

def fn(v, d=2):
    if v is None: return "  N/A"
    return f"{v:.{d}f}"

def pad(s, w, align="<"):
    return format(str(s), f"{align}{w}")

# ── database ──────────────────────────────────────────────────────────────────

def load_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    gainers = conn.execute("""
        SELECT dg.date, dg.rank, dg.symbol,
               dg.price_change_pct  AS app_pct,
               dg.current_price     AS entry_px,
               dg.volume_usdt,
               ch.volume_ratio, ch.body_pct, ch.upper_wick,
               ch.is_breakout, ch.days_since_last_pump
        FROM   daily_gainers dg
        LEFT JOIN candle_history ch
               ON dg.symbol = ch.symbol AND dg.date = ch.date
        ORDER  BY dg.date, dg.rank
    """).fetchall()

    candles = conn.execute(
        "SELECT symbol, date, close FROM candle_history ORDER BY symbol, date"
    ).fetchall()
    conn.close()

    sym_dates = defaultdict(list)
    close_map = {}
    for r in candles:
        sym_dates[r["symbol"]].append(r["date"])
        close_map[(r["symbol"], r["date"])] = r["close"]

    return gainers, sym_dates, close_map


# ── build price paths ─────────────────────────────────────────────────────────

def build_paths(gainers, sym_dates, close_map):
    paths = []
    for g in gainers:
        sym      = g["symbol"]
        edate    = g["date"]
        entry_px = g["entry_px"]
        if not entry_px or entry_px == 0:
            continue
        dates = sym_dates.get(sym, [])
        try:
            idx = dates.index(edate)
        except ValueError:
            continue

        daily = []
        for offset in range(1, MAX_DAYS + 1):
            fi = idx + offset
            if fi >= len(dates):
                break
            fc = close_map.get((sym, dates[fi]))
            if fc is None:
                continue
            daily.append({"day": offset,
                          "chg_pct": (fc - entry_px) / entry_px * 100})

        if not daily:
            continue

        paths.append({
            "symbol":      sym,
            "date":        edate,
            "rank":        g["rank"],
            "entry_px":    entry_px,
            "app_pct":     g["app_pct"],
            "vol_ratio":   g["volume_ratio"],
            "body_pct":    g["body_pct"],
            "is_breakout": g["is_breakout"],
            "daily":       daily,
        })
    return paths


# ── filter helper ─────────────────────────────────────────────────────────────

def apply_filter(paths, is_breakout=True, min_vol_ratio=2.0,
                 min_body_pct=None, min_app_pct=None):
    out = []
    for p in paths:
        if is_breakout and p["is_breakout"] != 1:
            continue
        vr = p["vol_ratio"]
        if vr is None or vr < min_vol_ratio:
            continue
        if min_body_pct is not None:
            bp = p["body_pct"]
            if bp is None or bp < min_body_pct:
                continue
        if min_app_pct is not None:
            ap = p["app_pct"]
            if ap is None or ap < min_app_pct:
                continue
        out.append(p)
    return out


# ── entry simulators ──────────────────────────────────────────────────────────

def sim_fixed_day(paths, entry_day, measure_days):
    """Enter at entry_day close. Measure at measure_days after appearance."""
    trades = []
    for p in paths:
        erow = next((d for d in p["daily"] if d["day"] == entry_day), None)
        if erow is None:
            continue
        epx_scale = 1 + erow["chg_pct"] / 100   # relative to appearance close
        if epx_scale == 0:
            continue

        # forward daily relative to retest entry
        post = []
        for d in p["daily"]:
            if d["day"] > entry_day:
                ret = ((1 + d["chg_pct"] / 100) / epx_scale - 1) * 100
                post.append({"day": d["day"], "ret": ret})

        d7  = next((d["ret"] for d in post if d["day"] == 7),  None)
        d14 = next((d["ret"] for d in post if d["day"] == 14), None)
        mdd = min((d["ret"] for d in post), default=0)

        trades.append({
            "symbol": p["symbol"], "date": p["date"],
            "d7": d7, "d14": d14, "mdd": mdd,
        })
    return trades


def sim_dip(paths, dip_pct, measure_days):
    """Enter when price drops dip_pct% from appearance. Measure measure_days after trigger."""
    trades = []
    for p in paths:
        trow = next((d for d in p["daily"] if d["chg_pct"] <= -dip_pct), None)
        if trow is None:
            continue
        epx_scale = 1 + trow["chg_pct"] / 100
        if epx_scale == 0:
            continue

        tday = trow["day"]
        post = []
        for d in p["daily"]:
            if d["day"] > tday:
                ret = ((1 + d["chg_pct"] / 100) / epx_scale - 1) * 100
                post.append({"days_after_trigger": d["day"] - tday, "ret": ret})

        d7  = next((d["ret"] for d in post if d["days_after_trigger"] == 7),  None)
        d14 = next((d["ret"] for d in post if d["days_after_trigger"] == 14), None)
        mdd = min((d["ret"] for d in post), default=0)

        trades.append({
            "symbol": p["symbol"], "date": p["date"],
            "trigger_day": tday,
            "d7": d7, "d14": d14, "mdd": mdd,
        })
    return trades


# ── metrics from trade list ───────────────────────────────────────────────────

def metrics(trades, horizon="d7"):
    rets = [t[horizon] for t in trades if t[horizon] is not None]
    mdds = [t["mdd"]   for t in trades]
    if not rets:
        return None
    best  = max(trades, key=lambda t: t[horizon] if t[horizon] is not None else -999)
    worst = min(trades, key=lambda t: t[horizon] if t[horizon] is not None else  999)
    return {
        "n":       len(trades),
        "n_meas":  len(rets),
        "wr":      win_rate(rets),
        "avg":     mean(rets),
        "pf":      profit_factor(rets),
        "mdd":     mean(mdds),
        "best":    best,
        "worst":   worst,
        "rets":    rets,
    }


# ── print a method block ──────────────────────────────────────────────────────

def print_method(lines_out, label, trades, n_signals_total):
    def p(s=""):
        print(s)
        lines_out.append(s)

    def rule(): p("  " + "─" * 70)

    m7  = metrics(trades, "d7")
    m14 = metrics(trades, "d14")

    triggered = len(trades)
    trig_pct  = triggered / n_signals_total * 100 if n_signals_total else 0

    p(f"  {label}")
    rule()
    p(f"  Signals (universe) : {n_signals_total}")
    p(f"  Triggered          : {triggered}  ({trig_pct:.0f}% of universe)")

    if not m7:
        p("  *** No trades with Day-7 data ***")
        p()
        return

    p()
    p(f"  {'Metric':<28}  {'Day-7':>10}  {'Day-14':>10}")
    rule()
    p(f"  {'Win Rate':<28}  {fp(m7['wr']):>10}  {fp(m14['wr'] if m14 else None):>10}")
    p(f"  {'Avg Return':<28}  {fp(m7['avg']):>10}  {fp(m14['avg'] if m14 else None):>10}")
    p(f"  {'Profit Factor':<28}  {fn(m7['pf']):>10}  {fn(m14['pf'] if m14 else None):>10}")
    p(f"  {'Avg Max DD from entry':<28}  {fp(m7['mdd']):>10}")
    p(f"  {'Measured trades':<28}  {m7['n_meas']:>10}  {m14['n_meas'] if m14 else 'N/A':>10}")

    p()
    b7  = m7["best"]
    w7  = m7["worst"]
    p(f"  Best  trade (D7): {b7['symbol']} on {b7['date']}  →  {fp(b7['d7'])}")
    p(f"  Worst trade (D7): {w7['symbol']} on {w7['date']}  →  {fp(w7['d7'])}")
    p()


# ── main analysis ─────────────────────────────────────────────────────────────

def run(paths, filter_label, filter_paths, lines_out):
    def p(s=""):
        print(s)
        lines_out.append(s)

    def sep(c="═", w=74): p(c * w)

    n = len(filter_paths)

    sep()
    p(f"  FILTER : {filter_label}")
    p(f"  Universe after filter : {n} appearances"
      f"  (of {len(paths)} total paths)")
    sep()
    p()

    if n == 0:
        p("  No appearances match this filter.")
        p()
        return

    METHODS = [
        ("M1 — Enter Day 3 close",
         lambda fp2: sim_fixed_day(fp2, 3,  [7, 14])),
        ("M2 — Enter Day 5 close",
         lambda fp2: sim_fixed_day(fp2, 5,  [7, 14])),
        ("M3 — Enter on -10% dip",
         lambda fp2: sim_dip(fp2, 10, [7, 14])),
        ("M4 — Enter on -15% dip",
         lambda fp2: sim_dip(fp2, 15, [7, 14])),
        ("M5 — Enter on -20% dip",
         lambda fp2: sim_dip(fp2, 20, [7, 14])),
    ]

    for label, sim_fn in METHODS:
        trades = sim_fn(filter_paths)
        print_method(lines_out, label, trades, n)

    # summary comparison table
    p("  SUMMARY — all methods at Day-7")
    p("  " + "─" * 70)
    p(f"  {'Method':<26}  {'Triggered':>10}  {'WR D7':>8}  "
      f"{'Avg D7':>8}  {'PF D7':>7}  {'MDD':>8}")
    p("  " + "─" * 70)
    for label, sim_fn in METHODS:
        trades = sim_fn(filter_paths)
        m7     = metrics(trades, "d7")
        short  = label.split("—")[0].strip() + " " + label.split("—")[1].strip()
        if m7:
            p(f"  {short:<26}  {len(trades):>10}  "
              f"{fp(m7['wr']):>8}  {fp(m7['avg']):>8}  "
              f"{fn(m7['pf']):>7}  {fp(m7['mdd']):>8}")
        else:
            p(f"  {short:<26}  {len(trades):>10}  {'N/A':>8}  "
              f"{'N/A':>8}  {'N/A':>7}  {'N/A':>8}")
    p()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found")
        sys.exit(1)

    gainers, sym_dates, close_map = load_db()
    paths = build_paths(gainers, sym_dates, close_map)

    conn = sqlite3.connect(DB_PATH)
    total_rows = conn.execute("SELECT COUNT(*) FROM daily_gainers").fetchone()[0]
    date_range = conn.execute(
        "SELECT MIN(date)||' → '||MAX(date) FROM daily_gainers"
    ).fetchone()[0]
    conn.close()

    lines_out = []

    def p(s=""):
        print(s)
        lines_out.append(s)

    def sep(c="═", w=74): p(c * w)

    sep()
    p("  FILTERED RETEST BACKTEST  —  gainers.db")
    p(f"  daily_gainers rows: {total_rows}   |   paths built: {len(paths)}   |   {date_range}")
    if len(paths) < 100:
        p()
        p(f"  ⚠  SMALL SAMPLE ({len(paths)} paths). Run historical_gainers.py")
        p(f"     to backfill 12 months (~1 460 appearances) for reliable stats.")
    sep()
    p()

    # ── Filter combinations ────────────────────────────────────────────────────

    FILTERS = [
        # label, kwargs for apply_filter
        (
            "Breakout=YES + VolRatio ≥ 2.0  (base filter)",
            dict(is_breakout=True, min_vol_ratio=2.0),
        ),
        (
            "Breakout=YES + VolRatio ≥ 2.0 + Body% ≥ 5%",
            dict(is_breakout=True, min_vol_ratio=2.0, min_body_pct=5.0),
        ),
        (
            "Breakout=YES + VolRatio ≥ 2.0 + App% ≥ 10%",
            dict(is_breakout=True, min_vol_ratio=2.0, min_app_pct=10.0),
        ),
        (
            "Breakout=YES + VolRatio ≥ 2.0 + Body% ≥ 5% + App% ≥ 10%",
            dict(is_breakout=True, min_vol_ratio=2.0, min_body_pct=5.0, min_app_pct=10.0),
        ),
    ]

    for label, kwargs in FILTERS:
        filtered = apply_filter(paths, **kwargs)
        run(paths, label, filtered, lines_out)

    # ── trade log for base filter ──────────────────────────────────────────────
    base_paths = apply_filter(paths, is_breakout=True, min_vol_ratio=2.0)
    sep()
    p("  TRADE LOG  (base filter: Breakout=YES + VolRatio ≥ 2x)")
    sep()
    p()
    p(f"  {'Date':<12}  {'#':<2}  {'Symbol':<14}  {'App%':>7}  "
      f"{'VolR':>6}  {'Body%':>6}  {'D7':>8}  {'D14':>8}")
    p("  " + "─" * 70)
    for path in sorted(base_paths, key=lambda x: (x["date"], x["rank"])):
        d7  = next((d["chg_pct"] for d in path["daily"] if d["day"] == 7),  None)
        d14 = next((d["chg_pct"] for d in path["daily"] if d["day"] == 14), None)
        vr  = f"{path['vol_ratio']:.1f}x" if path["vol_ratio"] is not None else "N/A"
        bp  = f"{path['body_pct']:.1f}%" if path["body_pct"] is not None else "N/A"
        p(f"  {path['date']:<12}  {path['rank']:<2}  {path['symbol']:<14}"
          f"  {fp(path['app_pct']):>7}  {vr:>6}  {bp:>6}  "
          f"{fp(d7):>8}  {fp(d14):>8}")
    p()
    sep()

    with open(OUTPUT_FILE, "w") as fh:
        fh.write("\n".join(lines_out) + "\n")

    print(f"\n  ✓ Saved → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
