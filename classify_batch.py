"""Fast batch classifier — classify multiple listings per API call"""
import sys, os, json, time, sqlite3, requests
sys.stdout.reconfigure(encoding='utf-8')

sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

from ai_client import ask_ai
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "fadh.db")

BATCH_PROMPT = """أنت عقاري سعودي خبير. حلل مجموعة إعلانات عقارية واستخرج المعلومات من كل واحد.

أرسل لي مصفوفة JSON فقط (بدون أي نص إضافي):
[
  {"id": 1, "property_type": "...", "city": "...", "district": "...", "price": null_or_number, "rooms": null_or_number, "description": "ملخص قصير"},
  ...
]

الأنواع الصحيحة: شقة|فيلا|أرض|عمارة|محل|مكتب|استراحة|عملي|null
القواعد:
- إذا ما تعرف النوع، حط null
- حوّل الأرقام العربية لعادية
- إذا السعر "X للمتر"، حط null
- المدينة: جدة / مكة / الرياض / المدينة / الدمام / null
- لا تكتب أي شي غير الـ JSON"""


def classify_batch(listings):
    """Classify a batch of listings in one API call"""
    if not listings:
        return None

    texts = ""
    for item in listings:
        text = (item["raw_text"] or "")[:400]
        texts += f"\n--- ID: {item['id']} ---\n{text}\n"

    result = ask_ai(BATCH_PROMPT, texts, max_tokens=2000, temperature=0.1)
    if not result:
        return None

    try:
        if "```" in result:
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
            result = result.strip()
        data = json.loads(result)
        if isinstance(data, dict):
            data = list(data.values()) if data else []
        return data
    except json.JSONDecodeError:
        return None


def reclassify_all():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, raw_text FROM listings WHERE property_type IS NULL OR price IS NULL ORDER BY id"
    ).fetchall()
    total = len(rows)
    print(f"Listings to classify: {total}")

    BATCH_SIZE = 8
    updated = 0
    errors = 0

    for batch_start in range(0, total, BATCH_SIZE):
        batch = [dict(r) for r in rows[batch_start:batch_start + BATCH_SIZE]]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        results = classify_batch(batch)

        if not results:
            errors += len(batch)
            print(f"  Batch {batch_num}/{total_batches}: FAILED")
            continue

        valid_types = ["شقة", "فيلا", "أرض", "عمارة", "محل", "مكتب", "استراحة", "عملي"]

        for item in results:
            lid = item.get("id")
            if not lid:
                continue

            ptype = item.get("property_type")
            if ptype not in valid_types:
                ptype = None

            existing = conn.execute(
                "SELECT property_type, city, district, price, rooms FROM listings WHERE id=?",
                (lid,)
            ).fetchone()

            if existing:
                conn.execute("""
                    UPDATE listings SET
                        property_type = COALESCE(?, property_type),
                        city = COALESCE(?, city),
                        district = COALESCE(?, district),
                        price = COALESCE(?, price),
                        rooms = COALESCE(?, rooms),
                        description = COALESCE(?, description)
                    WHERE id = ?
                """, (
                    ptype, item.get("city"), item.get("district"),
                    item.get("price"), item.get("rooms"),
                    item.get("description"), lid
                ))
                updated += 1

        print(f"  Batch {batch_num}/{total_batches}: {len(results)} classified")
        conn.commit()

        time.sleep(1)

    conn.close()
    print(f"\nDone: {updated} updated, {errors} errors out of {total}")


if __name__ == "__main__":
    reclassify_all()
