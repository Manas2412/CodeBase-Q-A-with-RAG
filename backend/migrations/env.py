# migrations/env.py
import asyncio
import os
from logging.config import fileConfig

from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool
from alembic import context
from dotenv import load_dotenv
# Import your models so Alembic can detect them
from app.db.database import Base, needs_ssl, normalise_dsn
from app.db.models import (
    BranchEvent,
    Checklist,
    Chunk,
    Commit,
    Project,
    Repo,  # backward-compat alias for Project
    Review,
    ReviewFinding,
    User,
)


load_dotenv()


config = context.config

# Load DB URL from environment
raw_url = os.getenv("DATABASE_URL", "")
db_url = normalise_dsn(raw_url)
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    ssl_arg = "require" if needs_ssl(db_url) else False
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"ssl": ssl_arg},  # auto: SSL for cloud, off for local
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()