"""
بوت الرد على العملاء — فذ العقارية (النظام الجديد)
- RAG: محرك استرجاع فوري من قاعدة المفروز (sorted.db)
- DeepSeek للردود الذكية على السياق المسترجع
- أزرار سريعة: عروض متاحة / تقصّد حي / تواصل
"""
import os
import sys
import logging
import asyncio

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

from database import get_sorted_conn, migrate

# ترقية بنية القواعد للإصدار الجديد (آمنة عند كل تشغيل)
migrate()

from deepseek_client import ask_ai
from rag import rag_search, rag_stats, rag_all
from websearch import web_search

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  تنسيق العروض المسترجعة
# ──────────────────────────────────────────────
def _yesno(v):
    v = str(v or "").strip()
    if v in ("1", "true", "True", "متاح", "ملحق", "سطح", "سطح متاح"):
        return "✔ "
    if v in ("0", "false", "False", "لا", "لا يوجد"):
        return "✘ "
    return "؟ "


def format_listing(doc, i):
    parts = []
    pt = doc.get("property_type") or ""
    if pt:
        parts.append(f"🏠 {pt}")
    if doc.get("district"):
        parts.append(f"📍 {doc['district']}")
    if doc.get("city"):
        parts.append(doc["city"])

    head = " | ".join(parts)
    lines = [f"{i}. {head}"]

    price = doc.get("price")
    unit = doc.get("price_unit") or ""
    price_s = ""
    if price is not None:
        try:
            price_s = f"{float(price):,.0f}"
        except Exception:
            price_s = str(price)
        if unit:
            unit_label = {"إجمالي": "إجمالي", "سنوي": "/سنة", "شهري": "/شهر",
                          "مترمربع": "/م²", "سهم": "/سهم"}.get(unit, f"/{unit}")
            price_s += f" ريال {unit_label}"
        lines.append(f"💰 السعر: {price_s}")

    meta = []
    if doc.get("rooms"):
        meta.append(f"🚪 {doc['rooms']} غرف")
    if doc.get("bathrooms"):
        meta.append(f"🚿 {doc['bathrooms']} حمامات")
    if doc.get("kitchen") and doc.get("kitchen") != "لا يوجد":
        meta.append(f"🍳 {doc['kitchen']}")
    if doc.get("finishing"):
        meta.append(f"✨ تشطيب {doc['finishing']}")
    if meta:
        lines.append(" ".join(meta))

    if doc.get("features"):
        try:
            feats = doc["features"]
            if isinstance(feats, str):
                feats = [f.strip() for f in feats.split("،") if f.strip()]
            if feats:
                lines.append("▫️ " + " · ".join(feats[:6]))
        except Exception:
            pass

    if doc.get("short_desc"):
        lines.append(f"📝 {doc['short_desc']}")

    if doc.get("posted_date"):
        lines.append(f"🗓 تاريخ النشر: {doc['posted_date']}")

    return "\n".join(lines)


def format_results(results, limit=5):
    if not results:
        return ""
    out = []
    for i, (doc, _score) in enumerate(results[:limit], 1):
        out.append(format_listing(doc, i))
    return "\n\n".join(out)


# ──────────────────────────────────────────────
#  النص الخام + رقم التواصل
# ──────────────────────────────────────────────
import re

BUSINESS_CONTACT = "0503660663"  # أبو سامي الحربي — يُعرض عند الطلب إذا لم يوجد رقم في النص
BUSINESS_LINE = "هذا العرض من طرف أبو سامي الحربي وفذ العقارية"

# قنوات فذ العقارية الخاصة بسامي — أي عرض من غيرها يُعرض كقناة خارجية بالتعاون
SAMI_CHANNELS = {"https://t.me/arudas4"}

_CHANNEL_NAME_CACHE = {}


def channel_display_name(channel_url):
    """اسم قناة العرض للعرض للزبون؛ يقرأ جدول channels في القاعدة ثم يرجع لاسم الرابط"""
    if not channel_url:
        return "القناة الإعلانية"
    if channel_url in _CHANNEL_NAME_CACHE:
        return _CHANNEL_NAME_CACHE[channel_url]
    name = None
    try:
        from database import get_raw_conn
        conn = get_raw_conn()
        row = conn.execute(
            "SELECT name FROM channels WHERE url=?", (channel_url,)
        ).fetchone()
        conn.close()
        if row and row["name"]:
            name = row["name"]
    except Exception as e:
        print(f"channel name err: {e}", flush=True)
    if not name:
        m = re.search(r"t\.me/([A-Za-z0-9_]+)", channel_url or "")
        name = m.group(1) if m else channel_url
    _CHANNEL_NAME_CACHE[channel_url] = name
    return name


def contact_reply(doc, num):
    """رسالة رقم التواصل حسب مصدر العرض:
    - من قنوات سامي: قل للبائع أن العرض من طرف المسوق سامي الحربي وفذ العقارية
    - من قناة خارجية: رقم صاحب الإعلان + العرض من القناة الفلانية بالتعاون مع فذ العقارية
    """
    phone = _format_phone(num)
    channel = (doc or {}).get("channel", "") if doc else ""
    if channel and channel not in SAMI_CHANNELS:
        ch_name = channel_display_name(channel)
        return (
            "هذا رقم المالك للتواصل معه مباشرة:\n\n"
            f"\u2066{phone}\u2069\n\n"
            f"فضلاً لا أمراً، يُرجى توضيح أنك وصلت عبر (فذ العقارية وقناة {ch_name}) فور تواصلك معه "
            "ليعرف مصدر العرض ولحفظ حقوق المتابعة بيننا.\n\nبالتوفيق إن شاء الله!"
        )
    return CONTACT_MESSAGE.format(phone=phone)

_RAW_CACHE = {}


def get_raw_texts(raw_ids):
    """جلب نصوص خام متعددة بذهاب واحد للقاعدة (بدل اتصال لكل معرّف)."""
    ids = [i for i in (raw_ids or []) if i is not None]
    if not ids:
        return {}
    missing = [i for i in ids if i not in _RAW_CACHE]
    if missing:
        try:
            from database import get_raw_conn
            conn = get_raw_conn()
            rows = conn.execute(
                "SELECT id, raw_text FROM raw_messages WHERE id IN (%s)"
                % ",".join(["?"] * len(missing)),
                tuple(missing),
            ).fetchall()
            conn.close()
            for r in rows:
                _RAW_CACHE[r["id"]] = r["raw_text"] or ""
        except Exception as e:
            print(f"raw batch fetch err: {e}", flush=True)
    return {i: _RAW_CACHE.get(i, "") for i in ids}


def get_raw_text(raw_id):
    """جلب النص الخام للعرض من قاعدة الخام (بالـ raw_id)"""
    if raw_id is None:
        return ""
    got = get_raw_texts([raw_id])
    return got.get(raw_id, "")


def extract_phone(text):
    """استخراج رقم جوال من النص — أنماط عربية وهندية. يعيد الرقم بلا مسافات أو None"""
    if not text:
        return None
    ar2en = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    t = text.translate(ar2en)
    # إزالة المسافات والشرطات داخل الرقم بشرط وجود 5 أولاً ثم أرقام
    # الأنماط المعروفة: +966 54 887 8876 | 9665xxxxxxxx | +96659 088 5010 | 05xxxxxxxx | 050xxx xxx xxx
    patterns = [
        r"\+?966\s*5\d\s*\d{3}\s*\d{4}",   # +966 54 887 8876 | +96659 088 5010
        r"9665\d{8}",                       # 9665xxxxxxxx
        r"(?<!\d)05\d\s*\d{3}\s*\d{4}(?!\d)",  # 054 887 8876 | 059 088 5010
        r"(?<!\d)05\d{8}(?!\d)",            # 0503660663
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            return re.sub(r"[^\d]", "", m.group(0))
    return None


def get_listing_contact(doc):
    """رقم التواصل للعرض: رقم المالك من النص الخام أولاً، ثم رقم أبو سامي"""
    raw_id = doc.get("raw_id")
    if raw_id:
        raw = get_raw_text(raw_id)
        num = extract_phone(raw)
        if num:
            return num
    # آخر محاولة: مالك مؤكد في المفروز
    oc = doc.get("owner_contact") or ""
    if oc:
        m = re.sub(r"[^\d]", "", oc)
        if m and len(m) >= 9:
            return m if m.startswith("05") else ("05" + m[-8:] if not m.startswith("966") else m)
    return BUSINESS_CONTACT


def get_customer_requests(limit=10):
    """طلبات الزبائن من قاعدة المفروز (listing_type='طلب') لتزويد المسوقين.

    تُعرض بدون رقم التواصل — رقم الزبون فقط يُسلَّم لمن عنده عرض مناسب."""
    conn = get_sorted_conn()
    try:
        rows = conn.execute(
            "SELECT id, property_type, deal_type, city, district, price, price_unit, short_desc "
            "FROM sorted_listings WHERE listing_type='طلب' AND is_junk=0 "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def format_customer_requests(docs):
    """تنسيق طلبات الزبائن لقائمة تصلح للمسوق"""
    if not docs:
        return "لا توجد طلبات زبائن في الوقت الحالي."
    lines = []
    for i, d in enumerate(docs, 1):
        head = []
        if d.get("property_type"):
            head.append(d["property_type"])
        if d.get("deal_type"):
            head.append(d["deal_type"])
        if head:
            lines.append(f"{i}. {' | '.join(head)}")
        loc = []
        if d.get("city"):
            loc.append(d["city"])
        if d.get("district"):
            loc.append(d["district"])
        if loc:
            lines.append(f"   📍 {'، '.join(loc)}")
        price = d.get("price")
        if price is not None:
            unit = d.get("price_unit") or ""
            try:
                ps = f"{float(price):,.0f} ريال"
            except Exception:
                ps = f"{price} ريال"
            if unit:
                ps += f" {unit}"
            lines.append(f"   💰 {ps}")
        if d.get("short_desc"):
            lines.append(f"   📝 {d['short_desc']}")
        lines.append("")
    lines.append("🔥 طلبات الزبائن أعلاه — إذا عندك عرض مناسب أرسله لنا عبر البوت.")
    return "\n".join(lines)


def relevance_block(results, limit=5):
    """سياق نصي يُحقن في برومت DeepSeek (يعتمد عليه ولا يختلق)"""
    if not results:
        return ""
    lines = []
    for i, (doc, _s) in enumerate(results[:limit], 1):
        price = doc.get("price")
        price_s = f"{float(price):,.0f}" if isinstance(price, (int, float)) else str(price or "")
        lines.append(
            f"[{i}] {doc.get('property_type') or ''} | حي {doc.get('district') or '؟'} | "
            f"{doc.get('city') or ''} | تعامل: {doc.get('deal_type') or '؟'} | "
            f"غرف: {doc.get('rooms') or '؟'} | حمامات: {doc.get('bathrooms') or '؟'} | "
            f"السعر: {price_s} {doc.get('price_unit') or ''} | "
            f"المميزات: {doc.get('features') or '—'} | "
            f"الوصف: {doc.get('short_desc') or '—'}"
        )
    return "\n".join(lines)


def parse_budget(query):
    """استخراج ميزانية من كلام العميل: 'خمسمية الف', '500 الف', 'أقل من مليون'"""
    import re
    query_n = normalize_nums(query)

    # "أقل من مليون" / "أقل من 500 ألف" — سقف بدون رقم محدد
    m_less = re.search(r"(?:اقل|أقل)\s*من\s*(?:(\d[\d ,.]*\d?)\s*)?(مليون|الف|ألف)", query_n)
    if m_less:
        if m_less.group(1):
            try:
                num = float("".join(c for c in m_less.group(1) if c.isdigit() or c == "."))
                return int(num * (1_000_000 if m_less.group(2) == "مليون" else 1000))
            except Exception:
                pass
        return 999_000_000 if m_less.group(2) == "مليون" else 999_000

    # أنماط "حدود 500 ألف" و"500 ألف" و"1.5 مليون" و"800,000"
    m = re.search(r"(?:حدود|حوالى|حوالين|حوالي|بحدود|من)\s*(\d[\d ,.]*\d?)\s*(فالف|الف|ألف|مليون|الف ريال|ألف ريال)?", query_n)
    if not m:
        m = re.search(r"(\d[\d ,.]*\d?)\s*(فالف|الف|ألف|مليون|الف ريال|ألف ريال)", query_n)
    if not m:
        return None
    try:
        num = float("".join(c for c in m.group(1) if c.isdigit() or c == "."))
        unit = m.group(2) if len(m.groups()) > 1 else None
        if unit and "مليون" in (unit or ""):
            return int(num * 1_000_000)
        if unit and ("الف" in (unit or "") or "ألف" in (unit or "") or "فالف" in (unit or "")):
            return int(num * 1000)
        # رقم عشوائي بدون وحدة عملة (مثل "4" من "عرض رقم 4") → تجاهل
        if num < 1000:
            return None
        return int(num)
    except Exception:
        return None


def normalize_nums(text):
    tr = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    return text.translate(tr)


_PROPERTY_TYPES = {
    "شقة": ("شقة", "شقق", "روف", "ملحق", "دوبلكس", "ستوديو", "شقق", "بنتهاوس"),
    "فيلا": ("فيلا", "فلل", "قصر", "قصور"),
    "أرض": ("أرض", "ارض", "اراضي", "أراضي", "ارضي", "بلك", "قطعة", "قطعتين", "قطع", "صك", "راس"),
    "عمارة": ("عمارة", "عمائر", "بناية", "بنايات"),
    "محل": ("محل", "محلات", "معرض", "مخزن", "مخازن", "مستودع", "مستودعات"),
    "مكتب": ("مكتب", "مكاتب", "عيادة", "عيادات"),
    "استراحة": ("استراحة", "استراحات", "شاليه", "شاليهات"),
}


def _norm_ar(text):
    """تطبيع خفيف: توحيد الهمزات والألف والتاء المربوطة لتقبل الكتابة العامية"""
    import unicodedata as _u
    t = _u.normalize("NFKC", str(text or ""))
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ة", "ه")
    t = t.replace("ى", "ي")
    return t


def parse_property_type(query):
    """استخراج نوع العقار المطلوب من كلام العميل، أو None إن لم يحدد"""
    import re as _re
    q = _norm_ar(normalize_nums(query))
    # نوع الأرض يُفحص أولاً: "اراضي/ارض/بلك" غالباً مقصد صريح حتى لو وردت كلمات أخرى
    order = ["أرض"] + [p for p in _PROPERTY_TYPES if p != "أرض"]
    for prop in order:
        for kw in _PROPERTY_TYPES[prop]:
            if _re.search(r"(?:^|\s)" + _re.escape(_norm_ar(kw)) + r"(?:\s|$)", q):
                return prop
    return None


_DEAL_KEYWORDS = {
    "تمليك": ("تمليك", "تملك", "للبيع", "شراء", "بيع", "امتلاك"),
    "ايجار": ("ايجار", "إيجار", "للإيجار", "بالايجار", "أجار", "اجار", "استئجار"),
}


def parse_deal_type(query):
    """استخراج نوع التعامل (تمليك/ايجار) من كلام العميل، أو None"""
    import re as _re
    q = _norm_ar(normalize_nums(query))
    for deal, keywords in _DEAL_KEYWORDS.items():
        for kw in keywords:
            if _re.search(r"(?:^|\s)" + _re.escape(_norm_ar(kw)) + r"(?:\s|$)", q):
                return deal
    return None


_DISTRICT_CACHE = None


def _district_names():
    """أسماء الأحياء الحقيقية من قاعدة المفروز (مطبَّعة) — تُبنى مرة واحدة"""
    global _DISTRICT_CACHE
    if _DISTRICT_CACHE is not None:
        return _DISTRICT_CACHE
    names = set()
    try:
        conn = get_sorted_conn()
        rows = conn.execute(
            "SELECT DISTINCT district FROM sorted_listings "
            "WHERE listing_type='عرض' AND status='نشط' AND district IS NOT NULL"
        ).fetchall()
        conn.close()
        for r in rows:
            d = _norm_ar(str(r[0])).strip()
            if d:
                names.add(d)
    except Exception:
        pass
    # مرادفات شائعة تكتب عامية
    names.update({"ابحر", "بحر", "شمال", "وسط", "جدة", "مكة", "الرياض"})
    _DISTRICT_CACHE = names
    return names


def parse_district(query):
    """استخراج حي مذكور في كلام العميل (عامي بلا همزات مقبول) أو None"""
    q = _norm_ar(normalize_nums(query))
    if not q:
        return None
    for d in _district_names():
        if d and d in q:
            return d
    return None


# ──────────────────────────────────────────────
#  خريطة مناطق جدة (شمال/وسط/شرق/جنوب) ومكة والرياض
#  — تُستخدم لتصفية وبوّابة أفضليات المرشحين قبل النموذج
# ──────────────────────────────────────────────
JEDDAH_AREAS = {
    "شمال جدة": {
        "أبحر الشمالية", "أبحر الجنوبية", "ابحر", "بحر", "الشاطئ", "المرجان", "البساتين",
        "المحمدية", "النعيم", "النهضة", "الزهراء", "السلامة", "الروضة", "الخالدية",
        "الأصالة", "الياقوت", "اللؤلؤ", "الشراع", "الأمواج", "الصواري", "الزمرد",
        "الفنار", "المنارات", "البحيرات", "طيبة", "الرحيلي", "خليج سلمان", "ذهبان",
        "الشاطئ الذهبي", "درة العروس", "جوهرة العروس",
    },
    "وسط جدة": {
        "الحمراء", "الأندلس", "الرويس", "الشرفية", "مشرفة", "العزيزية", "الرحاب",
        "بني مالك", "النسيم", "الورود", "السليمانية", "الفيحاء", "البلد", "البغدادية",
        "الكندرة", "الصحيفة", "العمارية", "الهنداوية", "السبيل", "النزهة", "المروة",
        "الربوة", "البوادي", "الصفا", "الفيصلية", "وسط جدة",
    },
    "شرق جدة": {
        "الحمدانية", "الصالحية", "الفلاح", "الرحمانية", "الفروسية", "الرياض", "الوفاء",
        "السامر", "الأجواد", "المنار", "الواحة", "بريمان", "التوفيق", "مريخ", "النخيل",
        "الرغامة", "الحرازات", "أم السلم", "المنتزهات", "الريان", "الصفوة", "درب الحرمين",
    },
    "جنوب جدة": {
        "الجامعة", "الثغر", "الروابي", "النزلة", "مدائن الفهد", "القريات", "غليل",
        "بترومين", "المحجر", "الوزيرية", "الجوهرة", "السنابل", "الأجاويد", "الأمير فواز",
        "الأمير عبدالمجيد", "الخمرة", "الفضيلة", "القرينية", "القوزين", "المليساء",
        "العدل", "الحسينية", "السلامة 2", "التيسير", "الفضيل", "الفضل",
    },
}

MAKKAH_ATTRACTIONS = {
    "التخصصي", "الزايدي", "الخالدية", "جبل عمر", "العزيزية", "الجميزة", "الملك فهد",
}
RIYADH_DISTRICTS = {
    "الرحاب", "السرورية", "الصفا", "النزهة", "النهضة", "الريان", "ضاحية الجوهرة",
    "مخطط الرياض", "الملقا", "النسيم", "الخليج", "النرجس", "الرياض",
}

_AREA_CACHE = None


def _area_map():
    """قائمة (اسم المنطقة، كلماتها المبدئية) — تُبنى مرة واحدة"""
    global _AREA_CACHE
    if _AREA_CACHE is None:
        import re as _re
        pairs = []
        for area, names in list(JEDDAH_AREAS.items()) + [("مكة", MAKKAH_ATTRACTIONS), ("الرياض", RIYADH_DISTRICTS)]:
            pairs.append((area, [_norm_ar(x) for x in names]))
        _AREA_CACHE = [(_re.compile(r"(^|\s)" + _re.escape(x) + r"($|\s)"), area)
                       for area, names in pairs for x in names]
    return _AREA_CACHE


def match_area(district, area):
    """هل حي معيّن يقع ضمن منطقة (مثلاً شمال جدة)؟"""
    d = _norm_ar(str(district or ""))
    if not d:
        return area == "شمال جدة" and False
    keywords = dict(list(JEDDAH_AREAS.items()) + [("مكة", MAKKAH_ATTRACTIONS), ("الرياض", RIYADH_DISTRICTS)])
    for kw in keywords.get(area, ()):
        if _norm_ar(kw) in d:
            return True
    return False


def area_hint(query):
    """استخراج منطقة (شمال/وسط/شرق/جنوب جدة، مكة، الرياض) من كلام العميل أو None"""
    import re as _re
    q = _norm_ar(normalize_nums(query))
    hints = []
    _AREA_DIR = {"شمال": "شمال جدة", "وسط": "وسط جدة", "جنوب": "جنوب جدة", "شرق": "شرق جدة",
                 "غرب": "غرب جدة"}
    # أنماط صريحة: "شمال جدة", "جنوب المدينة", "وسط جدة", "شمال الرياض"
    for pat, area in [
        (r"شمال\s+([^\s]+)", None), (r"جنوب\s+([^\s]+)", None),
        (r"وسط\s+([^\s]+)", None), (r"شرق\s+([^\s]+)", None),
        (r"غرب\s+([^\s]+)", None),
    ]:
        m = _re.search(pat, q)
        if m:
            direction, city = m.group(0).split()[0], m.group(1)
            if "جده" in city:
                hints.append(_AREA_DIR.get(direction) or direction)
            elif "مكه" in city:
                hints.append("مكة")
            elif "رياض" in city:
                hints.append("الرياض")
    if "مكه" in q or "مكة" in q:
        hints.append("مكة")
    if "الرياض" in q or "رياض" in q:
        hints.append("الرياض")
    return list(dict.fromkeys(hints))


def prioritize_by_area(docs, area):
    """إعادة ترتيب المرشحين: أحياء المنطقة المطلوبة تتصدر، ثم الباقي حسب ترتيبه"""
    in_area = [d for d in docs if match_area(d.get("district"), area)]
    rest = [d for d in docs if d not in in_area]
    return in_area + rest


# ──────────────────────────────────────────────
#  إحصاءات حقيقية من المفروز
# ──────────────────────────────────────────────
def listing_stats():
    try:
        conn = get_sorted_conn()
        total = conn.execute(
            "SELECT COUNT(*) FROM sorted_listings WHERE listing_type='عرض' AND status='نشط'"
        ).fetchone()[0]
        by_type = conn.execute(
            "SELECT property_type, COUNT(*) as c FROM sorted_listings "
            "WHERE listing_type='عرض' AND status='نشط' AND property_type IS NOT NULL "
            "GROUP BY property_type ORDER BY c DESC"
        ).fetchall()
        conn.close()
        breakdown = "، ".join(f"{r['property_type']}: {r['c']}" for r in by_type)
        return total, breakdown
    except Exception as e:
        print(f"stats err: {e}", flush=True)
        return 0, ""


# ──────────────────────────────────────────────
#  الذاكرة — تذكُّر الزبون والمحادثة
# ──────────────────────────────────────────────
def _memory_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS chat_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            role TEXT,
            content TEXT,
            ts TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_filters (
            user_id TEXT PRIMARY KEY,
            property_type TEXT,
            deal_type TEXT,
            city TEXT,
            district TEXT,
            max_price INTEGER,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS shown_listings (
            user_id TEXT,
            raw_id INTEGER,
            shown_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, raw_id)
        )"""
    )


def save_memory(user_id, role, content):
    """حفظ دور (زبون/بوت) في ذاكرة المحادثة"""
    try:
        conn = get_sorted_conn()
        _memory_table(conn)
        conn.execute(
            "INSERT INTO chat_memory (user_id, role, content) VALUES (?, ?, ?)",
            (str(user_id), role, (content or "")[:3000]),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"memory save err: {e}", flush=True)


def load_memory(user_id, limit=8):
    """آخر رسائل المحادثة مع الزبون (أحدثها أولاً)"""
    try:
        conn = get_sorted_conn()
        _memory_table(conn)
        rows = conn.execute(
            "SELECT role, content, ts FROM chat_memory "
            "WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (str(user_id), limit),
        ).fetchall()
        conn.close()
        return list(reversed(rows))
    except Exception as e:
        print(f"memory load err: {e}", flush=True)
        return []


def memory_block(user_id, limit=8):
    """نص المحادثة السابقة يُحقن في البرومت — يعطي البوت ذاكرة"""
    hist = load_memory(user_id, limit=limit)
    if not hist:
        return ""
    lines = []
    for r in hist:
        who = "الزبون" if r["role"] == "user" else "أنت (فذ العقارية)"
        lines.append(f"{who}: {r['content'][:500]}")
    return "المحادثة السابقة مع هذا الزبون (تذكرها وأكمل منها):\n" + "\n".join(lines)


# ──────────────────────────────────────────────
#  ذاكرة فلاتر الزبون الدائمة + تتبع العروض المعروضة
# ──────────────────────────────────────────────
def save_user_filters(user_id, ptype=None, deal=None, city=None, district=None, budget=None):
    """دمج فلاتر الزبون المعروفة — تُحدّث وتدوم بين الجلسات"""
    try:
        conn = get_sorted_conn()
        _memory_table(conn)
        old = conn.execute(
            "SELECT * FROM user_filters WHERE user_id=?", (str(user_id),)
        ).fetchone()
        new_ptype = ptype or (old["property_type"] if old else None)
        new_deal = deal or (old["deal_type"] if old else None)
        new_city = city or (old["city"] if old else None)
        new_dist = district or (old["district"] if old else None)
        new_budget = budget if budget is not None else (old["max_price"] if old else None)
        conn.execute(
            """INSERT INTO user_filters (user_id, property_type, deal_type, city, district, max_price, updated_at)
               VALUES (?,?,?,?,?,?, datetime('now'))
               ON CONFLICT(user_id) DO UPDATE SET
                 property_type=excluded.property_type, deal_type=excluded.deal_type,
                 city=excluded.city, district=excluded.district, max_price=excluded.max_price,
                 updated_at=datetime('now')""",
            (str(user_id), new_ptype, new_deal, new_city, new_dist, new_budget),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"filters save err: {e}", flush=True)


def load_user_filters(user_id):
    """فلاتر الزبون المحفوظة"""
    try:
        conn = get_sorted_conn()
        _memory_table(conn)
        row = conn.execute(
            "SELECT * FROM user_filters WHERE user_id=?", (str(user_id),)
        ).fetchone()
        conn.close()
        return dict(row) if row else {}
    except Exception as e:
        print(f"filters load err: {e}", flush=True)
        return {}


def mark_listing_shown(user_id, raw_ids):
    """تسجيل العروض المعروضة لهذا الزبون"""
    if not raw_ids:
        return
    try:
        conn = get_sorted_conn()
        _memory_table(conn)
        for rid in raw_ids:
            conn.execute(
                "INSERT OR IGNORE INTO shown_listings (user_id, raw_id) VALUES (?, ?)",
                (str(user_id), rid),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"shown save err: {e}", flush=True)


def shown_raw_ids(user_id):
    """معرّفات العروض المعروضة سابقاً لهذا الزبون"""
    try:
        conn = get_sorted_conn()
        _memory_table(conn)
        rows = conn.execute(
            "SELECT raw_id FROM shown_listings WHERE user_id=?", (str(user_id),)
        ).fetchall()
        conn.close()
        return {r["raw_id"] for r in rows}
    except Exception as e:
        print(f"shown load err: {e}", flush=True)
        return set()


def last_shown_listing(user_id):
    """أحدث عرض تم عرضه للزبون (من قاعدة shown_listings) أو None"""
    try:
        conn = get_sorted_conn()
        _memory_table(conn)
        row = conn.execute(
            "SELECT raw_id FROM shown_listings WHERE user_id=? "
            "ORDER BY shown_at DESC, rowid DESC LIMIT 1",
            (str(user_id),),
        ).fetchone()
        conn.close()
        if not row:
            return None
        rid = row["raw_id"]
        conn = get_sorted_conn()
        doc = conn.execute(
            "SELECT * FROM sorted_listings WHERE raw_id=? AND listing_type=?",
            (rid, "عرض"),
        ).fetchone()
        conn.close()
        return dict(doc) if doc else None
    except Exception as e:
        print(f"last shown err: {e}", flush=True)
        return None
        return set()


def get_rule_block():
    """قواعد بوت المشرف — تُحقن في برومبت بوت فذ لتتحقق تلقائياً"""
    try:
        conn = get_sorted_conn()
        _memory_table(conn)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bot_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule TEXT, created_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        rows = conn.execute("SELECT rule FROM bot_rules ORDER BY id").fetchall()
        conn.close()
        if not rows:
            return ""
        rules = [r["rule"] for r in rows]
        return "قواعد المشرف الإضافية (التزم بها بصرامة في كل رد):\n" + "\n".join(f"- {r}" for r in rules)
    except Exception as e:
        print(f"rules load err: {e}", flush=True)
        return ""


# ──────────────────────────────────────────────
#  Prompts
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """أنت مساعد عقاري ذكي لشركة "فذ العقارية" في السعودية، تعمل كأفضل مستشار عقاري محترف. أنت واسع المعرفة العامة والجغرافية السعودية.

مهمتك:
1. مساعدة العملاء في إيجاد عقارات مناسبة من العروض الحقيقية المرفقة لك في السياق.
2. الإجابة بمعرفتك عن أي سؤال عام يتعلق بالسعودية: الأحياء وأسماؤها وتقسيماتها (مثل أحياء شمال جدة، جنوب جدة، أحياء مكة والرياض)، مواقع المعالم، معلومات عامة، نصائح استثمار عقاري، وغيرها — أجب مباشرة من معرفتك كخبير دون أن تشير إلى أنك بلا قاعدة بيانات.
   - إذا لم تكن متأكداً تماماً من خلاصة معلوماتية حديثة (مثل قوائم أحياء حالية أو اتجاهات السوق الآن)، فابدأ ردك بسطر واحد بصيغة: [بحث: استعلامك الدقيق بالعربية]
   - ثم بعد الرسالة سنجري لك البحث تلقائياً ونعطيك النتائج لتكمل الرد عليهما بدقة.

القواعد:
- رد بالعربي السعودي الودود المحترم، بأسلوب مفصل ومنظم وواضح
- لا تستخدم ترميز Markdown إطلاقاً في ردك (لا نجمتين ** ولا شرطة سفلية _) — اكتب النص عادياً بدون رموز تنسيق
- افهم كلام العميل العامي كما هو (كتب بدون همزات أو بأخطاء إملائية مثل "ارض" و"ابحر" و"فيلا" و"شقه") — أنت تفهم السياق والمقصود، فلا تتقيد بالحرف بل بالمعنى
- رحّب بالزبون بحرارة وبأسلوب ودود، وحاول أن تفهم طلبه تماماً بنفسك من سياق كلامه — بدون فلاتر صارمة وبدون إلحاح بأسئلة توضيحية متكررة، وإذا احتجت أي توضيح فاسأل سؤالاً واحداً لطيفاً فقط
- قرار العرض النهائي لك وحدك: فهم طلب العميل بنفسك من السياق، ومن قائمة العروض المرقمة المرفقة اختر المطابق واكتب ردك كاملاً بلا الحاجة لرسائل منفصلة أو أسئلة متكررة
- اكتب ردك في رسالة واحدة موحّدة تشمل: ترحيباً قصيراً، ثم القائمة المرقمة للعروض المختارة ببياناتها الحقيقية (النوع، الموقع، السعر) من السياق، ثم اسأل العميل أي عرض يريده — لا أزرار، فقط كتابة رقم العرض
- حافظ على الأرقام والبيانات كما وردت حرفياً في قائمة السياق، ولا تعيد ترتيبها بترقيم جديد خارجها
- اعرض العروض الحقيقية المرفقة في السياق فقط (النوع، الموقع، السعر، الغرف) بترتيب مناسب
- حدد نوع العقار ونطاقه الذي طلبه العميل بدقة والتزم به: اعرض فقط عروض نفس النوع ضمن النطاق المطلوب (الحي/المنطقة)، ولا تعرض نوعاً آخر أو نطاقاً أوسع أبداً إلا إذا سمح العميل صراحةً بذلك
- إذا لم يوجد نوعه المطلوب في نطاقه، فأعرض له عروض نفس نوعه ببساطة في نطاق أوسع أو قُل بصدق "ما عندنا مطابق حالياً"، ولا تعرض أنواعاً مختلفة كبديل
    - لا تختلق عروضاً أو أرقاماً غير موجودة في السياق أبداً
    - المساحة: لا تخمّنها أبداً ولا تشتقّها من كلام العميل (مثل "نص أرض"). إن وُذكرت في وصف العرض فاستخدمها حرفياً، وإلا قُل "المساحة غير مذكورة في العرض".
    - التاريخ: إن سُئلت عن تاريخ أو قدم العرض، استخدم "تاريخ النشر" المرفق مع كل عرض إن وُجد، ولا تقل "لا أملك التاريخ".
- قاعدة عدم الاختلاق (صارمة جداً): أرقام العروض التي تذكرها وحيّها وسعرها وعدد غرفها يجب أن تكون مطابقة حرفياً لما في قائمة السياق المرقمة (تبدأ من 1). ممنوع اختلاق أي عرض أو رقم ترتيب أو حي أو سعر أو عدد غرف غير موجود في القائمة المرقمة. إذا لم يتوفر لك عرض حقيقي من تلك القائمة يناسب الطلب، فلا تختلق — قُل ببساطة: "ما عندنا مطابق متاح حالياً في هذا النطاق/الحي" واعرض بديلاً حقيقياً من نفس النوع فقط إن وجد في القائمة
- في الردود العامة عن الأحياء/التصنيف الجغرافي (مثل "ماهي أحياء شمال جدة؟" أو "في أي نطاق يقع الحي؟" أو "أين يقع الحي؟") أجب من معرفتك فقط ودون إرفاق قائمة عروض مرقّمة أو أسعار أو أرقام، إلا إذا طلب العميل العروض صراحةً بعد ذلك
- إذا وُجد عرض مطابق لطلب العميل فاعرضه بثقة، وإذا لم يوجد قُل ببساطة: "للأسف هذا الطلب غير متوفر ضمن عروضنا الحالية" ثم اسأل عن بديل ممكن، دون أي تبرير
- لا تعتذر عن قلة العروض ولا تذكر حالة العروض الداخلية أو أنها طلبات أو بلا سعر — هذا يضعف الثقة بالشركة
- لا تعرض أبداً أي طلب زبون (مثل "مطلوب أرض" أو "شخص يبحث عن") كعرض أو ضمن قائمة العروض — فقط العروض الفعلية المعروضة للتمليك/الإيجار
- لا تخلط بين طلبات الزبائن والعروض المتاحة في ردك، وإذا ورد طلب في السياق فتجاهله تماماً ولا تذكره
- لا تكرر عرضاً سبق عرضه على هذا الزبون ورفضه؛ اقترح غيره أو اسأل عن احتياجه
- عندما يسألك العميل عن عدد العروض، استخدم الرقم الحقيقي من الإحصاءات المرفقة
- عند الطلب حدد نوع العقار والحي والميزانية واسأل عن الناقص
- إن كان العميل يبحث عن شيء معيّن (حي، ميزانية، عدد غرف) استخدم الفلاتر المستخرجة
- إذا طلب العميل عروضاً "إضافية/أخرى" بعد بحث محدد (مثل شقق تمليك)، فلا تعرض له أنواعاً مختلفة (أراضي، فلل، عمارات) أبداً إلا إذا صرّح أنه يريد تغيير نوع العقار — التزم بنفس النوع"""


# مرجع توزيع أحياء جدة — معلومات إرشادية للبوت، يُبني عليها ولا يتقيد بها حرفياً
JEDDAH_DISTRICTS_REF = """🔎 مرجع توزيع أحياء جدة (معلومات إرشادية تُساعدك على التقدير، ليست دقيقة مطلقة ولا تُقيدك):
- شمال جدة: أبحر الشمالية، أبحر الجنوبية، الشاطئ، المرجان، البساتين، المحمدية، النعيم، النهضة، الزهراء، السلامة، الروضة، الخالدية، الأصالة، الياقوت، اللؤلؤ، الشراع، الأمواج، الصواري، الزمرد، الفنار، المنارات، البحيرات، طيبة (الرحيلي)، خليج سلمان، ذهبان، الشاطئ الذهبي.
- وسط جدة: الحمراء، الأندلس، الرويس، الشرفية، مشرفة، العزيزية، الرحاب، بني مالك، النسيم، الورود، السليمانية، الفيحاء، البلد (جدة التاريخية)، البغدادية الشرقية، البغدادية الغربية، الكندرة، الصحيفة، العمارية، الهنداوية، السبيل، النزهة، المروة، الربوة، البوادي، الصفا، الفيصلية.
- شرق جدة: الحمدانية، الصالحية، الفلاح، الرحمانية، الفروسية، الرياض، الوفاء، السامر، الأجواد، المنار، الواحة، بريمان، التوفيق، مريخ، النخيل، الرغامة، الحرازات، أم السلم، المنتزهات، الريان، الكيلو 14.
- جنوب جدة: الجامعة، الثغر، الروابي، النزلة الشرقية، النزلة اليمانية، مدائن الفهد، القريات، غليل، بترومين، المحجر، الوزيرية، الجوهرة، السنابل، الأجاويد، الأمير فواز الشمالي، الأمير فواز الجنوبي، الأمير عبد المجيد، الخمرة، الفضيلة، القرينية، القوزين، المليساء.
ملاحظات مرجعية (تعامل بمرونة):
- الحمدانية والمخططات المجاورة (الرياض، الصالحية، الرحمانية) قد يصفها البعض شمالاً بينما تُحسب تجارياً شرق/شمال شرق — تعامل مع "الحمدانية" كطلب مستقل عند البحث.
- الصفا والمروة والربوة وسطياً لكنها قريبة من امتداد الشرق والشمال — اربطها مع "وسط/شمال وسط"."""


FALLBACK_REPLY = "أهلين! 👋 أنا مساعد فذ العقارية.\n\nاكتب لي مثلاً:\n- \"شقة تمليك في جدة حدود 500 ألف\"\n- \"فيلا للإيجار في حي الصفا\"\n- \"عندكم عروض في حي الروضة؟\""


# ──────────────────────────────────────────────
#  Handlers
# ──────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = str(user.id)
    uname = user.first_name or user.username or uid
    keyboard = [
        [
            InlineKeyboardButton("🏠 عروض متاحة", callback_data="browse"),
            InlineKeyboardButton("📞 تواصل مع مكتب", callback_data="contact"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    greeting = (
        f"أهلين {user.first_name}! 👋\n"
        "أنا مساعد فذ العقارية العقاري.\n\n"
        "اكتب لي وش تبي وبدور لك في العروض المتاحة، مثل:\n"
        "• \"شقة في حي الروضة حدود 500 ألف\"\n"
        "• \"فيلا إيجار 4 غرف\"\n"
        "• \"بغيت أبحث لك على أرض في جدة\"\n\n"
        "أو اكتب «معروض» لعرض كل عروضنا، و«مطلوب» لعرض طلبات الزبائن."
    )
    log_conversation(uid, uname, "/start", greeting)
    save_memory(uid, "assistant", greeting)
    await update.message.reply_text(greeting, reply_markup=reply_markup)


import re as _re

_INTENT_DETAILS = _re.compile(r"(تفاصيل|التفاصيل|تفصيل|الوصف|وصف.+|قلي.+تفاصيل|معلومات.+(العرض|الموقع)|وش.+(المواصفات|التفاصيل))", _re.IGNORECASE)
_INTENT_PHONE = _re.compile(
    r"(رقم\s+(صاحب|صاحبه|صاحبها|المالك|البائع|المعلن|الاعلان|الإعلان|العقار|الوسيط|التواصل|الجوال|الاتصال)"
    r"|جوال\s+(صاحب|صاحبه|صاحبها|المالك|البائع)"
    r"|(رقمك|نمره|نمرة|تلفون|جوالك|اعطني\s*رقم|أعطني\s*رقم)"
    r"|(ابي|أبي|ابغى|ابغي|أبغي|بغيت|بغى|اريد|أريد|ودي|نبغى|نبي|عايز|اعطيني|أعطيني|عطيني|وريني|اكتب|أكتب|قل|قول|ابيك|أبيك).+(رقم|جوال|نمره|نمرة|تواصل|مباشرة)"
    r"|(ابي|أبي|ابغى|ابغي|أبغي|بغيت|بغى|اريد|أريد|ودي|نبغى|نبي|عايز|اعطيني|أعطيني|وريني|خلني|دلني).+(صاحب|صاحبه|صاحبها|المالك|البائع|المعلن|الموزع)"
    r"|(كيف|شلون|طريقة|بأي|بكيف|وش|بشنو).*(اوصل|ا?صل|أصل|اصل|اتصل|أتواصل|اتواصل|اواصل|أواصل|ريحني|سرني|أكلّم|اكلم).*(صاحب|صاحبه|صاحبها|المالك|البائع|المعلن)"
    r"|(اوصلني|اصلني|وصّلني|أوصلني).+(مباشرة|لصاحب|بصاحب|صاحبه|للمالك|للبائع)"
    r"|(صاحبه|صاحبها|المالك|البائع|المعلن).+(رقم|جوال|مباشرة|اتصال|اتصل)"
    r"|(اتصل|أتصل|يتصل|تواصل|تواصلت|اكلم|أكلّم|نوصل|أوصل|اوصل|ا?واصل|أواصل|اواصل).*(صاحب|صاحبه|صاحبها|المالك|البائع|للبائع|للمالك|بائع|معلن))",
    _re.IGNORECASE,
)
_INTENT_RAW = _re.compile(r"(خام|الخام|كامل|الكامل|الأصلي|كما ورد|كما هو|بدون تعديل|النص الكامل|نصه|الصور[هة]|نص الاعلان|نص الإعلان)", _re.IGNORECASE)
_INTENT_REQUESTS = _re.compile(
    r"(طلبات\s*(ال)?(زبائن|زباين|عملاء|زبون|الزبائن|العملاء|المسوقين|الوكلاء|الزبون|البيع)?"
    r"|(اعطيني|عطني|عرضلي|وريني|ارسلي|ابي|ابغى|ابغا|بغيت|منب)\s*.{0,6}طلبات"
    r"|عندكم\s*طلبات|عندك\s*طلبات|عندي\s*طلبات|فيه?\s*طلبات|في\s*طلبات|هلا\s*طلبات|المطلوبات"
    r"|الطلبات\s*(موجودة|متاحة|الان|اللي|الي)?)",
    _re.IGNORECASE,
)
_NUM_REF = _re.compile(r"(?:عرض|العرض|رقم|الرقم)\s*#?\s*(\d{1,2})", _re.IGNORECASE)


def _format_phone(p):
    """05XXXXXXXX → 05X XXXXXXX لعرض أوضح؛ تحويل 9665... إلى 05..."""
    if not p:
        return p
    if p.startswith("966") and len(p) == 12:
        p = "0" + p[3:]   # 966548878876 → 0548878876
    if len(p) == 10 and p.startswith("05"):
        return f"{p[:3]} {p[3:6]} {p[6:]}"
    return p


def clean_md(text):
    """إزالة ترميز Markdown (نجمتان/شرطة سفلية) حتى لا تظهر حرفياً للعميل"""
    if not text:
        return text
    t = text.replace("**", "").replace("__", "")
    t = re.sub(r"(?<!\s)_(?=\S)|(?<=\S)_(?!\s)", "", t)  # _مائل_ بلا مسافات
    return t


CONTACT_MESSAGE = """هذا رقم المالك للتواصل معه مباشرة:

\u2066{phone}\u2069

فضلاً لا أمراً، يُرجى توضيح أنك طرف (أبو سامي / فذ العقارية) فور تواصلك معه ليعرف مصدر العرض ولحفظ حقوق المتابعة بيننا.

بالتوفيق إن شاء الله!"""


def _listing_buttons(doc):
    """أزرار تفاصيل / رقم تحت العرض (محذوفة الاستخدام — البطاقات في رسالة واحدة)"""
    return None

_INTENT_ALL = _re.compile(r"(كل العروض|جميع العروض|كل العرض|كل ها|كلها|الكل|عطني كل|الكل دفعة|مرة واحدة)", _re.IGNORECASE)
_INTENT_MORE = _re.compile(r"(زودني|زيدني|زيد|المزيد|باقي|عروض ثانية|ثانية|اكمل|اكمل|أكمل|كمل|زيادة|نشوف اكثر|أكثر)", _re.IGNORECASE)


def _compact(doc, i):
    """بطاقة عرض سطرية مختصرة للعرض في قائمة مرقمة موحدة"""
    pt = doc.get("property_type") or ""
    loc = " - ".join(x for x in (doc.get("district"), doc.get("city")) if x)
    line = f"{i}. {pt}" + (f" في {loc}" if loc else "")
    price = doc.get("price")
    unit = (doc.get("price_unit") or "").strip()
    if price is not None:
        try:
            ps = f"{float(price):,.0f} ريال"
        except Exception:
            ps = f"{price} ريال"
        u = {u: {"إجمالي": "", "سنوي": "/سنة", "شهري": "/شهر", "مترمربع": "/م²", "سهم": "/سهم"}.get(u, "" if u == u else f"/{u}") for u in [unit]}[unit]
        line += f" | {ps}{u}" if u else f" | {ps}"
    bits = []
    if doc.get("rooms"):
        bits.append(f"{doc['rooms']} غرف")
    if doc.get("bathrooms"):
        bits.append(f"{doc['bathrooms']} حمام")
    if doc.get("finishing"):
        bits.append(f"تشطيب {doc['finishing']}")
    if bits:
        line += " | " + "، ".join(bits)
    return line


def _ctx_line(doc, i):
    """صيغة غنية للسياق المُمرَّر لـ DeepSeek (تشمل الوصف والتاريخ لمنع الاختلاق)"""
    pt = doc.get("property_type") or ""
    loc = " - ".join(x for x in (doc.get("district"), doc.get("city")) if x)
    price = doc.get("price")
    unit = (doc.get("price_unit") or "").strip()
    ps = f"{price} {unit}" if price is not None else "بدون سعر"
    line = f"{i}. {pt} | {loc} | السعر: {ps}"
    if doc.get("rooms"):
        line += f" | {doc['rooms']} غرف"
    if doc.get("short_desc"):
        line += f" | الوصف: {doc['short_desc']}"
    if doc.get("raw_text"):
        line += f" | النص الأصلي: {doc['raw_text'].replace(chr(10), ' ')}"
    if doc.get("posted_date"):
        line += f" | تاريخ النشر: {doc['posted_date']}"
    return line


def _offer_block(docs, start=1, limit=10):
    """قائمة مرقمة موحدة في رسالة واحدة"""
    chunk = docs[start - 1: start - 1 + limit]
    if not chunk:
        return ""
    shown = "\n".join(_compact(d, start + idx) for idx, d in enumerate(chunk))
    extra = f"\n\nاكتب زودني عشان أشوف الباقي 👌" if len(docs) > start - 1 + limit else ""
    return shown + extra


def _get_listings(listing_type):
    """جلب كل العروض/الطلبات النشطة من المفروز (الأحدث أولاً)"""
    conn = get_sorted_conn()
    rows = conn.execute(
        """SELECT id, raw_id, message_id, listing_type, property_type, deal_type, city, district,
                  rooms, bathrooms, kitchen, rooftop, annex, driver_room, maid_room, finishing,
                  price, price_unit, features, short_desc, channel, owner_contact
           FROM sorted_listings
           WHERE status='نشط' AND listing_type=?
           ORDER BY id DESC""",
        (listing_type,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _plain_block(docs, start, limit):
    chunk = docs[start - 1: start - 1 + limit]
    return "\n".join(_compact(d, start + i) for i, d in enumerate(chunk))


async def _send_listing_chunks(update, docs, label):
    total = len(docs)
    if not total:
        await update.message.reply_text(f"لا توجد {label} حالياً.")
        return
    await update.message.reply_text(f"{label}\n📊 الإجمالي: {total}")
    CHUNK = 25
    for s in range(0, total, CHUNK):
        block = _plain_block(docs, start=s + 1, limit=CHUNK)
        if block:
            await update.message.reply_text(block)
            await asyncio.sleep(0.3)


async def cmd_offers(update, context):
    await _send_listing_chunks(update, _get_listings("عرض"), "🏠 عروضنا المتاحة:")


async def cmd_requests(update, context):
    await _send_listing_chunks(update, _get_listings("طلب"), "📋 طلبات الزبائن:")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_message = (update.message.text or "").strip()
    uid = str(user.id)
    ud = context.user_data or {}
    uname = user.first_name or user.username or str(user.id)

    # ── أوامر عربية نصية: /معروض و /مطلوب (تليجرام لا يقبل أوامر عربية فتُعامل نصاً) ──
    _cmd = user_message.lstrip("/").strip().split()
    _cmd = _cmd[0] if _cmd else ""
    if _cmd in ("معروض", "عروض", "المعروض", "العروض"):
        await cmd_offers(update, context)
        return
    if _cmd in ("مطلوب", "طلبات", "طلب", "المطلوب", "الطلبات"):
        await cmd_requests(update, context)
        return

    last = ud.get("last_listing")
    offers_list = ud.get("last_offers") or []

    # ── تفاصيل عرض محدد: "تفاصيل عرض رقم 5" ──
    if _INTENT_DETAILS.search(user_message) or _INTENT_RAW.search(user_message):
        m = _NUM_REF.search(user_message)
        target = None
        if m and offers_list:
            n = int(m.group(1))
            target = offers_list[n - 1] if 1 <= n <= len(offers_list) else None
        doc = target or last
        if doc:
            raw = get_raw_text(doc.get("raw_id"))
            if raw:
                reply = (
                    f"📄 تفاصيل العرض:\n\n{format_listing(doc, 1)}\n\n"
                    f"النص الكامل كما ورد:\n{raw}\n\n{BUSINESS_LINE}"
                )
            else:
                reply = format_listing(doc, 1)
            log_conversation(user.id, uname, user_message, reply)
            save_memory(uid, "assistant", reply)
            await update.message.reply_text(clean_md(reply))
            return

    # ── رقم التواصل: "رقم التواصل" / "جوال المالك" / "رقمك" — يُعطى دائماً بلا رفض ──
    if _INTENT_PHONE.search(user_message):
        m = _NUM_REF.search(user_message)
        doc = None
        if m and offers_list:
            n = int(m.group(1))
            doc = offers_list[n - 1] if 1 <= n <= len(offers_list) else None
        doc = doc or last
        if not doc:
            # لا يوجد عرض محفوظ في الجلسة — أحدث عرض معروض لهذا الزبون هو المقصود
            try:
                doc = last_shown_listing(uid)
            except Exception as e:
                print(f"phone last shown err: {e}", flush=True)
        if not doc:
            # لا يوجد عرض معروض محفوظ — نحاول استرجاع العرض المطابق من السياق
            try:
                histq = load_memory(uid, limit=6)
                q = user_message + " " + " ".join(
                    r["content"] for r in reversed(histq) if r["role"] == "user"
                )
                cands = rag_search(q, k=5) or []
                cands = [c for c in cands if c.get("property_type") and c.get("channel")]
                if cands:
                    doc = cands[0]
            except Exception as e:
                print(f"phone search err: {e}", flush=True)
        if doc:
            num = get_listing_contact(doc)
            reply = contact_reply(doc, num)
        else:
            # لا يوجد عرض محفوظ — نعطي رقم الشركة مباشرة، كل شيء متاح للعميل
            reply = (
                "📞 رقم التواصل معنا مباشرة:\n\n"
                f"\u2066{_format_phone(BUSINESS_CONTACT)}\u2069\n\n"
                "تفضل، نحن في خدمتك — تواصل بنا من أنواع فذ العقارية وأخبرنا بطلبك وسنساعدك على الفور."
            )
        log_conversation(user.id, uname, user_message, reply)
        save_memory(uid, "assistant", reply)
        await update.message.reply_text(clean_md(reply))
        return

    # ── "رقم N" بدون كلمة أخرى: إعطاء رقم صاحب ذلك العرض مباشرة ──
    if offers_list and _re.match(r"^\s*رقم\s*(\d+)\s*$", user_message, _re.IGNORECASE):
        m = _re.search(r"(\d+)", user_message)
        n = int(m.group(1))
        if 1 <= n <= len(offers_list):
            doc = offers_list[n - 1]
            num = get_listing_contact(doc)
            reply = contact_reply(doc, num)
            log_conversation(user.id, uname, user_message, reply)
            save_memory(uid, "assistant", reply)
            await update.message.reply_text(clean_md(reply))
            return

    # ── عرض محدد بدون كلمة تفاصيل: "عرض رقم 3" ──
    if offers_list and not _INTENT_DETAILS.search(user_message) and not _INTENT_PHONE.search(user_message):
        m = _NUM_REF.search(user_message)
        if m:
            n = int(m.group(1))
            if 1 <= n <= len(offers_list):
                doc = offers_list[n - 1]
                reply = format_listing(doc, n) + "\n\n(تبي تفاصيله كاملة أو رقم التواصل؟ اكتب ذلك وسأعطيك إياه)"
                log_conversation(user.id, uname, user_message, reply)
                save_memory(uid, "assistant", reply)
                await update.message.reply_text(clean_md(reply))
                return

    save_memory(uid, "user", user_message)
    mem = memory_block(uid, limit=8)

    now_ptype = parse_property_type(user_message)
    now_budget = parse_budget(user_message)
    now_deal = parse_deal_type(user_message)

    # ── طلبات الزبائن للمسوق: "اعطيني طلبات الزبائن" ──
    if _INTENT_REQUESTS.search(user_message):
        reqs = get_customer_requests(limit=10)
        reply = format_customer_requests(reqs)
        log_conversation(user.id, uname, user_message, reply)
        save_memory(uid, "assistant", reply)
        await update.message.reply_text(clean_md(reply), disable_web_page_preview=True)
        return

    saved = load_user_filters(uid)
    budget = now_budget if now_budget is not None else saved.get("max_price")
    ptype = now_ptype or saved.get("property_type")
    deal = now_deal or saved.get("deal_type")
    hist = load_memory(uid, limit=6)
    for r in hist:
        if r["role"] != "user":
            continue
        if not budget:
            budget = parse_budget(r["content"])
        if not ptype:
            ptype = parse_property_type(r["content"])
        if not deal:
            deal = parse_deal_type(r["content"])
    save_user_filters(uid, ptype=ptype, deal=deal, budget=budget)

    # ── سؤال عدّ: "كم عرض عندكم" / "كم عدد الشقق" → إجابة مباشرة بالإحصاءات ──
    if _re.search(r"(كم\s+(عرض|عروض|عدد)|عدد\s+(العروض|الشقق|الفلل|الأراضي)|عندكم\s+\d|عندك\s+\d)", user_message):
        filters = {}
        if ptype:
            filters["property_type"] = ptype
        if deal:
            filters["deal_type"] = deal
        if budget:
            filters["max_price"] = budget
        results = rag_search(user_message, k=200, filters=filters or None)
        total, breakdown = listing_stats()
        n = len(results)
        if n:
            reply = f"عندنا حالياً {n} عرض مطابق لطلبك.\n\n{breakdown}\n\nتبي أشوفلك القائمة كاملة؟"
        else:
            reply = f"ما عندنا عروض مطابقة حالياً. الإحصاءات المتاحة:\n{breakdown}"
        log_conversation(user.id, uname, user_message, reply)
        save_memory(uid, "assistant", reply)
        await update.message.reply_text(clean_md(reply))
        return

    # ── طلب المزيد من عروض قائمة محفوظة: زودني / المزيد ──
    if (
        _INTENT_MORE.search(user_message)
        and not now_ptype and not now_budget and not now_deal
        and offers_list and len(offers_list) > 10
    ):
        page = ud.get("offers_page", 1)
        block = _offer_block(offers_list, start=10 * (page - 1) + 11, limit=10)
        if block:
            text = f"📋 وهذي عروض إضافية:\n\n{block}"
            ud["offers_page"] = page + 1
            log_conversation(user.id, uname, user_message, text)
            save_memory(uid, "assistant", text)
            await update.message.reply_text(clean_md(text))
            return
        await update.message.reply_text("هذي كل العروض المتاحة حالياً عندنا بهذا الطلب 👍")
        return

    # ── طلب القائمة/العروض مجدداً: "اين العروض" / "وين القائمة" → نعيد آخر عروض ──
    if offers_list and _re.search(r"(اين\s*(العروض|القائمة)|وين\s*(العروض|القائمة)|أين\s*(العروض|القائمة)|فين\s*(العروض|القائمة)|نزل\s*(القائمة|العروض)|رجع\s*(لي|عني)\s*(القائمة|العروض))", user_message, _re.IGNORECASE):
        block = _offer_block(offers_list, start=1, limit=10)
        if block:
            text = f"📋 تفضل، ها العروض:\n\n{block}"
            ud["offers_page"] = 1
            log_conversation(user.id, uname, user_message, text)
            save_memory(uid, "assistant", text)
            await update.message.reply_text(clean_md(text))
            return
        await update.message.reply_text("ما عندي قائمة عروض محفوظة. اكتب لي نوع العقار اللي تبي (مثلاً: شقة تمليك).")
        return

    # ── رسالة عامة/سؤال غير عقاري → رد مباشر من المعرفة أو بحث ويب ──
    _REQUEST = _re.compile(
        r"(ابغ|ابي|أبي|بغيت|عندك|عندكم|شوف|دور|عرض|شقة|شقق|فيلا|فلل|أرض|ارض|اراضي|"
        r"عمارة|عمائر|تمليك|إيجار|ايجار|بأجّر|أجّر|استأجر|اشتري|شراء|بيع|اسعار|سعر|"
        r"ميزانية|مطلوب|أنبح|بحي|حي|أحياء|منطقة|السعر)", _re.IGNORECASE)
    current_request = bool(_REQUEST.search(user_message)) or now_ptype or now_deal or now_budget

    if not current_request:
        filters = {}
        if budget:
            filters["max_price"] = budget
        if ptype:
            filters["property_type"] = ptype
        if deal:
            filters["deal_type"] = deal
        results = rag_search(user_message, k=8, filters=filters or None)
        total, breakdown = listing_stats()
        stats_block = (
            f"الإحصاءات الحقيقية حالياً:\n"
            f"- إجمالي العروض النشطة: {total}\n"
            f"- التفصيل حسب النوع: {breakdown}"
        )
        system = SYSTEM_PROMPT + "\n\n" + stats_block
        rules_block = get_rule_block()
        if rules_block:
            system += "\n\n" + rules_block
        if mem:
            system += "\n\n" + mem
        bot_reply = ask_ai(system, user_message)
        bot_reply = bot_reply or FALLBACK_REPLY

        web_m = _re.search(r"\[بحث:\s*(.+?)\]", bot_reply)
        if web_m:
            web_results = web_search(web_m.group(1).strip())
            if web_results:
                web_system = (SYSTEM_PROMPT
                              + "\n\nهذه نتائج بحث حديثة من الإنترنت عن:\n" + web_m.group(1).strip()
                              + "\n\n" + web_results
                              + "\n\nأعد كتابة ردنا النهائي للعميل بالعربية باستخدام هذه المعلومات بدقة، بلا وسوم [بحث:...].")
                final = ask_ai(web_system, user_message, max_tokens=700)
                if final and not _re.search(r"\[بحث:", final):
                    bot_reply = final
        log_conversation(user.id, uname, user_message, bot_reply)
        save_memory(uid, "assistant", bot_reply)
        await update.message.reply_text(clean_md(bot_reply))
        return

    # ── إذا طلب كل العروض برسالة واحدة → نعرضها كلها (قبل الأسئلة التوضيحية) ──
    if _INTENT_ALL.search(user_message):
        filters = {}
        if budget:
            filters["max_price"] = budget
        if ptype:
            filters["property_type"] = ptype
        if deal:
            filters["deal_type"] = deal
        # "كل العروض" → نعرض كل ما يطابق الفلاتر، لا بحث نصي (جملة "وريني كلها" لا تطابق)
        all_res = rag_search("", k=60, filters=filters or None) or rag_search(user_message, k=60, filters=filters or None)
        if not all_res and shown_raw_ids(uid):
            all_res = rag_search("", k=60, filters={k: v for k, v in filters.items() if k != "exclude_raw_ids"} or None)
        all_docs = [d for d, _ in all_res]
        if not all_docs:
            await update.message.reply_text("للأسف ما عندنا عروض مطابقة متاحة حالياً.")
            return
        ud["last_offers"] = all_docs
        ud["last_listing"] = all_docs[0]
        ud["offers_page"] = 1
        text = f"📋 هذه كل العروض المتاحة ({len(all_docs)}):\n\n" + "\n".join(
            _compact(d, i) for i, d in enumerate(all_docs[:45], 1)
        )
        log_conversation(user.id, uname, user_message, text[:4000])
        save_memory(uid, "assistant", text)
        await update.message.reply_text(clean_md(text))
        return

    # ── العرض: بايثون يجلب العروض النشطة والبيانات الحقيقية، والنموذج يختار الأرقام فقط ──
    all_docs = rag_all()   # كل العروض النشطة من المفروز
    # تصفية أولية بالنوع/التعامل/الميزانية المعروفة حتى لا يرى النموذج غير المطابق للنوع
    kptype = ptype or saved.get("property_type")
    kdeal = deal or saved.get("deal_type")
    candidates = all_docs
    if kptype:
        candidates = [d for d in candidates if (d.get("property_type") or "") == kptype]
    if kdeal:
        _dd = {"تمليك": "شراء"}.get(kdeal, kdeal)
        candidates = [d for d in candidates if (d.get("deal_type") or "") == _dd or (d.get("deal_type") or "") == kdeal]
    if not candidates:
        candidates = all_docs

    # أولوية النطاق الجغرافي: "شمال جدة" → أحياء شمال جدة تتصدر المرشحين أولاً
    _hint = area_hint(user_message)
    if _hint:
        _area = next((h for h in _hint if "جدة" in h), None)
        if _area and candidates:
            candidates = prioritize_by_area(candidates, _area)

    candidates = list(candidates)[:18]

    # إثراء كل مرشح بالنص الأصلي (مختصر) ليطابق النموذج الصياغة الحرفية للإعلان — دفعة واحدة
    _raws = get_raw_texts([d.get("raw_id") for d in candidates])
    for _i, _d in enumerate(candidates):
        if not isinstance(_d, dict):
            _d = dict(_d)
            candidates[_i] = _d
        if not _d.get("raw_text"):
            _rt = _raws.get(_d.get("raw_id"))
            if _rt:
                _d["raw_text"] = _rt[:200]

    ud["last_offers"] = candidates
    if candidates:
        ud["last_listing"] = candidates[0]
    ud["offers_page"] = 1

    if not candidates:
        stats_block = f"الإحصاءات الحقيقية حالياً:\n- إجمالي العروض النشطة: 0"
        system = SYSTEM_PROMPT + "\n\n" + stats_block + ("\n\n" + get_rule_block() if get_rule_block() else "")
        if mem:
            system += "\n\n" + mem
        bot_reply = ask_ai(system, user_message) or FALLBACK_REPLY
        log_conversation(user.id, uname, user_message, bot_reply)
        save_memory(uid, "assistant", bot_reply)
        await update.message.reply_text(clean_md(bot_reply))
        return

    total, breakdown = listing_stats()
    # القائمة المرقمة تُحقن للنموذج (المصدر الوحيد الحقيقي)
    context_block = (
        f"لكل عرض رقم ثابت انطلاقاً من 1 لا يتغير. العروض الحقيقية الكاملة (ن = {len(candidates)}):\n"
        + "\n".join(_ctx_line(d, i) for i, d in enumerate(candidates, 1))
    )
    stats_block = (
        f"الإحصاءات الحقيقية حالياً:\n"
        f"- إجمالي العروض النشطة: {total}\n"
        f"- التفصيل حسب النوع: {breakdown}"
    )
    system = SYSTEM_PROMPT + "\n\n" + stats_block
    rules_block = get_rule_block()
    if rules_block:
        system += "\n\n" + rules_block
    if mem:
        system += "\n\n" + mem
    if context_block:
        system += "\n\n" + context_block

    select_system = system + "\n\n" + JEDDAH_DISTRICTS_REF + (
        "\n\nافهم طلب العميل بنفسك (بما فيه الكتابة العامية بلا همزات: ارضي، ارض، ابحر...)."
        "\nمهمتك الآن اختيار العروض المطابقة فقط. أخرج إجابتك كـ JSON بالصيغة التالية حصراً ولا شيء غيرها:"
        '\n{"chosen": [أرقام العروض المختارة من القائمة المرقمة فقط], "reply": "الترحيب والرسالة للعميل"}'
        "\nالقواعد:"
        "\n- chosen: مصفوفة أرقام تدل على أرقام العروض في القائمة المرقمة (من 1 إلى ن). اختر العروض المطابقة لنوع الطلب ونطاقه فقط."
        "\n- التزام صارم بالنوع المطلوب: لا تختار أبداً عرضاً من نوع مختلف (أرض مقابل عمارة، شقة مقابل أرض...)."
        "\n- الالتزام بالنطاق المطلوب: لا تختار عروضاً خارج الحي/المنطقة التي طلبها ما لم يطلب تكبيره."
        "\n- ما قبل {reply}: اكتب فيها الترحيب القصير وسؤال العميل فقط، ممنوع تماماً كتابة أي بيانات عروض أو أرقام أو أسعار فيها — القائمة سيُرسلها النظام تلقائياً بعد تنسيقها."
        "\n- إذا لم يوجد أي عرض مطابق → أخرج {\"chosen\": []} مع reply تعتذر بلطف وتذكر ما هو متاح بنفس النوع إن وُجد في نطاق أوسع."
        "\n- لا تختلق أي رقم خارج القائمة، ولا أي سعر أو حي أو نوع غير موجود حرفياً فيها."
    )
    select_out = ask_ai(select_system, user_message, max_tokens=600)
    if not select_out:
        select_out = '{"chosen": []}'

    # استخراج الأرقام المختارة من الرد بصيغة JSON
    import json as _json
    chosen_nums = []
    intro = ""
    try:
        m = _re.search(r"\{.*\}", select_out, _re.DOTALL)
        if m:
            data = _json.loads(m.group(0))
            chosen_nums = [int(n) for n in (data.get("chosen") or [])]
            intro = str(data.get("reply") or "").strip()
    except Exception:
        chosen_nums = []
        intro = ""

    # تأكيد الأرقام ضمن النطاق فقط
    chosen = []
    for n in chosen_nums:
        if isinstance(n, int) and 1 <= n <= len(candidates):
            chosen.append(candidates[n - 1])

    if chosen:
        ud["last_offers"] = chosen
        ud["last_listing"] = chosen[0]
        mark_listing_shown(uid, [d.get("raw_id") for d in chosen])
        # بناء القائمة من البيانات الحقيقية في القاعدة — لا اختلاق
        body = "\n".join(_compact(d, i) for i, d in enumerate(chosen, 1))
        bot_reply = (intro + "\n\n" if intro else "") + body + "\n\nأي عرض منها يروق لك؟ خبرني رقمه وأجيك."
    else:
        if intro:
            bot_reply = intro + "\n\nما عندنا عرض مطابق بهذي المواصفات حالياً. قل لي إذا تبغى نوسّع البحث أو نوع ثاني."
        else:
            bot_reply = "ما عندنا عرض مطابق بهذي المواصفات حالياً. قل لي إذا تبغى نوسّع البحث أو نوع ثاني."

    log_conversation(user.id, uname, user_message, bot_reply)
    save_memory(uid, "assistant", bot_reply)
    await update.message.reply_text(clean_md(bot_reply))


def log_conversation(user_id, user_name, message, bot_reply):
    try:
        conn = get_sorted_conn()
        conn.execute(
            "CREATE TABLE IF NOT EXISTS conversations ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " user_id TEXT, user_name TEXT, message TEXT, bot_reply TEXT,"
            " reviewed INTEGER DEFAULT 0, review_note TEXT,"
            " ts TEXT DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO conversations (user_id, user_name, message, bot_reply) VALUES (?, ?, ?, ?)",
            (str(user_id), user_name, message[:2000], bot_reply[:4000]),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"log err: {e}", flush=True)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "browse":
        all_docs = rag_all()
        text = format_results([(d, 0.0) for d in all_docs], 8) if all_docs else "لا توجد عروض حالياً."
        text = "🏠 أحدث العروض:\n\n" + text + "\n\nتبي تفاصيل أكثر؟ اكتب اسم الحي أو نوع العقار."
        await query.edit_message_text(clean_md(text))
    elif query.data == "contact":
        await query.edit_message_text(
            "📞 للتواصل مع مكتب عقاري:\n\n"
            "اكتب اسمك ورقم جوالك وسنوصلك بأقرب مكتب مناسب."
        )
    elif query.data.startswith("det:"):
        raw_id = query.data.split(":", 1)[1]
        raw = get_raw_text(int(raw_id))
        if raw:
            reply = f"📄 النص الخام كما ورد في القناة:\n\n{raw}\n\n{BUSINESS_LINE}"
        else:
            reply = "لا يتوفر نص خام لهذا العرض."
        await query.edit_message_text(clean_md(reply))
    elif query.data.startswith("num:"):
        raw_id = query.data.split(":", 1)[1]
        # نبحث عن العرض بالـ raw_id
        doc = None
        for d in rag_all():
            if str(d.get("raw_id")) == raw_id:
                doc = d
                break
        num = get_listing_contact(doc) if doc else BUSINESS_CONTACT
        reply = contact_reply(doc, num)
        await query.edit_message_text(clean_md(reply))


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Exception: {context.error}", exc_info=context.error)


# ──────────────────────────────────────────────
#  التشغيل
# ──────────────────────────────────────────────
def run_bot():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN غير موجود! اضفه في ملف .env")
        return

    logger.info("جاري تشغيل بوت فذ العقارية (RAG + DeepSeek)...")
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("offers", cmd_offers))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    run_bot()