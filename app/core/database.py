"""Async database sessions, with the database created on first start.

Schema changes follow the same rule as the school systems and Ideation Vault: tables come from the
models (`create_all`), and every column added later goes in `COLUMN_MIGRATIONS` as raw, idempotent
SQL that is safe to run on every start. Alembic is not used.
"""

import uuid
from importlib import import_module
from typing import AsyncGenerator, Optional

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.models.base import Base

# Columns added after a table first shipped. Each runs in its own savepoint.
COLUMN_MIGRATIONS: list[str] = [
    "CREATE SEQUENCE IF NOT EXISTS order_number_seq START 1001",
    "CREATE SEQUENCE IF NOT EXISTS design_ref_seq START 1",
    "ALTER TABLE equity_grants ADD COLUMN IF NOT EXISTS agreement_id UUID",
    "ALTER TABLE marked_moments ADD COLUMN IF NOT EXISTS source VARCHAR(12) NOT NULL DEFAULT 'producer'",
    "ALTER TABLE marked_moments ADD COLUMN IF NOT EXISTS kind VARCHAR(16) NOT NULL DEFAULT 'highlight'",
    "ALTER TABLE marked_moments ADD COLUMN IF NOT EXISTS note VARCHAR(300)",
    "ALTER TABLE clips ADD COLUMN IF NOT EXISTS moment_id UUID",
    "CREATE INDEX IF NOT EXISTS ix_clips_moment_id ON clips (moment_id)",
    "ALTER TABLE clips ADD COLUMN IF NOT EXISTS views_fetched BIGINT",
    "ALTER TABLE clips ADD COLUMN IF NOT EXISTS views_fetched_at TIMESTAMPTZ",
    "ALTER TABLE clips ADD COLUMN IF NOT EXISTS fetch_note VARCHAR(200)",
    "ALTER TABLE clips ADD COLUMN IF NOT EXISTS platform_channel VARCHAR(120)",
    "ALTER TABLE clips ADD COLUMN IF NOT EXISTS count_updated_at TIMESTAMPTZ",
    "ALTER TABLE clips ADD COLUMN IF NOT EXISTS count_note VARCHAR(200)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS step_in JSON NOT NULL DEFAULT '[]'",
    "ALTER TABLE equipment ADD COLUMN IF NOT EXISTS streamer_id UUID REFERENCES streamers(id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_equipment_streamer_id ON equipment (streamer_id)",
    "ALTER TABLE equipment ADD COLUMN IF NOT EXISTS expense_id UUID REFERENCES expenses(id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_equipment_expense_id ON equipment (expense_id)",
    "ALTER TABLE streamers ADD COLUMN IF NOT EXISTS producer_user_id UUID REFERENCES users(id) ON DELETE SET NULL",
    "CREATE INDEX IF NOT EXISTS ix_streamers_producer_user_id ON streamers (producer_user_id)",
    "ALTER TABLE streamers ADD COLUMN IF NOT EXISTS segments JSON NOT NULL DEFAULT '[]'",
    "ALTER TABLE streamers ADD COLUMN IF NOT EXISTS moderators VARCHAR(300)",
    "ALTER TABLE streamers ADD COLUMN IF NOT EXISTS chat_rules TEXT",
    "ALTER TABLE streams ADD COLUMN IF NOT EXISTS scheduled_end TIMESTAMPTZ",
    "ALTER TABLE streams ADD COLUMN IF NOT EXISTS notice_kind VARCHAR(10)",
    "ALTER TABLE streams ADD COLUMN IF NOT EXISTS notice_minutes INTEGER",
    "ALTER TABLE streams ADD COLUMN IF NOT EXISTS notice_note VARCHAR(300)",
    "ALTER TABLE streams ADD COLUMN IF NOT EXISTS notice_at TIMESTAMPTZ",
    "ALTER TABLE equipment ADD COLUMN IF NOT EXISTS purchased_on DATE",
    "ALTER TABLE equipment ADD COLUMN IF NOT EXISTS serial_number VARCHAR(80)",
    "CREATE SEQUENCE IF NOT EXISTS interest_number_seq START 1",
    "ALTER TABLE posts ADD COLUMN IF NOT EXISTS job JSON",
    "ALTER TABLE link_pages ADD COLUMN IF NOT EXISTS shares INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE investor_documents ADD COLUMN IF NOT EXISTS opened_at TIMESTAMPTZ",
    "ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_sample BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_sample BOOLEAN NOT NULL DEFAULT false",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_sample BOOLEAN NOT NULL DEFAULT false",
    # The audit log keeps a post's slug as its id, and slugs run to 80 characters (was 64)
    "DO $$ BEGIN IF (SELECT character_maximum_length FROM information_schema.columns WHERE table_schema = current_schema() "
    "AND table_name = 'audit_log' AND column_name = 'entity_id') < 160 THEN "
    "ALTER TABLE audit_log ALTER COLUMN entity_id TYPE VARCHAR(160); END IF; END $$",
]


def load_models() -> None:
    for module in settings.DB_MODELS:
        import_module(module)


class DatabaseSessionManager:
    def __init__(self):
        self.engine: Optional[AsyncEngine] = None
        self.session_factory: Optional[async_sessionmaker] = None

    async def init(self, url: Optional[str] = None, setup: bool = True):
        url = url or settings.database_url
        self.engine = create_async_engine(
            url, pool_size=10, max_overflow=5, pool_recycle=300, pool_pre_ping=True, echo=settings.SQL_ECHO,
            connect_args={"prepared_statement_cache_size": 0},
        )
        if setup:
            try:
                async with self.engine.begin() as conn:
                    await setup_schema(conn)
            except asyncpg.exceptions.InvalidCatalogNameError:
                await _create_database(url)
                async with self.engine.begin() as conn:
                    await setup_schema(conn)
        self.session_factory = async_sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

    async def close(self):
        if self.engine:
            await self.engine.dispose()


async def setup_schema(conn) -> None:
    load_models()
    await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "pgcrypto"'))
    await conn.run_sync(Base.metadata.create_all)
    for i, statement in enumerate(COLUMN_MIGRATIONS):
        await conn.execute(text(f"SAVEPOINT m{i}"))
        try:
            await conn.execute(text(statement))
            await conn.execute(text(f"RELEASE SAVEPOINT m{i}"))
        except Exception as exc:  # one bad statement must not stop the rest
            await conn.execute(text(f"ROLLBACK TO SAVEPOINT m{i}"))
            print(f"Migration skipped ({exc.__class__.__name__}): {statement}")
    await ensure_departments(conn)
    from app.services.AgreementService import ensure_templates

    await ensure_templates(conn)


async def ensure_departments(conn) -> None:
    """The eight departments are fixed by the org chart, so every database has them, seeded or not."""
    from sqlalchemy.dialects.postgresql import insert

    from app.core.access import DEPARTMENTS
    from app.models.people import Department

    rows = [{"id": uuid.uuid4(), "slug": slug, "name": name, "sort": i} for i, (slug, name) in enumerate(DEPARTMENTS)]
    await conn.execute(insert(Department).values(rows).on_conflict_do_nothing())


async def _create_database(url: str) -> None:
    parsed = make_url(url)
    conn = await asyncpg.connect(
        user=parsed.username, password=parsed.password, host=parsed.host, port=parsed.port or 5432,
        database="postgres",
    )
    try:
        await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


session_manager = DatabaseSessionManager()


async def aget_db() -> AsyncGenerator[AsyncSession, None]:
    if session_manager.session_factory is None:
        raise RuntimeError("Database is not initialised")
    async with session_manager.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
