"""
فذ العقارية — بوت متكامل (سحب لحظي + تصنيف + رد على العملاء) بتشغيل مستمر.

الهندسة (مخصّصة لطبقة Render المجانية التي "تنام" عند غياب زيارات HTTP):
- خيط رئيسي: Flask على $PORT:
    /health      → مراقبة (يُقرع من cron خارجي مجاني كل 10 دقائق ليُبقي الخدمة مستيقظة)
    /cron?key=…  → دورة سحب+تصنيف فورية اختيارية
- خيط ثانٍ: بوت الرد الكامل (نواة reply_bot: RAG + DeepSeek + الذاكرة + الفلاتر)
- خيط ثالث: مراقب لحظي — اتصال دائم بقنوات القاعدة (Telethon):
    أي رسالة جديدة تنزل → تُحفظ في الخام → تُصنَّف فوراً في المفروز.
    عند انتهاء الجلسة → تنبيه للمشرف فوراً بدل الصمت.
- أمر /sync (للمشرف) دورة فورية من التليجرام.

شغّل بالنشر على Render (سحب لحظي بلا جهازك).
"""
import asyncio
import base64
import datetime
import os
import sys
import threading

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request, jsonify
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ── نواة الرد كاملة من reply_bot ──
from reply_bot import (
    BOT_TOKEN,
    button_callback,
    cmd_offers,
    cmd_requests,
    error_handler,
    handle_message,
    logger,
    start,
)
from pull_now import pull
from classify_all import analyze_batch

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "")
CRON_SECRET = os.getenv("CRON_SECRET", "fadh-secret")
PULL_INTERVAL_H = float(os.getenv("PULL_INTERVAL_H", "6"))
PORT = int(os.getenv("PORT", "10000"))

web_app = Flask(__name__)


def _ensure_session():
    """فكّ جلسة التليفون من SESSION_B64 إذا لم تكن موجودة (السحابة بلا ملف محفوظ)."""
    sess_path = os.path.join(os.path.dirname(__file__), "fadh_session_phone.session")
    if os.path.exists(sess_path):
        return
    b64 = os.getenv("SESSION_B64", "").strip()
    if not b64:
        print("[sync] لا جلسة ولا SESSION_B64 — ولن يعمل السحب.", flush=True)
        return
    try:
        with open(sess_path, "wb") as f:
            f.write(base64.b64decode(b64))
        print("[sync] فُكّت جلسة التليفون من SESSION_B64.", flush=True)
    except Exception as e:
        print(f"[sync] فشل فك الجلسة: {e}", flush=True)


def _notify_admin(text):
    """تنبيه فوري للمشرف عبر التليجرام (يُستدعى من أي خيط). لا يقوم عند غياب ADMIN_USER_ID."""
    if not ADMIN_USER_ID:
        print(f"[notify] (لا ADMIN_USER_ID) {text}", flush=True)
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": ADMIN_USER_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"[notify] فشل إرسال التنبيه: {e}", flush=True)


def _run_cycle():
    """دورة كاملة: سحب القناة ← تصنيف الجديد ← ملخص."""
    print("[sync] بدء دورة السحب والتصنيف...", flush=True)
    try:
        pull()
    except Exception as e:
        print(f"[sync] خطأ في السحب: {e}", flush=True)
    try:
        analyze_batch(limit=200, sleep_sec=0.5)
    except Exception as e:
        print(f"[sync] خطأ في التصنيف: {e}", flush=True)
    print("[sync] انتهت الدورة.", flush=True)


def _run_classify():
    """تصنيف الجديد فقط (بلا سحب — يستخدمه المجدول الداخلي؛ المراقب اللحظي هو من يسحب)."""
    print("[sync] تصنيف عالق/جديد...", flush=True)
    try:
        analyze_batch(limit=200, sleep_sec=0.5)
    except Exception as e:
        print(f"[sync] خطأ في التصنيف: {e}", flush=True)


def _spawn_thread(fn):
    threading.Thread(target=fn, daemon=True).start()


# ── نقط HTTP (Flask في الخيط الرئيسي) ──
@web_app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "fadh-akariya-bot",
                    "time": datetime.datetime.now().isoformat()})


@web_app.route("/cron", methods=["GET", "POST"])
def cron_trigger():
    """مطلق خارجي/يدوي لدورة كاملة — محمي بمفتاح CRON_SECRET."""
    key = request.args.get("key", "")
    if key != CRON_SECRET:
        return jsonify({"ok": False, "error": "bad key"}), 403
    _spawn_thread(_run_cycle)
    return jsonify({"ok": True, "started": True})


# ── جدولة دورية (تصنيف العالق فقط؛ السحب يقع لحظياً من المراقب) ──
async def _cycle_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        await asyncio.to_thread(_run_classify)
    except Exception as e:
        print(f"[sync] فشل الدورة المجدولة: {e}", flush=True)


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """سحب/تصنيف فوري من التليجرام — للمشرف فقط."""
    uid = str(update.effective_user.id)
    if ADMIN_USER_ID and uid != ADMIN_USER_ID:
        await update.message.reply_text("هذا الأمر للمشرف فقط.")
        return
    await update.message.reply_text("🔄 جاري سحب القناة وتصنيف الجديد... سأخبرك بالنتيجة")
    try:
        await asyncio.to_thread(_run_cycle)
        await update.message.reply_text("✅ تمت الدورة — راجع العروض في القاعدة.")
    except Exception as e:
        await update.message.reply_text(f"⚠️ فشلت الدورة: {str(e)[:200]}")


# ── خيط بوت الرد (Polling + مجدول داخلي) ──
def _run_bot_thread():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN غير موجود!")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("offers", cmd_offers))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    if app.job_queue:
        app.job_queue.run_repeating(
            _cycle_job,
            interval=PULL_INTERVAL_H * 3600,
            first=max(600, int(PULL_INTERVAL_H * 3600 * 0.5)),
        )
        logger.info(f"مجدد التصنيف يعمل كل {PULL_INTERVAL_H:g} ساعات.")

    app.run_polling(allowed_updates=Update.ALL_TYPES,
                    ready_callback=lambda _: logger.info("البوت جاهز (polling)."))


# ── خيط المراقب اللحظي ──
async def _handle_new_message(event, allowed_urls):
    from spam_filter import is_spam
    from database import get_raw_conn

    msg = event.message
    if not msg or not msg.text:
        return
    try:
        ch_url = (event.chat.username or "") or ""
        allowed = {u.strip() for u in allowed_urls}
        if not any(n in ch_url for n in allowed) and allowed:
            return
        ch_url = "https://t.me/" + ch_url
    except Exception:
        return

    spam, reason = is_spam(msg.text)
    try:
        conn = get_raw_conn()
        cur = conn.execute(
            """INSERT OR IGNORE INTO raw_messages
            (message_id, channel, raw_text, datetime, is_spam, spam_reason)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (msg.id, ch_url, msg.text, msg.date.isoformat(),
             1 if spam else 0, reason if spam else None),
        )
        inserted = cur.rowcount > 0
        if inserted:
            conn.execute(
                "UPDATE channels SET last_message_id=?, last_pull=? WHERE url=?",
                (msg.id, msg.date.isoformat(), ch_url),
            )
        conn.commit()
        conn.close()
        if inserted and not spam:
            print(f"[watcher] رسالة جديدة {msg.id} — تصنيف فوري…", flush=True)
            await asyncio.to_thread(analyze_batch, 5, 0.5)
    except Exception as e:
        print(f"[watcher] خطأ حفظ/تصنيف {msg.id}: {e}", flush=True)


async def _watcher_main():
    from telethon import events
    from database import get_raw_conn

    while True:
        try:
            from telegram_agent import ensure_authorized
            client = await ensure_authorized()

            urls = [r["url"] for r in get_raw_conn().execute("SELECT url FROM channels").fetchall()]
            logger.info(f"المراقب اللحظي جاهز — قنوات: {urls}")

            async def handler(event):
                await _handle_new_message(event, urls)

            client.add_event_handler(handler, events.NewMessage())
            await client.run_until_disconnected()
        except Exception as e:
            from telegram_agent import SessionNeedsLogin
            if isinstance(e, SessionNeedsLogin):
                _notify_admin(
                    "⚠️ جلسة السحب انتهت — تحتاج رمز تسجيل دخول جديد.\n"
                    "ادخل للخادم وأعد تعيين SESSION_B64 جلسةً جديدة، أو أرسل رمز التأكيد."
                )
                print("[watcher] الجلسة انتهت — أُخطر المشرف.", flush=True)
            else:
                print(f"[watcher] خطأ: {e} — إعادة محاولة بعد 60ث", flush=True)
            await asyncio.sleep(60)


def _run_watcher_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_watcher_main())


def run_server():
    _ensure_session()
    _spawn_thread(_run_bot_thread)
    _spawn_thread(_run_watcher_thread)
    logger.info(f"خادم HTTP يعمل على المنفذ {PORT}… (بوت + مراقب لحظي)")
    web_app.run(host="0.0.0.0", port=PORT, threaded=True)


if __name__ == "__main__":
    run_server()