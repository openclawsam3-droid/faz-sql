
"""
?? ???????? - v2 (??????)
???? ????? ?????? ????? ????? sqlite3 ??? ??? PostgreSQL (Supabase).
"""
import os
import re
import logging

logger = logging.getLogger("fadh_db")

try:
    import psycopg2
except Exception as e:
    psycopg2 = None
    logger.warning("psycopg2 ??? ?????: %s", e)


def _conn_params():
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        cleaned = (url.replace("postgresql+psycopg2://", "postgresql://")
                     .replace("postgres://", "postgresql://"))
        return {"dsn": cleaned}
    host = os.getenv("DB_HOST", "")
    if not host:
        return None
    return {
        "host": host,
        "port": os.getenv("DB_PORT", "5432"),
        "dbname": os.getenv("DB_NAME", "postgres"),
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", ""),
    }


class Row(dict):
    def __getitem__(self, k):
        if isinstance(k, int):
            return super().__getitem__(list(self.keys())[k])
        return super().__getitem__(k)


def _translate(sql):
    """????? ??? SQLite ??? PostgreSQL. ????: (sql, conflict_flag)."""
    s = sql
    conflict = False
    s = re.sub(r"(\w+)\s+INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", r"\1 BIGSERIAL PRIMARY KEY", s)
    s = s.replace("?", "%s")
    s = re.sub(r"datetime\(['\"]now['\"](?:,\s*['\"]localtime['\"])?\)", "now()", s)
    s = re.sub(r"datetime\(['\"]now['\"],\s*['\"]([^'\"]+)['\"]\)", r"(now() - interval '\1')", s)
    s = re.sub(r"date\(['\"]now['\"]\)", "current_date", s)
    if re.search(r"INSERT\s+OR\s+IGNORE\s+INTO", s):
        s = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", s)
        conflict = True
    if re.search(r"INSERT\s+OR\s+REPLACE\s+INTO", s):
        s = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", s)
        conflict = True
    s = re.sub(r"\browid\b", "id", s)
    return s, conflict


class PGConnection:
    def __init__(self, conn, cur):
        self._conn = conn
        self._cur = cur

    def execute(self, sql, params=None):
        tsql, conflict = _translate(sql)
        if conflict:
            tsql = re.sub(r";\s*$", "", tsql) + " ON CONFLICT DO NOTHING"
        try:
            self._cur.execute(tsql, params) if params is not None else self._cur.execute(tsql)
        except Exception as e:
            logger.error("PG err: %s | %s", tsql, e)
            self._conn.rollback()
            raise
        return self

    def executescript(self, script):
        for stmt in _split(script):
            if stmt.strip():
                self.execute(stmt)
        return self

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        try:
            self._conn.commit()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass

    def fetchall(self):
        rows = self._cur.fetchall()
        cols = [d[0] for d in (self._cur.description or [])]
        return [Row(dict(zip(cols, r))) for r in rows]

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def fetchmany(self, n=1):
        return self.fetchall()[:n]


def _split(script):
    parts, cur, in_str = [], "", None
    i = 0
    while i < len(script):
        ch = script[i]
        if in_str:
            cur += ch
            if ch == in_str:
                in_str = None
        elif ch in "'\"":
            in_str = ch
            cur += ch
        elif ch == ";":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        parts.append(cur)
    return parts


def _get_conn():
    if psycopg2 is None:
        raise RuntimeError("psycopg2 ??? ????")
    params = _conn_params()
    if not params:
        raise RuntimeError("DATABASE_URL ?? DB_HOST ??? ?????")
    kwargs = {"sslmode": "require"}
    if "dsn" in params:
        kwargs["dsn"] = params["dsn"]
    else:
        kwargs.update(params)
    conn = psycopg2.connect(**kwargs)
    return PGConnection(conn, conn.cursor())


def get_sorted_conn():
    return _get_conn()


def get_raw_conn():
    return _get_conn()


def init_db():
    c = get_sorted_conn()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS raw_messages (
        id BIGSERIAL PRIMARY KEY,
        message_id BIGINT UNIQUE,
        channel TEXT,
        raw_text TEXT,
        datetime TEXT,
        is_spam INTEGER DEFAULT 0,
        spam_reason TEXT,
        pulled_at TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS')
    );
    CREATE TABLE IF NOT EXISTS channels (
        id BIGSERIAL PRIMARY KEY,
        name TEXT,
        url TEXT UNIQUE,
        last_message_id BIGINT DEFAULT 0,
        last_pull TEXT
    );
    CREATE TABLE IF NOT EXISTS sorted_listings (
        id BIGSERIAL PRIMARY KEY,
        raw_id INTEGER UNIQUE,
        message_id BIGINT,
        channel TEXT,
        listing_type TEXT,
        property_type TEXT,
        deal_type TEXT,
        city TEXT,
        district TEXT,
        rooms INTEGER,
        bathrooms INTEGER,
        kitchen TEXT,
        rooftop TEXT,
        annex TEXT,
        driver_room INTEGER,
        maid_room INTEGER,
        finishing TEXT,
        price DOUBLE PRECISION,
        price_unit TEXT,
        features TEXT,
        short_desc TEXT,
        owner_contact TEXT,
        posted_date TEXT,
        is_junk INTEGER DEFAULT 0,
        status TEXT DEFAULT '???',
        analyzed_at TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS'),
        confidence DOUBLE PRECISION,
        needs_review INTEGER DEFAULT 1,
        price_assumed_unit TEXT
    );
    CREATE TABLE IF NOT EXISTS conversations (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT, user_name TEXT, message TEXT, bot_reply TEXT,
        reviewed INTEGER DEFAULT 0, review_note TEXT,
        ts TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS')
    );
    CREATE TABLE IF NOT EXISTS chat_memory (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT, role TEXT, content TEXT,
        ts TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS')
    );
    CREATE TABLE IF NOT EXISTS user_filters (
        user_id TEXT PRIMARY KEY,
        property_type TEXT, deal_type TEXT, city TEXT, district TEXT,
        max_price INTEGER,
        updated_at TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS')
    );
    CREATE TABLE IF NOT EXISTS shown_listings (
        user_id TEXT, raw_id INTEGER,
        shown_at TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS'),
        PRIMARY KEY (user_id, raw_id)
    );
    CREATE TABLE IF NOT EXISTS bot_rules (
        id BIGSERIAL PRIMARY KEY,
        rule TEXT, created_by TEXT,
        created_at TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS')
    );
    CREATE TABLE IF NOT EXISTS admin_chats (
        chat_id BIGINT PRIMARY KEY,
        name TEXT
    );
    CREATE TABLE IF NOT EXISTS conversation_reviews (
        id BIGSERIAL PRIMARY KEY,
        conv_id INTEGER, score INTEGER, verdict TEXT, reason TEXT,
        suggested_reply TEXT,
        reviewed_at TEXT DEFAULT to_char(now(),'YYYY-MM-DD HH24:MI:SS')
    );
    """)
    c.commit()
    c.close()


def ensure_columns(conn, table, columns):
    cols = {r["column_name"].lower() for r in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s",
        (table.lower(),),
    ).fetchall()}
    for name, ddl in columns.items():
        if name.lower() not in cols:
            pg_type = ddl.replace("AUTOINCREMENT", "").replace("INTEGER PRIMARY KEY", "BIGINT")
            conn.execute(f'ALTER TABLE "{table.lower()}" ADD COLUMN IF NOT EXISTS "{name}" {pg_type}')


def migrate():
    conn = get_sorted_conn()
    ensure_columns(conn, "conversations", {"reviewed": "INTEGER DEFAULT 0", "review_note": "TEXT"})
    ensure_columns(conn, "sorted_listings", {
        "confidence": "DOUBLE PRECISION",
        "needs_review": "INTEGER DEFAULT 1",
        "price_assumed_unit": "TEXT",
    })
    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("OK: tables created")
