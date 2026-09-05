"""
RAG — محرك استرجاع العروض من قاعدة المفروز (sorted.db) للبوت.
تطبيع عربي + فهرس مقلوب BM25 + فلاتر (نوع/حي/سعر/غرف/تعامل).
البناء: فهرس في الذاكرة يُبنى عند أول استخدام ويُتحدّث عند الإضافة الجديدة.
"""
import math
import os
import re
import sys
import unicodedata

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_sorted_conn

# ── تطبيع عربي ──
_ALEF = re.compile("[أإآا]")
_HAMZA = re.compile("ة")
_LAMALEF = re.compile("لا")
_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670]")
_HAMZA2 = re.compile("[\u0623\u0625\u0622]")
_TAMARBUTA = re.compile("ة$")


def normalize_ar(text):
    """تطبيع: إزالة التشكيل، توحيد الألف والهمزة والتاء المربوطة"""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", str(text))
    t = _DIACRITICS.sub("", t)
    t = _HAMZA2.sub("ا", t)
    t = _LAMALEF.sub("لا", t)
    t = _ALEF.sub("ا", t)
    t = _TAMARBUTA.sub("ه", t)
    return t


# مرادفات: توحيد المصطلحات التي تعني نفس الشيء
_SYNONYMS = {
    "تمليك": "شراء",
    "تملك": "شراء",
    "بيع": "شراء",
    "للبيع": "شراء",
    "ببيع": "شراء",
    "شراء": "شراء",
    "أجار": "ايجار",
    "ايجار": "ايجار",
    "إيجار": "ايجار",
    "للايجار": "ايجار",
    "بالايجار": "ايجار",
    "استئجار": "ايجار",
}


def tokenize(text):
    """تقسيم النص لكلمات، مع توحيد المرادفات وحفظ الأرقام"""
    words = re.findall(r"[\w\u0600-\u06FF]+", normalize_ar(text).lower())
    out = []
    for w in words:
        if not w or w in _STOP_WORDS:
            continue
        w = _SYNONYMS.get(w, w)
        out.append(w)
    return out


_STOP_WORDS = {
    "في", "من", "على", "الى", "إلى", "عند", "الا", "مع", "او", "أو", "و",
    "اللي", "بعد", "قبل", "كان", "هذا", "هذي", "مثل", "عن", "لا", "ما",
    "سعر", "السعر", "للايجار", "بإيجار", "بالايجار", "ريال",
    "متر", "سنتين", "للإيجار", "العرض", "عرض", "أبي", "ابي", "أبغى", "ابغى",
}


# ── حقل وزن لكل سمة ──
_FIELD_WEIGHTS = {
    "district": 6.0,
    "property_type": 5.0,
    "city": 6.0,
    "deal_type": 4.0,
    "features": 2.0,
    "short_desc": 1.5,
    "channel": 1.0,
}


def fetch_listings():
    """قراءة عناصر المفروز — العروض النشطة فقط (لا طلبات الزبائن)"""
    conn = get_sorted_conn()
    rows = conn.execute(
        """SELECT id, raw_id, message_id, listing_type, property_type, deal_type, city, district,
                  rooms, bathrooms, kitchen, rooftop, annex,
                  driver_room, maid_room, finishing,
                  price, price_unit, features, short_desc, channel, owner_contact
           FROM sorted_listings
           WHERE status='نشط' AND listing_type='عرض'
           ORDER BY id"""
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        out.append(d)
    return out


class RagIndex:
    """فهرس مقلوب BM25 مع مخبأ يتحقق من آخر إدخال"""

    def __init__(self, refresh_sec=60):
        self.refresh_sec = refresh_sec
        self._last_check = 0
        self._max_id = -1
        self._doc_ids = []
        self._docs = {}
        self._postings = {}   # term -> {doc_index: tf}
        self._doc_len = []    # عدد الكلمات في كل وثيقة
        self._avg_len = 1.0
        self._k1 = 1.5
        self._b = 0.75
        self._N = 0
        self._freq = {}       # term -> عدد الوثائق الذي فيه
        self._pool = False
        self.refresh(force=True)

    # ── بناء/تحديث ──
    def _current_max_id(self):
        try:
            conn = get_sorted_conn()
            row = conn.execute("SELECT MAX(id) FROM sorted_listings").fetchone()
            conn.close()
            return row[0] if row and row[0] else -1
        except Exception:
            return -1

    def refresh(self, force=False):
        now = sys.maxsize  # استخدام خارج do_last للتحديث اليدوي
        if not force:
            import time as _t
            now = _t.time()
            if now - self._last_check < self.refresh_sec:
                return
        new_max = self._current_max_id()
        if new_max > self._max_id:
            self._rebuild()
            self._max_id = new_max
        self._last_check = now if not force else 0

    def _rebuild(self):
        docs = fetch_listings()
        self._docs = {}
        self._doc_ids = []
        self._postings = {}
        self._doc_len = []
        self._freq = {}
        self._N = 0

        for doc in docs:
            idx = self._N
            doc_id = doc["id"]
            self._docs[doc_id] = doc
            self._doc_ids.append(doc_id)

            tokens = []
            for field, weight in _FIELD_WEIGHTS.items():
                val = doc.get(field) or ""
                if isinstance(val, (list, tuple)):
                    val = " ".join(str(x) for x in val)
                tokens.extend([tok for tok in tokenize(val)])
            # أرقام ككلمات (لا تُطبع لكن تُفتّش)
            for num_field in ("rooms", "price"):
                v = doc.get(num_field)
                if v is not None:
                    tokens.append(str(v))
            tokens = tokens[:200]
            self._doc_len.append(len(tokens) or 1)
            seen = set()
            for t in tokens:
                if t not in self._postings:
                    self._postings[t] = {}
                cur = self._postings[t].get(idx, 0)
                self._postings[t][idx] = cur + 1
                if idx not in seen:
                    seen.add(idx)
            self._N += 1

        total = sum(self._doc_len)
        self._avg_len = (total / self._N) if self._N else 1.0
        self._freq = {t: len(postings) for t, postings in self._postings.items()}
        self._pool = True

    # ── استرجاع ──
    def search(self, query, k=8, filters=None):
        """بحث BM25 مع فلاتر. filters: dict {deal_type, city, district, property_type, min_price, max_price, rooms}"""
        self.refresh()
        tokens = tokenize(query)
        if not tokens:
            return self._all_by_filter(filters, k)

        filters = filters or {}
        scores = {}
        matched_idx = {}

        n = self._N
        if n == 0:
            return []

        # فهرس وثائق مطابقة الفلاتر أولاً
        for idx, doc_id in enumerate(self._doc_ids):
            doc = self._docs[doc_id]
            if self._passes_filters(doc, filters):
                matched_idx[idx] = doc_id

        if not matched_idx:
            return []

        doc_len = self._doc_len
        avg = self._avg_len
        k1, bnd = self._k1, self._b

        for t in tokens:
            df = self._freq.get(t, 0)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            postings = self._postings.get(t, {})
            for idx in postings:
                if idx not in matched_idx:
                    continue
                tf = postings[idx]
                dl = doc_len[idx]
                denom = tf + k1 * (1 - bnd + bnd * (dl / avg))
                scores[idx] = scores.get(idx, 0.0) + idf * (tf * (k1 + 1)) / denom

        ranked = sorted(scores.keys(), key=lambda i: -scores[i])
        result = []
        for idx in ranked[:k]:
            result.append((self._docs[matched_idx[idx]], scores[idx]))
        if not result and filters:
            # لا مطابقة نصية (كلمات stop words مثلاً) → نعيد عروضاً بالفلاتر فقط
            return self._all_by_filter(filters, k)
        return result

    def _all_by_filter(self, filters, k):
        out = []
        for doc_id in self._doc_ids:
            doc = self._docs[doc_id]
            if self._passes_filters(doc, filters):
                out.append((doc, 0.0))
            if len(out) >= k:
                break
        return out

    def _passes_filters(self, doc, filters):
        if filters.get("deal_type") and doc.get("deal_type"):
            f = _SYNONYMS.get(filters["deal_type"], filters["deal_type"])
            d = _SYNONYMS.get(doc["deal_type"], doc["deal_type"])
            if f != d:
                return False
        if filters.get("city") and doc.get("city"):
            f = filters["city"]; d = doc["city"] or ""
            if f not in d and d not in f:
                return False
        if filters.get("property_type") and doc.get("property_type"):
            f = filters["property_type"]; d = doc["property_type"] or ""
            if f not in d and d not in f:
                return False
        if filters.get("district") and doc.get("district"):
            f = filters["district"]; d = doc["district"] or ""
            if f not in d and d not in f:
                return False
        if filters.get("rooms"):
            if (doc.get("rooms") or 0) < int(filters["rooms"]):
                return False
        if filters.get("min_price") is not None:
            p = doc.get("price")
            if p is None or p < filters["min_price"]:
                return False
        if filters.get("max_price") is not None:
            p = doc.get("price")
            if p is not None and p > filters["max_price"]:
                return False
        if filters.get("exclude_raw_ids"):
            if doc.get("raw_id") in filters["exclude_raw_ids"]:
                return False
        return True

    def stats(self):
        self.refresh()
        return self._N

    def all(self):
        self.refresh()
        return [self._docs[d] for d in self._doc_ids]


# ── واجهة عامة ──
_index = None


def get_index():
    global _index
    if _index is None:
        _index = RagIndex()
    return _index


def rag_search(query, k=8, filters=None):
    return get_index().search(query, k=k, filters=filters)


def rag_stats():
    return get_index().stats()


def rag_all():
    return get_index().all()


if __name__ == "__main__":
    idx = RagIndex(force=True)
    print(f"وثائق في الفهرس: {idx.stats()}")
    q = input("بحث: ").strip() or "شقة جدة"
    res = rag_search(q, k=5)
    for doc, score in res:
        print(f"  [{score:.3f}] {doc.get('property_type')} | {doc.get('district')} | {doc.get('city')} | {doc.get('price')} | {doc.get('short_desc')}")