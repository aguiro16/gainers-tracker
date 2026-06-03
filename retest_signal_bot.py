"""
retest_signal_bot.py
====================
Monitors Binance for retest entry opportunities and sends Telegram alerts.

Signal fires when ALL of:
  1. Coin appeared in Top-4 gainers in last 1–7 days
  2. Appearance day: is_breakout=1, volume_ratio >= 2.0,
                    app_pct >= 10%, body_pct >= 5%
  3. Current price has dropped -15% or more from appearance close

Run modes:
  python3 retest_signal_bot.py           # single check
  python3 retest_signal_bot.py --daemon  # loop every 4 h + result check every 24 h

Env vars required:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""

import os
import sys
import sqlite3
import time
import logging
import requests
import schedule
from datetime import date, datetime, timedelta, timezone

# ── paths ──────────────────────────────────────────────────────────────────────

GAINERS_DB  = "/root/gainers/gainers.db"
SIGNALS_DB  = "/root/gainers/retest_signals.db"
BINANCE_BASE = "https://api.binance.us"

# ── strategy parameters ────────────────────────────────────────────────────────

LOOKBACK_DAYS   = 7      # scan appearances up to this many days old
MIN_VOL_RATIO   = 2.0
MIN_APP_PCT     = 10.0
MIN_BODY_PCT    = 5.0
DIP_TRIGGER_PCT = 15.0   # signal fires when price drops this % from appearance

SL_PCT   = 25.0
TP1_PCT  = 30.0
TP2_PCT  = 60.0
TP3_PCT  = 100.0

WIN_RATE_DISPLAY = "~37%"
PF_DISPLAY       = "1.54"

# ── logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg_token():
    t = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not t:
        log.error("TELEGRAM_BOT_TOKEN not set")
    return t

def tg_chat():
    c = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not c:
        log.error("TELEGRAM_CHAT_ID not set")
    return c

def send_telegram(text: str) -> bool:
    token = tg_token()
    chat  = tg_chat()
    if not token or not chat:
        log.warning("Telegram not configured — message not sent")
        log.info("Would have sent:\n%s", text)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        resp.raise_for_status()
        log.info("Telegram message sent (chat %s)", chat)
        return True
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return False

# ── signals database ───────────────────────────────────────────────────────────

def get_signals_db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(SIGNALS_DB), exist_ok=True)
    conn = sqlite3.connect(SIGNALS_DB)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_signals_db():
    with get_signals_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date_sent       TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            appearance_date TEXT NOT NULL,
            appearance_price REAL NOT NULL,
            entry_price     REAL NOT NULL,
            drop_pct        REAL NOT NULL,
            sl              REAL NOT NULL,
            tp1             REAL NOT NULL,
            tp2             REAL NOT NULL,
            tp3             REAL NOT NULL,
            status          TEXT NOT NULL DEFAULT 'OPEN',
            last_checked    TEXT,
            UNIQUE(symbol, appearance_date)
        );
        """)
    log.info("Signals DB ready: %s", SIGNALS_DB)


def signal_already_sent(symbol: str, appearance_date: str) -> bool:
    with get_signals_db() as conn:
        row = conn.execute(
            "SELECT id FROM signals WHERE symbol=? AND appearance_date=?",
            (symbol, appearance_date),
        ).fetchone()
    return row is not None


def save_signal(symbol, appearance_date, appearance_price,
                entry_price, drop_pct, sl, tp1, tp2, tp3):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_signals_db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO signals
            (date_sent, symbol, appearance_date, appearance_price,
             entry_price, drop_pct, sl, tp1, tp2, tp3, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (now, symbol, appearance_date, appearance_price,
              entry_price, drop_pct, sl, tp1, tp2, tp3, "OPEN"))


def get_open_signals():
    with get_signals_db() as conn:
        return conn.execute(
            "SELECT * FROM signals WHERE status='OPEN' ORDER BY date_sent"
        ).fetchall()


def update_signal_status(signal_id: int, status: str):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_signals_db() as conn:
        conn.execute(
            "UPDATE signals SET status=?, last_checked=? WHERE id=?",
            (status, now, signal_id),
        )

# ── Binance price fetch ────────────────────────────────────────────────────────

def get_current_price(symbol: str) -> float | None:
    try:
        resp = requests.get(
            f"{BINANCE_BASE}/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception as e:
        log.warning("Price fetch failed for %s: %s", symbol, e)
        return None

# ── gainers.db scan ────────────────────────────────────────────────────────────

def get_qualified_appearances() -> list[dict]:
    """
    Return all appearances from the last LOOKBACK_DAYS days that meet
    the quality filter: is_breakout=1, vol_ratio>=2, app_pct>=10, body_pct>=5.
    """
    if not os.path.exists(GAINERS_DB):
        log.error("gainers.db not found at %s", GAINERS_DB)
        return []

    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()

    conn = sqlite3.connect(GAINERS_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT dg.date        AS appearance_date,
               dg.symbol,
               dg.price_change_pct AS app_pct,
               dg.current_price    AS appearance_price,
               ch.volume_ratio,
               ch.body_pct,
               ch.is_breakout
        FROM   daily_gainers dg
        JOIN   candle_history ch
               ON dg.symbol = ch.symbol AND dg.date = ch.date
        WHERE  dg.date >= ?
          AND  ch.is_breakout = 1
          AND  ch.volume_ratio >= ?
          AND  dg.price_change_pct >= ?
          AND  ch.body_pct >= ?
        ORDER  BY dg.date DESC
    """, (cutoff, MIN_VOL_RATIO, MIN_APP_PCT, MIN_BODY_PCT)).fetchall()
    conn.close()

    return [dict(r) for r in rows]

# ── signal message builders ────────────────────────────────────────────────────

def build_signal_message(row: dict, current_price: float,
                         drop_pct: float, days_ago: int,
                         sl: float, tp1: float, tp2: float, tp3: float) -> str:
    return (
        f"🎯 <b>RETEST ENTRY SIGNAL</b>\n\n"
        f"📌 Symbol: <b>{row['symbol']}</b>\n"
        f"📅 Appeared in Top-4: {row['appearance_date']} ({days_ago} day{'s' if days_ago != 1 else ''} ago)\n"
        f"📈 Appearance pump: +{row['app_pct']:.1f}%\n"
        f"📊 Vol Ratio: {row['volume_ratio']:.1f}x | Breakout: YES | Body: +{row['body_pct']:.1f}%\n"
        f"💰 Appearance price: ${row['appearance_price']:.4f}\n"
        f"📉 Current price: ${current_price:.4f}\n"
        f"📉 Drop from appearance: {drop_pct:.1f}%\n\n"
        f"✅ Entry: current price <b>${current_price:.4f}</b>\n"
        f"🛑 SL: -{SL_PCT:.0f}% from entry = <b>${sl:.4f}</b>\n"
        f"🎯 TP1: +{TP1_PCT:.0f}% = <b>${tp1:.4f}</b>\n"
        f"🎯 TP2: +{TP2_PCT:.0f}% = <b>${tp2:.4f}</b>\n"
        f"🎯 TP3: +{TP3_PCT:.0f}% = <b>${tp3:.4f}</b>\n\n"
        f"⚠️ Win Rate: {WIN_RATE_DISPLAY} | PF: {PF_DISPLAY} | Risk 2% only"
    )


def build_update_message(symbol: str, entry_price: float,
                         current_price: float, status: str) -> str:
    change_pct = (current_price - entry_price) / entry_price * 100
    sign       = "+" if change_pct >= 0 else ""
    emoji      = "✅" if "TP" in status else "❌"
    return (
        f"📊 <b>SIGNAL UPDATE: {symbol}</b>\n"
        f"Status: <b>{status} {emoji}</b>\n"
        f"Entry: ${entry_price:.4f} | Current: ${current_price:.4f} | "
        f"Change: {sign}{change_pct:.1f}%"
    )

# ── core scan ──────────────────────────────────────────────────────────────────

def run_signal_check():
    log.info("── Signal check started ──────────────────────────────")
    appearances = get_qualified_appearances()
    log.info("Qualified appearances in last %d days: %d", LOOKBACK_DAYS, len(appearances))

    signals_sent = 0
    today = date.today()

    for row in appearances:
        symbol          = row["symbol"]
        appearance_date = row["appearance_date"]
        appearance_px   = row["appearance_price"]

        # skip if already signalled
        if signal_already_sent(symbol, appearance_date):
            log.debug("Already signalled: %s on %s", symbol, appearance_date)
            continue

        current_px = get_current_price(symbol)
        if current_px is None:
            continue

        drop_pct = (current_px - appearance_px) / appearance_px * 100

        if drop_pct > -DIP_TRIGGER_PCT:
            log.debug("%s drop %.1f%% — below trigger (need -%.0f%%)",
                      symbol, drop_pct, DIP_TRIGGER_PCT)
            continue

        # trigger!
        days_ago = (today - date.fromisoformat(appearance_date)).days
        sl  = current_px * (1 - SL_PCT  / 100)
        tp1 = current_px * (1 + TP1_PCT / 100)
        tp2 = current_px * (1 + TP2_PCT / 100)
        tp3 = current_px * (1 + TP3_PCT / 100)

        log.info("SIGNAL: %s — appeared %s (%d days ago), drop %.1f%%",
                 symbol, appearance_date, days_ago, drop_pct)

        msg = build_signal_message(row, current_px, drop_pct, days_ago,
                                   sl, tp1, tp2, tp3)

        if send_telegram(msg):
            save_signal(symbol, appearance_date, appearance_px,
                        current_px, drop_pct, sl, tp1, tp2, tp3)
            signals_sent += 1
        else:
            # save anyway so we don't spam on next run if Telegram is down
            save_signal(symbol, appearance_date, appearance_px,
                        current_px, drop_pct, sl, tp1, tp2, tp3)

        time.sleep(0.5)   # small pause between Binance calls

    log.info("Signal check done — %d new signal(s) sent", signals_sent)


# ── result tracker ─────────────────────────────────────────────────────────────

def run_result_check():
    log.info("── Result check started ──────────────────────────────")
    open_signals = get_open_signals()
    log.info("Open signals to check: %d", len(open_signals))

    for sig in open_signals:
        symbol      = sig["symbol"]
        entry_price = sig["entry_price"]
        sl          = sig["sl"]
        tp1         = sig["tp1"]
        tp2         = sig["tp2"]
        tp3         = sig["tp3"]
        sig_id      = sig["id"]

        current_px = get_current_price(symbol)
        if current_px is None:
            continue

        # determine status (check highest TP first)
        new_status = None
        if current_px >= tp3:
            new_status = "TP3 HIT"
        elif current_px >= tp2:
            new_status = "TP2 HIT"
        elif current_px >= tp1:
            new_status = "TP1 HIT"
        elif current_px <= sl:
            new_status = "SL HIT"

        if new_status:
            log.info("Signal %d (%s): %s", sig_id, symbol, new_status)
            update_signal_status(sig_id, new_status)
            msg = build_update_message(symbol, entry_price, current_px, new_status)
            send_telegram(msg)
        else:
            # update last_checked timestamp without changing status
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            with get_signals_db() as conn:
                conn.execute(
                    "UPDATE signals SET last_checked=? WHERE id=?", (now, sig_id)
                )
            change = (current_px - entry_price) / entry_price * 100
            log.info("Signal %d (%s): still open, current %.4f (%+.1f%%)",
                     sig_id, symbol, current_px, change)

        time.sleep(0.3)

    log.info("Result check done")

# ── entry point ────────────────────────────────────────────────────────────────

def main():
    daemon_mode = "--daemon" in sys.argv
    init_signals_db()

    if not daemon_mode:
        # single run
        run_signal_check()
        run_result_check()
        return

    # daemon mode — run immediately, then on schedule
    log.info("Starting daemon mode  (signals every 4 h, results every 24 h)")
    run_signal_check()
    run_result_check()

    schedule.every(4).hours.do(run_signal_check)
    schedule.every(24).hours.do(run_result_check)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
