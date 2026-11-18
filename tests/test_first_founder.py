"""A brand-new database: no seed, no users. The Founder is created by the setup script, gets in through the
emailed link, and invites the first co-founder from People."""

import re

import pytest
from sqlalchemy import select, text

from app.core.database import load_models, session_manager, setup_schema
from app.main import create_app
from app.models.base import Base
from app.models.people import Department, OnboardingRun, User
from app.services import SesEmailService
from scripts.create_founder import create, resend
from tests.conftest import last_code, new_client

EMAIL = "founder@first-run.test"
PASSWORD = "First-Run-Pass-2026"


@pytest.fixture(scope="module")
async def empty(engine):
    load_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP SEQUENCE IF EXISTS order_number_seq"))
        await conn.execute(text("DROP SEQUENCE IF EXISTS design_ref_seq"))
        await setup_schema(conn)
    SesEmailService.OUTBOX.clear()


@pytest.fixture(scope="module")
def app(empty):
    return create_app(lifespan_on=False)


def invite_token(email: str) -> str:
    for mail in reversed(SesEmailService.OUTBOX):
        if email in mail["to"]:
            m = re.search(r"set-password\?token=([^&\"]+)", mail["html"])
            if m:
                return m.group(1)
    raise AssertionError(f"No invite emailed to {email}")


async def test_empty_database_has_departments_and_no_users(empty):
    async with session_manager.session_factory() as db:
        assert len((await db.execute(select(Department))).scalars().all()) == 8
        assert not (await db.execute(select(User))).scalars().all()


async def test_setup_twice_does_not_duplicate_departments(empty):
    async with session_manager.engine.begin() as conn:
        await setup_schema(conn)
    async with session_manager.session_factory() as db:
        assert len((await db.execute(select(Department))).scalars().all()) == 8


async def test_founder_is_created_once_and_invited(empty):
    async with session_manager.session_factory() as db:
        user, _ = await create(db, "First Founder", EMAIL, "Founder & President")
        await db.commit()
        assert user.role == "founder" and user.status == "invited" and user.password_hash is None
        assert await db.scalar(select(OnboardingRun).where(OnboardingRun.user_id == user.id, OnboardingRun.kind == "staff"))
        leadership = await db.scalar(select(Department).where(Department.slug == "leadership"))
        assert leadership.lead_user_id == user.id
    with pytest.raises(SystemExit):
        async with session_manager.session_factory() as db:
            await create(db, "Second Founder", "second@first-run.test", "Founder")
    assert invite_token(EMAIL)


async def test_resend_replaces_the_old_link(app):
    old = invite_token(EMAIL)
    async with session_manager.session_factory() as db:
        await resend(db)
        await db.commit()
    new = invite_token(EMAIL)
    assert new != old
    client = new_client(app)
    assert (await client.get(f"/api/v1/auth/invite/{old}")).status_code >= 400
    assert (await client.get(f"/api/v1/auth/invite/{new}")).json()["role"] == "founder"


async def test_founder_sets_password_signs_in_and_invites_a_cofounder(app):
    client = new_client(app)
    r = await client.post("/api/v1/auth/invite/accept", json={"token": invite_token(EMAIL), "password": PASSWORD})
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    body = r.json()
    if body["step"] == "code":
        r = await client.post("/api/v1/auth/otp/verify", json={"challenge_id": body["challenge_id"], "code": last_code(EMAIL)})
        assert r.status_code == 200, r.text
    me = (await client.get("/api/v1/auth/me")).json()
    assert me["role"] == "founder"
    r = await client.post("/api/v1/people/invite", json={"name": "First Cofounder", "email": "cofounder@first-run.test",
                                                         "role": "cofounder", "department": "marketing",
                                                         "title": "Co-Founder · Partnerships and Growth", "desk": "partnerships"})
    assert r.status_code == 200, r.text
    assert invite_token("cofounder@first-run.test")


async def test_resend_refuses_once_the_founder_is_active(app):
    with pytest.raises(SystemExit):
        async with session_manager.session_factory() as db:
            await resend(db)
