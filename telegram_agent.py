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


async def pull_messages(channel_url, months_back=5, min_id=0):
    """سحب رسائل قناة. لو min_id>0: سحب تزايدي (الأحدث فقط). غير ذلك: أول سحب بالمدى الزمني."""
    if not API_ID or not API_HASH:
        return {"error": "يحتاج TELEGRAM_API_ID و TELEGRAM_API_HASH في .env"}

    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    # أولوية الجلسة: هاتف > بوت. البوت ممنوع من قراءة سجل القناة.
    if PHONE_NUMBER:
        await client.start(phone=PHONE_NUMBER)
    elif BOT_TOKEN:
        await client.start(bot_token=BOT_TOKEN)
    else:
        await client.start()

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
    finally:
        await client.disconnect()


def extract_links(text):
    if not text:
        return ""
    return "، ".join(re.findall(r'(?:https?://|t\.me/)\S+', text))
