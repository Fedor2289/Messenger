"""
migrations.py — Автоматические миграции при старте

Добавляет новые колонки в уже существующие таблицы.
Безопасно: если колонка уже есть — ничего не делает.
"""
import logging
from sqlalchemy import text
from database import engine

logger = logging.getLogger(__name__)


def run():
    """Запускает все миграции. Вызывается один раз при старте."""
    with engine.connect() as conn:
        _add_column_if_missing(conn, "users",    "display_name", "VARCHAR(64)")
        _add_column_if_missing(conn, "users",    "bio",          "VARCHAR(200)")
        _add_column_if_missing(conn, "users",    "avatar_img",   "TEXT")
        _add_column_if_missing(conn, "rooms",    "created_by",   "INTEGER")
        _add_column_if_missing(conn, "messages", "msg_type",     "VARCHAR(16) DEFAULT 'text' NOT NULL")
        _add_column_if_missing(conn, "messages", "media_data",   "TEXT")
        _add_column_if_missing(conn, "messages", "media_mime",   "VARCHAR(64)")
        _add_column_if_missing(conn, "messages", "media_size",   "BIGINT")
        _create_reactions_table_if_missing(conn)
        conn.commit()
    logger.info("Migrations OK")


def _add_column_if_missing(conn, table: str, column: str, col_type: str):
    """Добавляет колонку если её нет."""
    # Работает и для PostgreSQL и для SQLite
    try:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}"))
        logger.info(f"Migration: added {table}.{column}")
    except Exception:
        pass  # Колонка уже существует — игнорируем


def _create_reactions_table_if_missing(conn):
    """Создаёт таблицу реакций если её нет."""
    try:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS message_reactions (
                id         SERIAL PRIMARY KEY,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                user_id    INTEGER NOT NULL REFERENCES users(id)    ON DELETE CASCADE,
                emoji      VARCHAR(32) NOT NULL,
                UNIQUE(message_id, user_id, emoji)
            )
        """))
        logger.info("Migration: message_reactions table OK")
    except Exception as e:
        logger.warning(f"message_reactions table: {e}")
