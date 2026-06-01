"""
retest_analysis.py
==================
Analyses gainers.db to find the optimal retest entry strategy.

Hypothesis: coins in Top-4 gainers dump for several days after the
appearance, then rally. Find the best entry point after the dump.

Uses candle_history to build day-by-day forward price paths (no
dependency on followup_prices — works with any data volume).

Output: printed tables + retest_analysis.txt
"""

import sqlite3
import os
import sys
from collections import defaultdict
from datetime import datetime

DB_PATH     = "gainers.db"
OUTPUT_FILE = "retest_analysis.txt"
MAX_DAYS    = 20          # how many forward trading days to track

# ── tiny stats helpers ────────────────────────────────────────────────────────

def mean(lst):
    return sum(lst) / len(lst) if lst else None

def median(lst):
    if not lst:
        return None
    s = sorted(lst)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2

def win_rate(lst):
    return sum(1 for x in lst if x > 0) / len(lst) * 100 if lst else None

def pct_where(lst, fn):
    return sum(1 for x in lst if fn(x)) / len(lst) * 100 if lst else None

def f(v, d=1, sfx=""):
    if v is None:
        return "N/A"
    sign = "+" if isinstance(v, float) and v > 0 else ""
    return f"{sign}{v:.{d}f}{sfx}"

def fp(v):    return f(v, 1, "%") if v is not None else "N/A"
def col(s, w, align="<"):  return format(str(s), f"{align}{w}")

# ── database ──────────────────────────────────────────────────────────────────

def load_all():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    gainers = conn.execute("""
        SELECT dg.date, dg.rank, dg.symbol,
               dg.price_change_pct, dg.current_price, dg.volume_usdt,
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

    # build {symbol: [sorted date strings]}  and  {(symbol,date): close}
    sym_dates  = defaultdict(list)
    close_map  = {}
    for r in candles:
        sym_dates[r["symbol"]].append(r["date"])
        close_map[(r["symbol"], r["date"])] = r["close"]

    return gainers, sym_dates, close_map


# ── build forward price paths ─────────────────────────────────────────────────

def build_paths(gainers, sym_dates, close_map):
    """
    For every gainer appearance build a dict with:
      - meta (symbol, date, rank, appearance_pct, candle flags)
      - daily_changes: list of {day, chg_pct} from entry close
      - max_dd, dd_day, peak_gain, recovered, new_high_{10,20,30},
        recovery_days
    """
    paths = []

    for g in gainers:
        sym        = g["symbol"]
        entry_date = g["date"]
        entry_px   = g["current_price"]

        if not entry_px or entry_px == 0:
            continue

        dates = sym_dates.get(sym, [])
        try:
            idx = dates.index(entry_date)
        except ValueError:
            continue

        daily = []
        for offset in range(1, MAX_DAYS + 1):
            fi = idx + offset
            if fi >= len(dates):
                break
            fd  = dates[fi]
            fc  = close_map.get((sym, fd))
            if fc is None:
                continue
            daily.append({"day": offset, "date": fd,
                          "chg_pct": (fc - entry_px) / entry_px * 100})

        if not daily:
            continue

        # drawdown / peak
        min_row = min(daily, key=lambda x: x["chg_pct"])
        dd_pct  = min_row["chg_pct"]
        dd_day  = min_row["day"]
        peak    = max(d["chg_pct"] for d in daily)

        # recovery after dd_day
        post_dd    = [d for d in daily if d["day"] > dd_day]
        recovered  = any(d["chg_pct"] >= 0   for d in post_dd)
        nh10       = any(d["chg_pct"] >= 10  for d in post_dd)
        nh20       = any(d["chg_pct"] >= 20  for d in post_dd)
        nh30       = any(d["chg_pct"] >= 30  for d in post_dd)

        rec_days = None
        for d in post_dd:
            if d["chg_pct"] >= 0:
                rec_days = d["day"] - dd_day
                break

        paths.append({
            "symbol":        sym,
            "entry_date":    entry_date,
            "rank":          g["rank"],
            "entry_px":      entry_px,
            "app_pct":       g["price_change_pct"],
            "vol_ratio":     g["volume_ratio"],
            "body_pct":      g["body_pct"],
            "upper_wick":    g["upper_wick"],
            "is_breakout":   g["is_breakout"],
            "daily":         daily,
            "max_dd":        dd_pct,
            "dd_day":        dd_day,
            "peak":          peak,
            "recovered":     recovered,
            "nh10":          nh10,
            "nh20":          nh20,
            "nh30":          nh30,
            "recovery_days": rec_days,
            "n_days":        len(daily),
        })

    return paths


# ── entry simulators ──────────────────────────────────────────────────────────

def sim_fixed_day(paths, entry_day, measure_days):
    """
    Buy at end of `entry_day` after appearance.
    Returns {measure_day: [returns_pct]} measured from that entry price.
    """
    out = {md: [] for md in measure_days}
    for p in paths:
        erow = next((d for d in p["daily"] if d["day"] == entry_day), None)
        if erow is None:
            continue
        epx = p["entry_px"] * (1 + erow["chg_pct"] / 100)
        if epx == 0:
            continue
        for md in measure_days:
            if md <= entry_day:
                continue
            mrow = next((d for d in p["daily"] if d["day"] == md), None)
            if mrow is None:
                continue
            mpx = p["entry_px"] * (1 + mrow["chg_pct"] / 100)
            out[md].append((mpx - epx) / epx * 100)
    return out


def sim_dip(paths, dip_pct, measure_days):
    """
    Buy the first time price drops `dip_pct`% below appearance close.
    Returns {measure_day: [returns]} plus n_triggered count.
    """
    out = {md: [] for md in measure_days}
    n   = 0
    for p in paths:
        trow = next((d for d in p["daily"] if d["chg_pct"] <= -dip_pct), None)
        if trow is None:
            continue
        n  += 1
        epx = p["entry_px"] * (1 + trow["chg_pct"] / 100)
        if epx == 0:
            continue
        for md in measure_days:
            mrow = next((d for d in p["daily"]
                         if d["day"] > trow["day"] and d["day"] <= trow["day"] + md), None)
            # measure exactly `md` days after trigger
            target_day = trow["day"] + md
            mrow = next((d for d in p["daily"] if d["day"] == target_day), None)
            if mrow is None:
                continue
            mpx = p["entry_px"] * (1 + mrow["chg_pct"] / 100)
            out[md].append((mpx - epx) / epx * 100)
    out["n"] = n
    return out


# ── analysis ──────────────────────────────────────────────────────────────────

def analyse(paths, total_rows, date_range):
    lines = []

    def p(*a):
        line = " ".join(str(x) for x in a)
        print(line)
        lines.append(line)

    def sep(c="═", w=74):  p(c * w)
    def rule(w=74):         p("─" * w)
    def blank():            p("")

    n = len(paths)

    # ── header ────────────────────────────────────────────────────────────────
    sep()
    p(f"  RETEST ENTRY STRATEGY ANALYSIS  —  gainers.db")
    p(f"  daily_gainers rows : {total_rows}   |   paths built : {n}   |   {date_range}")
    if n < 100:
        blank()
        p(f"  ⚠  SMALL SAMPLE ({n} paths). Run historical_gainers.py to backfill")
        p(f"     12 months (~1 460 appearances) for statistically solid results.")
    sep()

    # ══════════════════════════════════════════════════════════════════════════
    blank()
    sep()
    p("  1. DUMP DEPTH ANALYSIS")
    sep()
    blank()

    all_dd  = [p2["max_dd"] for p2 in paths]
    all_ddd = [p2["dd_day"] for p2 in paths]

    p(f"  Avg max drawdown from entry :  {fp(mean(all_dd))}")
    p(f"  Median max drawdown         :  {fp(median(all_dd))}")
    p(f"  Worst drawdown              :  {fp(min(all_dd))}")
    p(f"  Avg day of max drawdown     :  Day {f(mean(all_ddd), 1)}")
    p(f"  Median day of max drawdown  :  Day {f(median(all_ddd), 1)}")
    blank()

    p(f"  {'Threshold':<22}  {'% of appearances':>17}  {'Count':>6}")
    rule()
    for th in [0, -5, -10, -15, -20, -25, -30]:
        label = "Any drop" if th == 0 else f"Drop > {abs(th)}%"
        cnt   = sum(1 for x in all_dd if x <= th)
        pct   = cnt / n * 100 if n else 0
        p(f"  {label:<22}  {pct:>16.1f}%  {cnt:>6}")

    blank()
    p(f"  Day-by-day avg change from entry close:")
    blank()
    p(f"  {'Day':<6}  {'Avg %':>8}  {'Median %':>9}  {'Win Rate':>9}  {'Samples':>8}")
    rule()
    for day in range(1, MAX_DAYS + 1):
        chgs = [d["chg_pct"] for p2 in paths for d in p2["daily"] if d["day"] == day]
        if not chgs:
            break
        p(f"  {'Day '+str(day):<6}  {fp(mean(chgs)):>8}  {fp(median(chgs)):>9}"
          f"  {fp(win_rate(chgs)):>9}  {len(chgs):>8}")

    # ══════════════════════════════════════════════════════════════════════════
    blank()
    sep()
    p("  2. RECOVERY ANALYSIS  (after the max drawdown)")
    sep()
    blank()

    dumped = [p2 for p2 in paths if p2["max_dd"] < 0]
    nd     = len(dumped)

    if nd == 0:
        p("  No appearances with a drawdown found.")
    else:
        p(f"  Of {nd} appearances that had a drawdown:")
        blank()
        p(f"  {'Outcome':<38}  {'Rate':>8}  {'Count':>6}")
        rule()
        rows_rec = [
            ("Recovered to entry price (≥ 0%)",   "recovered",  lambda p2: p2["recovered"]),
            ("Went on to +10% from entry",         "nh10",       lambda p2: p2["nh10"]),
            ("Went on to +20% from entry",         "nh20",       lambda p2: p2["nh20"]),
            ("Went on to +30% from entry",         "nh30",       lambda p2: p2["nh30"]),
        ]
        for label, _, fn in rows_rec:
            cnt = sum(1 for p2 in dumped if fn(p2))
            pct = cnt / nd * 100
            p(f"  {label:<38}  {pct:>7.1f}%  {cnt:>6}")

        rec_days_l = [p2["recovery_days"] for p2 in dumped if p2["recovery_days"] is not None]
        blank()
        if rec_days_l:
            p(f"  Avg days to recover after dump   :  {f(mean(rec_days_l), 1)}")
            p(f"  Median days to recover           :  {f(median(rec_days_l), 1)}")
            p(f"  Fastest / slowest                :  {min(rec_days_l)} / {max(rec_days_l)} days")
        else:
            p("  Recovery timing : not enough forward data yet")

    # ══════════════════════════════════════════════════════════════════════════
    blank()
    sep()
    p("  3. OPTIMAL ENTRY TIMING")
    sep()

    measure_days = [7, 10, 14]
    hdr_cols = "".join(
        f"  {col('D'+str(m)+' WR', 8, '>')}  {col('D'+str(m)+' Avg', 9, '>')}"
        for m in measure_days
    )

    # ── 3a fixed day ──────────────────────────────────────────────────────────
    blank()
    p("  3a. Enter on fixed day after appearance")
    blank()
    p(f"  {'Entry':<10}{hdr_cols}")
    rule()
    for ed in [1, 2, 3, 5, 7]:
        res  = sim_fixed_day(paths, ed, measure_days)
        cols = "".join(
            f"  {fp(win_rate(res[m])):>8}  {fp(mean(res[m])):>9}"
            for m in measure_days
        )
        p(f"  {'Day '+str(ed):<10}{cols}")

    # ── 3b dip trigger ────────────────────────────────────────────────────────
    blank()
    p("  3b. Enter when price drops X% from appearance close  "
      "(return measured D days after trigger)")
    blank()
    p(f"  {'Trigger':<12}  {'Hits':>5}{hdr_cols}")
    rule()
    for dip in [5, 10, 15, 20, 25, 30]:
        res  = sim_dip(paths, dip, measure_days)
        nt   = res["n"]
        cols = "".join(
            f"  {fp(win_rate(res[m])):>8}  {fp(mean(res[m])):>9}"
            for m in measure_days
        )
        p(f"  {'-'+str(dip)+'% dip':<12}  {nt:>5}{cols}")

    # ══════════════════════════════════════════════════════════════════════════
    blank()
    sep()
    p("  4. PATTERN FILTERS  (Day-7 from entry, not from trigger)")
    sep()

    def compare(label_a, ga, label_b, gb):
        blank()
        p(f"  ── {label_a}  vs  {label_b}")
        blank()
        p(f"  {'Group':<22}  {'N':>5}  {'Avg DD':>8}  {'DD Day':>7}"
          f"  {'Recovered':>10}  {'D7 WR':>7}  {'D7 Avg':>8}  {'D14 WR':>7}  {'D14 Avg':>8}")
        rule()
        for label, grp in [(label_a, ga), (label_b, gb)]:
            ng = len(grp)
            if ng == 0:
                p(f"  {label:<22}  {'0':>5}  (no data)")
                continue
            dds  = [x["max_dd"] for x in grp]
            ddds = [x["dd_day"] for x in grp]
            recs = sum(1 for x in grp if x["recovered"]) / ng * 100
            d7   = [d["chg_pct"] for x in grp for d in x["daily"] if d["day"] == 7]
            d14  = [d["chg_pct"] for x in grp for d in x["daily"] if d["day"] == 14]
            p(f"  {label:<22}  {ng:>5}  {fp(mean(dds)):>8}  "
              f"{f(mean(ddds),1):>7}  {fp(recs):>10}  "
              f"{fp(win_rate(d7)):>7}  {fp(mean(d7)):>8}  "
              f"{fp(win_rate(d14)):>7}  {fp(mean(d14)):>8}")

    bo_y = [p2 for p2 in paths if p2["is_breakout"] == 1]
    bo_n = [p2 for p2 in paths if p2["is_breakout"] == 0]
    compare("Breakout = YES", bo_y, "Breakout = NO", bo_n)

    vr_h = [p2 for p2 in paths if p2["vol_ratio"] is not None and p2["vol_ratio"] >= 2]
    vr_l = [p2 for p2 in paths if p2["vol_ratio"] is not None and p2["vol_ratio"] <  2]
    compare("Vol Ratio ≥ 2x", vr_h, "Vol Ratio < 2x", vr_l)

    bd_h = [p2 for p2 in paths if p2["body_pct"] is not None and p2["body_pct"] >= 10]
    bd_l = [p2 for p2 in paths if p2["body_pct"] is not None and p2["body_pct"] < 10]
    compare("Body % ≥ 10%", bd_h, "Body % < 10%", bd_l)

    # ══════════════════════════════════════════════════════════════════════════
    blank()
    sep()
    p("  5. TRADE LOG  (every appearance)")
    sep()
    blank()
    p(f"  {'Date':<12}  {'#':<2}  {'Symbol':<14}  {'App%':>6}"
      f"  {'Max DD':>7}  {'DD Day':>6}  {'Recov':>5}"
      f"  {'D7':>7}  {'D14':>7}  {'BO':>2}  {'VolR':>6}")
    rule()
    for p2 in sorted(paths, key=lambda x: (x["entry_date"], x["rank"])):
        d7  = next((d["chg_pct"] for d in p2["daily"] if d["day"] == 7),  None)
        d14 = next((d["chg_pct"] for d in p2["daily"] if d["day"] == 14), None)
        vr  = f"{p2['vol_ratio']:.1f}x" if p2["vol_ratio"] is not None else "N/A"
        p(f"  {p2['entry_date']:<12}  {p2['rank']:<2}  {p2['symbol']:<14}"
          f"  {fp(p2['app_pct']):>6}  {fp(p2['max_dd']):>7}"
          f"  Day {p2['dd_day']:>2}  {'Y' if p2['recovered'] else 'N':>5}"
          f"  {fp(d7):>7}  {fp(d14):>7}"
          f"  {'Y' if p2['is_breakout'] else 'N':>2}  {vr:>6}")

    # ══════════════════════════════════════════════════════════════════════════
    blank()
    sep()
    p("  6. STRATEGY RECOMMENDATION")
    sep()
    blank()

    # Find best fixed-day entry by Day-7 mean (need ≥5 samples)
    best_fd, best_fd_ret, best_fd_wr = None, -999, 0
    for ed in [1, 2, 3, 5, 7]:
        r = sim_fixed_day(paths, ed, [7])
        rets = r[7]
        if len(rets) >= 5 and mean(rets) is not None and mean(rets) > best_fd_ret:
            best_fd_ret = mean(rets)
            best_fd_wr  = win_rate(rets)
            best_fd     = ed

    # Find best dip entry by Day-7 mean (need ≥5 samples)
    best_dip, best_dip_ret, best_dip_wr, best_dip_n = None, -999, 0, 0
    for dip in [5, 10, 15, 20, 25, 30]:
        r  = sim_dip(paths, dip, [7])
        rets = r[7]
        if len(rets) >= 5 and mean(rets) is not None and mean(rets) > best_dip_ret:
            best_dip_ret = mean(rets)
            best_dip_wr  = win_rate(rets)
            best_dip     = dip
            best_dip_n   = r["n"]

    avg_dd     = mean(all_dd) or -15
    sl_suggest = round(avg_dd * 1.3, 0)   # 30% beyond avg DD

    d7_all   = [d["chg_pct"] for p2 in paths for d in p2["daily"] if d["day"] == 7]
    d14_all  = [d["chg_pct"] for p2 in paths for d in p2["daily"] if d["day"] == 14]
    avg_d7   = mean(d7_all)
    avg_d14  = mean(d14_all)

    p("  ★ ENTRY CONDITION")
    p("    Coin appears in daily Top-4 gainers")
    p("    For best quality: Breakout = YES  and  Volume Ratio ≥ 2x")
    blank()

    if best_fd is not None:
        r14 = sim_fixed_day(paths, best_fd, [14])
        p(f"  ★ BEST FIXED-DAY ENTRY  →  enter at close of Day {best_fd}"
          f" after appearance")
        p(f"    Day-7 win rate : {fp(best_fd_wr)}   Day-7 avg return : {fp(best_fd_ret)}")
        p(f"    Day-14 win rate: {fp(win_rate(r14[14]))}   Day-14 avg return: {fp(mean(r14[14]))}")
    else:
        p("  ★ BEST FIXED-DAY ENTRY  →  insufficient data for firm pick")
        p(f"    Directionally: enter Day 3 after appearance")

    blank()

    if best_dip is not None:
        r14 = sim_dip(paths, best_dip, [14])
        trig_pct = best_dip_n / n * 100
        p(f"  ★ BEST DIP-TRIGGER ENTRY  →  enter when price drops -{best_dip}%"
          f" from appearance close")
        p(f"    Triggered on {best_dip_n} / {n} appearances  ({trig_pct:.0f}%)")
        p(f"    Day-7 win rate : {fp(best_dip_wr)}   Day-7 avg return : {fp(best_dip_ret)}")
        p(f"    Day-14 win rate: {fp(win_rate(r14[14]))}   Day-14 avg return: {fp(mean(r14[14]))}")
    else:
        p("  ★ BEST DIP-TRIGGER ENTRY  →  insufficient data for firm pick")
        p(f"    Directionally: use -10% dip trigger")

    blank()
    p("  ★ RISK MANAGEMENT")
    p(f"    Avg max drawdown from entry  :  {fp(avg_dd)}")
    p(f"    Suggested stop-loss          :  {fp(sl_suggest)}  (avg DD × 1.3)")
    p(f"    Avg Day-7 return (all entries):  {fp(avg_d7)}")
    p(f"    Avg Day-14 return (all entries): {fp(avg_d14)}")
    blank()

    if n >= 100:
        p("  ★ SUGGESTED STRATEGY  (statistically backed)")
    else:
        p("  ★ SUGGESTED STRATEGY  (directional — backfill for confidence)")
    p()
    p("    1. Screen daily Top-4 for Breakout=YES + VolRatio ≥ 2x")
    p("    2. Wait for the dump — enter on Day 3 close OR on -10% dip,")
    p("       whichever comes first")
    p("    3. Stop-loss below avg max-drawdown × 1.3")
    p("    4. Take partial profit at Day-7 return level, let rest run to Day-14")
    blank()
    if n < 100:
        p(f"  ⚠  Only {n} paths in current dataset. Run historical_gainers.py")
        p( "     to backfill 12 months and re-run this script for reliable stats.")
    sep()

    return lines


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found")
        sys.exit(1)

    gainers, sym_dates, close_map = load_all()

    conn = sqlite3.connect(DB_PATH)
    total_rows  = conn.execute("SELECT COUNT(*) FROM daily_gainers").fetchone()[0]
    date_range  = conn.execute(
        "SELECT MIN(date)||' → '||MAX(date) FROM daily_gainers"
    ).fetchone()[0]
    conn.close()

    paths = build_paths(gainers, sym_dates, close_map)
    lines = analyse(paths, total_rows, date_range)

    with open(OUTPUT_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  ✓ Saved → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
