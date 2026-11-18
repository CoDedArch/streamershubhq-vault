"""Deploy safety: files go to S3 whenever a bucket is set (keys or the task role), production refuses development
defaults, and only one instance runs a scheduler pass at a time."""

import asyncio
import logging

import pytest
from sqlalchemy import select, text

from app.core.config import settings
from app.core.database import session_manager
from app.main import refuse_unsafe_production
from app.models.people import User
from app.services import S3Service, Scheduler
from tests.conftest import DOMAIN, sign_in

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


def test_s3_whenever_a_bucket_is_set_and_local_only_outside_production(monkeypatch):
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", "sh-bucket")
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", None)
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", None)
    monkeypatch.setenv("SH_STORAGE", "local")
    assert not S3Service.use_s3()  # local dev keeps SH_STORAGE=local
    monkeypatch.delenv("SH_STORAGE")
    assert S3Service.use_s3()  # no keys needed: the task role signs
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", None)
    assert not S3Service.use_s3()

    # Production never stores on the container's disk, and says so when it starts
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(RuntimeError, match="AWS_S3_BUCKET"):
        S3Service.put("posts", "flyer.png", b"\x89PNG", "image/png")
    monkeypatch.setenv("SH_STORAGE", "local")
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", "sh-bucket")
    assert S3Service.use_s3()
    assert S3Service.production_problems() == ["SH_STORAGE=local is for development only."]


def test_s3_uses_the_keys_when_both_are_set_otherwise_the_default_chain(monkeypatch):
    import boto3

    calls = []
    monkeypatch.setattr(boto3, "client", lambda *a, **kw: calls.append(kw))
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", None)
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", None)
    S3Service._client()
    monkeypatch.setattr(settings, "AWS_ACCESS_KEY_ID", "id")
    monkeypatch.setattr(settings, "AWS_SECRET_ACCESS_KEY", "secret")
    S3Service._client()
    assert "aws_access_key_id" not in calls[0]
    assert calls[1]["aws_access_key_id"] == "id" and calls[1]["aws_secret_access_key"] == "secret"


def test_production_refuses_development_defaults(monkeypatch, caplog):
    default_key = type(settings).model_fields["SECRET_KEY"].default
    monkeypatch.setattr(settings, "SECRET_KEY", default_key)
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", None)
    refuse_unsafe_production()  # not production: never checked

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    with pytest.raises(RuntimeError) as err:
        refuse_unsafe_production()
    assert "SECRET_KEY" in str(err.value) and "AWS_S3_BUCKET" in str(err.value)

    monkeypatch.setattr(settings, "SECRET_KEY", "a-long-random-production-value")
    monkeypatch.setattr(settings, "AWS_S3_BUCKET", "sh-bucket")
    monkeypatch.delenv("SH_STORAGE", raising=False)
    monkeypatch.setattr(settings, "SH_DATA_KEY", None)
    with caplog.at_level(logging.WARNING):
        refuse_unsafe_production()
    assert "SH_DATA_KEY is not set" in caplog.text
    caplog.clear()
    monkeypatch.setattr(settings, "SH_DATA_KEY", "a-separate-data-key")
    refuse_unsafe_production()
    assert "SH_DATA_KEY" not in caplog.text


async def test_one_scheduler_pass_at_a_time(engine, monkeypatch):
    passes = []

    async def slow_pass():
        passes.append(1)
        await asyncio.sleep(0.3)
        return ["commission_payable"]

    monkeypatch.setattr(Scheduler, "_run_due_jobs", slow_pass)
    # Two instances at the same moment: one runs, the other skips
    results = await asyncio.gather(Scheduler.run_due_jobs(), Scheduler.run_due_jobs())
    assert sorted(results) == [[], ["commission_payable"]] and len(passes) == 1
    # The lock is let go afterwards
    assert await Scheduler.run_due_jobs() == ["commission_payable"]

    # Held by another instance: skipped
    async with engine.connect() as other:
        await other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": Scheduler.LOCK_KEY})
        assert await Scheduler.run_due_jobs() == []
        await other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": Scheduler.LOCK_KEY})
    assert len(passes) == 2


async def test_a_profile_photo_keeps_its_file_and_gets_a_fresh_link_each_time(app):
    client = await sign_in(app, "trigger")
    r = await client.post("/api/v1/me/photo", files={"file": ("me.png", PNG, "image/png")})
    assert r.status_code == 200 and "sig=" in r.json()["avatar_url"]
    # Saved: the file key, never a signed link (those die with the credentials that signed them)
    async with session_manager.session_factory() as db:
        user = (await db.execute(select(User).where(User.email == f"trigger@{DOMAIN}"))).scalar_one()
    assert user.avatar_url.startswith(f"{settings.SH_S3_PREFIX}/avatars/") and "?" not in user.avatar_url
    for path in ("/api/v1/auth/me", "/api/v1/onboarding/me"):
        body = (await client.get(path)).json()
        shown = body["avatar_url"] if "avatar_url" in body else body["profile"]["avatar_url"]
        assert shown.startswith("http://api.test/api/v1/files/") and "sig=" in shown
        assert (await client.get(shown.removeprefix("http://api.test"))).content == PNG
