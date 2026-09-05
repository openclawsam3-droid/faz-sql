# -*- coding: utf-8 -*-
"""
المراقب الذكي للجودة — فذ العقارية.
يقيم الردود غير المراجعة بالذكاء الاصطناعي، يسجل التقييم، وفي الردود الضعيفة:
- ينبّه بوت المشرف فوراً
- يولّد قاعدة تحسين تلقائياً ويضيفها لبرومبت بوت فذ (تحسّن يلقائي)
التشغيل: cron كل 10 دقائق — ./venv/bin/python quality_monitor.py
"""
import os
import sys
import json
import re

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import get_sorted_conn, migrate
from deepseek_client import ask_ai
from admin_bot import notify_admins, save_rule

# ترقية بنية القاعدة (أعمدة المراجعة) قبل الاستعلام
migrate()

LIMIT = 20  # كم محادثة نقيّم في الجولة الواحدة
ALERT_THRESHOLD = 6  # إرسال تنبيه للمشرف عند الدرجة ≤ هذا الحد


def db():
    conn = get_sorted_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS conversation_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_id INTEGER UNIQUE,
            score INTEGER,
            verdict TEXT,
            reason TEXT,
            suggested_reply TEXT,
            reviewed_at TEXT DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    return conn


def pending_conversations(conn, limit=LIMIT):
    rows = conn.execute(
        """SELECT c.id, c.user_id, c.user_name, c.message, c.bot_reply, c.ts
           FROM conversations c
           WHERE c.reviewed=0
             AND (c.bot_reply IS NOT NULL AND c.bot_reply <> '')
             AND c.user_id NOT IN ('0')  -- استثناء محادثات النظام
           ORDER BY c.id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return rows


def evaluate(conversation):
    """تقييم رد بوت واحد بذكاء اصطناعي — يعيد (score، verdict، reason، suggested)"""
    system = """أنت مشرف جودة لشركة عقارية سعودية. راجع رد البوت على رسالة العميل.
قيم رد البوت بالنقاط التالية واحكم بدقة عالية:
- هل أجاب البوت على طلب العميل فعلياً؟ (الأهم)
- هل كرر عرضاً سبق عرضه؟ هل عرض نوعاً مختلفاً عما طلب العميل (شقق/أراضي/فلل...)؟
- هل فهم سياق المحادثة؟
- هل اختلق أرقاماً أو عروضاً غير مؤكدة؟
- هل استمر بالمحادثة بسؤال منطقي في النهاية؟

أجب بصيغة JSON فقط:
{"score": 1-10, "verdict": "ممتاز"|"جيد"|"متوسط"|"ضعيف", "reason": "شرح المشكلة بجملة",
 "suggested_reply": "نص أفضل مقترح في الحالات الضعيفة فقط أو فارغ"}"""

    user = f"رسالة العميل:\n{conversation['message'][:800]}\n\nرد البوت:\n{conversation['bot_reply'][:1500]}"

    out = ask_ai(system, user, max_tokens=400, temperature=0.2)
    if not out:
        return None
    try:
        if "```" in out:
            out = out.split("```")[1]
            if out.startswith("json"):
                out = out[4:]
        data = json.loads(out.strip())
        if isinstance(data, list):
            data = data[0]
        return data
    except (json.JSONDecodeError, ValueError):
        # رد غير JSON — نحاول التقاط score من النص
        import re
        m = re.search(r"(\d{1,2})", out)
        return {"score": int(m.group(1)) if m else 5, "verdict": "غير معروف",
                "reason": out[:300], "suggested_reply": ""}


def generate_rule(reason, suggested, conv):
    """توليد قاعدة تحسين قصيرة تمنع تكرار الخطأ — من وصف المشكلة"""
    system = """أنت خبير في تحسين ردود بوت عقاري سعودي. من الخطأ الموصوف، اكتب قاعدة تحسين واحدة فقط:
- قصيرة واضحة، بصيغة أمر/امتناع موجَّه للبوت
- لا تذكر تفاصيل خاصة بمحادثة معينة (أسماء أو أرقام)
- لا تُكرر قواعد عامة موجودة؛ ركّز على سبب الخطأ النوعي
أجب سطراً واحداً فقط بدون مقدمات."""

    prompt = (
        f"الخطأ في رد البوت:\n{reason}\n\n"
        f"(ملاحظة إضافية: {conv['user_name']} — المفترض المعروض)\n"
        f"الاقتراح المتاح: {suggested}\n"
    )
    out = ask_ai(system, prompt[:1200], max_tokens=120, temperature=0.2)
    if not out:
        return None
    rule = " ".join(out.split())[:200].strip(" .،،؛")
    if len(rule) < 8:
        return None
    return rule


def add_rule_if_new(conn, rule):
    """إضافة القاعدة إن لم تكن موجودة تقريباً — يمنع التكرار"""
    existing = conn.execute("SELECT rule FROM bot_rules").fetchall()
    for r in existing:
        a, b = set(rule.split()), set(r["rule"].split())
        if a and b:
            overlap = len(a & b) / max(len(a), len(b))
            if overlap >= 0.4:
                return False
    conn.execute(
        "INSERT INTO bot_rules (rule, created_by) VALUES (?,?)",
        (rule, "تلقائي"),
    )
    return True


def main():
    conn = db()
    convs = pending_conversations(conn)
    if not convs:
        print("لا توجد محادثات بانتظار المراجعة")
        conn.close()
        return

    flagged = 0
    for row in convs:
        conv = dict(row)
        ev = evaluate(conv)
        if not ev:
            continue
        score = int(ev.get("score", 5))
        verdict = ev.get("verdict", "غير معروف")
        reason = (ev.get("reason") or "")[:500]
        suggested = (ev.get("suggested_reply") or "")[:1000]
        conn.execute(
            """INSERT OR REPLACE INTO conversation_reviews
               (conv_id, score, verdict, reason, suggested_reply)
               VALUES (?,?,?,?,?)""",
            (conv["id"], score, verdict, reason, suggested),
        )
        conn.execute("UPDATE conversations SET reviewed=1 WHERE id=?", (conv["id"],))
        if score <= ALERT_THRESHOLD:
            flagged += 1
            print(f"[ضعيف {score}/10] #{conv['id']} {conv['user_name']} | {reason[:80]}", flush=True)
            # تنبيه فوري لبوت المشرف مع تفاصيل + اقتراح الإصلاح
            try:
                from admin_bot import notify_admins
                alert = (
                    f"⚠️ رد ضعيف: {score}/10 ({verdict}) — محادثة #{conv['id']}\n"
                    f"الزبون: {conv['user_name']}\n"
                    f"الوقت: {conv['ts']}\n"
                    f"السبب: {reason}\n\n"
                    f"رسالة الزبون: {(conv['message'] or '')[:400]}\n\n"
                    f"رد البوت: {(conv['bot_reply'] or '')[:600]}\n\n"
                    f"اقتراح إصلاح: {(suggested or '—')[:600]}\n\n"
                    f"رد بقاعدة تصحيح مثل: لا تكرر عرضاً سبق عرضه على هذا الزبون"
                )
                notify_admins(alert, conn=conn)
            except Exception as e:
                print(f"تنبيه المشرف فشل: {e}", flush=True)

            # ├─ تحسين تلقائي: توليد قاعدة تصحيح وأضفها لبرومبت بوت فذ
            try:
                rule = generate_rule(reason, suggested, conv)
                if rule:
                    add_rule_if_new(conn, rule)
                    print(f"  + قاعدة تلقائية: {rule[:60]}", flush=True)
            except Exception as e:
                print(f"فشل توليد القاعدة: {e}", flush=True)
        conn.commit()

    stats = conn.execute(
        "SELECT COUNT(*) c, ROUND(AVG(score),1) a FROM conversation_reviews WHERE reviewed_at >= date('now')"
    ).fetchone()
    print(f"تم تقييم {len(convs)} محادثة | ضعيفة: {flagged} | متوسط اليوم: {stats['a']} ({stats['c']} مراجعة)")
    conn.close()


if __name__ == "__main__":
    main()