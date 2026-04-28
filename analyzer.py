import requests
import pandas as pd
import numpy as np
from config import BINANCE_BASE_URL, BINANCE_FUTURES_BASE_URL

STABLECOINS = {"USDC","BUSD","TUSD","DAI","FDUSD","USDP","GUSD","FRAX","LUSD","SUSD","AEUR","EURI","BFUSD"}
MIN_RR      = 1.5

def get_top_symbols(market_type, limit=60):
    try:
        if market_type == "FUTURES":
            url = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/24hr"
        else:
            url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr"
        tickers = requests.get(url, timeout=15).json()
        pairs = []
        for t in tickers:
            sym = t.get("symbol","")
            if not sym.endswith("USDT"):
                continue
            if sym[:-4] in STABLECOINS:
                continue
            try:
                pairs.append((sym, float(t["quoteVolume"])))
            except:
                continue
        pairs.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in pairs[:limit]]
    except Exception as e:
        print(f"Error getting symbols: {e}")
        return []

def get_klines(symbol, interval, limit, market_type):
    try:
        if market_type == "FUTURES":
            url = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/klines"
        else:
            url = f"{BINANCE_BASE_URL}/api/v3/klines"
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        resp   = requests.get(url, params=params, timeout=15)
        klines = resp.json()
        if not isinstance(klines, list) or len(klines) == 0:
            return pd.DataFrame()
        df = pd.DataFrame(klines, columns=[
            'time','open','high','low','close','volume',
            'close_time','quote_vol','trades','taker_buy_base','taker_buy_quote','ignore'
        ])
        for col in ['open','high','low','close','volume']:
            df[col] = df[col].astype(float)
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index('time', inplace=True)
        return df
    except Exception as e:
        print(f"Klines error {symbol} {interval}: {e}")
        return pd.DataFrame()

def get_trend(df_4h):
    if len(df_4h) < 51:
        return None
    ema50 = df_4h['close'].ewm(span=50, adjust=False).mean().iloc[-1]
    last  = df_4h['close'].iloc[-1]
    if last > ema50:
        return "LONG"
    if last < ema50:
        return "SHORT"
    return None

def find_swing_points(df_1h, window=5):
    highs = df_1h['high'].values
    lows  = df_1h['low'].values
    n     = len(df_1h)
    sh = sl = None
    for i in range(window, n - window):
        is_sh = (all(highs[i] >= highs[i-j] for j in range(1, window+1)) and
                 all(highs[i] >= highs[i+j] for j in range(1, window+1)))
        if is_sh and (sh is None or highs[i] > sh):
            sh = highs[i]
        is_sl = (all(lows[i] <= lows[i-j] for j in range(1, window+1)) and
                 all(lows[i] <= lows[i+j] for j in range(1, window+1)))
        if is_sl and (sl is None or lows[i] < sl):
            sl = lows[i]
    return sh, sl

def calc_fib_levels(swing_high, swing_low, direction):
    diff = swing_high - swing_low
    if diff <= 0:
        return None
    if direction == "LONG":
        return {
            'ote_low':  swing_high - 0.786 * diff,
            'ote_high': swing_high - 0.618 * diff,
            'tp1':      swing_high - 0.618 * diff,
            'tp2':      swing_low  + diff * 0.764,
            'tp3':      swing_high,
            'sl':       swing_low * 0.999,
        }
    else:
        return {
            'ote_low':  swing_low + 0.618 * diff,
            'ote_high': swing_low + 0.786 * diff,
            'tp1':      swing_low + 0.618 * diff,
            'tp2':      swing_high - diff * 0.764,
            'tp3':      swing_low,
            'sl':       swing_high * 1.001,
        }

def in_ote(price, fib):
    low  = min(fib['ote_low'], fib['ote_high'])
    high = max(fib['ote_low'], fib['ote_high'])
    return low <= price <= high

def detect_bos(df_15m, direction):
    if len(df_15m) < 6:
        return False
    prev = df_15m.iloc[-6:-1]
    last = df_15m['close'].iloc[-1]
    if direction == "LONG":
        return last > prev['high'].max()
    else:
        return last < prev['low'].min()

def build_tv_url(symbol):
    return f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}&interval=60"

def analyze_symbol(symbol, market_type):
    try:
        df_4h  = get_klines(symbol, "4h",  200, market_type)
        df_1h  = get_klines(symbol, "1h",  100, market_type)
        df_15m = get_klines(symbol, "15m",  50, market_type)
        if df_4h.empty or df_1h.empty or df_15m.empty:
            return None
        direction = get_trend(df_4h)
        if not direction:
            return None
        swing_high, swing_low = find_swing_points(df_1h, window=5)
        if swing_high is None or swing_low is None or swing_high <= swing_low:
            return None
        fib = calc_fib_levels(swing_high, swing_low, direction)
        if fib is None:
            return None
        price = df_15m['close'].iloc[-1]
        if not in_ote(price, fib):
            return None
        if not detect_bos(df_15m, direction):
            return None
        risk = abs(price - fib['sl'])
        if risk == 0:
            return None
        rr = round(abs(fib['tp3'] - price) / risk, 2)
        if rr < MIN_RR:
            return None
        wave_size = round((swing_high - swing_low) / swing_low * 100, 1)
        return {
            'symbol':          symbol,
            'market_type':     market_type,
            'direction':       direction,
            'entry_price':     round(price, 6),
            'sl':              round(fib['sl'], 6),
            'tp1':             round(fib['tp1'], 6),
            'tp2':             round(fib['tp2'], 6),
            'tp3':             round(fib['tp3'], 6),
            'swing_high':      round(swing_high, 6),
            'swing_low':       round(swing_low, 6),
            'fib_618':         round(fib['ote_high'], 6),
            'fib_786':         round(fib['ote_low'], 6),
            'rr':              rr,
            'wave_size':       wave_size,
            'timeframe':       '4H/1H/15M',
            'tradingview_url': build_tv_url(symbol),
        }
    except Exception as e:
        print(f"Analyze error {symbol}: {e}")
        return None

def scan_all_markets():
    results = []
    seen    = set()
    for market_type in ["FUTURES", "SPOT"]:
        symbols = get_top_symbols(market_type, limit=60)
        print(f"Scanning {len(symbols)} {market_type} symbols...")
        for symbol in symbols:
            if symbol in seen:
                continue
            seen.add(symbol)
            signal = analyze_symbol(symbol, market_type)
            if signal:
                results.append(signal)
                print(f"  Signal: {symbol} {market_type} {signal['direction']} RR:{signal['rr']}")
    return results
