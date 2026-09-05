"""
المزامنة التلقائية — تعكس قاعدة المفروز إلى Google Sheets عبر Webhook.
يعمل كل ساعة بعد التحليل. يرتبط الجدول بعدها بـ Google Notebook للتحليل.
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from database import get_sorted_conn

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

WEBHOOK_URL = os.getenv("SHEETS_WEBHOOK_URL", "")

HEADERS = [
    "رقم", "نوع العرض", "نوع العقار", "التعامل", "المدينة", "الحي",
    "الغرف", "الحمامات", "المطبخ", "السطح", "الملحق",
    "غرفة سائق", "غرفة خادمة", "التشطيب",
    "السعر", "وحدة السعر", "المميزات", "الوصف المختصر", "تاريخ المنشور",
]


def sync_to_sheets():
    if not WEBHOOK_URL:
        print("✗ SHEETS_WEBHOOK_URL غير موجود — أضفه في .env", flush=True)
        return

    conn = get_sorted_conn()
    rows = conn.execute(
        """SELECT id, listing_type, property_type, deal_type, city, district,
                  rooms, bathrooms, kitchen, rooftop, annex,
                  driver_room, maid_room, finishing,
                  price, price_unit, features, short_desc, posted_date
           FROM sorted_listings
           WHERE status='نشط'
           ORDER BY id"""
    ).fetchall()
    conn.close()

    values = []
    for r in rows:
        values.append([
            r["id"],
            r["listing_type"] or "",
            r["property_type"] or "",
            r["deal_type"] or "",
            r["city"] or "",
            r["district"] or "",
            r["rooms"] or "",
            r["bathrooms"] or "",
            r["kitchen"] or "",
            r["rooftop"] or "",
            r["annex"] or "",
            r["driver_room"] or "",
            r["maid_room"] or "",
            r["finishing"] or "",
            r["price"] or "",
            r["price_unit"] or "",
            r["features"] or "",
            r["short_desc"] or "",
            r["posted_date"] or "",
        ])

    payload = {"headers": HEADERS}
    if values:
        payload["rows"] = values
    data_b = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(WEBHOOK_URL, data=data_b,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read().decode("utf-8", "replace")
            print(f"✓ تمت المزامنة: {len(rows)} صف (HTTP {resp.status}) {body[:80]}", flush=True)
    except Exception as e:
        print(f"✗ مزامنة الشيت فشلت: {e}", flush=True)


if __name__ == "__main__":
    sync_to_sheets()