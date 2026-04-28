import time
import pytz
import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from database import init_db, get_next_signal_number, save_signal, update_signal_message_id, get_open_signals
from analyzer import scan_all_markets
from monitor import monitor_open_signals
from report import send_daily_report, send_weekly_report
from telegram_bot import send_message, format_signal_message

active_symbols = set()

def run_scan():
    print("Running market scan...")
    signals = scan_all_markets()
    for signal in signals:
        key = f"{signal['symbol']}_{signal['market_type']}_{signal['direction']}"
        if key in active_symbols:
            continue
        signal['signal_number'] = get_next_signal_number()
        save_signal(signal)
        msg = format_signal_message(signal)
        message_id = send_message(msg)
        if message_id:
            update_signal_message_id(signal['signal_number'], message_id)
        active_symbols.add(key)
        print(f"Signal #{signal['signal_number']} sent: {signal['symbol']} {signal['direction']}")

def cleanup_active_symbols():
    open_signals = get_open_signals()
    open_keys    = {f"{s['symbol']}_{s['market_type']}_{s['direction']}" for s in open_signals}
    closed_keys  = active_symbols - open_keys
    for k in closed_keys:
        active_symbols.discard(k)

def main():
    print("Fibonacci Signal Bot Starting...")
    init_db()
    send_message("🚀 <b>بوت فيبوناتشي يعمل الآن!</b>\n📊 يراقب أعلى 60 عملة\n⏰ فحص كل 15 دقيقة")
    scheduler = BackgroundScheduler(timezone=pytz.utc)
    scheduler.add_job(run_scan, IntervalTrigger(minutes=15), id="market_scan", next_run_time=datetime.datetime.utcnow())
    scheduler.add_job(monitor_open_signals, IntervalTrigger(minutes=1), id="monitor_signals")
    scheduler.add_job(cleanup_active_symbols, IntervalTrigger(hours=1), id="cleanup")
    scheduler.add_job(send_daily_report, CronTrigger(hour=8, minute=0, timezone=pytz.utc), id="daily_report")
    scheduler.add_job(send_weekly_report, CronTrigger(day_of_week='sat', hour=8, minute=0, timezone=pytz.utc), id="weekly_report")
    scheduler.start()
    print("Bot is running...")
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()

if __name__ == "__main__":
    main()
