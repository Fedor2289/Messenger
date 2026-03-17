"""
migrations.py — DDL миграции при старте

Ключевой принцип: каждая операция в AUTOCOMMIT режиме.
В PostgreSQL DDL транзакционен — если делать rollback внутри той же
транзакции, все ALTER TABLE откатываются. Решение: отдельное соединение
с AUTOCOMMIT для каждой DDL-операции.
"""
import logging
from sqlalchemy import text
from database import engine

logger = logging.getLogger(__name__)


def run():
    _add_columns()
    _create_tables()
    _migrate_data()
    # Сбрасываем пул соединений — новые соединения увидят обновлённую схему
    engine.dispose()
    logger.info("Migrations OK")


def _add_columns():
    """Добавляем колонки через AUTOCOMMIT — каждая операция немедленно фиксируется"""
    cols = [
        ("users",        "display_name",   "VARCHAR(64)"),
        ("users",        "bio",            "VARCHAR(200)"),
        ("users",        "avatar_img",     "TEXT"),
        ("users",        "last_seen",      "TIMESTAMP"),
        ("rooms",        "description",    "VARCHAR(200)"),
        ("rooms",        "room_type",      "VARCHAR(16) DEFAULT 'chat'"),
        ("rooms",        "created_by",     "INTEGER"),
        ("rooms",        "pinned_msg_id",  "INTEGER"),
        ("room_members", "is_admin",       "BOOLEAN DEFAULT FALSE"),
        ("messages",     "msg_type",       "VARCHAR(16) DEFAULT 'text'"),
        ("messages",     "media_data",     "TEXT"),
        ("messages",     "media_mime",     "VARCHAR(64)"),
        ("messages",     "media_size",     "BIGINT"),
        ("messages",     "reply_to_id",    "INTEGER"),
        ("messages",     "is_deleted",     "BOOLEAN DEFAULT FALSE"),
        ("messages",     "is_pinned",      "BOOLEAN DEFAULT FALSE"),
        ("messages",     "edited_at",      "TIMESTAMP"),
    ]
    for table, col, col_type in cols:
        _add_col(table, col, col_type)


def _add_col(table: str, col: str, col_type: str):
    """Добавляет колонку. Использует отдельное AUTOCOMMIT-соединение."""
    # Получаем raw psycopg2 соединение напрямую (минуя SQLAlchemy транзакции)
    raw_conn = engine.raw_connection()
    try:
        raw_conn.set_session(autocommit=True)
        with raw_conn.cursor() as cur:
            # IF NOT EXISTS — PostgreSQL 9.6+
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_type}")
        logger.info(f"Column {table}.{col} OK")
    except Exception as e:
        logger.warning(f"Column {table}.{col}: {e}")
    finally:
        raw_conn.close()


def _create_tables():
    """Создаём новые таблицы через AUTOCOMMIT"""
    tables = [
        ("message_reactions", """
            id         SERIAL PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            user_id    INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
            emoji      VARCHAR(32) NOT NULL,
            UNIQUE(message_id, user_id, emoji)
        """),
        ("calls", """
            id           SERIAL PRIMARY KEY,
            room_id      INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            caller_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
            call_type    VARCHAR(8) DEFAULT 'voice',
            status       VARCHAR(16) DEFAULT 'missed',
            started_at   TIMESTAMP DEFAULT NOW(),
            ended_at     TIMESTAMP,
            duration_sec INTEGER
        """),
    ]
    for name, cols in tables:
        raw_conn = engine.raw_connection()
        try:
            raw_conn.set_session(autocommit=True)
            with raw_conn.cursor() as cur:
                cur.execute(f"CREATE TABLE IF NOT EXISTS {name} ({cols})")
            logger.info(f"Table {name} OK")
        except Exception as e:
            logger.warning(f"Table {name}: {e}")
        finally:
            raw_conn.close()


def _migrate_data():
    """Конвертируем is_group → room_type и убираем NOT NULL с is_group"""
    raw_conn = engine.raw_connection()
    try:
        raw_conn.set_session(autocommit=True)
        with raw_conn.cursor() as cur:
            # Проверяем есть ли колонка is_group
            cur.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name='rooms' AND column_name='is_group'
            """)
            if cur.fetchone():
                # Снимаем NOT NULL ограничение и ставим DEFAULT FALSE
                # чтобы новый код мог не передавать это поле
                cur.execute("ALTER TABLE rooms ALTER COLUMN is_group SET DEFAULT FALSE")
                cur.execute("ALTER TABLE rooms ALTER COLUMN is_group DROP NOT NULL")
                # Обновляем room_type по старому is_group
                cur.execute("""
                    UPDATE rooms SET room_type = CASE
                        WHEN is_group = TRUE THEN 'group'
                        ELSE 'chat'
                    END WHERE room_type = 'chat'
                """)
                logger.info("Migrated is_group → room_type, removed NOT NULL")
    except Exception as e:
        logger.warning(f"Data migration: {e}")
    finally:
        raw_conn.close()
