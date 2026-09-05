"""
فذ العقارية — Telegram Bot via Webhook
يستقبل التحديثات من تلغرام مباشرة عبر Webhook (ما يحتاج Polling مناسب للسيرفر)
"""
import os
import sys
import logging
import json
import requests
from flask import Flask, request, jsonify

from dotenv import load_dotenv
from database import get_connection
from ai_client import ask_ai as ask_ai_ext

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "")  # e.g. https://fadh.example.com
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8443"))

app = Flask(__name__)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ──────────────────────────────────────────────
#  System Prompt
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """أنت مساعد عقاري ذكي لشركة "فذ العقارية" في جدة، السعودية.
مهمتك مساعدة العملاء في العثور على العقارات المناسبة.

قواعد:
- رد بالعربي السعودي بشكل ودود ومحترف
- إذا وجدت عروض في القاعدة، اعرضها على العميل بشكل منظم
- لا تختلق معلومات
- لا تذكر أي معلومات حساسة
- خلّ ردودك قصيرة ومباشرة"""


# ──────────────────────────────────────────────
#  Database helpers
# ──────────────────────────────────────────────
def search_listings(query):
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM listings WHERE offer_status='نشط'
               AND (
                   property_type LIKE ? OR city LIKE ? OR district LIKE ?
                   OR raw_text LIKE ? OR description LIKE ?
               )
               ORDER BY id DESC LIMIT 10""",
            (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]
        rows = conn.execute(
            "SELECT * FROM listings WHERE offer_status='نشط' ORDER BY id DESC LIMIT 10"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_conversation(user_id, user_name, message, bot_reply):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO conversations (user_id, user_name, message, bot_reply) VALUES (?, ?, ?, ?)",
            (str(user_id), user_name, message, bot_reply),
        )
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────
#  AI (Gemini أساسي — OpenRouter احتياطي)
# ──────────────────────────────────────────────
def ask_ai(user_message, listings_context=""):
    context_block = ""
    if listings_context:
        context_block = f"\n\nالعروض المتاحة:\n{listings_context}"
    return ask_ai_ext(SYSTEM_PROMPT + context_block, user_message)


# ──────────────────────────────────────────────
#  Telegram helpers
# ──────────────────────────────────────────────
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"sendMessage error: {e}")


def set_webhook():
    url = f"{TELEGRAM_API}/setWebhook"
    webhook_url = f"{WEBHOOK_HOST}/webhook"
    resp = requests.post(url, json={"url": webhook_url, "max_connections": 40})
    data = resp.json()
    logger.info(f"Webhook set: {data}")
    return data


def delete_webhook():
    url = f"{TELEGRAM_API}/deleteWebhook"
    resp = requests.post(url)
    logger.info(f"Webhook deleted: {resp.json()}")


# ──────────────────────────────────────────────
#  Handlers
# ──────────────────────────────────────────────
def handle_message(chat_id, user_id, user_name, text):
    if text.startswith("/start"):
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🏠 عروض متاحة", "callback_data": "browse"},
                    {"text": "📞 تواصل مع مكتب", "callback_data": "contact"},
                ],
                [{"text": "❓ سؤال سريع", "callback_data": "ask"}],
            ]
        }
        send_message(
            chat_id,
            f"أهلين {user_name}! 👋\nأنا مساعد فذ العقارية العقاري.\n\nتقدر تتصفح العروض أو تسأل عن أي شي يخص العقارات في جدة.",
            reply_markup=keyboard,
        )
        return

    listings = search_listings(text)
    listings_text = ""
    if listings:
        for i, l in enumerate(listings[:5], 1):
            parts = []
            for key, label in [("property_type", "النوع"), ("district", "الموقع"),
                               ("city", "المدينة"), ("price", "السعر"), ("rooms", "الغرف")]:
                val = l.get(key)
                if val:
                    if key == "price":
                        parts.append(f"{label}: {val:,.0f} ريال")
                    elif key == "rooms":
                        parts.append(f"{label}: {val}")
                    else:
                        parts.append(f"{label}: {val}")
            raw = (l.get("raw_text") or l.get("description") or "")[:300]
            if raw:
                parts.append(f"تفاصيل الإعلان: {raw}")
            listings_text += f"\n\n--- عرض {i} ---\n" + "\n".join(parts) if parts else ""

    ai_reply = ask_ai(text, listings_text)

    if ai_reply:
        bot_reply = ai_reply
    elif listings_text:
        bot_reply = f"لقيت بعض العروض قد تهمك:\n{listings_text}\n\nتبي تعرف تفاصيل أكثر عن أي عرض؟"
    else:
        bot_reply = "أهلين! 👋 أنا مساعد فذ العقارية العقاري. كيف أقدر أساعدك؟\n\nمثلاً تقدر تقولي:\n- \"أبي شقة في حي الروضة\"\n- \"فيلا 4 غرف بسعر أقل من مليون\"\n- \"أرض للإيجار في جدة\""

    log_conversation(user_id, user_name, text, bot_reply)
    send_message(chat_id, bot_reply)


def handle_callback(chat_id, user_id, user_name, data):
    if data == "browse":
        listings = search_listings("")
        if listings:
            text = "🏠 أحدث العروض المتاحة:\n\n"
            for i, l in enumerate(listings[:5], 1):
                parts = []
                if l.get("property_type"):
                    parts.append(l["property_type"])
                if l.get("district"):
                    parts.append(l["district"])
                if l.get("price"):
                    parts.append(f"{l['price']:,.0f} ريال")
                if l.get("rooms"):
                    parts.append(f"{l['rooms']} غرف")
                text += f"{i}. {' - '.join(parts)}\n"
            text += "\nتبي تفاصيل أكثر؟ اكتب اسم الحي أو نوع العقار."
        else:
            text = "لا توجد عروض متاحة حالياً. نعمل على إضافة عروض جديدة قريباً! 🏗️"
        send_message(chat_id, text)

    elif data == "contact":
        send_message(
            chat_id,
            "📞 للتواصل مع مكتب عقاري في جدة:\n\n"
            "يمكنك ترك اسمك ورقم جوالك وسنوصلك بأقرب مكتب عقاري مناسب.\n\n"
            "أو اكتب اسم الحي اللي تبيه ونساعدك تلاقي المكتب المناسب."
        )

    elif data == "ask":
        send_message(
            chat_id,
            "اكتب سؤالك العقاري وبنجاوبك مباشرة! 💬\n\n"
            "مثال: \"أبي شقة في حي الروضة بسعر أقل من 3000﷼ شهرياً\""
        )


# ──────────────────────────────────────────────
#  Flask routes
# ──────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        if not update:
            return jsonify({"ok": False, "error": "empty body"}), 400

        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]
            user_name = msg["from"].get("first_name") or msg["from"].get("username", str(user_id))
            text = msg.get("text", "")

            if not text:
                return jsonify({"ok": True})

            handle_message(chat_id, user_id, user_name, text)

        elif "callback_query" in update:
            cq = update["callback_query"]
            chat_id = cq["message"]["chat"]["id"]
            user_id = cq["from"]["id"]
            user_name = cq["from"].get("first_name") or cq["from"].get("username", str(user_id))
            data = cq.get("data", "")

            # Answer callback query
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": cq["id"]},
                timeout=5,
            )

            handle_callback(chat_id, user_id, user_name, data)

        return jsonify({"ok": True})

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "fadh-akariya-bot"})


# ──────────────────────────────────────────────
#  CLI
# ──────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="فذ العقارية — Bot Webhook")
    parser.add_argument("--setup-webhook", action="store_true", help="Set webhook URL and exit")
    parser.add_argument("--delete-webhook", action="store_true", help="Delete webhook and exit")
    parser.add_argument("--port", type=int, default=8443, help="Port to listen on")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    if args.setup_webhook:
        if not WEBHOOK_HOST:
            print("ERROR: WEBHOOK_HOST env var is required for webhook setup")
            print("Usage: WEBHOOK_HOST=https://your-domain.com python webhook_bot.py --setup-webhook")
            sys.exit(1)
        result = set_webhook()
        print(f"Webhook status: {result}")
        sys.exit(0)

    if args.delete_webhook:
        result = delete_webhook()
        print(f"Webhook deleted: {result}")
        sys.exit(0)

    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN not set in .env")
        sys.exit(1)

    print(f"Starting webhook server on {args.host}:{args.port}")
    print(f"Set webhook with: WEBHOOK_HOST=https://your.domain python webhook_bot.py --setup-webhook")
    app.run(host=args.host, port=args.port)
