# app/db/database.py
import os
from urllib.parse import urlparse
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from dotenv import load_dotenv

load_dotenv()


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