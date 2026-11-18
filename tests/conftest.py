"""Test harness: a real PostgreSQL database, reset and seeded with the sample data for each test module.

Set SH_TEST_DATABASE_URL to a disposable database whose name contains "test", e.g.
    postgresql+asyncpg://postgres@127.0.0.1:5432/streamers_hub_test
Email is kept in memory (SH_EMAIL_MODE=log), files are stored locally, and payments use the test provider.
"""

import os
import re
import tempfile

import pytest

TEST_URL = os.getenv("SH_TEST_DATABASE_URL")
if not TEST_URL or "test" not in TEST_URL.rsplit("/", 1)[-1]:
    raise pytest.UsageError("Set SH_TEST_DATABASE_URL to a database whose name contains 'test'. It is wiped by the tests.")

os.environ.update({"SH_DATABASE_URL": TEST_URL, "SH_EMAIL_MODE": "log", "ENVIRONMENT": "test", "SH_RUN_SCHEDULER": "false",
                   "SH_STORAGE": "local", "SH_LOCAL_STORAGE": tempfile.mkdtemp(prefix="sh-files-"), "SH_PAYSTACK_SECRET_KEY": "",
                   "SH_PAYMENTS_TEST_MODE": "true", "SH_FRONTEND_URL": "http://site.test", "SH_PROXY_HOPS": "0",
                   "SECRET_KEY": "test-secret-key-not-for-production"})

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import load_models, session_manager, setup_schema  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.services import SesEmailService  # noqa: E402

PASSWORD = "Sample-Pass-2026"
DOMAIN = "streamershub.test"


@pytest.fixture(scope="session")
async def engine():
    assert settings.database_url == TEST_URL
    await session_manager.init(TEST_URL, setup=False)
    yield session_manager.engine
    await session_manager.close()


@pytest.fixture(scope="module")
async def seeded(engine):
    """Wipe the database and load the sample data once per test module."""
    from scripts.seed import seed

    load_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP SEQUENCE IF EXISTS order_number_seq"))
        await conn.execute(text("DROP SEQUENCE IF EXISTS design_ref_seq"))
        await conn.execute(text("DROP SEQUENCE IF EXISTS interest_number_seq"))
        await setup_schema(conn)
    async with session_manager.session_factory() as db:
        out = await seed(db, reset=True)
        # The existing store tests pay at checkout; tests of interest-only mode switch it back themselves
        from app.models.people import AppSetting

        db.add(AppSetting(key="store", value={"mode": "checkout"}))
        await db.commit()
    SesEmailService.OUTBOX.clear()
    from app.services.PaymentProvider import TEST_OUTCOMES

    TEST_OUTCOMES.clear()
    return out


@pytest.fixture(scope="module")
def app(seeded):
    return create_app(lifespan_on=False)


@pytest.fixture
async def db(seeded):
    async with session_manager.session_factory() as session:
        yield session


def new_client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test")


def last_code(email: str) -> str:
    for mail in reversed(SesEmailService.OUTBOX):
        if email in mail["to"]:
            m = re.search(r"code: (\d{6})", mail["subject"])
            if m:
                return m.group(1)
    raise AssertionError(f"No code emailed to {email}")


async def sign_in(app, key: str) -> httpx.AsyncClient:
    """A client signed in as a sample account (password, then the emailed one-time code)."""
    client = new_client(app)
    email = key if "@" in key else f"{key}@{DOMAIN}"
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    if body["step"] == "code":
        r = await client.post("/api/v1/auth/otp/verify", json={"challenge_id": body["challenge_id"], "code": last_code(email)})
        assert r.status_code == 200, r.text
    return client


async def vault_sign_in(app, holder_id: str, contact: str) -> httpx.AsyncClient:
    client = new_client(app)
    r = await client.post("/api/v1/auth/investor/login", json={"holder_id": holder_id, "contact": contact})
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/auth/investor/verify", json={"challenge_id": r.json()["challenge_id"], "code": last_code(contact)})
    assert r.status_code == 200, r.text
    return client
