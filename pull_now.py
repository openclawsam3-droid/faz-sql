"""
السحب الهادئ التزايدي — يقرأ قناة واحدة تزايدياً (أحدث ما بعد آخر رسالة)
ويحفظ في قاعدة الخام مع فلتر السبام. يعمل كل ساعة بلا ضغط على السيرفر.
"""
import asyncio
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from telegram_agent import pull_messages
from database import get_raw_conn, init_db
from spam_filter import is_spam


def pull():
    init_db()
    conn = get_raw_conn()

    channels = conn.execute("SELECT url, last_message_id FROM channels").fetchall()
    if not channels:
        print("لا توجد قنوات — أضف قناة في جدول channels", flush=True)
        return

    for ch in channels:
        url = ch["url"]
        last_id = ch["last_message_id"] or 0

        result = asyncio.run(pull_messages(url, months_back=5, min_id=last_id))
        if "error" in result:
            print(f"خطأ سحب من {url}: {result['error']}", flush=True)
            continue

        count = result["count"]
        print(f"سحب {url}: {count} رسالة جديدة (منذ id={last_id})", flush=True)
        if count == 0:
            continue

        new_saved = 0
        new_spam = 0
        max_id = last_id
        last_date = None

        for msg in result["messages"]:
            spam, reason = is_spam(msg["text"])
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO raw_messages
                    (message_id, channel, raw_text, datetime, is_spam, spam_reason)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (msg["id"], url, msg["text"], msg["date"],
                     1 if spam else 0, reason if spam else None),
                )
                if cur.rowcount > 0:
                    if spam:
                        new_spam += 1
                    else:
                        new_saved += 1
            except Exception as e:
                print(f"حفظ msg {msg['id']} فشل: {e}", flush=True)

            max_id = max(max_id, msg["id"])
            last_date = msg["date"]

        conn.execute(
            "UPDATE channels SET last_message_id=?, last_pull=? WHERE url=?",
            (max_id, last_date, url),
        )
        conn.commit()
        print(f"  → جديد صالح: {new_saved} | سبام مرفوض: {new_spam}", flush=True)

    # ملخص
    total = conn.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0]
    clean = conn.execute("SELECT COUNT(*) FROM raw_messages WHERE is_spam=0").fetchone()[0]
    conn.close()
    print(f"إجمالي الخام: {total} | صالح: {clean}", flush=True)


if __name__ == "__main__":
    pull()
