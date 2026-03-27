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

        # Заполняем avatar_color для существующих пользователей у которых NULL
        _exec(conn, """
            UPDATE users SET avatar_color = (
                CASE (id % 12)
                    WHEN 0 THEN '#E17055' WHEN 1 THEN '#00B894'
                    WHEN 2 THEN '#0984E3' WHEN 3 THEN '#6C5CE7'
                    WHEN 4 THEN '#FDCB6E' WHEN 5 THEN '#E84393'
                    WHEN 6 THEN '#00CEC9' WHEN 7 THEN '#55EFC4'
                    WHEN 8 THEN '#74B9FF' WHEN 9 THEN '#A29BFE'
                    WHEN 10 THEN '#FD79A8' ELSE '#FAB1A0'
                END
            ) WHERE avatar_color IS NULL
        """)

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
        _exec(conn, """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                id           SERIAL PRIMARY KEY,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                endpoint     TEXT NOT NULL,
                subscription TEXT NOT NULL,
                created_at   TIMESTAMP DEFAULT NOW(),
                UNIQUE(user_id, endpoint)
            )
        """)

        # Music tables
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS music_rooms (
                id SERIAL PRIMARY KEY,
                name VARCHAR(64) NOT NULL,
                description TEXT,
                created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                current_track_id INTEGER,
                current_started_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                current_started_at TIMESTAMP,
                is_playing BOOLEAN DEFAULT FALSE,
                paused_at_sec FLOAT DEFAULT 0
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS music_room_members (
                id SERIAL PRIMARY KEY,
                room_id INTEGER NOT NULL REFERENCES music_rooms(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                joined_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(room_id, user_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS music_tracks (
                id SERIAL PRIMARY KEY,
                room_id INTEGER REFERENCES music_rooms(id) ON DELETE CASCADE,
                title VARCHAR(128) NOT NULL,
                artist VARCHAR(128),
                duration_sec FLOAT,
                yadisk_key TEXT NOT NULL,
                cover_url TEXT,
                uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                is_shared BOOLEAN DEFAULT FALSE
            )
        """))
        conn.execute(text("""
            DO $$ BEGIN
                ALTER TABLE music_rooms ADD CONSTRAINT fk_current_track
                    FOREIGN KEY (current_track_id) REFERENCES music_tracks(id) ON DELETE SET NULL;
            EXCEPTION WHEN duplicate_object THEN NULL; END $$
        """))
        conn.commit()
        logger.info("Music tables OK")

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
