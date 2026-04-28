import sqlite3
import json
from datetime import datetime

DB_PATH = "signals.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_number INTEGER UNIQUE,
            symbol TEXT,
            market_type TEXT,
            direction TEXT,
            entry_price REAL,
            sl REAL,
            tp1 REAL,
            tp2 REAL,
            tp3 REAL,
            swing_high REAL,
            swing_low REAL,
            fib_618 REAL,
            fib_786 REAL,
            rr REAL,
            timeframe TEXT,
            status TEXT DEFAULT 'OPEN',
            result TEXT,
            pnl_pct REAL,
            telegram_message_id INTEGER,
            tradingview_url TEXT,
            created_at TEXT,
            closed_at TEXT,
            raw_data TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS signal_counter (
            id INTEGER PRIMARY KEY,
            last_number INTEGER DEFAULT 0
        )
    """)
    c.execute("INSERT OR IGNORE INTO signal_counter (id, last_number) VALUES (1, 0)")
    conn.commit()
    conn.close()

def get_next_signal_number():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE signal_counter SET last_number = last_number + 1 WHERE id = 1")
    c.execute("SELECT last_number FROM signal_counter WHERE id = 1")
    number = c.fetchone()[0]
    conn.commit()
    conn.close()
    return number

def save_signal(data):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT OR IGNORE INTO signals (
            signal_number, symbol, market_type, direction,
            entry_price, sl, tp1, tp2, tp3,
            swing_high, swing_low, fib_618, fib_786,
            rr, timeframe, tradingview_url, created_at, raw_data
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data['signal_number'], data['symbol'], data['market_type'], data['direction'],
        data['entry_price'], data['sl'], data['tp1'], data['tp2'], data['tp3'],
        data['swing_high'], data['swing_low'], data['fib_618'], data['fib_786'],
        data['rr'], data['timeframe'], data['tradingview_url'],
        datetime.utcnow().isoformat(), json.dumps(data)
    ))
    conn.commit()
    conn.close()

def update_signal_message_id(signal_number, message_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE signals SET telegram_message_id=? WHERE signal_number=?",
              (message_id, signal_number))
    conn.commit()
    conn.close()

def get_open_signals():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM signals WHERE status='OPEN'")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_today_signals():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    from datetime import timedelta
    since = (datetime.utcnow() - timedelta(hours=24)).isoformat()
    c.execute("SELECT * FROM signals WHERE created_at >= ?", (since,))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_signal_by_number(signal_number):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM signals WHERE signal_number=?", (signal_number,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def close_signal(signal_number, result, pnl_pct):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE signals SET status='CLOSED', result=?, pnl_pct=?, closed_at=?
        WHERE signal_number=?
    """, (result, pnl_pct, datetime.utcnow().isoformat(), signal_number))
    conn.commit()
    conn.close()
