import time
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import init_db
from analyzer import scan_all_markets
from monitor import monitor_open_signals
from report import send_daily_report
from telegram_bot import send_message, format_signal_message, update_signal_message_id
from database import get_next_signal_number, save_signal

# متابعة العملات التي أرسلنا لها إشارة مفتوحة لتجنب التكرار

active_symbols = set()

def run_scan():
“”“فحص السوق كل 15 دقيقة”””
print(“Running market scan…”)
signals = scan_all_markets()

```
for signal in signals:
    key = f"{signal['symbol']}_{signal['market_type']}_{signal['direction']}"
    if key in active_symbols:
        continue

    # ترقيم الإشارة
    signal['signal_number'] = get_next_signal_number()

    # حفظ في قاعدة البيانات
    save_signal(signal)

    # إرسال على التليجرام
    msg = format_signal_message(signal)
    message_id = send_message(msg)

    if message_id:
        from database import update_signal_message_id
        update_signal_message_id(signal['signal_number'], message_id)

    active_symbols.add(key)
    print(f"Signal #{signal['signal_number']} sent: {signal['symbol']} {signal['direction']}")
```

def cleanup_active_symbols():
“”“تنظيف العملات المغلقة من الذاكرة كل ساعة”””
from database import get_open_signals
open_signals = get_open_signals()
open_keys = {
f”{s[‘symbol’]}*{s[‘market_type’]}*{s[‘direction’]}”
for s in open_signals
}
closed_keys = active_symbols - open_keys
for k in closed_keys:
active_symbols.discard(k)
print(f”Cleaned {len(closed_keys)} closed symbols from memory.”)

def main():
print(“🤖 Fibonacci Signal Bot Starting…”)
init_db()

```
send_message("🚀 <b>بوت فيبوناتشي يعمل الآن!</b>\n📊 يراقب أعلى 60 عملة Futures + Spot\n⏰ فحص كل 15 دقيقة")

saudi_tz = pytz.timezone("Asia/Riyadh")
scheduler = BackgroundScheduler(timezone=pytz.utc)

# فحص السوق كل 15 دقيقة
scheduler.add_job(
    run_scan,
    IntervalTrigger(minutes=15),
    id="market_scan",
    next_run_time=__import__('datetime').datetime.utcnow()
)

# مراقبة الإشارات المفتوحة كل دقيقة
scheduler.add_job(
    monitor_open_signals,
    IntervalTrigger(minutes=1),
    id="monitor_signals"
)

# تنظيف الذاكرة كل ساعة
scheduler.add_job(
    cleanup_active_symbols,
    IntervalTrigger(hours=1),
    id="cleanup"
)

# تقرير يومي الساعة 11:00 صباحاً بتوقيت السعودية = 08:00 UTC
scheduler.add_job(
    send_daily_report,
    CronTrigger(hour=8, minute=0, timezone=pytz.utc),
    id="daily_report"
)

scheduler.start()
print("✅ Scheduler started. Bot is running...")

try:
    while True:
        time.sleep(60)
except (KeyboardInterrupt, SystemExit):
    scheduler.shutdown()
    print("Bot stopped.")
```

if **name** == “**main**”:
main()
