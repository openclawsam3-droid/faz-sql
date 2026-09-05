"""
الواجهة الموحدة للذكاء الاصطناعي — فذ العقارية.
النظام الجديد: DeepSeek فقط (تصنيف + ردود). بدون Gemini/OpenRouter/Ollama.
يبقى بنفس الواجهة القديمة (ask_ai / classify_listing) كي لا نكسر الكود الموجود.
"""
from deepseek_client import ask_ai, classify_listing

__all__ = ["ask_ai", "classify_listing"]
