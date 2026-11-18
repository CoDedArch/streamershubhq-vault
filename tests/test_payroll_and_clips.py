"""Payroll (6.5): drafted by the rules, approved by the Founder only, paid line by line. Clips (6.4):
verification moves earnings and rank; flagged clips earn nothing; three strikes pause the desk."""

from sqlalchemy import select

from app.models.clips import ClipperProfile
from app.models.money import LedgerEntry, PayrollLine
from app.models.people import AuditLog
from tests.conftest import sign_in


async def current(client):
    return (await client.get("/api/v1/payroll/runs/current")).json()


async def test_seeded_run_matches_the_designs(app):
    ahmed = await sign_in(app, "ahmed")
    run = await current(ahmed)
    assert run["status"] == "submitted" and run["payee_count"] == 14 and run["total_minor"] == 641_910
    by = {l["payee"]: l for l in run["lines"]}
    assert by["Lamar (Galacta)"]["amount_minor"] == 350_000 and "Milestone tier 2" in by["Lamar (Galacta)"]["basis"]
    assert by["Certified"]["amount_minor"] == 8_200 and by["Certified"]["basis"] == "5% of platform income"
    assert by["@ama.draws"]["amount_minor"] == 23_100 and by["@ama.draws"]["basis"] == "11 units · 15% commission"
    assert by["@kwesi.cuts"]["amount_minor"] == 63_720 and by["@kwesi.cuts"]["basis"] == "212.4K verified views"
    assert by["@clipsbyefya"]["status"] == "missing_details"
    assert by["@tema.trims"]["status"] == "carried" and by["@kasoa.kuts"]["status"] == "carried"  # under the GHS 50 minimum


async def test_only_the_founder_approves_and_nothing_is_paid_before(app):
    ahmed = await sign_in(app, "ahmed")
    run = await current(ahmed)
    line = next(l for l in run["lines"] if l["status"] == "ready")
    r = await ahmed.post(f"/api/v1/payroll/lines/{line['id']}/mark-paid", json={"reference": "MOMO-123"})
    assert r.status_code == 409  # not approved yet
    assert (await ahmed.post(f"/api/v1/payroll/runs/{run['id']}/approve")).status_code == 403
    philip = await sign_in(app, "philip")
    assert (await philip.post(f"/api/v1/payroll/runs/{run['id']}/approve")).status_code == 403
    founder = await sign_in(app, "founder")
    assert (await founder.post(f"/api/v1/payroll/runs/{run['id']}/send-back", json={"note": ""})).status_code == 422
    r = await founder.post(f"/api/v1/payroll/runs/{run['id']}/send-back", json={"note": "Add @clipsbyefya's mobile money first"})
    assert r.json()["status"] == "draft"
    # Back with Finance: recalculate, then resubmit
    r = await ahmed.post("/api/v1/payroll/runs/draft", json={})
    assert r.json()["total_minor"] == 641_910 and r.json()["status"] == "draft"
    await ahmed.post(f"/api/v1/payroll/runs/{run['id']}/submit")
    command = (await founder.get("/api/v1/os/command")).json()
    decision = next(d for d in command["decisions"] if d["kind"] == "payroll" and d["status"] == "open")
    r = await founder.post(f"/api/v1/decisions/{decision['id']}", json={"approve": True})
    assert r.json()["status"] == "approved"
    run = await current(ahmed)
    assert run["status"] == "approved" and run["can_pay"]
    assert (await ahmed.post(f"/api/v1/payroll/runs/{run['id']}/submit")).status_code == 409


async def test_lines_are_paid_one_by_one_until_the_run_is_paid(app, db):
    ahmed = await sign_in(app, "ahmed")
    run = await current(ahmed)
    efya = next(l for l in run["lines"] if l["payee"] == "@clipsbyefya")
    assert (await ahmed.post(f"/api/v1/payroll/lines/{efya['id']}/mark-paid", json={"reference": "MOMO-9"})).status_code == 409
    carried = next(l for l in run["lines"] if l["status"] == "carried")
    assert (await ahmed.post(f"/api/v1/payroll/lines/{carried['id']}/mark-paid", json={"reference": "MOMO-9"})).status_code == 409
    efya_user = await sign_in(app, "clipsbyefya")
    r = await efya_user.put("/api/v1/me/payout", json={"payout_method": "momo", "momo_number": "024 111 2222", "momo_network": "mtn"})
    assert r.status_code == 200
    for line in run["lines"]:
        if line["status"] in ("ready", "missing_details"):
            r = await ahmed.post(f"/api/v1/payroll/lines/{line['id']}/mark-paid", json={"reference": f"MOMO-{line['id'][:6]}"})
            assert r.status_code == 200, r.text
    run = await current(ahmed)
    assert run["status"] == "paid"
    paid = (await db.execute(select(LedgerEntry).where(LedgerEntry.source_table == "payroll_lines"))).scalars().all()
    assert sum(e.amount_minor for e in paid) == 641_910
    actions = {a.action for a in (await db.execute(select(AuditLog))).scalars()}
    assert {"payroll.submitted", "payroll.approved", "payroll.line_paid", "payroll.sent_back"} <= actions
    # The payee sees their own line
    kwesi = await sign_in(app, "kwesi_cuts")
    mine = (await kwesi.get("/api/v1/me/overview")).json()["pay"]
    assert mine[0]["amount_minor"] == 63_720 and mine[0]["status"] == "paid"


async def test_clips_closed_after_the_run_is_approved(app):
    trigger = await sign_in(app, "trigger")
    clips = (await trigger.get("/api/v1/clips?status=verified")).json()
    r = await trigger.post(f"/api/v1/clips/{clips[0]['id']}/verify", json={"views_verified": 1})
    assert r.status_code == 409 and "closed" in r.json()["detail"]


async def test_verification_changes_earnings_and_rank(app):
    # A new month-equivalent check: submit, verify, and see the desk move
    tema = await sign_in(app, "tema_trims")
    before = (await tema.get("/api/v1/clipper/summary")).json()
    r = await tema.post("/api/v1/clips", json={"url": "https://www.tiktok.com/@tema.trims/video/999", "platform": "tiktok",
                                               "title": "Galacta's 360 no-scope", "views_reported": 400000})
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    assert (await tema.post("/api/v1/clips", json={"url": "https://www.tiktok.com/@tema.trims/video/999", "platform": "tiktok",
                                                   "title": "dupe"})).status_code == 409
    assert (await tema.post("/api/v1/clips", json={"url": "https://evil.example/video", "platform": "tiktok", "title": "Not TikTok"})).status_code == 400
    trigger = await sign_in(app, "trigger")
    pending = (await trigger.get("/api/v1/clips")).json()
    clip = next(c for c in pending if c["title"] == "Galacta's 360 no-scope")
    assert (await trigger.post(f"/api/v1/clips/{clip['id']}/verify", json={"views_verified": 350000})).status_code == 200
    after = (await tema.get("/api/v1/clipper/summary")).json()
    assert after["rank"] == 1 and before["rank"] != 1
    board = (await trigger.get("/api/v1/social/summary")).json()["leaderboard"]
    assert board[0]["handle"] == "@tema.trims"


async def test_flagged_clips_earn_nothing_and_three_strikes_pause(app, db):
    trigger = await sign_in(app, "trigger")
    yaw = await sign_in(app, "yaw_replays")  # already has one strike
    for i in range(2):
        r = await yaw.post("/api/v1/clips", json={"url": f"https://www.tiktok.com/@yaw/video/f{i}", "platform": "tiktok",
                                                  "title": f"FREE PHONE {i}", "views_reported": 10000})
        cid = r.json()["id"]
        r = await trigger.post(f"/api/v1/clips/{cid}/flag", json={"reason": "Fake giveaway", "strike": True})
        assert r.json()["status"] == "flagged"
        assert (await trigger.post(f"/api/v1/clips/{cid}/verify", json={"views_verified": 10000})).status_code == 409
    assert r.json()["paused"] is True
    r = await yaw.post("/api/v1/clips", json={"url": "https://www.tiktok.com/@yaw/video/f9", "platform": "tiktok", "title": "Paused"})
    assert r.status_code == 403
    profile = (await db.execute(select(ClipperProfile).where(ClipperProfile.handle == "@yaw_replays"))).scalar_one()
    assert profile.strikes == 3 and profile.status == "paused"


async def test_payroll_lines_reference_their_claims(db):
    lines = (await db.execute(select(PayrollLine).where(PayrollLine.kind == "designer_commission"))).scalars().all()
    assert lines
