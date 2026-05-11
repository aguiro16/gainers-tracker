import anthropic
from datetime import datetime, date, timedelta
from telegram_bot import send_message, format_daily_report
from config import ANTHROPIC_API_KEY
import sqlite3

DB_PATH = "signals.db"

# ─────────────────────────────────────────────
# جلب الإشارات المغلقة فقط ضمن نطاق زمني
# ─────────────────────────────────────────────
def get_closed_signals_in_range(from_dt, to_dt):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT * FROM signals
        WHERE status = 'CLOSED'
          AND closed_at >= ?
          AND closed_at < ?
    """, (from_dt, to_dt))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

def get_open_signals_count():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM signals WHERE status = 'OPEN'")
    count = c.fetchone()[0]
    conn.close()
    return count

def get_today_signals():
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end   = today_start + timedelta(days=1)
    return get_closed_signals_in_range(
        today_start.isoformat(),
        today_end.isoformat()
    )

def get_week_signals():
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start  = today_start - timedelta(days=7)
    week_end    = today_start + timedelta(days=1)
    return get_closed_signals_in_range(
        week_start.isoformat(),
        week_end.isoformat()
    )

# ─────────────────────────────────────────────
# حساب الإحصائيات
# ─────────────────────────────────────────────
def calc_stats(signals, label):
    open_count = get_open_signals_count()
    wins   = [s for s in signals if s['pnl_pct'] and s['pnl_pct'] > 0]
    losses = [s for s in signals if s['pnl_pct'] and s['pnl_pct'] < 0]
    total_pnl = sum(s['pnl_pct'] for s in signals if s['pnl_pct'])
    best_signal  = "—"
    worst_signal = "—"
    if wins:
        best = max(wins, key=lambda x: x['pnl_pct'])
        best_signal = f"#{best['signal_number']} {best['symbol']} (+{best['pnl_pct']:.2f}%)"
    if losses:
        worst = min(losses, key=lambda x: x['pnl_pct'])
        worst_signal = f"#{worst['signal_number']} {worst['symbol']} ({worst['pnl_pct']:.2f}%)"
    return {
        'date':         label,
        'total':        len(signals),
        'wins':         len(wins),
        'losses':       len(losses),
        'open':         open_count,
        'total_pnl':    round(total_pnl, 2),
        'best_signal':  best_signal,
        'worst_signal': worst_signal,
        'signals':      signals,
    }

def get_daily_stats():
    return calc_stats(get_today_signals(), date.today().strftime("%Y-%m-%d"))

def get_weekly_stats():
    from_date = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    to_date   = date.today().strftime("%Y-%m-%d")
    return calc_stats(get_week_signals(), f"{from_date} → {to_date}")

# ─────────────────────────────────────────────
# بناء البرومبت اليومي
# ─────────────────────────────────────────────
def build_daily_prompt(stats):
    losing = [s for s in stats['signals'] if s['pnl_pct'] and s['pnl_pct'] < 0]
    if not losing:
        return ""
    lines = ["أنت خبير تحليل تقني متخصص في فيبوناتشي وSMC."]
    lines.append("حلل الإشارات الخاسرة التالية:\n")
    for s in losing:
        lines.append(
            f"إشارة #{s['signal_number']}: {s['symbol']} {s['direction']} "
            f"| خسارة: {s['pnl_pct']}% | R:R: {s['rr']} | الشارت: {s['tradingview_url']}"
        )
    lines.append("\nالمطلوب بالعربية:\n1. الأخطاء\n2. التوصيات")
    return "\n".join(lines)

# ─────────────────────────────────────────────
# بناء البرومبت الأسبوعي
# ─────────────────────────────────────────────
def build_weekly_prompt(stats):
    signals  = stats['signals']
    wins     = [s for s in signals if s['pnl_pct'] and s['pnl_pct'] > 0]
    losses   = [s for s in signals if s['pnl_pct'] and s['pnl_pct'] < 0]
    closed   = wins + losses
    win_rate = round(len(wins) / len(closed) * 100, 1) if closed else 0
    symbol_stats = {}
    for s in closed:
        sym = s['symbol']
        if sym not in symbol_stats:
            symbol_stats[sym] = {'wins': 0, 'losses': 0, 'pnl': 0}
        if s['pnl_pct'] > 0:
            symbol_stats[sym]['wins'] += 1
        else:
            symbol_stats[sym]['losses'] += 1
        symbol_stats[sym]['pnl'] += s['pnl_pct']
    best_symbols  = sorted(symbol_stats.items(), key=lambda x: x[1]['pnl'], reverse=True)[:3]
    worst_symbols = sorted(symbol_stats.items(), key=lambda x: x[1]['pnl'])[:3]
    long_trades  = [s for s in closed if s['direction'] == 'LONG']
    short_trades = [s for s in closed if s['direction'] == 'SHORT']
    long_wr  = round(len([s for s in long_trades  if s['pnl_pct'] > 0]) / len(long_trades)  * 100, 1) if long_trades  else 0
    short_wr = round(len([s for s in short_trades if s['pnl_pct'] > 0]) / len(short_trades) * 100, 1) if short_trades else 0
    return f"""أنت خبير تحليل تقني متخصص في فيبوناتشي OTE.
إحصائيات الأسبوع:
- إجمالي مغلقة: {len(closed)} | مفتوحة حالياً: {stats['open']} | Win Rate: {win_rate}%
- PnL: {stats['total_pnl']}% | LONG WR: {long_wr}% | SHORT WR: {short_wr}%
- أفضل العملات: {', '.join([f"{s[0]} ({s[1]['pnl']:.1f}%)" for s in best_symbols])}
- أسوأ العملات: {', '.join([f"{s[0]} ({s[1]['pnl']:.1f}%)" for s in worst_symbols])}

المطلوب بالعربية:
1. تقييم الأداء العام
2. مشكلة في LONG أم SHORT؟
3. العملات التي يجب استبعادها
4. تعديلات محددة على الكود مع اسم المتغير والقيمة الجديدة
"""

# ─────────────────────────────────────────────
# تحليل كلود
# ─────────────────────────────────────────────
def analyze_with_claude(prompt):
    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        print(f"Claude API error: {e}")
        return "⚠️ تعذر الحصول على تحليل كلود"

# ─────────────────────────────────────────────
# تنسيق التقرير الأسبوعي
# ─────────────────────────────────────────────
def format_weekly_report(stats, claude_analysis):
    total    = stats['total']
    wins     = stats['wins']
    win_rate = round(wins / total * 100, 1) if total > 0 else 0
    is_positive = stats['total_pnl'] >= 0
    header  = "📊✅" if is_positive else "📊❌"
    pnl_str = f"+{stats['total_pnl']:.2f}%" if is_positive else f"{stats['total_pnl']:.2f}%"
    msg = f"""
{header} <b>التقرير الأسبوعي</b>
━━━━━━━━━━━━━━━━━━━━━
📅 {stats['date']}

<b>ملخص الأسبوع:</b>
  📨 إجمالي مغلقة: {total}
  ✅ الرابحة: {wins}
  ❌ الخاسرة: {stats['losses']}
  ⏳ المفتوحة حالياً: {stats['open']}
  🎯 نسبة الفوز: {win_rate}%
  💰 إجمالي PnL: {pnl_str}
  📈 أفضل إشارة: {stats['best_signal']}
  📉 أسوأ إشارة: {stats['worst_signal']}
━━━━━━━━━━━━━━━━━━━━━
🤖 <b>تحليل وتوصيات كلود AI:</b>

{claude_analysis}
━━━━━━━━━━━━━━━━━━━━━
""".strip()
    return msg

# ─────────────────────────────────────────────
# إرسال التقارير
# ─────────────────────────────────────────────
def send_daily_report():
    print("Generating daily report...")
    stats = get_daily_stats()
    is_negative = stats['total_pnl'] < 0 or stats['losses'] > stats['wins']
    claude_analysis = None
    if is_negative and stats['losses'] > 0:
        prompt = build_daily_prompt(stats)
        if prompt:
            claude_analysis = analyze_with_claude(prompt)
    msg = format_daily_report(stats, claude_analysis)
    send_message(msg)
    print("Daily report sent.")

def send_weekly_report():
    print("Generating weekly report...")
    stats = get_weekly_stats()
    if stats['total'] == 0:
        send_message("📊 <b>التقرير الأسبوعي</b>\n\nلا توجد إشارات مغلقة هذا الأسبوع.")
        return
    prompt   = build_weekly_prompt(stats)
    analysis = analyze_with_claude(prompt)
    msg      = format_weekly_report(stats, analysis)
    send_message(msg)
    print("Weekly report sent.")
