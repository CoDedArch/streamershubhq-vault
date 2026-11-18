"""Applications (6.1) from the Join page to a working account, and what the public site may show."""

import json

from sqlalchemy import select

from app.models.people import User
from tests.conftest import last_code, new_client, sign_in

CLIPPER = {"handle": "@new.cutter", "email": "newcutter@example.test", "tiktok_url": "tiktok.com/@new.cutter",
           "software": "CapCut", "best_clip_url": "tiktok.com/@new.cutter/video/1", "what_travels": "The first second.", "agree": True}


async def apply(client, track, data, files=None):
    return await client.post("/api/v1/applications", data={"track": track, "data": json.dumps(data)}, files=files or [])


async def test_public_home_never_shows_earnings_or_terms(app):
    r = (await new_client(app).get("/api/v1/public/home")).json()
    text = json.dumps(r).lower()
    for word in ("earned", "salary", "commission_minor", "percent", "valuation", "equity", "contract", "amount_minor"):
        assert word not in text, word
    assert len(r["partners"]) == 20 and r["roster"][0]["stage_name"] == "Galacta" and len(r["seats"]) == 3
    assert [row["handle"] for row in r["clip_table"]["rows"][:3]] == ["@kwesi.cuts", "@nanaframes", "@clipsbyefya"]
    assert r["clip_table"]["rows"][0]["move"] == "▲ 1"
    assert r["join"]["clipper_rate_minor"] is None  # the rate is not public until it is decided


async def test_application_needs_every_field_and_the_tick(app):
    c = new_client(app)
    r = await apply(c, "clipper", {**CLIPPER, "agree": False})
    assert r.status_code == 400
    r = await apply(c, "clipper", {k: v for k, v in CLIPPER.items() if k != "software"})
    assert r.status_code == 400 and "software" in r.json()["detail"]["missing"]
    r = await apply(c, "designer", {"full_name": "Kojo", "handle": "@kojo", "email": "kojo@example.test", "portfolio_url": "x",
                                    "tools": "Procreate", "first_design": "A tee", "agree": True})
    assert r.status_code == 400  # designers must upload samples


async def test_clipper_application_to_working_desk(app, db):
    c = new_client(app)
    r = await apply(c, "clipper", CLIPPER)
    assert r.status_code == 200 and r.json()["reference"].startswith("SH-APP-CLP-")
    assert (await apply(c, "clipper", CLIPPER)).status_code == 409  # already in the queue
    assert not (await db.execute(select(User).where(User.email == CLIPPER["email"]))).scalar_one_or_none()  # no access before approval
    trigger = await sign_in(app, "trigger")
    rows = (await trigger.get("/api/v1/applications?track=clipper")).json()
    row = next(a for a in rows if a["handle"] == "@new.cutter")
    assert (await trigger.post(f"/api/v1/applications/{row['id']}/decision", json={"decision": "decline"})).status_code == 400
    r = await trigger.post(f"/api/v1/applications/{row['id']}/decision", json={"decision": "approve"})
    assert r.json()["status"] == "approved"
    user = (await db.execute(select(User).where(User.email == CLIPPER["email"]))).scalar_one()
    assert user.role == "clipper" and user.status == "invited" and user.handle == "@new.cutter"
    # The invite link sets a password; then sign in with a code and the desk opens
    from app.core.security import hash_token
    from app.services import SesEmailService

    invite = next(m for m in reversed(SesEmailService.OUTBOX) if CLIPPER["email"] in m["to"] and "Clipper Desk" in m["subject"])
    token = invite["html"].split("token=")[1].split("&")[0]
    assert user.invite_token_hash == hash_token(token)
    assert (await c.post("/api/v1/auth/invite/accept", json={"token": token, "password": "Clips-all-day-2026"})).status_code == 200
    c2 = new_client(app)
    r = await c2.post("/api/v1/auth/login", json={"email": CLIPPER["email"], "password": "Clips-all-day-2026"})
    await c2.post("/api/v1/auth/otp/verify", json={"challenge_id": r.json()["challenge_id"], "code": last_code(CLIPPER["email"])})
    me = (await c2.get("/api/v1/auth/me")).json()
    assert me["home"] == "/desk" and me["onboarding"]["kind"] == "clipper"
    assert (await c2.get("/api/v1/clipper/summary")).status_code == 200


async def test_designer_samples_and_review(app):
    c = new_client(app)
    png = b"\x89PNG\r\n\x1a\n" + b"1" * 100
    r = await apply(c, "designer", {"full_name": "Kojo Mensah", "handle": "kojo.prints", "email": "kojo@example.test",
                                    "portfolio_url": "behance.net/kojo", "tools": "Procreate", "first_design": "Accra nights tee", "agree": True},
                    files=[("files", ("a.png", png, "image/png"))])
    assert r.status_code == 200 and r.json()["reference"].startswith("SH-APP-DSN-")
    km = await sign_in(app, "kingmille")
    rows = (await km.get("/api/v1/applications?track=designer")).json()
    row = next(a for a in rows if a["handle"] == "@kojo.prints")
    detail = (await km.get(f"/api/v1/applications/{row['id']}")).json()
    assert detail["files"][0]["url"]
    r = await km.post(f"/api/v1/applications/{row['id']}/decision", json={"decision": "decline", "note": "Needs more print-ready work"})
    assert r.json()["status"] == "declined"


async def test_streamer_approval_invites_to_assessment_then_founder_offers_a_seat(app, db):
    certified = await sign_in(app, "certified")
    rows = (await certified.get("/api/v1/applications?track=streamer")).json()
    row = next(a for a in rows if a["reference"] == "SH-APP-STR-9P2M")
    r = await certified.post(f"/api/v1/applications/{row['id']}/decision", json={"decision": "approve"})
    assert r.json()["status"] == "approved"
    founder = await sign_in(app, "founder")
    command = (await founder.get("/api/v1/os/command")).json()
    offer = next(d for d in command["decisions"] if d["kind"] == "seat_offer" and "9P2M" in d["title"])
    r = await founder.post(f"/api/v1/decisions/{offer['id']}", json={"approve": True, "stage_name": "Nova"})
    assert r.json()["status"] == "approved"
    nova = (await db.execute(select(User).where(User.email == "sh_app_str_9p2m@streamershub.test"))).scalar_one()
    assert nova.role == "streamer" and nova.status == "invited"


async def test_applications_are_rate_limited_by_ip(app):
    c = new_client(app)
    codes = []
    for i in range(12):
        r = await apply(c, "clipper", {**CLIPPER, "handle": f"@spam{i}", "email": f"spam{i}@example.test"})
        codes.append(r.status_code)
    assert 429 in codes
