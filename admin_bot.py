# -*- coding: utf-8 -*-
"""
بوت المشرف — فذ العقارية.
بوت ثانٍ تتحكم أنت فيه عبر تيليجرام:
- تنبيه فوري بالردود الضعيفة (≤6/10) القادمة من quality_monitor
- إضافة قواعد تحسّن برومبت بوت فذ تلقائياً لكل الزبائن
- عرض/حذف القواعد

التشغيل على السيرفر: systemd أو nohup مع ADMIN_BOT_TOKEN في .env
"""
import os
import sys
import logging

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from database import get_sorted_conn, migrate
from deepseek_client import ask_ai

load_dotenv()
migrate()

ADMIN_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("fadh_admin")


# ──────────────────────────────────────────────
#  قاعدة البيانات: قواعد البرومبت + قنوات المشرف
# ──────────────────────────────────────────────
def _tables(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bot_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule TEXT,
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS admin_chats (
            chat_id TEXT PRIMARY KEY,
            name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )


def save_rule(rule, created_by):
    conn = get_sorted_conn()
    _tables(conn)
    conn.execute("INSERT INTO bot_rules (rule, created_by) VALUES (?,?)", (rule[:500], str(created_by)))
    conn.commit()
    conn.close()


def list_rules():
    conn = get_sorted_conn()
    _tables(conn)
    rows = conn.execute("SELECT * FROM bot_rules ORDER BY id").fetchall()
    conn.close()
    return rows


def delete_rule(rid):
    conn = get_sorted_conn()
    _tables(conn)
    conn.execute("DELETE FROM bot_rules WHERE id=?", (rid,))
    conn.commit()
    conn.close()


def register_chat(chat_id, name):
    conn = get_sorted_conn()
    _tables(conn)
    conn.execute(
        "INSERT OR REPLACE INTO admin_chats (chat_id, name) VALUES (?,?)",
        (str(chat_id), (name or "")[:100]),
    )
    conn.commit()
    conn.close()


def admin_chat_ids(conn=None):
    """محادثات المشرفين — يقبل conn موجوداً لتجنب قفل قاعدة في عمليات متزامنة"""
    if conn is not None:
        _tables(conn)
        rows = conn.execute("SELECT chat_id FROM admin_chats").fetchall()
        return [r["chat_id"] for r in rows]
    conn = get_sorted_conn()
    _tables(conn)
    rows = conn.execute("SELECT chat_id FROM admin_chats").fetchall()
    conn.close()
    return [r["chat_id"] for r in rows]


def notify_admins(message, conn=None):
    """إرسال تنبيه لكل المشرفين عبر HTTP مباشر (يستخدمه quality_monitor خارج البوت)"""
    import requests
    for chat_id in admin_chat_ids(conn):
        try:
            requests.post(
                f"https://api.telegram.org/bot{ADMIN_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": message[:4000]},
                timeout=15,
            )
        except Exception as e:
            print(f"admin notify err: {e}", flush=True)


# ──────────────────────────────────────────────
#  تقارير الحالة — تُبنى من قاعدة البيانات
# ──────────────────────────────────────────────
def _memory_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bot_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule TEXT, created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS conversation_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id INTEGER UNIQUE, score INTEGER, verdict TEXT,
            reason TEXT, suggested_reply TEXT,
            reviewed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT, user_name TEXT, message TEXT, bot_reply TEXT,
            reviewed INTEGER DEFAULT 0, review_note TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )


def status_report():
    """تقرير سريع عن النشاط اليوم + متوسط الجودة + القواعد"""
    conn = get_sorted_conn()
    _memory_table(conn)
    today = conn.execute(
        "SELECT COUNT(*) c, ROUND(AVG(score),1) a FROM conversation_reviews WHERE reviewed_at >= date('now')"
    ).fetchone()
    weak = conn.execute(
        "SELECT COUNT(*) c FROM conversation_reviews WHERE score <= 6 AND reviewed_at >= date('now')"
    ).fetchone()
    conv_today = conn.execute(
        "SELECT COUNT(*) c FROM conversations WHERE ts >= datetime('now','-24 hours')"
    ).fetchone()
    bad_today = conn.execute(
        "SELECT COUNT(*) c FROM conversations WHERE ts >= datetime('now','-24 hours') AND reviewed=1 AND id IN (SELECT conv_id FROM conversation_reviews WHERE score <= 6)"
    ).fetchone()
    rules_n = conn.execute("SELECT COUNT(*) c FROM bot_rules").fetchone()
    try:
        listings_n = conn.execute("SELECT COUNT(*) c FROM sorted_listings WHERE status='نشط' AND listing_type='عرض'").fetchone()
    except Exception:
        listings_n = {"c": 0}
    conn.close()
    return (
        "📊 حالة بوت فذ العقارية:\n\n"
        f"• محادثات آخر 24 ساعة: {conv_today['c']}\n"
        f"• مراجعات اليوم: {today['c']} (متوسط الدرجة {today['a']})\n"
        f"• ردود ضعيفة اليوم: {weak['c']}\n"
        f"• العروض النشطة: {listings_n['c']}\n"
        f"• القواعد المطبقة: {rules_n['c']}\n"
        "اكتب اقتراحاً أو سؤالاً عن البوت وسأحلله لك."
    )


def day_report():
    """تقرير نهاية اليوم — يُرسل تلقائياً كل ليلة وللطلب اليدوي"""
    conn = get_sorted_conn()
    _memory_table(conn)
    today = conn.execute(
        """SELECT COUNT(*) c, ROUND(AVG(score),1) a,
                  SUM(CASE WHEN score <= 6 THEN 1 ELSE 0 END) weak
           FROM conversation_reviews WHERE reviewed_at >= date('now')"""
    ).fetchone()
    conv = conn.execute(
        "SELECT COUNT(*) c FROM conversations WHERE ts >= datetime('now','-24 hours')"
    ).fetchone()
    last_weak = conn.execute(
        """SELECT c.id, c.user_name, r.score, r.reason, r.suggested_reply
           FROM conversation_reviews r JOIN conversations c ON c.id=r.conv_id
           WHERE r.reviewed_at >= date('now') AND r.score <= 6
           ORDER BY r.reviewed_at DESC LIMIT 5"""
    ).fetchall()
    rules_today = conn.execute(
        "SELECT rule FROM bot_rules WHERE created_at >= datetime('now','-24 hours')"
    ).fetchall()
    conn.close()
    lines = [
        "📋 تقرير فذ العقارية اليومي:\n",
        f"• مراجعات اليوم: {today['c']} | متوسط الدرجة: {today['a']}/10",
        f"• ردود ضعيفة: {today['weak'] or 0} | محادثات: {conv['c']}",
    ]
    if rules_today:
        lines.append(f"\n🛠️ قواعد جديدة طُبقت اليوم ({len(rules_today)}):")
        for r in rules_today[:8]:
            lines.append(f"  - {r['rule']}")
    if last_weak:
        lines.append("\n⚠️ أضعف الردود اليوم:")
        for r in last_weak:
            lines.append(f"  - [{r['score']}/10] {r['user_name']}: {(r['reason'] or '')[:80]}")
    else:
        lines.append("\n✅ لا توجد ردود ضعيفة اليوم.")
    return "\n".join(lines)


def send_daily_report():
    """إرسال تقرير نهاية اليوم للمشرفين — تستدعيه cron"""
    try:
        notify_admins(day_report())
        print("أُرسل التقرير اليومي", flush=True)
    except Exception as e:
        print(f"فشل التقرير اليومي: {e}", flush=True)


# ──────────────────────────────────────────────
#  أوامر بوت المشرف
# ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    register_chat(chat.id, chat.title or update.effective_user.first_name)
    await update.message.reply_text(
        "👋 أهلاً بك — أنا بوت مشرف فذ العقارية.\n\n"
        "الأوامر:\n"
        "/status — تقرير سريع عن حالة البوت اليوم\n"
        "/rule <القاعدة> — أضف قاعدة تحسّن برومبت بوت فذ (مثال: /rule لا تعرض أرض مع شقة أبداً)\n"
        "/rules — عرض كل القواعد\n"
        "/delrule <رقم> — حذف قاعدة\n"
        "/help — هذه المساعدة\n\n"
        "اكتب أي سؤال أو اقتراح عن البوت وسأشرح لك حالته وأقترح التحسينات.\n"
        "وستصلك تنبيهات الردود الضعيفة تلقائياً."
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report = status_report()
    await update.message.reply_text(report)


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    report = day_report()
    await update.message.reply_text(report)


async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_rules()
    if not rows:
        await update.message.reply_text("لا توجد قواعد بعد. أضف أولاً عبر /rule <النص>")
        return
    lines = [f"{r['id']}. {r['rule']}  (بواسطة {r['created_by']})" for r in rows]
    await update.message.reply_text("📌 القواعد الحالية (تُحقن في برومبت بوت فذ):\n\n" + "\n".join(lines))


async def cmd_add_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    rule = text[len("/rule"):].strip()
    if not rule:
        await update.message.reply_text("اكتب القاعدة بعد /rule، مثال:\n/rule لا تعرض أرضاً مع شقق أبداً")
        return
    save_rule(rule, update.effective_user.first_name or "مشرف")
    await update.message.reply_text(f"✅ أُضيفت القاعدة:\n\"{rule}\"\nستُطبق على ردود بوت فذ لجميع الزبائن من الآن.")


async def cmd_del_rule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    try:
        rid = int(text[len("/delrule"):].strip())
    except Exception:
        await update.message.reply_text("اكتب رقم القاعدة بعد /delrule، مثال:\n/delrule 3")
        return
    delete_rule(rid)
    await update.message.reply_text(f"🗑️ حُذفت القاعدة رقم {rid}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مساعدتك يا مشرف 👌\n\n"
        "/status — حالة البوت اليوم\n"
        "/report — تقرير نهاية اليوم\n"
        "/rule <نص> — أضف قاعدة تحسين تلقائية للبوت\n"
        "/rules — عرض القواعد\n"
        "/delrule <رقم> — حذف قاعدة\n"
        "/help — المساعدة\n\n"
        "اكتب أي سؤال عن أداء البوت وسأحلله وأقترح تحسينات، أو رُد بقاعدة تبدأ بحرف الأمر لتطبَّق مباشرة."
    )


async def echo_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أي رسالة نصية:
    - إن بدأت بفعل أمري (لا/التزم/اعرض/اجعل/تأكد...) تُسجَّل قاعدة تحسين.
    - وإلا تُعالج كمحادثة ذكية عن أداء البوت (تشاور الجهة المشرفة).
    """
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return

    import re as _re
    is_rule = bool(_re.search(r"^(لا|التزم|اعرض|اجعل|تأكد|تجنب|منع|اطلب|حذر|بألا|دائماً|أبداً|إياك|تنبيه|ملاحظة|اقتراح|مشكلة|عيب)", text))
    if is_rule or text.count(" ") <= 12:
        save_rule(text, update.effective_user.first_name or "مشرف")
        await update.message.reply_text(
            f"📥 سُجّلت كقاعدة تحسين: \"{text}\"\nطُبقت على برومبت بوت فذ لجميع الزبائن من الآن."
        )
        return

    # محادثة ذكية: نحلل وضع البوت بذكاء اصطناعي
    ctx = status_report()
    system = (
        "أنت مشرف وباحث جودة لشركة عقارية سعودية (بوت فذ العقارية). "
        "المشرف الرئيسي يحادثك عن وضع البوت ويريد فهمه وتحسينه.\n"
        "أجب بالعربية، بتحليل واضح عملي، وبناءً على الوضع الفعلي:\n\n"
        f"{ctx}\n\n"
        "إذا رأيت تحسيناً قابلاً للتطبيق فاقترحه بصيغة قاعدة جاهزة تبدأ بكلمة الأمر (مثل: لا تعرض ...)، "
        "وإذا كان كلام المشرف مجرد سؤال عن الوضع فشرحه مباشرة."
    )
    reply = ask_ai(system, text, max_tokens=600)
    if not reply:
        await update.message.reply_text("ما أقدر أحلل لحظياً (مشكلة تقنية). جرب بعد شوي أو استخدم /status.")
        return
    await update.message.reply_text(clean_md_am(reply))


def clean_md_am(text):
    if not text:
        return text
    return text.replace("**", "").replace("__", "")


# ──────────────────────────────────────────────
#  التشغيل
# ──────────────────────────────────────────────
def run_admin():
    if not ADMIN_TOKEN:
        logger.error("ADMIN_BOT_TOKEN غير موجود في .env — أضفه ثم أعد التشغيل")
        return
    app = Application.builder().token(ADMIN_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("rule", cmd_add_rule))
    app.add_handler(CommandHandler("delrule", cmd_del_rule))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo_rules))
    logger.info("بوت المشرف شغال...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--daily":
        # تشغيل لتقرير نهاية اليوم فقط (cron)
        send_daily_report()
    else:
        run_admin()