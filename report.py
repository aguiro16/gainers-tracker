import anthropic
from datetime import datetime, date
import pytz
from database import get_today_signals
from telegram_bot import send_message, format_daily_report
from config import ANTHROPIC_API_KEY

def get_daily_stats() -> dict:
signals = get_today_signals()
today_str = date.today().strftime(”%Y-%m-%d”)

```
wins   = [s for s in signals if s['status'] == 'CLOSED' and s['pnl_pct'] and s['pnl_pct'] > 0]
losses = [s for s in signals if s['status'] == 'CLOSED' and s['pnl_pct'] and s['pnl_pct'] < 0]
open_s = [s for s in signals if s['status'] == 'OPEN']

total_pnl = sum(s['pnl_pct'] for s in signals if s['status'] == 'CLOSED' and s['pnl_pct'])

best_signal  = "—"
worst_signal = "—"

if wins:
    best = max(wins, key=lambda x: x['pnl_pct'])
    best_signal = f"#{best['signal_number']} {best['symbol']} (+{best['pnl_pct']:.2f}%)"

if losses:
    worst = min(losses, key=lambda x: x['pnl_pct'])
    worst_signal = f"#{worst['signal_number']} {worst['symbol']} ({worst['pnl_pct']:.2f}%)"

return {
    'date':         today_str,
    'total':        len(signals),
    'wins':         len(wins),
    'losses':       len(losses),
    'open':         len(open_s),
    'total_pnl':    round(total_pnl, 2),
    'best_signal':  best_signal,
    'worst_signal': worst_signal,
    'signals':      signals,
}
```

def build_claude_prompt(stats: dict) -> str:
“”“بناء البرومبت لكلود لتحليل الإشارات الخاسرة”””
losing = [s for s in stats[‘signals’] if s[‘status’] == ‘CLOSED’ and s[‘pnl_pct’] and s[‘pnl_pct’] < 0]
if not losing:
return “”

```
lines = []
lines.append("أنت خبير تحليل تقني متخصص في استراتيجية فيبوناتشي وSmart Money Concepts.")
lines.append("فيما يلي الإشارات الخاسرة اليوم. حللها وحدد الأخطاء وكيف نطور الاستراتيجية:\n")

for s in losing:
    lines.append(f"""
```

إشارة #{s[‘signal_number’]}:

- العملة: {s[‘symbol’]} | السوق: {s[‘market_type’]} | الاتجاه: {s[‘direction’]}
- نقطة الدخول: {s[‘entry_price’]} | وقف الخسارة: {s[‘sl’]}
- TP1: {s[‘tp1’]} | TP2: {s[‘tp2’]} | TP3: {s[‘tp3’]}
- Swing High: {s[‘swing_high’]} | Swing Low: {s[‘swing_low’]}
- Fib 0.618: {s[‘fib_618’]} | Fib 0.786: {s[‘fib_786’]}
- R:R: 1:{s[‘rr’]}
- الخسارة: {s[‘pnl_pct’]}%
- رابط الشارت: {s[‘tradingview_url’]}
  “””)
  
  lines.append(”””
  المطلوب:

1. ما الأخطاء الشائعة في هذه الإشارات؟
1. هل هناك مشكلة في تحديد الاتجاه أو الدخول أو وقف الخسارة؟
1. كيف نحسن استراتيجية الفيبوناتشي لتجنب هذه الأخطاء؟
1. توصيات عملية لتطوير البوت.
   أجب بالعربية بشكل مختصر ومنظم (نقاط).
   “””)
   return “\n”.join(lines)

def analyze_with_claude(prompt: str) -> str:
try:
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
response = client.messages.create(
model=“claude-opus-4-5”,
max_tokens=1500,
messages=[{“role”: “user”, “content”: prompt}]
)
return response.content[0].text
except Exception as e:
print(f”Claude API error: {e}”)
return “⚠️ تعذر الحصول على تحليل كلود AI”

def send_daily_report():
“”“يُستدعى كل يوم الساعة 11:00 بتوقيت السعودية”””
print(“Generating daily report…”)
stats = get_daily_stats()
is_negative = stats[‘total_pnl’] < 0 or stats[‘losses’] > stats[‘wins’]

```
claude_analysis = None
if is_negative and stats['losses'] > 0:
    print("Negative day detected, requesting Claude analysis...")
    prompt = build_claude_prompt(stats)
    if prompt:
        claude_analysis = analyze_with_claude(prompt)

msg = format_daily_report(stats, claude_analysis)
send_message(msg)
print("Daily report sent.")
```
