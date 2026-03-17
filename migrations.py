import logging
from sqlalchemy import text
from database import engine

logger = logging.getLogger(__name__)

def run():
    with engine.connect() as conn:
        # Users
        _col(conn,"users","display_name","VARCHAR(64)")
        _col(conn,"users","bio","VARCHAR(200)")
        _col(conn,"users","avatar_img","TEXT")
        _col(conn,"users","last_seen","TIMESTAMP")
        # Rooms
        _col(conn,"rooms","description","VARCHAR(200)")
        _col(conn,"rooms","room_type","VARCHAR(16) DEFAULT 'chat'")
        _col(conn,"rooms","created_by","INTEGER")
        _col(conn,"rooms","pinned_msg_id","INTEGER")
        # RoomMembers
        _col(conn,"room_members","is_admin","BOOLEAN DEFAULT FALSE")
        # Messages
        _col(conn,"messages","msg_type","VARCHAR(16) DEFAULT 'text'")
        _col(conn,"messages","media_data","TEXT")
        _col(conn,"messages","media_mime","VARCHAR(64)")
        _col(conn,"messages","media_size","BIGINT")
        _col(conn,"messages","reply_to_id","INTEGER")
        _col(conn,"messages","is_deleted","BOOLEAN DEFAULT FALSE")
        _col(conn,"messages","is_pinned","BOOLEAN DEFAULT FALSE")
        _col(conn,"messages","edited_at","TIMESTAMP")
        # Fix old is_group → room_type
        _migrate_room_type(conn)
        # New tables
        _table(conn,"message_reactions","""
            id SERIAL PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            emoji VARCHAR(32) NOT NULL,
            UNIQUE(message_id,user_id,emoji)""")
        _table(conn,"calls","""
            id SERIAL PRIMARY KEY,
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            caller_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            call_type VARCHAR(8) DEFAULT 'voice',
            status VARCHAR(16) DEFAULT 'missed',
            started_at TIMESTAMP DEFAULT NOW(),
            ended_at TIMESTAMP,
            duration_sec INTEGER""")
        conn.commit()
    logger.info("Migrations OK")

def _col(conn, table, col, col_type):
    try:
        conn.execute(text(f"SAVEPOINT sp_{table}_{col}"))
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}"))
        conn.execute(text(f"RELEASE SAVEPOINT sp_{table}_{col}"))
        logger.info(f"Added {table}.{col}")
    except Exception:
        try: conn.execute(text(f"ROLLBACK TO SAVEPOINT sp_{table}_{col}"))
        except: pass

def _table(conn, name, cols):
    try:
        conn.rollback()
    except: pass
    try:
        conn.execute(text(f"CREATE TABLE IF NOT EXISTS {name} ({cols})"))
        logger.info(f"Table {name} OK")
    except Exception as e:
        logger.warning(f"Table {name}: {e}")

def _migrate_room_type(conn):
    """Конвертируем старое is_group в room_type"""
    try:
        # Проверяем есть ли старая колонка
        conn.execute(text("SAVEPOINT sp_migrate_rt"))
        conn.execute(text("""
            UPDATE rooms SET room_type = CASE
                WHEN is_group = TRUE THEN 'group'
                ELSE 'chat'
            END WHERE room_type = 'chat' AND is_group IS NOT NULL
        """))
        conn.execute(text("RELEASE SAVEPOINT sp_migrate_rt"))
    except Exception:
        try: conn.execute(text("ROLLBACK TO SAVEPOINT sp_migrate_rt"))
        except: pass
