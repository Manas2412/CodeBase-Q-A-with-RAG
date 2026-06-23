# app/db/database.py
import json
import os
from urllib.parse import urlparse

import asyncpg
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from dotenv import load_dotenv

load_dotenv()


async def register_jsonb_codecs(conn: asyncpg.Connection) -> None:
    """Register pg_catalog.jsonb + pg_catalog.json codecs on a single connection.

    Without this, asyncpg returns JSONB columns as raw strings. We register
    json.dumps / json.loads so list[str] / dict columns (branches_to_review,
    last_reviewed_sha, severity_counts, token_usage, etc.) round-trip as
    Python lists / dicts.

    Used by:
      • the FastAPI lifespan (init callback for the pool)
      • the Celery worker tasks (each task creates its own conn)
    """
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def needs_ssl(dsn: str) -> bool:
    """
    Cloud Postgres (Neon, RDS, etc.) requires SSL; local containers don't.
    Auto-detect from the hostname so the same code runs in both worlds.

    Local categories detected:
      • Loopback addresses: localhost, 127.0.0.1, 0.0.0.0, ::1
      • Unqualified hostnames (no dots): docker-compose service names,
        kubernetes pod names, internal short names like 'postgres' or 'db'.
        Real production DBs always have qualified names.

    Override with DB_SSL=true|false if you need to force one side.
    """
    override = os.getenv("DB_SSL")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "require")
    host = (urlparse(dsn).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return False
    if "." not in host:
        # compose / k8s / docker bridge: internal, never SSL
        return False
    return True


def normalise_dsn(raw: str) -> str:
    """Normalise to postgresql+asyncpg:// and strip query params."""
    url = raw.replace("postgres://", "postgresql+asyncpg://", 1)
    if raw.startswith("postgresql://"):
        url = raw.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url.split("?")[0]


raw_url = os.getenv("DATABASE_URL", "")
DATABASE_URL = normalise_dsn(raw_url)

_connect_args = {"ssl": "require"} if needs_ssl(DATABASE_URL) else {"ssl": False}

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    pool_size=5,
    max_overflow=10,
    connect_args=_connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    pass