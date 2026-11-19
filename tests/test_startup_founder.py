"""A brand-new database (as in production) gets the Founder's account on startup: Kelvin Gbolo, emailed a link to
set a password. Never a second Founder, never a second email, and never over someone else's account."""

import re

import pytest
from sqlalchemy import func, select, text

from app.core.database import load_models, session_manager, setup_schema
from app.main import create_app
from app.models.base import Base
from app.models.people import Department, User
from app.models.posts import Post
from app.services import Bootstrap, SesEmailService
from tests.conftest import new_client

EMAIL = "streamershubhq@gmail.com"


@pytest.fixture(scope="module")
async def fresh(engine):
    load_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        for seq in ("order_number_seq", "design_ref_seq", "interest_number_seq"):
            await conn.execute(text(f"DROP SEQUENCE IF EXISTS {seq}"))
        await setup_schema(conn)
    SesEmailService.OUTBOX.clear()
    return create_app(lifespan_on=False)


def invites() -> list[dict]:
    return [m for m in SesEmailService.OUTBOX if EMAIL in m["to"]]


async def test_startup_creates_the_founder_and_emails_the_invite(fresh):
    async with session_manager.session_factory() as db:
        done = await Bootstrap.run(db)
    assert done["founder_v1"] == "Kelvin Gbolo invited"
    async with session_manager.session_factory() as db:
        founder = (await db.execute(select(User).where(User.role == "founder"))).scalar_one()
        assert (founder.name, founder.email, founder.status, founder.password_hash) == ("Kelvin Gbolo", EMAIL, "invited", None)
        leadership = await db.scalar(select(Department).where(Department.slug == "leadership"))
        assert leadership.lead_user_id == founder.id
        # The live post that goes up in the same start is his
        post = (await db.execute(select(Post).where(Post.slug == Bootstrap.GTA6_LIVE["slug"]))).scalar_one()
        assert post.created_by == founder.id
    assert len(invites()) == 1
    token = re.search(r"set-password\?token=([^&\"]+)", invites()[0]["html"]).group(1)
    invite = (await new_client(fresh).get(f"/api/v1/auth/invite/{token}")).json()
    assert invite["role"] == "founder" and invite["email"] == EMAIL and invite["first_time"] is True


async def test_a_restart_never_makes_a_second_founder_or_sends_again(fresh):
    async with session_manager.session_factory() as db:
        assert await Bootstrap.run(db) == {}
        assert await Bootstrap.founder(db) == "there's a Founder already; left alone"
        # Someone else already holding the address is left alone too
        founder = (await db.execute(select(User).where(User.role == "founder"))).scalar_one()
        founder.role = "cofounder"
        await db.flush()
        assert await Bootstrap.founder(db) == "that email already has an account; left alone"
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(User).where(User.role == "founder")) == 1
    assert len(invites()) == 1
