# -*- coding: utf-8 -*-
"""
بحث ويب خفيف — يستخدمه بوت فذ للأسئلة العامة والمعلوماتية
(أحياء، مواقع، اتجاهات، معلومات عامة) التي ليست ضمن قاعدة العقارات.
"""
import re
import requests
from html import unescape

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def _clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def web_search(query, num=5):
    """بحث عبر DuckDuckGo HTML (بدون مفتاح API) — يعيد نص ملخص أو فارغ عند الفشل"""
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": UA},
            timeout=15,
        )
        resp.raise_for_status()
        html = resp.text
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', html, re.S)
        snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html, re.S)
        results = []
        for i, t in enumerate(titles[:num]):
            t = _clean(t)
            s = _clean(snips[i]) if i < len(snips) else ""
            if t:
                results.append(f"- {t}: {s}" if s else f"- {t}")
        return "\n".join(results) if results else ""
    except Exception as e:
        print(f"web_search err: {e}", flush=True)
        return ""