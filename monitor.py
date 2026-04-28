import requests
from config import BINANCE_BASE_URL, BINANCE_FUTURES_BASE_URL
from database import get_open_signals, get_signal_by_number, close_signal
from telegram_bot import send_message, format_result_message

def get_current_price(symbol, market_type):
    try:
        if market_type == "FUTURES":
            url = f"{BINANCE_FUTURES_BASE_URL}/fapi/v1/ticker/price"
        else:
            url = f"{BINANCE_BASE_URL}/api/v3/ticker/price"
        resp = requests.get(url, params={"symbol": symbol}, timeout=5)
        return float(resp.json()['price'])
    except Exception as e:
        print(f"Price error {symbol}: {e}")
        return None

def check_signal(signal):
    price = get_current_price(signal['symbol'], signal['market_type'])
    if price is None:
        return None
    direction = signal['direction']
    sl  = signal['sl']
    tp1 = signal['tp1']
    tp2 = signal['tp2']
    tp3 = signal['tp3']
    if direction == "LONG":
        if price <= sl:    return "SL"
        elif price >= tp3: return "TP3"
        elif price >= tp2: return "TP2"
        elif price >= tp1: return "TP1"
    else:
        if price >= sl:    return "SL"
        elif price <= tp3: return "TP3"
        elif price <= tp2: return "TP2"
        elif price <= tp1: return "TP1"
    return None

def calc_pnl(signal, result):
    entry = signal['entry_price']
    targets = {
        'TP1': signal['tp1'],
        'TP2': signal['tp2'],
        'TP3': signal['tp3'],
        'SL':  signal['sl'],
    }
    exit_price = targets.get(result, signal['sl'])
    if signal['direction'] == "LONG":
        pnl = ((exit_price - entry) / entry) * 100
    else:
        pnl = ((entry - exit_price) / entry) * 100
    if result == 'SL' and pnl > 0:
        pnl = -abs(pnl)
    elif result in ('TP1','TP2','TP3') and pnl < 0:
        pnl = abs(pnl)
    return round(pnl, 2)

def monitor_open_signals():
    open_signals = get_open_signals()
    if not open_signals:
        return
    print(f"Monitoring {len(open_signals)} open signals...")
    for signal in open_signals:
        result = check_signal(signal)
        if result:
            pnl = calc_pnl(signal, result)
            close_signal(signal['signal_number'], result, pnl)
            updated = get_signal_by_number(signal['signal_number'])
            if updated:
                msg = format_result_message(updated)
                send_message(msg)
                print(f"Signal #{signal['signal_number']} closed: {result} | PnL: {pnl}%")
