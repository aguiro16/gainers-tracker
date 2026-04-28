import requests
from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID

BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

def send_message(text, parse_mode="HTML"):
    try:
        resp = requests.post(f"{BASE_URL}/sendMessage", json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": False
        })
        data = resp.json()
        if data.get("ok"):
            return data["result"]["message_id"]
        return None
    except Exception as e:
        print(f"Send message error: {e}")
        return None

def format_signal_message(signal):
    direction_emoji = "📈" if signal['direction'] == "LONG" else "📉"
    market_emoji    = "🔵" if signal['market_type'] == "FUTURES" else "🟡"
    direction_ar    = "شراء LONG" if signal['direction'] == "LONG" else "بيع SHORT"
    wave_size       = signal.get('wave_size', '—')
    return f"""
{direction_emoji} <b>إشارة #{signal['signal_number']}</b> | {market_emoji} {signal['symbol']} | {signal['market_type']}
━━━━━━━━━━━━━━━━━━━━━
<b>الاتجاه:</b> {direction_ar}
<b>الفريم:</b> {signal['timeframe']}

📐 <b>مستويات فيبوناتشي:</b>
  حجم الموجة: <b>{wave_size}%</b>
  Swing High: <code>{signal['swing_high']}</code>
  Swing Low:  <code>{signal['swing_low']}</code>
  0.618 🟡:   <code>{signal['fib_618']}</code>
  0.786 🔴:   <code>{signal['fib_786']}</code>

🎯 <b>نقطة الدخول:</b> <code>{signal['entry_price']}</code>

🏆 <b>الأهداف:</b>
  TP1: <code>{signal['tp1']}</code>
  TP2: <code>{signal['tp2']}</code>
  TP3: <code>{signal['tp3']}</code>

🛑 <b>وقف الخسارة:</b> <code>{signal['sl']}</code>
⚖️ <b>R:R =</b> 1:{signal['rr']}

🔗 <a href="{signal['tradingview_url']}">الشارت على TradingView</a>
━━━━━━━━━━━━━━━━━━━━━
""".strip()

def format_result_message(signal):
    if signal['pnl_pct'] > 0:
        emoji  = "✅"
        status = "ربح"
        pnl_str = f"+{signal['pnl_pct']:.2f}%"
    else:
        emoji  = "❌"
        status = "خسارة"
        pnl_str = f"{signal['pnl_pct']:.2f}%"
    result_map = {
        'TP1': '🎯 TP1 تم الوصول',
        'TP2': '🎯🎯 TP2 تم الوصول',
        'TP3': '🎯🎯🎯 TP3 هدف كامل!',
        'SL':  '🛑 وقف الخسارة',
    }
    result_text  = result_map.get(signal['result'], signal['result'])
    market_emoji = "🔵" if signal['market_type'] == "FUTURES" else "🟡"
    direction_ar = "LONG شراء" if signal['direction'] == "LONG" else "SHORT بيع"
    from datetime import datetime
    try:
        created  = datetime.fromisoformat(signal['created_at'])
        closed   = datetime.fromisoformat(signal['closed_at'])
        diff     = closed - created
        hours    = int(diff.total_seconds() // 3600)
        minutes  = int((diff.total_seconds() % 3600) // 60)
        duration = f"{hours}س {minutes}د"
    except:
        duration = "—"
    return f"""
{emoji} <b>نتيجة إشارة #{signal['signal_number']}</b>
━━━━━━━━━━━━━━━━━━━━━
{market_emoji} <b>{signal['symbol']}</b> | {direction_ar}
{result_text}

💵 <b>النتيجة:</b> {status} <b>{pnl_str}</b>
⏱️ <b>المدة:</b> {duration}

🔗 <a href="{signal['tradingview_url']}">الشارت على TradingView</a>
━━━━━━━━━━━━━━━━━━━━━
""".strip()

def format_daily_report(stats, claude_analysis=None):
    win_rate = 0
    if stats['total'] > 0:
        win_rate = round((stats['wins'] / stats['total']) * 100, 1)
    is_positive = stats['total_pnl'] >= 0
    header_emoji = "📊✅" if is_positive else "📊❌"
    pnl_str = f"+{stats['total_pnl']:.2f}%" if stats['total_pnl'] >= 0 else f"{stats['total_pnl']:.2f}%"
    msg = f"""
{header_emoji} <b>التقرير اليومي</b>
━━━━━━━━━━━━━━━━━━━━━
📅 {stats['date']}

<b>ملخص الإشارات:</b>
  📨 إجمالي: {stats['total']}
  ✅ الرابحة: {stats['wins']}
  ❌ الخاسرة: {stats['losses']}
  ⏳ المفتوحة: {stats['open']}
  🎯 نسبة الفوز: {win_rate}%

<b>الأداء:</b>
  💰 إجمالي PnL: {pnl_str}
  📈 أفضل إشارة: {stats.get('best_signal','—')}
  📉 أسوأ إشارة: {stats.get('worst_signal','—')}
━━━━━━━━━━━━━━━━━━━━━
""".strip()
    if claude_analysis:
        msg += f"\n\n🤖 <b>تحليل كلود AI:</b>\n{claude_analysis}"
    return msg
