"""
السحب الدوري التلقائي — يجمع سحب القنوات + التحليل العميق في خطوة واحدة.
يُستدعى من cron كل 15 دقيقة. يكتب ملخصاً في /tmp/cron_pull.log
"""
import os
import sys
import time
import logging

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    filename="/tmp/cron_pull.log",
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("cron_pull")

from pull_now import pull
from classify_all import analyze_batch


def main():
    start = time.time()
    log.info("بدء السحب الدوري")
    try:
        pull()
    except Exception as e:
        log.error(f"خطأ في السحب: {e}")
    try:
        analyze_batch(limit=30, sleep_sec=0.5)
    except Exception as e:
        log.error(f"خطأ في التحليل: {e}")
    dur = round(time.time() - start, 1)
    log.info(f"انتهت الدورة في {dur} ثانية")


if __name__ == "__main__":
    main()
