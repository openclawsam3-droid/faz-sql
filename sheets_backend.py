"""
الواجهة الخلفية لـ Google Sheets — عبر gspread (الحساب الرسمي).
يتطلب ملف Service Account JSON + رابط الشيت في .env:
  GOOGLE_SERVICE_ACCOUNT=/path/to/creds.json
  SPREADSHEET_URL=https://docs.google.com/spreadsheets/d/...
"""
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))


class SheetsBackend:
    def __init__(self):
        import gspread
        creds_path = os.getenv("GOOGLE_SERVICE_ACCOUNT")
        url = os.getenv("SPREADSHEET_URL")
        if not creds_path or not os.path.exists(creds_path):
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT غير موجود — ضع مسار ملف Service Account في .env")
        if not url:
            raise RuntimeError("SPREADSHEET_URL غير موجود في .env")
        self.gc = gspread.service_account(filename=creds_path)
        self.sheet = self.gc.open_by_url(url)

    def _worksheet(self, tab):
        try:
            return self.sheet.worksheet(tab)
        except Exception:
            return self.sheet.add_worksheet(title=tab, rows="1000", cols="20")

    def reset_tab(self, tab, headers):
        ws = self._worksheet(tab)
        ws.clear()
        if headers:
            ws.append_row(headers)

    def append_rows(self, tab, rows):
        ws = self._worksheet(tab)
        for chunk in _chunks(rows, 50):
            ws.append_rows(chunk, value_input_option="USER_ENTERED")

    def read_tab(self, tab):
        ws = self._worksheet(tab)
        return ws.get_all_values()

    def get_sheet(self):
        return {
            "title": self.sheet.title,
            "sheets": [w.title for w in self.sheet.worksheets()],
        }


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]
