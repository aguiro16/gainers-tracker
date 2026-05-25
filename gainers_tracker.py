"""
Top Gainers Tracker
====================
Runs daily at 00:01 UTC.  Tracks Binance USDT top-4 gainers, stores candle
metrics, and follows up on each coin for the next 14 days.

Usage:
  python3 gainers_tracker.py            # run once immediately
  python3 gainers_tracker.py --daemon   # run once, then schedule daily
  python3 gainers_tracker.py --report   # print analysis report and exit

Data: Binance US public API + CoinGecko (no API keys required)
DB  : gainers.db (SQLite, created automatically)
"""

import sys
import sqlite3
import requests
import schedule
import time
import traceback
from datetime import datetime, date, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────

BINANCE_BASE   = "https://api.binance.com"
COINGECKO_URL  = "https://api.coingecko.com/api/v3/coins/markets"
DB_PATH        = "gainers.db"
TOP_N          = 4
FOLLOWUP_DAYS  = 14

STABLECOINS = {
    "USDTUSDT","BUSDUSDT","TUSDUSDT","USDCUSDT","DAIUSDT","FDUSDUSDT",
    "USTUSDT","USDPUSDT","FRAXUSDT","EURCUSDT","EURUSDT","GBPUSDT",
}

# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS daily_gainers (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            date             TEXT NOT NULL,
            rank             INTEGER NOT NULL,
            symbol           TEXT NOT NULL,
            price_change_pct REAL,
            current_price    REAL,
            volume_usdt      REAL,
            high_24h         REAL,
            low_24h          REAL,
            open_24h         REAL,
            recorded_at      TEXT NOT NULL,
            UNIQUE(date, rank)
        );

        CREATE TABLE IF NOT EXISTS candle_history (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol                TEXT NOT NULL,
            date                  TEXT NOT NULL,
            open                  REAL,
            high                  REAL,
            low                   REAL,
            close                 REAL,
            volume                REAL,
            volume_ratio          REAL,
            body_pct              REAL,
            upper_wick            REAL,
            is_breakout           INTEGER,
            days_since_last_pump  INTEGER,
            UNIQUE(symbol, date)
        );

        CREATE TABLE IF NOT EXISTS followup_prices (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol               TEXT NOT NULL,
            entry_date           TEXT NOT NULL,
            entry_price          REAL,
            days_after           INTEGER NOT NULL,
            price                REAL,
            change_from_entry_pct REAL,
            recorded_at          TEXT NOT NULL,
            UNIQUE(symbol, entry_date, days_after)
        );
        """)
    print(f"  DB ready: {DB_PATH}")

# ── API helpers ───────────────────────────────────────────────────────────────

def _get(url, params=None, timeout=15):
    r = requests.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def get_excluded_symbols() -> set:
    try:
        data = _get(COINGECKO_URL,
                    params={"vs_currency":"usd","order":"market_cap_desc","per_page":20})
        excluded = {c["symbol"].upper() + "USDT" for c in data}
        return excluded | STABLECOINS
    except Exception as e:
        print(f"  [WARN] CoinGecko failed ({e}), using stablecoin list only")
        return STABLECOINS


def fetch_top_gainers(excluded: set) -> list:
    data = _get(f"{BINANCE_BASE}/api/v3/ticker/24hr")
    usdt = [
        t for t in data
        if t["symbol"].endswith("USDT")
        and t["symbol"] not in excluded
        and float(t.get("quoteVolume", 0)) > 50_000
    ]
    usdt.sort(key=lambda t: float(t.get("priceChangePercent", 0)), reverse=True)
    top = usdt[:TOP_N]

    result = []
    for t in top:
        chg_pct = float(t["priceChangePercent"]) / 100
        price   = float(t["lastPrice"])
        result.append({
            "symbol":           t["symbol"],
            "price_change_pct": float(t["priceChangePercent"]),
            "current_price":    price,
            "volume_usdt":      float(t["quoteVolume"]),
            "high_24h":         float(t["highPrice"]),
            "low_24h":          float(t["lowPrice"]),
            "open_24h":         price / (1 + chg_pct) if chg_pct != -1 else 0,
        })
    return result


def fetch_candles(symbol: str, limit: int = 30) -> list:
    raw = _get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={"symbol": symbol, "interval": "1d", "limit": limit},
    )
    candles = []
    for r in raw:
        ts = int(r[0])
        candles.append({
            "date":   datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
            "open":   float(r[1]),
            "high":   float(r[2]),
            "low":    float(r[3]),
            "close":  float(r[4]),
            "volume": float(r[5]),
        })
    return candles


def compute_candle_metrics(candles: list) -> list:
    enriched = []
    volumes = [c["volume"] for c in candles]
    closes  = [c["close"]  for c in candles]

    for i, c in enumerate(candles):
        prev_vols = volumes[max(0, i-20): i]
        vol_ratio = (c["volume"] / (sum(prev_vols)/len(prev_vols))
                     if prev_vols else 1.0)

        rng      = c["high"] - c["low"]
        body_pct = (c["close"] - c["open"]) / c["open"] * 100 if c["open"] else 0
        upper_wick = ((c["high"] - c["close"]) / rng) if rng > 0 else 0

        prev_closes = closes[max(0, i-30): i]
        is_breakout = int(bool(prev_closes) and c["close"] > max(prev_closes))

        days_since = None
        for j in range(i-1, -1, -1):
            prev_vols_j = volumes[max(0, j-20): j]
            if prev_vols_j:
                vr_j = volumes[j] / (sum(prev_vols_j)/len(prev_vols_j))
                if vr_j > 2.0:
                    days_since = i - j
                    break

        enriched.append({
            **c,
            "volume_ratio":         round(vol_ratio, 3),
            "body_pct":             round(body_pct, 3),
            "upper_wick":           round(upper_wick, 3),
            "is_breakout":          is_breakout,
            "days_since_last_pump": days_since,
        })
    return enriched


def fetch_current_price(symbol: str) -> float | None:
    try:
        data = _get(f"{BINANCE_BASE}/api/v3/ticker/price",
                    params={"symbol": symbol})
        return float(data["price"])
    except Exception:
        return None

# ── Storage ───────────────────────────────────────────────────────────────────

def store_gainers(gainers: list, today: str, now: str):
    with get_db() as conn:
        for rank, g in enumerate(gainers, 1):
            conn.execute("""
                INSERT OR REPLACE INTO daily_gainers
                (date,rank,symbol,price_change_pct,current_price,volume_usdt,
                 high_24h,low_24h,open_24h,recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (today, rank, g["symbol"], g["price_change_pct"],
                  g["current_price"], g["volume_usdt"],
                  g["high_24h"], g["low_24h"], g["open_24h"], now))


def store_candles(symbol: str, candles: list):
    with get_db() as conn:
        for c in candles:
            conn.execute("""
                INSERT OR REPLACE INTO candle_history
                (symbol,date,open,high,low,close,volume,volume_ratio,
                 body_pct,upper_wick,is_breakout,days_since_last_pump)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (symbol, c["date"], c["open"], c["high"], c["low"],
                  c["close"], c["volume"], c["volume_ratio"],
                  c["body_pct"], c["upper_wick"], c["is_breakout"],
                  c["days_since_last_pump"]))


def update_followups(today: str, now: str):
    cutoff = (date.today() - timedelta(days=FOLLOWUP_DAYS)).isoformat()
    with get_db() as conn:
        rows = conn.execute("""
            SELECT DISTINCT symbol, date, current_price
            FROM daily_gainers
            WHERE date >= ? AND date < ?
        """, (cutoff, today)).fetchall()

    updated = []
    for row in rows:
        sym        = row["symbol"]
        entry_date = row["date"]
        entry_px   = row["current_price"]
        price      = fetch_current_price(sym)
        if price is None or entry_px == 0:
            continue

        days_after = (date.fromisoformat(today) - date.fromisoformat(entry_date)).days
        chg        = (price - entry_px) / entry_px * 100

        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO followup_prices
                (symbol,entry_date,entry_price,days_after,price,change_from_entry_pct,recorded_at)
                VALUES (?,?,?,?,?,?,?)
            """, (sym, entry_date, entry_px, days_after, price, round(chg,4), now))

        updated.append((sym, entry_date, days_after, chg))

    return updated

# ── Console output ────────────────────────────────────────────────────────────

def print_summary(gainers: list, today: str, followups: list, candle_map: dict):
    W = 62
    print()
    print("═" * W)
    print(f"  === TOP {TOP_N} GAINERS [{today}] ===")
    print("═" * W)

    for rank, g in enumerate(gainers, 1):
        sym        = g["symbol"]
        candles    = candle_map.get(sym, [])
        last       = candles[-1] if candles else {}
        vol_ratio  = last.get("volume_ratio", 0)
        breakout   = "YES" if last.get("is_breakout") else "NO"
        body       = last.get("body_pct", 0)
        print(f"  {rank}. {sym:<14} {g['price_change_pct']:>+7.2f}%  │  "
              f"Vol: {vol_ratio:.1f}x  │  Body: {body:+.1f}%  │  Breakout: {breakout}")

    print()
    print("  === FOLLOWUP CHECK ===")
    if followups:
        for sym, entry_date, days_after, chg in sorted(followups, key=lambda x: x[2]):
            direction = "▲" if chg >= 0 else "▼"
            print(f"  {sym:<14} (appeared {days_after:>2}d ago): "
                  f"{direction} {chg:>+7.2f}% from entry")
    else:
        print("  (no followup data yet)")
    print("═" * W)
    print()

# ── Main collection run ───────────────────────────────────────────────────────

def main():
    now_dt  = datetime.now(timezone.utc)
    today   = now_dt.strftime("%Y-%m-%d")
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{now_str} UTC]  Starting daily collection ...")

    try:
        excluded = get_excluded_symbols()
        print(f"  Excluding {len(excluded)} symbols (stablecoins + top-20 market cap)")
    except Exception as e:
        print(f"  [ERROR] get_excluded_symbols: {e}")
        excluded = STABLECOINS

    try:
        gainers = fetch_top_gainers(excluded)
        print(f"  Fetched top {len(gainers)} gainers")
        store_gainers(gainers, today, now_str)
    except Exception as e:
        print(f"  [ERROR] fetch_top_gainers: {e}")
        traceback.print_exc()
        gainers = []

    candle_map = {}
    for g in gainers:
        sym = g["symbol"]
        try:
            raw_candles = fetch_candles(sym, limit=60)
            enriched    = compute_candle_metrics(raw_candles)
            store_candles(sym, enriched)
            candle_map[sym] = enriched
            print(f"    {sym}: stored {len(enriched)} candles")
        except Exception as e:
            print(f"  [ERROR] candles for {sym}: {e}")

    try:
        followups = update_followups(today, now_str)
        print(f"  Updated {len(followups)} followup price records")
    except Exception as e:
        print(f"  [ERROR] update_followups: {e}")
        traceback.print_exc()
        followups = []

    print_summary(gainers, today, followups, candle_map)
    print(f"  Done.  [{datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC]")

# ── Report ────────────────────────────────────────────────────────────────────

def generate_report():
    W = 72
    print()
    print("═" * W)
    print("  TOP GAINERS TRACKER — ANALYSIS REPORT")
    print(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("═" * W)

    with get_db() as conn:
        rows = conn.execute("""
            SELECT date, rank, symbol, price_change_pct, volume_usdt
            FROM daily_gainers ORDER BY date DESC, rank
        """).fetchall()

        if not rows:
            print("  No data yet.  Run the collector first.")
            return

        print(f"\n  Total appearances recorded: {len(rows)}")
        print(f"  Date range: {rows[-1]['date']}  →  {rows[0]['date']}")

        coins = conn.execute("""
            SELECT symbol, COUNT(*) as appearances,
                   AVG(price_change_pct) as avg_chg,
                   MAX(price_change_pct) as max_chg
            FROM daily_gainers
            GROUP BY symbol ORDER BY appearances DESC
        """).fetchall()

        print(f"\n  Coins that appeared in Top-4 ({len(coins)} unique):")
        print(f"  {'Symbol':<14} {'Appearances':>11}  {'Avg +%':>8}  {'Best +%':>8}")
        print("  " + "─" * 46)
        for c in coins[:20]:
            print(f"  {c['symbol']:<14} {c['appearances']:>11}  "
                  f"{c['avg_chg']:>+8.2f}%  {c['max_chg']:>+8.2f}%")

        print(f"\n  Average performance after appearance (all coins):")
        print(f"  {'Day':>5}  {'Avg %':>8}  {'Win Rate':>10}  {'Samples':>8}")
        print("  " + "─" * 40)
        for day in [1, 2, 3, 5, 7, 10, 14]:
            fp = conn.execute("""
                SELECT change_from_entry_pct FROM followup_prices
                WHERE days_after = ?
            """, (day,)).fetchall()
            if fp:
                pcts     = [r["change_from_entry_pct"] for r in fp]
                avg_pct  = sum(pcts) / len(pcts)
                win_rate = sum(1 for p in pcts if p > 0) / len(pcts) * 100
                print(f"  {f'Day {day}':>5}  {avg_pct:>+8.2f}%  {win_rate:>9.1f}%  {len(pcts):>8}")
            else:
                print(f"  {f'Day {day}':>5}  {'N/A':>8}   {'N/A':>9}   {'N/A':>8}")

        print(f"\n  Breakout vs Non-Breakout performance (Day 1-3):")
        for is_bo, label in [(1, "Breakout"), (0, "Non-Breakout")]:
            fp = conn.execute("""
                SELECT fp.change_from_entry_pct
                FROM followup_prices fp
                JOIN candle_history ch
                  ON fp.symbol = ch.symbol AND fp.entry_date = ch.date
                WHERE ch.is_breakout = ? AND fp.days_after BETWEEN 1 AND 3
            """, (is_bo,)).fetchall()
            if fp:
                pcts    = [r["change_from_entry_pct"] for r in fp]
                avg_pct = sum(pcts) / len(pcts)
                wr      = sum(1 for p in pcts if p > 0) / len(pcts) * 100
                print(f"  {label:<16}  avg {avg_pct:>+6.2f}%  win rate {wr:.0f}%  (n={len(fp)})")
            else:
                print(f"  {label:<16}  (no data yet)")

        print(f"\n  Pre-pump patterns (candle metrics on appearance day):")
        ch = conn.execute("""
            SELECT dg.symbol, dg.date, ch.volume_ratio, ch.body_pct,
                   ch.is_breakout, ch.days_since_last_pump
            FROM daily_gainers dg
            JOIN candle_history ch ON dg.symbol = ch.symbol AND dg.date = ch.date
        """).fetchall()
        if ch:
            avg_vr   = sum(r["volume_ratio"] or 0 for r in ch) / len(ch)
            avg_body = sum(r["body_pct"]    or 0 for r in ch) / len(ch)
            bo_pct   = sum(1 for r in ch if r["is_breakout"]) / len(ch) * 100
            avg_dslp = [r["days_since_last_pump"] for r in ch if r["days_since_last_pump"]]
            print(f"  Avg volume ratio:        {avg_vr:.2f}x")
            print(f"  Avg body size:           {avg_body:+.2f}%")
            print(f"  Had 30-day breakout:     {bo_pct:.0f}% of appearances")
            if avg_dslp:
                print(f"  Avg days since last pump:{sum(avg_dslp)/len(avg_dslp):.1f} days")
        else:
            print("  (no candle data joined yet)")

        print(f"\n  Next-day outcome (continued up vs dumped):")
        d1 = conn.execute("""
            SELECT change_from_entry_pct FROM followup_prices WHERE days_after = 1
        """).fetchall()
        if d1:
            pcts = [r["change_from_entry_pct"] for r in d1]
            up   = sum(1 for p in pcts if p > 0)
            dn   = len(pcts) - up
            print(f"  Continued up (+):    {up:>3}  ({up/len(pcts)*100:.0f}%)")
            print(f"  Dumped next day (-): {dn:>3}  ({dn/len(pcts)*100:.0f}%)")
            print(f"  Avg Day-1 change:    {sum(pcts)/len(pcts):>+.2f}%")
        else:
            print("  (no Day-1 followup data yet)")

        print(f"\n  Last 7 collection days:")
        recent = conn.execute("""
            SELECT date, GROUP_CONCAT(symbol||'('||ROUND(price_change_pct,1)||'%)', ', ')
                   as coins
            FROM daily_gainers GROUP BY date ORDER BY date DESC LIMIT 7
        """).fetchall()
        for r in recent:
            print(f"  {r['date']}  {r['coins']}")

    print()
    print("═" * W)

# ── Scheduler / entry point ───────────────────────────────────────────────────

def run_daemon():
    print("\n  Starting Top Gainers Tracker daemon ...")
    print("  Scheduled: daily at 00:01 UTC")
    print("  Press Ctrl-C to stop.\n")
    main()
    schedule.every().day.at("00:01").do(main)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    init_db()
    if "--report" in sys.argv:
        generate_report()
    elif "--daemon" in sys.argv:
        run_daemon()
    else:
        main()
