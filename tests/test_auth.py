"""Sign-in: password plus a one-time code, sessions that really end, invites, resets and rate limits."""

from datetime import timedelta

from sqlalchemy import select, update

from app.core.security import now
from app.models.people import LoginChallenge, User
from app.services import SesEmailService
from tests.conftest import DOMAIN, PASSWORD, last_code, new_client, sign_in


async def test_os_sign_in_needs_the_emailed_code(app):
    c = new_client(app)
    r = await c.post("/api/v1/auth/login", json={"email": f"ahmed@{DOMAIN}", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["step"] == "code"
    assert "sh_session" not in c.cookies  # no session before the code
    assert (await c.get("/api/v1/auth/me")).status_code == 401
    challenge = r.json()["challenge_id"]
    wrong = "000000" if last_code(f"ahmed@{DOMAIN}") != "000000" else "111111"
    r2 = await c.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge, "code": wrong})
    assert r2.status_code == 400 and "tries left" in r2.json()["detail"]
    r3 = await c.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge, "code": last_code(f"ahmed@{DOMAIN}")})
    assert r3.status_code == 200
    me = (await c.get("/api/v1/auth/me")).json()
    assert me["role"] == "finance" and me["home"] == "/os/finance" and "finance" in me["areas"]
    # the code works once
    r4 = await c.post("/api/v1/auth/otp/verify", json={"challenge_id": challenge, "code": last_code(f"ahmed@{DOMAIN}")})
    assert r4.status_code == 400


async def test_codes_lock_after_five_wrong_tries(app):
    c = new_client(app)
    r = await c.post("/api/v1/auth/login", json={"email": f"trigger@{DOMAIN}", "password": PASSWORD})
    cid = r.json()["challenge_id"]
    real = last_code(f"trigger@{DOMAIN}")
    wrong = "123456" if real != "123456" else "654321"
    for _ in range(5):
        await c.post("/api/v1/auth/otp/verify", json={"challenge_id": cid, "code": wrong})
    r = await c.post("/api/v1/auth/otp/verify", json={"challenge_id": cid, "code": real})
    assert r.status_code == 429


async def test_expired_code_is_refused(app, db):
    c = new_client(app)
    r = await c.post("/api/v1/auth/login", json={"email": f"bernard@{DOMAIN}", "password": PASSWORD})
    cid = r.json()["challenge_id"]
    await db.execute(update(LoginChallenge).where(LoginChallenge.id == cid).values(expires_at=now() - timedelta(seconds=1)))
    await db.commit()
    r = await c.post("/api/v1/auth/otp/verify", json={"challenge_id": cid, "code": last_code(f"bernard@{DOMAIN}")})
    assert r.status_code == 400 and "expired" in r.json()["detail"]


async def test_wrong_password_and_unknown_email_look_the_same(app):
    c = new_client(app)
    a = await c.post("/api/v1/auth/login", json={"email": f"ahmed@{DOMAIN}", "password": "wrong-password"})
    b = await c.post("/api/v1/auth/login", json={"email": f"nobody@{DOMAIN}", "password": "wrong-password"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


async def test_sign_out_revokes_the_session_for_good(app):
    c = await sign_in(app, "kingmille")
    token = c.cookies.get("sh_session")
    assert (await c.post("/api/v1/auth/logout")).status_code == 200
    replay = new_client(app)
    replay.cookies.set("sh_session", token)
    assert (await replay.get("/api/v1/auth/me")).status_code == 401


async def test_suspended_account_is_refused(app, db):
    c = await sign_in(app, "c9per")
    await db.execute(update(User).where(User.email == f"c9per@{DOMAIN}").values(status="suspended"))
    await db.commit()
    assert (await c.get("/api/v1/auth/me")).status_code == 403
    await db.execute(update(User).where(User.email == f"c9per@{DOMAIN}").values(status="active"))
    await db.commit()


async def test_invite_link_sets_password_once(app):
    c = new_client(app)
    r = await c.get("/api/v1/auth/invite/sample-invite-community-lead")
    assert r.status_code == 200 and r.json()["name"] == "Community lead"
    assert (await c.post("/api/v1/auth/invite/accept", json={"token": "sample-invite-community-lead", "password": "short"})).status_code == 422
    r = await c.post("/api/v1/auth/invite/accept", json={"token": "sample-invite-community-lead", "password": "A-long-new-password-1"})
    assert r.status_code == 200
    assert (await c.post("/api/v1/auth/invite/accept", json={"token": "sample-invite-community-lead", "password": "Another-password-2"})).status_code == 400
    r = await c.post("/api/v1/auth/login", json={"email": f"community@{DOMAIN}", "password": "A-long-new-password-1"})
    assert r.status_code == 200 and r.json()["step"] == "code"


async def test_forgot_password_answers_the_same_for_unknown_emails(app):
    c = new_client(app)
    before = len(SesEmailService.OUTBOX)
    a = await c.post("/api/v1/auth/password/forgot", json={"email": f"kofi_inks@{DOMAIN}"})
    b = await c.post("/api/v1/auth/password/forgot", json={"email": f"ghost@{DOMAIN}"})
    assert a.status_code == b.status_code == 200 and a.json() == b.json()
    assert len(SesEmailService.OUTBOX) == before + 1  # only the real account got a link


async def test_resend_is_throttled(app):
    c = new_client(app)
    r = await c.post("/api/v1/auth/login", json={"email": f"certified@{DOMAIN}", "password": PASSWORD})
    r2 = await c.post("/api/v1/auth/otp/resend", json={"challenge_id": r.json()["challenge_id"]})
    assert r2.status_code == 429


async def test_cookie_is_httponly_and_session_scoped(app):
    c = new_client(app)
    r = await c.post("/api/v1/auth/login", json={"email": f"philip@{DOMAIN}", "password": PASSWORD})
    r = await c.post("/api/v1/auth/otp/verify", json={"challenge_id": r.json()["challenge_id"], "code": last_code(f"philip@{DOMAIN}")})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    assert r.headers.get("cache-control") == "no-store"


async def test_rate_limit_by_contact(app, db):
    c = new_client(app)
    for _ in range(8):
        await c.post("/api/v1/auth/login", json={"email": f"aaron@{DOMAIN}", "password": "nope"})
    r = await c.post("/api/v1/auth/login", json={"email": f"aaron@{DOMAIN}", "password": PASSWORD})
    assert r.status_code == 429
    user = (await db.execute(select(User).where(User.email == f"aaron@{DOMAIN}"))).scalar_one()
    assert user.locked_until is not None  # 8 wrong passwords also lock the account for 15 minutes
