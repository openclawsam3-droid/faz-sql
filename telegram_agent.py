"""
وكيل الاستقبال — سحب من قنوات تلغرام عبر Telethon.
درس الجلسة: سحب تزايدي بـmin_id بدل إعادة سحب كل شيء كل مرة.
ملاحظة حرجة: البوتات (BOT_TOKEN) ممنوعة من GetHistoryRequest (قراءة سجل القناة).
لذلك نفضّل جلسة الهاتف (PHONE_NUMBER) إن وجدت، والبوت كخيار أخير فقط.
"""
import os
import re
from datetime import datetime, timedelta
from telethon import TelegramClient
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PHONE_NUMBER = os.getenv("PHONE_NUMBER", "")
SESSION_NAME = os.path.join(os.path.dirname(__file__), "fadh_session_phone")

# اتصال واحد دائم يُشارك بين السحب والمراقب اللحظي (يمنع تعارض جلسة التليفون بين سكربتين)
_client = None


def get_client():
    """العميل الدائم المشترك (تُبنى مرة واحدة فقط). الاتصال الفعلي يجريه المتصل."""
    global _client
    if _client is None:
        _client = TelegramClient(
            SESSION_NAME,
            API_ID,
            API_HASH,
            auto_reconnect=True,
            connection_retries=6,
            retry_delay=1,
            device_model="fadh-live",
        )
    return _client


async def close_client():
    global _client
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None


class SessionNeedsLogin(Exception):
    """الجلسة غير صالحة وتحتاج رمز تسجيل دخول جديد — لا نرسل رمزاً تلقائياً."""


async def ensure_authorized():
    """يربط العميل الدائم ويتحقق من الجلسة. لا يرسل رمز SMS أبداً.
    يرفع SessionNeedsLogin إذا انتهت الجلسة. يعيد العميل عند النجاح."""
    client = get_client()
    if not API_ID or not API_HASH:
        raise RuntimeError("يحتاج TELEGRAM_API_ID و TELEGRAM_API_HASH")
    await client.connect()
    if not await client.is_user_authorized():
        raise SessionNeedsLogin("الجلسة انتهت — تحتاج رمز تسجيل دخول جديد")
    return client


async def pull_messages(channel_url, months_back=5, min_id=0):
    """سحب رسائل قناة. لو min_id>0: سحب تزايدي (الأحدث فقط). غير ذلك: أول سحب بالمدى الزمني."""
    if not API_ID or not API_HASH:
        return {"error": "يحتاج TELEGRAM_API_ID و TELEGRAM_API_HASH في .env"}

    client = await ensure_authorized()

    try:
        entity = await client.get_entity(channel_url)
        messages = []

        if min_id and min_id > 0:
            async for message in client.iter_messages(entity, min_id=min_id, reverse=True):
                if message.text:
                    messages.append({
                        "id": message.id,
                        "text": message.text,
                        "date": message.date.isoformat(),
                    })
        else:
            limit_date = datetime.now() - timedelta(days=int(months_back) * 30)
            async for message in client.iter_messages(entity, offset_date=limit_date, reverse=True):
                if message.text:
                    messages.append({
                        "id": message.id,
                        "text": message.text,
                        "date": message.date.isoformat(),
                    })

        return {"success": True, "count": len(messages), "messages": messages}
    except Exception as e:
        return {"error": str(e)}


def extract_links(text):
    if not text:
        return ""
    return "، ".join(re.findall(r'(?:https?://|t\.me/)\S+', text))
