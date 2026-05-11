import requests
import pandas as pd
import numpy as np
from config import BINANCE_BASE_URL, BINANCE_FUTURES_BASE_URL

STABLECOINS = {“USDC”,“BUSD”,“TUSD”,“DAI”,“FDUSD”,“USDP”,“GUSD”,“FRAX”,“LUSD”,“SUSD”,“AEUR”,“EURI”,“BFUSD”}
MIN_RR           = 1.5
MIN_VOLUME_24H   = 5_000_000   # $5M حد أدنى لحجم التداول
MAX_SL_PCT       = 0.07        # 7% حد أقصى للـ SL
ATR_MULTIPLIER   = 1.5         # مضاعف ATR لحساب SL

# ─────────────────────────────────────────────

# جلب الرموز

# ─────────────────────────────────────────────

def get_top_symbols(market_type, limit=60):
try:
if market_type == “FUTURES”:
url = f”{BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/24hr”
else:
url = f”{BINANCE_BASE_URL}/api/v3/ticker/24hr”
tickers = requests.get(url, timeout=15).json()
pairs = []
for t in tickers:
sym = t.get(“symbol”, “”)
if not sym.endswith(“USDT”):
continue
if sym[:-4] in STABLECOINS:
continue
try:
vol = float(t[“quoteVolume”])
# ✅ فلتر الحجم: تجاهل العملات الضعيفة السيولة
if vol < MIN_VOLUME_24H:
continue
pairs.append((sym, vol))
except:
continue
pairs.sort(key=lambda x: x[1], reverse=True)
return [p[0] for p in pairs[:limit]]
except Exception as e:
print(f”Error getting symbols: {e}”)
return []

# ─────────────────────────────────────────────

# جلب الشموع

# ─────────────────────────────────────────────

def get_klines(symbol, interval, limit, market_type):
try:
if market_type == “FUTURES”:
url = f”{BINANCE_FUTURES_BASE_URL}/fapi/v1/klines”
else:
url = f”{BINANCE_BASE_URL}/api/v3/klines”
params = {“symbol”: symbol, “interval”: interval, “limit”: limit}
resp   = requests.get(url, params=params, timeout=15)
klines = resp.json()
if not isinstance(klines, list) or len(klines) == 0:
return pd.DataFrame()
df = pd.DataFrame(klines, columns=[
‘time’,‘open’,‘high’,‘low’,‘close’,‘volume’,
‘close_time’,‘quote_vol’,‘trades’,‘taker_buy_base’,‘taker_buy_quote’,‘ignore’
])
for col in [‘open’,‘high’,‘low’,‘close’,‘volume’]:
df[col] = df[col].astype(float)
df[‘time’] = pd.to_datetime(df[‘time’], unit=‘ms’)
df.set_index(‘time’, inplace=True)
return df
except Exception as e:
print(f”Klines error {symbol} {interval}: {e}”)
return pd.DataFrame()

# ─────────────────────────────────────────────

# الاتجاه: EMA50 + EMA200

# ─────────────────────────────────────────────

def get_trend(df_4h):
if len(df_4h) < 201:
return None
ema50  = df_4h[‘close’].ewm(span=50,  adjust=False).mean().iloc[-1]
ema200 = df_4h[‘close’].ewm(span=200, adjust=False).mean().iloc[-1]
last   = df_4h[‘close’].iloc[-1]
# ✅ اشتراط توافق السعر مع EMA50 و EMA200 معاً
if last > ema50 and ema50 > ema200:
return “LONG”
if last < ema50 and ema50 < ema200:
return “SHORT”
return None

# ─────────────────────────────────────────────

# نقاط التأرجح

# ─────────────────────────────────────────────

def find_swing_points(df_1h, window=5):
highs = df_1h[‘high’].values
lows  = df_1h[‘low’].values
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

# ─────────────────────────────────────────────

# مستويات فيبوناتشي

# ─────────────────────────────────────────────

def calc_fib_levels(swing_high, swing_low, direction):
diff = swing_high - swing_low
if diff <= 0:
return None
if direction == “LONG”:
return {
‘ote_low’:  swing_high - 0.786 * diff,
‘ote_high’: swing_high - 0.618 * diff,
‘tp1’:      swing_high - 0.618 * diff,
‘tp2’:      swing_low  + diff * 0.764,
‘tp3’:      swing_high,
‘sl’:       swing_low * 0.999,
}
else:
return {
‘ote_low’:  swing_low + 0.618 * diff,
‘ote_high’: swing_low + 0.786 * diff,
‘tp1’:      swing_low + 0.618 * diff,
‘tp2’:      swing_high - diff * 0.764,
‘tp3’:      swing_low,
‘sl’:       swing_high * 1.001,
}

def in_ote(price, fib):
low  = min(fib[‘ote_low’], fib[‘ote_high’])
high = max(fib[‘ote_low’], fib[‘ote_high’])
return low <= price <= high

# ─────────────────────────────────────────────

# ✅ جديد: ATR لحساب التذبذب الحقيقي

# ─────────────────────────────────────────────

def calculate_atr(df, period=14):
highs  = df[‘high’].values
lows   = df[‘low’].values
closes = df[‘close’].values
tr_list = []
for i in range(1, len(df)):
tr = max(
highs[i]  - lows[i],
abs(highs[i]  - closes[i-1]),
abs(lows[i]   - closes[i-1])
)
tr_list.append(tr)
if len(tr_list) < period:
return None
return sum(tr_list[-period:]) / period

# ─────────────────────────────────────────────

# ✅ جديد: SL ديناميكي بـ ATR

# ─────────────────────────────────────────────

def calc_atr_sl(df_15m, entry, direction):
atr = calculate_atr(df_15m, period=14)
if atr is None:
return None
sl_distance = atr * ATR_MULTIPLIER
if direction == “LONG”:
return entry - sl_distance
else:
return entry + sl_distance

# ─────────────────────────────────────────────

# ✅ جديد: CHoCH (Change of Character) الحقيقي

# يشترط إغلاق الشمعة فوق/تحت آخر قمة/قاع هيكلية

# وليس مجرد كسر الظل كما في BOS العادي

# ─────────────────────────────────────────────

def detect_choch(df_15m, direction, lookback=20):
if len(df_15m) < lookback + 2:
return False

```
closes = df_15m['close'].values
highs  = df_15m['high'].values
lows   = df_15m['low'].values

# آخر إغلاق
last_close = closes[-1]

# نبحث في آخر lookback شمعة (ماعدا الأخيرة)
window = lookback
ref_highs = highs[-window-1:-1]
ref_lows  = lows[-window-1:-1]

if direction == "LONG":
    # CHoCH صاعد: الإغلاق يتجاوز أعلى قمة في النافذة
    structural_high = ref_highs.max()
    return last_close > structural_high

else:
    # CHoCH هابط: الإغلاق يكسر أدنى قاع في النافذة
    structural_low = ref_lows.min()
    return last_close < structural_low
```

# ─────────────────────────────────────────────

# BOS (يُستخدم كتأكيد ثانوي فقط)

# ─────────────────────────────────────────────

def detect_bos(df_15m, direction):
if len(df_15m) < 6:
return False
prev = df_15m.iloc[-6:-1]
last = df_15m[‘close’].iloc[-1]
if direction == “LONG”:
return last > prev[‘high’].max()
else:
return last < prev[‘low’].min()

# ─────────────────────────────────────────────

def build_tv_url(symbol):
return f”https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}&interval=60”

# ─────────────────────────────────────────────

# التحليل الرئيسي

# ─────────────────────────────────────────────

def analyze_symbol(symbol, market_type):
try:
df_4h  = get_klines(symbol, “4h”,  250, market_type)   # زدنا لـ 250 لتغطية EMA200
df_1h  = get_klines(symbol, “1h”,  100, market_type)
df_15m = get_klines(symbol, “15m”,  60, market_type)    # زدنا لـ 60 لتغطية CHoCH
if df_4h.empty or df_1h.empty or df_15m.empty:
return None

```
    # ① الاتجاه (EMA50 + EMA200)
    direction = get_trend(df_4h)
    if not direction:
        return None

    # ② نقاط التأرجح ومستويات فيبو
    swing_high, swing_low = find_swing_points(df_1h, window=5)
    if swing_high is None or swing_low is None or swing_high <= swing_low:
        return None
    fib = calc_fib_levels(swing_high, swing_low, direction)
    if fib is None:
        return None

    # ③ السعر في منطقة OTE
    price = df_15m['close'].iloc[-1]
    if not in_ote(price, fib):
        return None

    # ④ ✅ CHoCH (اشتراط أساسي) + BOS (تأكيد ثانوي)
    has_choch = detect_choch(df_15m, direction, lookback=20)
    has_bos   = detect_bos(df_15m, direction)
    if not has_choch and not has_bos:
        return None

    # ⑤ ✅ SL ديناميكي: نأخذ الأكثر حماية بين ATR والفيبو
    atr_sl  = calc_atr_sl(df_15m, price, direction)
    fib_sl  = fib['sl']

    if direction == "LONG":
        # نختار الأعلى (الأقرب للسعر = أكثر أماناً وأقل خسارة)
        sl = max(atr_sl, fib_sl) if atr_sl else fib_sl
    else:
        # نختار الأدنى (الأقرب للسعر)
        sl = min(atr_sl, fib_sl) if atr_sl else fib_sl

    # ⑥ ✅ فلتر SL% — لا تجاوز 7%
    sl_pct = abs(price - sl) / price
    if sl_pct > MAX_SL_PCT:
        print(f"  [SKIP] {symbol} SL% {sl_pct:.1%} > {MAX_SL_PCT:.0%}")
        return None

    risk = abs(price - sl)
    if risk == 0:
        return None

    rr = round(abs(fib['tp3'] - price) / risk, 2)
    if rr < MIN_RR:
        return None

    return {
        'symbol':          symbol,
        'market_type':     market_type,
        'direction':       direction,
        'entry_price':     round(price, 6),
        'sl':              round(sl, 6),
        'sl_pct':          round(sl_pct * 100, 2),       # % الـ SL للعرض
        'tp1':             round(fib['tp1'], 6),
        'tp2':             round(fib['tp2'], 6),
        'tp3':             round(fib['tp3'], 6),
        'swing_high':      round(swing_high, 6),
        'swing_low':       round(swing_low, 6),
        'fib_618':         round(fib['ote_high'], 6),
        'fib_786':         round(fib['ote_low'], 6),
        'rr':              rr,
        'choch':           has_choch,                     # هل تأكد CHoCH
        'bos':             has_bos,
        'timeframe':       '4H/1H/15M',
        'tradingview_url': build_tv_url(symbol),
    }
except Exception as e:
    print(f"Analyze error {symbol}: {e}")
    return None
```

# ─────────────────────────────────────────────

# المسح الكامل

# ─────────────────────────────────────────────

def scan_all_markets():
results = []
seen    = set()
for market_type in [“FUTURES”, “SPOT”]:
symbols = get_top_symbols(market_type, limit=60)
print(f”Scanning {len(symbols)} {market_type} symbols…”)
for symbol in symbols:
if symbol in seen:
continue
seen.add(symbol)
signal = analyze_symbol(symbol, market_type)
if signal:
results.append(signal)
choch_tag = “CHoCH✓” if signal[‘choch’] else “BOS”
print(f”  Signal: {symbol} {market_type} {signal[‘direction’]} “
f”RR:{signal[‘rr’]} SL:{signal[‘sl_pct’]}% [{choch_tag}]”)
return results
