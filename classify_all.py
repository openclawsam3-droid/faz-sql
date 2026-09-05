"""
التحليل العميق — يقرأ رسائل الخام الصالحة (is_spam=0) غير المحللة
ويرسلها لـ DeepSeek لاستخراج كل التفاصيل، ثم يحفظ في قاعدة المفروز.
يعمل كل ساعة، يحلل فقط الجديد (لا يعيد تحليل المحلّل).
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from database import get_raw_conn, get_sorted_conn, init_db
from spam_filter import is_request


def _norm_dt(dt):
    """يحوّل تاريخ تليجرام '2026-03-16T11:04:48+00:00' إلى 'YYYY-MM-DD HH:MM'"""
    if not dt:
        return None
    s = dt.replace("T", " ").replace("+00:00", "").replace("Z", "")
    return s[:16]

# برومت التحليل العميق — يغطي كل أعمدة المفروز
ANALYZE_PROMPT = """أنت عقاري سعودي خبير ومحلل بيانات. حلل إعلان العقار التالي واستخرج كل التفاصيل بصيغة JSON فقط (بدون أي كلام إضافي):

{
  "listing_type": "عرض" أو "طلب",
  "property_type": "شقة|فيلا|أرض|عمارة|محل|مكتب|مخزن|استراحة|غير محدد",
  "deal_type": "إيجار" أو "شراء" أو null,
  "city": "اسم المدينة",
  "district": "اسم الحي أو null",
  "rooms": عدد الغرف أو null,
  "bathrooms": عدد الحمامات أو null,
  "kitchen": "راكب|غير مفروش|لا يوجد|null",
  "rooftop": "سطح متاح|لا يوجد|null",
  "annex": "ملحق|لا|null",
  "driver_room": 1 أو 0,
  "maid_room": 1 أو 0,
  "finishing": "فاخر|عادي|جديد|null",
  "price": السعر رقم صحيح أو null,
  "price_unit": "إجمالي|سنوي|شهري|مترمربع|سهم|null",
  "features": ["ميزة","ميزة"],
  "short_desc": "وصف مختصر بجملة أو جملتين يحافظ حرفياً على نوع العقار (مثل 'نص أرض'، 'أرض تجارية'، 'دوبلكس'، 'عمارة') ولا يحذفه",
  "owner_contact": "جوال المالك فقط بصيغة 05XXXXXXXX أو +966XXXXXXXXX وإلا null",
  "message_timestamp": "توقيت النص بصيغة YYYY-MM-DD HH:MM (انظر القاعدة) أو null",
  "confidence": رقم بين 0 و1,
  "needs_review": true أو false,
  "price_assumed_unit": "افتراضي-ألف" أو null
}

قواعد صارمة:
- listing_type: "عرض" = صاحب العقار يعرضه (للبيع/للإيجار). "طلب" = يبحث عن عقار.
- if نص تسويقي/قروبي بلا عقار → ارجع {"listing_type":"سبام"} فقط.
- السعر: إن ذكر "سعر المتر" و"الإجمالي" → خذ الإجمالي، ولو أخذت سعر المتر ضع price_unit="مترمربع".
- **قاعدة الآلاف (الأخطر):** في السوق السعودي، إن ورد رقم من 2 إلى 4 خانات (مثل 420، 550، 1200، 350) في سياق سعر عقار وبدون كلمة "ألف" أو "مليون"، فافترض أنه بالآلاف واضربه في 1000 (420→420000، 550→550000، 1200→1200000، 350→350000). في هذه الحالة اضبط price_assumed_unit="افتراضي-ألف"، و confidence=0.75، و needs_review=true، وأضف في short_desc ملاحظة "تم افتراض السعر بالألف".
- **تاريخ الرسالة:** ابحث عن توقيت بين قوسين مربعين في بداية النص مثل [09/02/48 09:05 ص]. استخرجه بصيغة YYYY-MM-DD HH:MM مع اعتبار السنة هجرية: اجعل السنة "14"+آخر رقمين (48→1448)، وحوّل ص=صباحاً (كما هي)، م=مساءً (أضف 12 للساعة إن كانت أقل من 12). مثال [09/02/48 09:05 ص] → "1448-02-09 09:05". واضبط needs_review=true (للتحويل الميلادي لاحقاً، لا تحوّله تلقائياً).
- **الحفاظ على وصف النوع:** في short_desc احتفظ حرفياً بكلمات تحدد نوع العقار/الأرض ('نص أرض'، 'أرض تجارية'، 'دوبلكس'، 'عمارة'، 'دور') ولا تستبدلها بلفظ عام ('أرض' فقط).
- حوّل الأرقام العربية/الهندية (٠١٢٣٤٥٦٧٨٩) لأرقام إنجليزية.
- confidence: 0.9-1.0 إن كانت البيانات واضحة ومكتملة، 0.5-0.75 إن وُجد غموض (سعر مختصر/حي غير واضح)، وأقل من 0.5 إن النص لا يحوي عقاراً واضحاً.
- needs_review: true إذا وُجد أي شك (تحويل التاريخ، السعر المختصر، حي غير معروف، تردد عرض/طلب).
- رد بالـ JSON فقط."""


def classify_listing(raw_text):
    """استدعاء DeepSeek للإعلان الواحد — يعيد dict أو None"""
    try:
        from deepseek_client import _chat
        result = _chat(
            [
                {"role": "system", "content": ANALYZE_PROMPT},
                {"role": "user", "content": raw_text[:3000]},
            ],
            max_tokens=800,
            temperature=0.1,
        )
        if not result:
            return None
        if "```" in result:
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        data = json.loads(result.strip())
        if isinstance(data, list):
            data = data[0] if data else {}
        return data
    except Exception as e:
        print(f"DeepSeek error: {e}", flush=True)
        return None


def analyze_batch(limit=50, sleep_sec=1.0):
    """تحليل الجديد غير المحلَّل في الخام (is_spam=0 وغير مكرر في المفروز)"""
    init_db()
    raw = get_raw_conn()
    sorted_db = get_sorted_conn()

    # الصفوف الصالحة غير المحللة — raw_ids المحللة من قاعدة المفروز (قاعدة منفصلة)
    done_ids = [r[0] for r in get_sorted_conn().execute(
        "SELECT raw_id FROM sorted_listings WHERE raw_id IS NOT NULL"
    ).fetchall()]
    done_set = set(done_ids)
    # نجلب الأحدث غير المحلّل أولاً حتى لا تتراكم الرسائل الجديدة خارج حد LIMIT
    rows = raw.execute(
        """SELECT id, message_id, channel, raw_text, datetime
           FROM raw_messages
           WHERE is_spam=0
           ORDER BY id DESC
           LIMIT ?""",
        (limit * 3,),
    ).fetchall()
    rows = [r for r in rows if r["id"] not in done_set][:limit]
    raw.close()

    print(f"رسائل للتحليل: {len(rows)}", flush=True)

    done = 0
    errors = 0
    for r in rows:
        text = r["raw_text"]
        ai = classify_listing(text)
        if not ai:
            errors += 1
            print(f"  msg {r['message_id']}: فشل التحليل", flush=True)
            continue

        # سبام مكتشف متأخراً → علّمه في الخام ولا تدخله للمفروز
        if ai.get("listing_type") == "سبام":
            c = get_raw_conn()
            c.execute("UPDATE raw_messages SET is_spam=1, spam_reason='اكتشفه AI' WHERE id=?", (r["id"],))
            c.commit()
            c.close()
            print(f"  msg {r['message_id']}: سبام", flush=True)
            continue

        req = (ai.get("listing_type") or "").lower() == "طلب"
        if req or is_request(text):
            ai["listing_type"] = "طلب"

        features = ai.get("features") or []
        if isinstance(features, list):
            features = "، ".join(str(f) for f in features)

        c = get_sorted_conn()
        try:
            c.execute(
                """INSERT OR REPLACE INTO sorted_listings
                (raw_id, message_id, channel, listing_type, property_type, deal_type,
                 city, district, rooms, bathrooms, kitchen, rooftop, annex,
                 driver_room, maid_room, finishing, price, price_unit,
                 features, short_desc, owner_contact, posted_date,
                 confidence, needs_review, price_assumed_unit)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    r["id"], r["message_id"], r["channel"],
                    ai.get("listing_type"), ai.get("property_type") or "غير محدد",
                    ai.get("deal_type"), ai.get("city"), ai.get("district"),
                    ai.get("rooms"), ai.get("bathrooms"), ai.get("kitchen"),
                    ai.get("rooftop"), ai.get("annex"),
                    1 if ai.get("driver_room") else 0,
                    1 if ai.get("maid_room") else 0,
                    ai.get("finishing"), ai.get("price"), ai.get("price_unit"),
                    features, ai.get("short_desc"), ai.get("owner_contact"),
                    _norm_dt(r["datetime"]),
                    ai.get("confidence", 0.7),
                    1 if ai.get("needs_review") else 0,
                    ai.get("price_assumed_unit"),
                ),
            )
            c.commit()
        except Exception as e:
            print(f"  msg {r['message_id']}: حفظ فشل {e}", flush=True)
        finally:
            c.close()

        done += 1
        print(f"  ✓ msg {r['message_id']}: {ai.get('listing_type')} | {ai.get('property_type')} | {ai.get('city')} | {ai.get('district')}", flush=True)
        time.sleep(sleep_sec)

    # ملخص
    sc = get_sorted_conn()
    total = sc.execute("SELECT COUNT(*) FROM sorted_listings").fetchone()[0]
    offers = sc.execute("SELECT COUNT(*) FROM sorted_listings WHERE listing_type='عرض'").fetchone()[0]
    requests = sc.execute("SELECT COUNT(*) FROM sorted_listings WHERE listing_type='طلب'").fetchone()[0]
    sc.close()
    print(f"\nالمفروز: إجمالي {total} | عروض {offers} | طلبات {requests}", flush=True)


if __name__ == "__main__":
    analyze_batch(limit=int(sys.argv[1]) if len(sys.argv) > 1 else 50)
