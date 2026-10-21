"""دیتابیس SQLite — روی Railway آن را روی یک Volume (مثلاً /data) بگذارید."""
import os
import sqlite3
import time
from datetime import datetime, timezone

import config

os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
_conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, isolation_level=None)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")

_conn.executescript(
    """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT, first_name TEXT,
    joined_at INTEGER, last_seen INTEGER,
    blocked INTEGER DEFAULT 0,
    balance INTEGER DEFAULT 0,
    voice_id TEXT, voice_name TEXT,
    model_id TEXT,
    stability REAL DEFAULT 0.5,
    similarity REAL DEFAULT 0.75,
    style REAL DEFAULT 0.0,
    speed REAL DEFAULT 1.0,
    lang TEXT DEFAULT 'auto',
    output TEXT DEFAULT 'voice'
);
CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER, day TEXT, used INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, day)
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, kind TEXT, title TEXT,
    file_id TEXT, file_type TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER, stars INTEGER, credits INTEGER,
    charge_id TEXT, created_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, id);
"""
)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def upsert_user(u) -> sqlite3.Row:
    now = int(time.time())
    _conn.execute(
        """INSERT INTO users (id, username, first_name, joined_at, last_seen, model_id)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET username=excluded.username,
               first_name=excluded.first_name, last_seen=excluded.last_seen""",
        (u.id, u.username, u.first_name, now, now, config.DEFAULT_MODEL_ID),
    )
    return get_user(u.id)


def get_user(uid: int):
    return _conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


ALLOWED_FIELDS = {"voice_id", "voice_name", "model_id", "stability", "similarity",
                  "style", "speed", "lang", "output", "blocked", "balance"}


def set_field(uid: int, field: str, value):
    assert field in ALLOWED_FIELDS
    _conn.execute(f"UPDATE users SET {field}=? WHERE id=?", (value, uid))


def add_balance(uid: int, amount: int):
    _conn.execute("UPDATE users SET balance = balance + ? WHERE id=?", (amount, uid))


def used_today(uid: int) -> int:
    r = _conn.execute("SELECT used FROM usage WHERE user_id=? AND day=?", (uid, _today())).fetchone()
    return r["used"] if r else 0


def remaining(uid: int) -> tuple[int, int]:
    """(باقی‌ماندهٔ رایگان امروز، موجودی خریداری‌شده)"""
    u = get_user(uid)
    free = max(0, config.DAILY_FREE_CREDITS - used_today(uid))
    return free, (u["balance"] if u else 0)


def spend(uid: int, amount: int, unlimited: bool = False):
    """اول از سهمیهٔ رایگان امروز، بعد از موجودی کم می‌کند.
    خروجی: (مقدار از رایگان، مقدار از موجودی) یا None اگر اعتبار کافی نباشد."""
    free, bal = remaining(uid)
    if not unlimited and amount > free + bal:
        return None
    from_free = amount if unlimited else min(free, amount)
    from_bal = amount - from_free
    _conn.execute(
        """INSERT INTO usage (user_id, day, used) VALUES (?, ?, ?)
           ON CONFLICT(user_id, day) DO UPDATE SET used = used + excluded.used""",
        (uid, _today(), amount),
    )
    if from_bal:
        add_balance(uid, -from_bal)
    return (from_free, from_bal)


def refund(uid: int, spent):
    if not spent:
        return
    from_free, from_bal = spent
    _conn.execute("UPDATE usage SET used = MAX(0, used - ?) WHERE user_id=? AND day=?",
                  (from_free + from_bal, uid, _today()))
    if from_bal:
        add_balance(uid, from_bal)


def add_history(uid: int, kind: str, title: str, file_id: str, file_type: str):
    _conn.execute(
        "INSERT INTO history (user_id, kind, title, file_id, file_type, created_at) VALUES (?,?,?,?,?,?)",
        (uid, kind, title[:80], file_id, file_type, int(time.time())),
    )


def get_history(uid: int, limit: int = 10):
    return _conn.execute(
        "SELECT * FROM history WHERE user_id=? ORDER BY id DESC LIMIT ?", (uid, limit)
    ).fetchall()


def get_history_item(uid: int, item_id: int):
    return _conn.execute("SELECT * FROM history WHERE user_id=? AND id=?", (uid, item_id)).fetchone()


def add_payment(uid: int, stars: int, credits: int, charge_id: str):
    _conn.execute(
        "INSERT INTO payments (user_id, stars, credits, charge_id, created_at) VALUES (?,?,?,?,?)",
        (uid, stars, credits, charge_id, int(time.time())),
    )
    add_balance(uid, credits)


def all_user_ids() -> list[int]:
    return [r["id"] for r in _conn.execute("SELECT id FROM users WHERE blocked=0")]


def stats() -> dict:
    day_ago = int(time.time()) - 86400
    q = lambda sql, *a: _conn.execute(sql, a).fetchone()[0] or 0  # noqa: E731
    return {
        "users": q("SELECT COUNT(*) FROM users"),
        "active_24h": q("SELECT COUNT(*) FROM users WHERE last_seen > ?", day_ago),
        "new_24h": q("SELECT COUNT(*) FROM users WHERE joined_at > ?", day_ago),
        "blocked": q("SELECT COUNT(*) FROM users WHERE blocked=1"),
        "used_today": q("SELECT SUM(used) FROM usage WHERE day=?", _today()),
        "used_total": q("SELECT SUM(used) FROM usage"),
        "generations": q("SELECT COUNT(*) FROM history"),
        "stars": q("SELECT SUM(stars) FROM payments"),
    }
