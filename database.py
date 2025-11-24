import aiosqlite
import pytz
from datetime import datetime
from typing import Optional, Tuple, List
from config import SETTINGS, TIMEZONE

class DatabaseManager:
    """Управляет единственным асинхронным подключением к aiosqlite."""
    _connection: Optional[aiosqlite.Connection] = None

    @classmethod
    async def get_connection(cls) -> aiosqlite.Connection:
        if cls._connection is None:
            cls._connection = await aiosqlite.connect(SETTINGS.DB_NAME, timeout=10)
            cls._connection.row_factory = aiosqlite.Row
        return cls._connection

    @classmethod
    async def close_connection(cls):
        if cls._connection:
            await cls._connection.close()
            cls._connection = None

    @classmethod
    async def init_db(cls):
        db = await cls.get_connection()
        await db.execute('''
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT,
                created_at DATETIME,
                moderated_at DATETIME,
                moderated_date_str TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_limits (
                user_id INTEGER,
                date_str TEXT,
                count INTEGER,
                PRIMARY KEY (user_id, date_str)
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id INTEGER PRIMARY KEY,
                banned_by INTEGER,
                banned_at DATETIME,
                reason TEXT
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS pending_posts (
                message_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                submitted_at DATETIME
            )
        ''')
        await db.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_users (
                user_id INTEGER PRIMARY KEY
            )
        ''')
        await db.commit()

# --- Вспомогательные функции ---

def _get_datetime_now_utc_str() -> str:
    return datetime.now(pytz.utc).isoformat()

def _get_limit_date_str() -> str:
    return datetime.now(TIMEZONE).strftime("%Y-%m-%d")

def _to_tz_datetime(iso_utc_str: str) -> datetime:
    dt_utc = datetime.fromisoformat(iso_utc_str).astimezone(pytz.utc)
    return dt_utc.astimezone(TIMEZONE)

# --- Функции запросов ---

async def async_db_is_banned(user_id: int) -> bool:
    db = await DatabaseManager.get_connection()
    async with db.execute("SELECT 1 FROM banned_users WHERE user_id = ?", (user_id,)) as cursor:
        return await cursor.fetchone() is not None

async def async_db_ban_user(user_id: int, moderator_id: int, reason: str = "Не указана"):
    now_utc_str = _get_datetime_now_utc_str()
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT OR REPLACE INTO banned_users (user_id, banned_by, banned_at, reason) VALUES (?, ?, ?, ?)",
        (user_id, moderator_id, now_utc_str, reason)
    )
    await db.commit()

async def async_db_unban_user(user_id: int):
    db = await DatabaseManager.get_connection()
    await db.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,))
    await db.commit()

async def async_db_get_current_limit_count(user_id: int) -> int:
    if user_id == SETTINGS.OWNER_ID: return 0
    today_str = _get_limit_date_str()
    db = await DatabaseManager.get_connection()
    async with db.execute("SELECT COALESCE(count, 0) FROM user_limits WHERE user_id = ? AND date_str = ?",
                          (user_id, today_str)) as cursor:
        result = await cursor.fetchone()
        return result[0] if result else 0

async def async_db_increment_limit(user_id: int):
    if user_id == SETTINGS.OWNER_ID: return
    today_str = _get_limit_date_str()
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT INTO user_limits (user_id, date_str, count) VALUES (?, ?, 1) "
        "ON CONFLICT(user_id, date_str) DO UPDATE SET count = count + 1",
        (user_id, today_str)
    )
    await db.commit()

async def async_db_decrement_limit(user_id: int):
    if user_id == SETTINGS.OWNER_ID: return
    today_str = _get_limit_date_str()
    db = await DatabaseManager.get_connection()
    await db.execute(
        "UPDATE user_limits SET count = count - 1 WHERE user_id = ? AND date_str = ? AND count > 0",
        (user_id, today_str)
    )
    await db.commit()

async def async_db_record_pending_post(message_id: int, user_id: int):
    submitted_at_utc_str = _get_datetime_now_utc_str()
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT INTO pending_posts (message_id, user_id, submitted_at) VALUES (?, ?, ?)",
        (message_id, user_id, submitted_at_utc_str)
    )
    await db.commit()

async def async_db_add_broadcast_user(user_id: int):
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT OR IGNORE INTO broadcast_users (user_id) VALUES (?)",
        (user_id,)
    )
    await db.commit()

async def async_db_get_all_broadcast_users() -> List[int]:
    db = await DatabaseManager.get_connection()
    async with db.execute("SELECT user_id FROM broadcast_users") as cursor:
        return [row[0] for row in await cursor.fetchall()]

async def async_db_get_pending_post_data(message_id: int) -> Optional[Tuple[int, datetime]]:
    db = await DatabaseManager.get_connection()
    async with db.execute("SELECT user_id, submitted_at FROM pending_posts WHERE message_id = ?",
                          (message_id,)) as cursor:
        result = await cursor.fetchone()
        if result:
            user_id = result['user_id']
            submitted_at_utc_str = result['submitted_at']
            submitted_at_tz = _to_tz_datetime(submitted_at_utc_str)
            return user_id, submitted_at_tz
        return None

async def async_db_add_stat(event_type: str, submitted_at_tz: Optional[datetime], message_id: Optional[int] = None):
    now_utc_str = _get_datetime_now_utc_str()
    now_tz = datetime.now(TIMEZONE)
    moderated_date_str = now_tz.strftime("%Y-%m-%d")

    submitted_utc_str = submitted_at_tz.astimezone(pytz.utc).isoformat() if submitted_at_tz else now_utc_str
    db = await DatabaseManager.get_connection()
    await db.execute(
        "INSERT INTO stats (event_type, created_at, moderated_at, moderated_date_str) VALUES (?, ?, ?, ?)",
        (event_type, submitted_utc_str, now_utc_str, moderated_date_str))

    if message_id:
        await db.execute("DELETE FROM pending_posts WHERE message_id = ?", (message_id,))
    await db.commit()

async def async_db_get_stats_counts(period: str = 'all') -> Tuple[int, int]:
    db = await DatabaseManager.get_connection()
    params = []
    if period == 'today':
        today_str = _get_limit_date_str()
        condition = "WHERE moderated_date_str = ?"
        params = [today_str]
    else:
        condition = ""

    query_pub = f"SELECT COUNT(*) FROM stats {condition} AND event_type = 'published'"
    query_rej = f"SELECT COUNT(*) FROM stats {condition} AND event_type = 'rejected'"

    if not condition:
        query_pub = query_pub.replace("AND ", "WHERE ", 1)
        query_rej = query_rej.replace("AND ", "WHERE ", 1)

    if period == 'all':
        query_pub = "SELECT COUNT(*) FROM stats WHERE event_type = 'published'"
        query_rej = "SELECT COUNT(*) FROM stats WHERE event_type = 'rejected'"
        params = []

    async with db.execute(query_pub, params) as cursor_pub:
        pub_count = (await cursor_pub.fetchone())[0]

    async with db.execute(query_rej, params) as cursor_rej:
        rej_count = (await cursor_rej.fetchone())[0]

    return pub_count, rej_count
