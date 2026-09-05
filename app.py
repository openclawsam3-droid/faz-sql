# -*- coding: utf-8 -*-
"""
لوحة فذ العقارية — Streamlit
- الإحصائيات العامة
- المحادثات الحية (كل تبادل زبون/بوت)
- مراجعة جودة الردود (من quality_monitor)
- إدارة العروض
"""
import os
import sys

import streamlit as st

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from database import get_sorted_conn, migrate, get_raw_conn
from rag import rag_stats, fetch_listings

# ترقية بنية القاعدة قبل القراءة
migrate()

st.set_page_config(page_title="فذ العقارية", page_icon="🏠", layout="wide")


# ──────────────────────────────────────────────
#  البيانات
# ──────────────────────────────────────────────
@st.cache_data(ttl=30)
def load_stats():
    conn = get_sorted_conn()
    total_offers = conn.execute(
        "SELECT COUNT(*) FROM sorted_listings WHERE listing_type='عرض' AND status='نشط'"
    ).fetchone()[0]
    by_type = conn.execute(
        "SELECT property_type, COUNT(*) c FROM sorted_listings "
        "WHERE listing_type='عرض' AND status='نشط' GROUP BY property_type ORDER BY c DESC"
    ).fetchall()
    today_conv = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE date(ts)=date('now')"
    ).fetchone()[0]
    total_conv = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    user_count = conn.execute("SELECT COUNT(DISTINCT user_id) FROM conversations").fetchone()[0]
    conn.close()
    return {
        "offers": total_offers,
        "by_type": {r["property_type"]: r["c"] for r in by_type},
        "today_conv": today_conv,
        "total_conv": total_conv,
        "users": user_count,
    }


@st.cache_data(ttl=15)
def load_reviews():
    conn = get_sorted_conn()
    rows = conn.execute(
        """SELECT r.conv_id, r.score, r.verdict, r.reason, r.suggested_reply, r.reviewed_at,
                  c.user_name, c.message, c.bot_reply, c.ts
           FROM conversation_reviews r
           JOIN conversations c ON c.id = r.conv_id
           ORDER BY r.score ASC, r.reviewed_at DESC
           LIMIT 100"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@st.cache_data(ttl=15)
def load_conversations():
    conn = get_sorted_conn()
    rows = conn.execute(
        """SELECT id, user_id, user_name, message, bot_reply, ts
           FROM conversations
           ORDER BY ts DESC, id DESC LIMIT 300"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@st.cache_data(ttl=60)
def load_raw_map():
    conn = get_raw_conn()
    rows = conn.execute("SELECT id, raw_text FROM raw_messages").fetchall()
    conn.close()
    return {r["id"]: r["raw_text"] for r in rows}


# ──────────────────────────────────────────────
#  الترويسة
# ──────────────────────────────────────────────
st.title("🏠 فذ العقارية — لوحة التحكم")
stats = load_stats()

col1, col2, col3, col4 = st.columns(4)
col1.metric("إجمالي العروض النشطة", stats["offers"])
col2.metric("محادثات اليوم", stats["today_conv"])
col3.metric("إجمالي المحادثات", stats["total_conv"])
col4.metric("العملاء", stats["users"])

st.write("**العروض حسب النوع:**")
cols = st.columns(len(stats["by_type"]) or 1)
if stats["by_type"]:
    for col, (ptype, cnt) in zip(cols, stats["by_type"].items()):
        col.metric(ptype, cnt)
else:
    st.info("لا توجد بيانات")


# ──────────────────────────────────────────────
#  تبويبات
# ──────────────────────────────────────────────
tab1, tab2, tab3, tab4 = st.tabs(["📋 المحادثات الحية", "⭐ مراجعة الجودة", "🏘️ العروض", "🔍 الخام ↔ المفروز"])

with tab1:
    st.subheader("المحادثات (كل العملاء — الأحدث أولاً)")
    convs = load_conversations()
    if not convs:
        st.info("لا توجد محادثات مسجلة بعد")
    for r in convs:
        with st.expander(f"#{r['id']} — {r['user_name']} — {r['ts']}"):
            st.caption(f"معرّف العميل: {r['user_id']}")
            st.write("**الزبون:**")
            st.write(r["message"] or "—")
            st.write("**البوت:**")
            st.write(r["bot_reply"] or "—")

with tab2:
    st.subheader("مراجعة جودة الردود (الأضعف أولاً)")
    reviews = load_reviews()
    if not reviews:
        st.info("لم تُقيّم محادثات بعد — شغّل quality_monitor.py أولاً أو بعد تفحيط محادثات جديدة")
    for r in reviews:
        color = "🟢" if r["score"] >= 7 else ("🟡" if r["score"] >= 5 else "🔴")
        with st.expander(f"{color} {r['score']}/10 — {r['verdict']} — #{r['conv_id']} ({r['user_name']})"):
            st.write(f"**السبب:** {r['reason']}")
            st.write(f"**رسالة الزبون:** {r['message']}")
            st.write(f"**رد البوت:** {r['bot_reply']}")
            if r["suggested_reply"]:
                st.write("**اقتراح إصلاح:**")
                st.success(r["suggested_reply"])
            st.caption(f"قيّم في: {r['reviewed_at']}")

with tab3:
    st.subheader("العروض النشطة")
    docs = fetch_listings()
    if not docs:
        st.info("لا توجد عروض")
    else:
        import pandas as pd
        df = pd.DataFrame([{
            "النوع": d.get("listing_type") or "",
            "العقار": d.get("property_type") or "",
            "المدينة": d.get("city") or "",
            "الحي": d.get("district") or "",
            "السعر": d.get("price"),
            "التعامل": d.get("deal_type") or "",
            "غرف": d.get("rooms"),
            "حمامات": d.get("bathrooms"),
            "مفروش/ملحق": d.get("finishing") or "",
        } for d in docs[:200]])
        st.dataframe(df, use_container_width=True, height=420)
        st.caption(f"إجمالي السجلات: {len(docs)} (معروض أول 200)")

with tab4:
    st.subheader("مقارنة الخام بالمفروز (للتدقيق والمراجعة)")
    docs = fetch_listings()
    try:
        raw_map = load_raw_map()
    except Exception as e:
        st.error(f"تعذر تحميل النصوص الخام: {e}")
        raw_map = {}
    st.write(f"إجمالي السجلات: {len(docs)} | النصوص الخام المحمولة: {len(raw_map)}")
    kind = st.radio("نوع السجل:", ["كل السجلات", "عروض فقط", "طلبات فقط"], horizontal=True)
    if kind == "عروض فقط":
        view = [d for d in docs if d.get("listing_type") == "عرض"]
    elif kind == "طلبات فقط":
        view = [d for d in docs if d.get("listing_type") == "طلب"]
    else:
        view = docs
    if view:
        import pandas as pd
        sdf = pd.DataFrame([{
            "النوع": d.get("listing_type") or "",
            "العقار": d.get("property_type") or "",
            "الحي": d.get("district") or "",
            "السعر": d.get("price"),
            "يوجد خام؟": "نعم" if d.get("raw_id") in raw_map else "لا",
        } for d in view[:200]])
        st.dataframe(sdf, use_container_width=True, height=300)
        opts = [f"{i+1}. {d.get('listing_type')} | {d.get('property_type') or '—'} | {d.get('district') or '—'} | {d.get('price') or 'بدون سعر'}" for i, d in enumerate(view)]
        choice = st.selectbox("اختر سجلاً لعرض تفاصيله (الخام والمفروز):", opts)
        idx = opts.index(choice)
        d = view[idx]
        rid = d.get("raw_id")
        raw = raw_map.get(rid, "(لا يوجد نص خام مطابق)")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**المفروز (الأعمدة المستخرجة):**")
            for k, v in d.items():
                if k in ("raw_id", "message_id", "id"):
                    continue
                if v in (None, "", 0):
                    continue
                st.write(f"- {k}: {v}")
        with c2:
            st.markdown(f"**الخام (raw_id={rid}):**")
            st.text_area("النص الأصلي", raw, height=360)
    else:
        st.info("لا توجد سجلات مطابقة")