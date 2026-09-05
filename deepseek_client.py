"""
عميل DeepSeek API — قلب النظام الجديد.
يستخدم DeepSeek فقط (بدون Gemini ولا OpenRouter ولا Ollama):
- تصنيف الإعلانات (الفرز)
- الرد على العملاء
"""
import os
import json
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

DEEPSEEK_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
API_URL = "https://api.deepseek.com/v1/chat/completions"


def _chat(messages, max_tokens=800, temperature=0.3, timeout=60):
    """استدعاء أساسي لواجهة DeepSeek"""
    if not DEEPSEEK_KEY:
        print("DEEPSEEK_API_KEY غير موجود في .env", flush=True)
        return None
    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "stream": False,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except requests.exceptions.HTTPError as e:
        print(f"DeepSeek HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
        return None
    except Exception as e:
        print(f"DeepSeek error: {e}", flush=True)
        return None


def ask_ai(system_prompt, user_message, max_tokens=800, temperature=0.3):
    """ردود البوت — DeepSeek"""
    return _chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        max_tokens=max_tokens,
        temperature=temperature,
    )


def classify_listing(raw_text):
    """تصنيف إعلان واحد — DeepSeek، يعيد dict أو None"""
    prompt = """أنت عقاري سعودي خبير. حلل نص الإعلان العقاري واستخرج المعلومات بصيغة JSON فقط (بدون أي نص إضافي):

{
  "property_type": "شقة|فيلا|أرض|عمارة|محل|مكتب|استراحة|غير محدد",
  "city": "اسم المدينة أو null",
  "district": "اسم الحي أو null",
  "price": السعر_رقم_صحيح_أو_null,
  "rooms": عدد_الغرف_رقم_صحيح_أو_null,
  "description": "ملخص قصير للعرض",
  "complete": true_أو_false
}

القواعد:
- إذا النص لا يوضح النوع: "غير محدد"
- حوّل الأرقام العربية/الهندية (٠١٢٣٤٥٦٧٨٩) لأرقام عادية
- المدن: جدة/مكة/الرياض/المدينة/الدمام/الخبر/الظهران/null
- الحي: ابحث عن "حي" أو "الحي"
- complete = true فقط إذا توفر النوع والسعر، وإلا false
- رد بالـ JSON فقط"""
    result = _chat(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": raw_text[:2000]},
        ],
        max_tokens=400,
        temperature=0.1,
    )
    if not result:
        return None
    try:
        if "```" in result:
            result = result.split("```")[1]
            if result.startswith("json"):
                result = result[4:]
        data = json.loads(result.strip())
        if isinstance(data, list):
            data = data[0] if data else {}
        return data
    except json.JSONDecodeError:
        return None
