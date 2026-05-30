"""
historical_gainers.py
=====================
Backfills the past 12 months of daily top-4 gainers into gainers.db.

Strategy (fast):
  • Fetch each symbol's full 12-month daily OHLCV history once  (~300 API calls)
  • Build an in-memory day→symbol→OHLCV map
  • For every calendar day, rank all pairs by % change and pick top-4
  • Write daily_gainers + candle_history + followup_prices

Progress is saved to progress.json so the run can be interrupted and resumed.

Usage:
  python3 historical_gainers.py            # full run
  python3 historical_gainers.py --reset    # wipe progress and start over
"""

import json
import math
import os
import sqlite3
import sys
import time
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests
from tqdm import tqdm

# ── Config ────────────────────────────────────────────────────────────────────

BINANCE_BASE  = "https://api.binance.us"
COINGECKO_URL = "https://api.coingecko.com/api/v3/coins/markets"

DB_PATH       = "gainers.db"          # same DB as gainers_tracker.py
PROGRESS_FILE = "progress.json"

TOP_N         = 4
API_SLEEP     = 0.15                  # seconds between Binance kline fetches
MIN_VOLUME    = 10_000                # min daily volume in USDT to consider

# Date range — past 12 months (today's candle not yet closed, stop yesterday)
END_DATE   = date.today() - timedelta(days=1)
START_DATE = END_DATE - timedelta(days=364)   # 365 days inclusive

FOLLOWUP_DAYS_LIST = [1, 2, 3, 5, 7, 10, 14]

STABLECOINS = {
    "USDTUSDT","BUSDUSDT","TUSDUSDT","USDCUSDT","DAIUSDT","FDUSDUSDT",
    "USTUSDT","USDPUSDT","FRAXUSDT","EURCUSDT","EURUSDT","GBPUSDT",
    "PYUSDUSDT","AEUSDUSDT","LISUSDUSDT",
}

# ── Database ──────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol                TEXT NOT NULL,
            entry_date            TEXT NOT NULL,
            entry_price           REAL,
            days_after            INTEGER NOT NULL,
            price                 REAL,
            change_from_entry_pct REAL,
            recorded_at           TEXT NOT NULL,
            UNIQUE(symbol, entry_date, days_after)
        );
        """)
    print(f"  DB ready: {DB_PATH}")

# ── Progress tracking ─────────────────────────────────────────────────────────

def load_progress() -> dict:
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {"fetched_symbols": [], "phase": "fetch"}


def save_progress(prog: dict):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(prog, f, indent=2)

# ── CoinGecko exclusion ───────────────────────────────────────────────────────

def get_excluded() -> set:
    print("  Fetching top-20 market cap from CoinGecko ...")
    try:
        r = requests.get(
            COINGECKO_URL,
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 20},
            timeout=15,
        )
        r.raise_for_status()
        syms = {c["symbol"].upper() + "USDT" for c in r.json()}
        result = syms | STABLECOINS
        print(f"  Excluding {len(result)} symbols")
        return result
    except Exception as e:
        print(f"  [WARN] CoinGecko failed ({e}), using stablecoin list only")
        return STABLECOINS | {
            "BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT","USDCUSDT",
            "ADAUSDT","DOGEUSDT","TRXUSDT","AVAXUSDT","LINKUSDT","TONUSDT",
            "SHIBUSDT","DOTUSDT","LTCUSDT","XLMUSDT","BCHUSDT","SUIUSDT",
            "UNIUSDT","NEARUSDT",
        }

# ── Binance helpers ───────────────────────────────────────────────────────────

def get_all_usdt_symbols() -> list[str]:
    """Return all active USDT spot pairs on Binance US."""
    print("  Fetching Binance US exchange info ...")
    r = requests.get(f"{BINANCE_BASE}/api/v3/exchangeInfo", timeout=30)
    r.raise_for_status()
    symbols = [
        s["symbol"] for s in r.json()["symbols"]
        if s["quoteAsset"] == "USDT"
        and s["status"] == "TRADING"
        and s["isSpotTradingAllowed"]
    ]
    print(f"  Found {len(symbols)} active USDT spot pairs")
    return symbols


def fetch_klines_range(symbol: str, start: date, end: date) -> list[dict]:
    """
    Fetch all daily klines for `symbol` between start and end (inclusive).
    Handles pagination automatically (Binance max 1000 rows per request).
    Returns list of dicts with date, open, high, low, close, volume.
    """
    start_ms = int(datetime(start.year, start.month, start.day,
                            tzinfo=timezone.utc).timestamp() * 1000)
    end_ms   = int(datetime(end.year, end.month, end.day, 23, 59, 59,
                            tzinfo=timezone.utc).timestamp() * 1000)

    rows, since = [], start_ms
    while since <= end_ms:
        try:
            resp = requests.get(
                f"{BINANCE_BASE}/api/v3/klines",
                params={
                    "symbol":    symbol,
                    "interval":  "1d",
                    "startTime": since,
                    "endTime":   end_ms,
                    "limit":     1000,
                },
                timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json()
        except Exception:
            time.sleep(1)
            try:
                resp = requests.get(
                    f"{BINANCE_BASE}/api/v3/klines",
                    params={
                        "symbol": symbol, "interval": "1d",
                        "startTime": since, "endTime": end_ms, "limit": 1000,
                    },
                    timeout=20,
                )
                resp.raise_for_status()
                batch = resp.json()
            except Exception as e2:
                print(f"    [ERROR] {symbol}: {e2}")
                break

        if not batch:
            break

        for k in batch:
            d = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc).date()
            rows.append({
                "date":         d.isoformat(),
                "open":         float(k[1]),
                "high":         float(k[2]),
                "low":          float(k[3]),
                "close":        float(k[4]),
                "volume":       float(k[5]),
                "quote_volume": float(k[7]),
            })

        last_ts = int(batch[-1][0])
        if last_ts >= end_ms or len(batch) < 1000:
            break
        since = last_ts + 86_400_000   # next day

    return rows

# ── Candle metrics ────────────────────────────────────────────────────────────

def compute_metrics_for_series(rows: list[dict]) -> list[dict]:
    """
    Enrich a sorted list of daily candle dicts with:
      volume_ratio, body_pct, upper_wick, is_breakout, days_since_last_pump
    """
    volumes  = [r["volume"] for r in rows]
    closes   = [r["close"]  for r in rows]
    enriched = []

    for i, r in enumerate(rows):
        prev_vols = volumes[max(0, i - 20): i]
        vol_ratio = (r["volume"] / (sum(prev_vols) / len(prev_vols))
                     if prev_vols else 1.0)

        rng        = r["high"] - r["low"]
        body_pct   = (r["close"] - r["open"]) / r["open"] * 100 if r["open"] else 0
        upper_wick = (r["high"] - r["close"]) / rng if rng > 0 else 0

        prev_closes = closes[max(0, i - 30): i]
        is_breakout = int(bool(prev_closes) and r["close"] > max(prev_closes))

        days_since = None
        for j in range(i - 1, -1, -1):
            pv = volumes[max(0, j - 20): j]
            if pv and volumes[j] / (sum(pv) / len(pv)) > 2.0:
                days_since = i - j
                break

        enriched.append({
            **r,
            "volume_ratio":         round(vol_ratio, 4),
            "body_pct":             round(body_pct, 4),
            "upper_wick":           round(upper_wick, 4),
            "is_breakout":          is_breakout,
            "days_since_last_pump": days_since,
        })

    return enriched

# ── Phase 1: Fetch all symbol histories ───────────────────────────────────────

def phase_fetch(symbols: list[str], excluded: set, prog: dict) -> dict[str, list[dict]]:
    """
    For every symbol not in `excluded`, fetch 12-month daily klines.
    Returns {symbol: [enriched candle dicts, sorted by date]}.
    Resumes from progress.json if interrupted.
    """
    fetch_start  = START_DATE - timedelta(days=40)
    already_done = set(prog.get("fetched_symbols", []))
    ohlcv: dict[str, list[dict]] = {}

    if already_done:
        print(f"  Resuming: {len(already_done)} symbols already fetched, reloading from DB ...")
        with get_db() as conn:
            for sym in already_done:
                rows = conn.execute(
                    "SELECT * FROM candle_history WHERE symbol=? ORDER BY date",
                    (sym,)
                ).fetchall()
                if rows:
                    ohlcv[sym] = [dict(r) for r in rows]

    to_fetch = [s for s in symbols if s not in excluded and s not in already_done]
    print(f"  Fetching {len(to_fetch)} symbols "
          f"({len(already_done)} already done, {len(excluded)} excluded) ...")

    for sym in tqdm(to_fetch, ncols=80, desc="  Fetching"):
        try:
            raw = fetch_klines_range(sym, fetch_start, END_DATE)
            if not raw:
                time.sleep(API_SLEEP)
                continue
            enriched  = compute_metrics_for_series(raw)
            ohlcv[sym] = enriched

            with get_db() as conn:
                conn.executemany("""
                    INSERT OR IGNORE INTO candle_history
                    (symbol,date,open,high,low,close,volume,volume_ratio,
                     body_pct,upper_wick,is_breakout,days_since_last_pump)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, [
                    (sym, c["date"], c["open"], c["high"], c["low"], c["close"],
                     c["volume"], c["volume_ratio"], c["body_pct"], c["upper_wick"],
                     c["is_breakout"], c["days_since_last_pump"])
                    for c in enriched
                ])

            prog["fetched_symbols"].append(sym)
            save_progress(prog)

        except Exception as e:
            tqdm.write(f"  [SKIP] {sym}: {e}")

        time.sleep(API_SLEEP)

    print(f"  Fetch complete: {len(ohlcv)} symbols with data")
    return ohlcv

# ── Phase 2: Build per-day lookup and rank gainers ────────────────────────────

def phase_rank(ohlcv: dict[str, list[dict]], excluded: set):
    """
    For every date in [START_DATE, END_DATE], find the top-4 gainers and
    write them to daily_gainers.
    """
    print("\n  Building day map ...")
    day_map: dict[str, dict[str, dict]] = defaultdict(dict)
    for sym, rows in ohlcv.items():
        for r in rows:
            day_map[r["date"]][sym] = r

    all_dates = sorted(
        d for d in day_map
        if START_DATE.isoformat() <= d <= END_DATE.isoformat()
    )
    print(f"  Ranking gainers across {len(all_dates)} days ...")

    now_str        = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    total_inserted = 0
    prev_close: dict[str, float] = {}
    all_available_dates = sorted(day_map.keys())

    with get_db() as conn:
        for date_str in tqdm(all_available_dates, ncols=80, desc="  Days"):
            day_data   = day_map[date_str]
            candidates = []

            for sym, row in day_data.items():
                if sym in excluded:
                    continue
                prev = prev_close.get(sym)
                if prev is None or prev == 0:
                    prev_close[sym] = row["close"]
                    continue

                pct_change   = (row["close"] - prev) / prev * 100
                quote_volume = row.get("quote_volume", row["volume"] * row["close"])

                if quote_volume < MIN_VOLUME:
                    prev_close[sym] = row["close"]
                    continue

                candidates.append({
                    "symbol":           sym,
                    "price_change_pct": pct_change,
                    "current_price":    row["close"],
                    "volume_usdt":      quote_volume,
                    "high_24h":         row["high"],
                    "low_24h":          row["low"],
                    "open_24h":         prev,
                })
                prev_close[sym] = row["close"]

            if date_str < START_DATE.isoformat():
                continue

            candidates.sort(key=lambda x: x["price_change_pct"], reverse=True)
            for rank, g in enumerate(candidates[:TOP_N], 1):
                conn.execute("""
                    INSERT OR IGNORE INTO daily_gainers
                    (date,rank,symbol,price_change_pct,current_price,volume_usdt,
                     high_24h,low_24h,open_24h,recorded_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                """, (
                    date_str, rank, g["symbol"], round(g["price_change_pct"], 4),
                    g["current_price"], g["volume_usdt"],
                    g["high_24h"], g["low_24h"], g["open_24h"], now_str,
                ))
                total_inserted += 1

        conn.commit()

    print(f"  Inserted {total_inserted} daily_gainers rows")

# ── Phase 3: Populate followup_prices ────────────────────────────────────────

def phase_followup(ohlcv: dict[str, list[dict]]):
    """
    For every row in daily_gainers, look up the coin's close price at
    days +1, +2, +3, +5, +7, +10, +14 and write to followup_prices.
    """
    print("\n  Populating followup_prices ...")

    close_map: dict[str, dict[str, float]] = {}
    for sym, rows in ohlcv.items():
        close_map[sym] = {r["date"]: r["close"] for r in rows}

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with get_db() as conn:
        gainers = conn.execute(
            "SELECT DISTINCT symbol, date, current_price FROM daily_gainers ORDER BY date"
        ).fetchall()

    rows_written = 0
    with get_db() as conn:
        for g in tqdm(gainers, ncols=80, desc="  Followup"):
            sym        = g["symbol"]
            entry_date = g["date"]
            entry_px   = g["current_price"]
            closes     = close_map.get(sym, {})
            entry_d    = date.fromisoformat(entry_date)

            if not closes or entry_px == 0:
                continue

            for days_after in FOLLOWUP_DAYS_LIST:
                target_d   = entry_d + timedelta(days=days_after)
                target_str = target_d.isoformat()
                if target_str > END_DATE.isoformat():
                    continue

                price = closes.get(target_str)
                if price is None:
                    for offset in [1, 2]:
                        alt   = (target_d + timedelta(days=offset)).isoformat()
                        price = closes.get(alt)
                        if price:
                            break

                if price is None:
                    continue

                chg = (price - entry_px) / entry_px * 100
                conn.execute("""
                    INSERT OR IGNORE INTO followup_prices
                    (symbol,entry_date,entry_price,days_after,price,
                     change_from_entry_pct,recorded_at)
                    VALUES (?,?,?,?,?,?,?)
                """, (sym, entry_date, entry_px, days_after, price,
                      round(chg, 4), now_str))
                rows_written += 1

        conn.commit()

    print(f"  Inserted {rows_written} followup_prices rows")

# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary():
    W = 68
    print()
    print("═" * W)
    print("  HISTORICAL BACKFILL — SUMMARY")
    print(f"  Range: {START_DATE}  →  {END_DATE}  ({(END_DATE - START_DATE).days + 1} days)")
    print("═" * W)

    with get_db() as conn:
        n_days     = conn.execute("SELECT COUNT(DISTINCT date) FROM daily_gainers").fetchone()[0]
        n_gainers  = conn.execute("SELECT COUNT(*) FROM daily_gainers").fetchone()[0]
        n_candles  = conn.execute("SELECT COUNT(*) FROM candle_history").fetchone()[0]
        n_followup = conn.execute("SELECT COUNT(*) FROM followup_prices").fetchone()[0]
        n_coins    = conn.execute("SELECT COUNT(DISTINCT symbol) FROM daily_gainers").fetchone()[0]

        print(f"  Days with data:       {n_days}")
        print(f"  Daily gainer rows:    {n_gainers}")
        print(f"  Unique coins seen:    {n_coins}")
        print(f"  Candle history rows:  {n_candles}")
        print(f"  Followup rows:        {n_followup}")

        top_coins = conn.execute("""
            SELECT symbol, COUNT(*) as n, AVG(price_change_pct) as avg_pct,
                   MAX(price_change_pct) as max_pct
            FROM daily_gainers
            GROUP BY symbol ORDER BY n DESC LIMIT 10
        """).fetchall()

        print(f"\n  Most frequent top-4 appearances:")
        print(f"  {'Symbol':<14} {'Days':>5}  {'Avg %':>8}  {'Best %':>8}")
        print("  " + "─" * 42)
        for c in top_coins:
            print(f"  {c['symbol']:<14} {c['n']:>5}  "
                  f"{c['avg_pct']:>+8.2f}%  {c['max_pct']:>+8.2f}%")

        print(f"\n  Average performance after appearing in top-4:")
        print(f"  {'Day':>6}  {'Avg %':>8}  {'Win Rate':>10}  {'Samples':>8}")
        print("  " + "─" * 40)
        for day in FOLLOWUP_DAYS_LIST:
            rows = conn.execute("""
                SELECT change_from_entry_pct FROM followup_prices WHERE days_after=?
            """, (day,)).fetchall()
            if rows:
                pcts = [r[0] for r in rows]
                avg  = sum(pcts) / len(pcts)
                wr   = sum(1 for p in pcts if p > 0) / len(pcts) * 100
                print(f"  {'Day '+str(day):>6}  {avg:>+8.2f}%  {wr:>9.1f}%  {len(pcts):>8}")
            else:
                print(f"  {'Day '+str(day):>6}  {'N/A':>8}   {'N/A':>9}   {'0':>8}")

    print("═" * W)
    print()

# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if "--reset" in sys.argv:
        for f in [PROGRESS_FILE]:
            if os.path.exists(f):
                os.remove(f)
        print("  Progress reset.  Starting fresh.")

    print()
    print("═" * 68)
    print("  HISTORICAL TOP-4 GAINERS BACKFILL")
    print(f"  Range: {START_DATE}  →  {END_DATE}")
    print(f"  DB:    {os.path.abspath(DB_PATH)}")
    print("═" * 68)

    init_db()
    prog     = load_progress()
    excluded = get_excluded()
    symbols  = get_all_usdt_symbols()

    # ── Phase 1: Fetch ────────────────────────────────────────────────────────
    print(f"\n[Phase 1/3]  Fetching historical OHLCV ...")
    ohlcv = phase_fetch(symbols, excluded, prog)

    prog["phase"] = "rank"
    save_progress(prog)

    # ── Phase 2: Rank gainers per day ─────────────────────────────────────────
    print(f"\n[Phase 2/3]  Ranking gainers for each day ...")
    phase_rank(ohlcv, excluded)

    prog["phase"] = "followup"
    save_progress(prog)

    # ── Phase 3: Followup prices ──────────────────────────────────────────────
    print(f"\n[Phase 3/3]  Computing followup prices ...")
    phase_followup(ohlcv)

    prog["phase"] = "done"
    save_progress(prog)

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary()

    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)
    print("  progress.json removed — run complete.")


if __name__ == "__main__":
    main()
