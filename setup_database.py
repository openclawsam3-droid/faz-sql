"""إعداد قاعدة البيانات: إنشاء الجداول + الترحيل الآمن للأعمدة الجديدة.

شغّله مرة واحدة قبل التشغيل:
    python setup_database.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from database import init_db, migrate


if __name__ == "__main__":
    init_db()
    migrate()
    print("✓ قاعدة البيانات جاهزة (data/raw.db + data/sorted.db)")
