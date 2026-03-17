"""
database.py — Подключение к базе данных

Локально:  SQLite  (файл messenger.db, ничего не нужно устанавливать)
Railway:   PostgreSQL (переменная DATABASE_URL добавляется автоматически)
"""

import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Railway даёт postgres://, SQLAlchemy требует postgresql://
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./messenger.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

_is_sqlite = "sqlite" in DATABASE_URL

engine = create_engine(
    DATABASE_URL,
    # SQLite: нужно для многопоточной работы с FastAPI
    connect_args={"check_same_thread": False} if _is_sqlite else {},
    # PostgreSQL: пул соединений
    **({} if _is_sqlite else {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,   # проверяем соединение перед использованием
        "pool_recycle": 300,     # пересоздаём соединения каждые 5 минут
    }),
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """FastAPI Dependency — открывает сессию БД на время запроса"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
