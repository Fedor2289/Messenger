"""
migrations.py — DDL миграции при старте сервера

Используем SQLAlchemy AUTOCOMMIT isolation level — самый надёжный способ.
Каждый ALTER TABLE выполняется немедленно вне транзакции.
"""
import logging
from sqlalchemy import text
from database import engine

logger = logging.getLogger(__name__)


def run():
    try:
        _run_migrations()
    except Exception as e:
        # НЕ падаем если миграции не прошли — сервер всё равно стартует
        logger.error(f"Migration error (non-fatal): {e}")


def _run_migrations():
    # AUTOCOMMIT: каждый DDL фиксируется мгновенно, нет транзакции которую можно откатить
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        # ── users ──────────────────────────────────────────────
        _col(conn, "users", "display_name",  "VARCHAR(64)")
        _col(conn, "users", "bio",           "VARCHAR(200)")
        _col(conn, "users", "avatar_img",    "TEXT")
        _col(conn, "users", "last_seen",     "TIMESTAMP")

        # ── rooms ──────────────────────────────────────────────
        _col(conn, "rooms", "description",   "VARCHAR(200)")
        _col(conn, "rooms", "room_type",     "VARCHAR(16) DEFAULT 'chat'")
        _col(conn, "rooms", "created_by",    "INTEGER")
        _col(conn, "rooms", "pinned_msg_id", "INTEGER")
        _col(conn, "rooms", "avatar_img",    "TEXT")
        _col(conn, "rooms", "avatar_color",  "VARCHAR(7)")

        # Старая колонка is_group — убираем NOT NULL чтобы новый код мог её не передавать
        _exec(conn, "ALTER TABLE rooms ALTER COLUMN is_group SET DEFAULT FALSE")
        _exec(conn, "ALTER TABLE rooms ALTER COLUMN is_group DROP NOT NULL")
        # Заполняем room_type по старому is_group
        _exec(conn, """
            UPDATE rooms SET room_type = CASE
                WHEN is_group = TRUE THEN 'group' ELSE 'chat'
            END WHERE room_type IS NULL OR room_type = ''
        """)

        # ── room_members ───────────────────────────────────────
        _col(conn, "room_members", "is_admin", "BOOLEAN DEFAULT FALSE")

        # ── messages ───────────────────────────────────────────
        _col(conn, "messages", "msg_type",    "VARCHAR(16) DEFAULT 'text'")
        _col(conn, "messages", "media_data",  "TEXT")
        _col(conn, "messages", "media_mime",  "VARCHAR(64)")
        _col(conn, "messages", "media_size",  "BIGINT")
        _col(conn, "messages", "reply_to_id", "INTEGER")
        _col(conn, "messages", "is_deleted",  "BOOLEAN DEFAULT FALSE")
        _col(conn, "messages", "is_pinned",   "BOOLEAN DEFAULT FALSE")
        _col(conn, "messages", "edited_at",   "TIMESTAMP")

        # ── новые таблицы ──────────────────────────────────────
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS message_reactions (
                id         SERIAL PRIMARY KEY,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id    INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
                emoji      VARCHAR(32) NOT NULL,
                UNIQUE(message_id, user_id, emoji)
            )
        """)
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS calls (
                id           SERIAL PRIMARY KEY,
                room_id      INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
                caller_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
                call_type    VARCHAR(8)  DEFAULT 'voice',
                status       VARCHAR(16) DEFAULT 'missed',
                started_at   TIMESTAMP   DEFAULT NOW(),
                ended_at     TIMESTAMP,
                duration_sec INTEGER
            )
        """)

        logger.info("Migrations OK")
    finally:
        conn.close()


def _col(conn, table: str, col: str, col_type: str):
    """ALTER TABLE ... ADD COLUMN IF NOT EXISTS — идемпотентно"""
    try:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}"))
        logger.info(f"  col {table}.{col} OK")
    except Exception as e:
        logger.debug(f"  col {table}.{col} skip: {e}")


def _exec(conn, sql: str):
    """Выполняет SQL, молча игнорирует ошибки (идемпотентность)"""
    try:
        conn.execute(text(sql))
    except Exception as e:
        logger.debug(f"  exec skip: {e}")
