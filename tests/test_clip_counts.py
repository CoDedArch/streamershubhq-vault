"""When clip views are counted: a first check after posting, a month-end count before the payout run, then the month
locks at submission. Counts change only with a reason, the draft run follows every change, and clips still waiting
when a month locks move to the next month instead of never being paid."""

import uuid

from sqlalchemy import select

from app.models.clips import Clip
from app.models.money import PayrollLine, PayrollRun
from app.models.people import AuditLog
from app.services.CompanyService import today
from app.services.money import month_key, next_month
from tests.conftest import sign_in

NOW = month_key(today())
NEXT = next_month(NOW)


async def a_verified_clip(db, period):
    return await db.scalar(select(Clip).where(Clip.period == period, Clip.status == "verified").order_by(Clip.posted_at).limit(1))


async def test_this_months_run_is_submitted_so_its_counts_are_locked(app, db):
    trigger = await sign_in(app, "trigger")
    clip = await a_verified_clip(db, NOW)
    r = await trigger.post(f"/api/v1/clips/{clip.id}/correct", json={"views_verified": 1, "reason": "Month-end count"})
    assert r.status_code == 409 and "Founder" in r.json()["detail"]
    assert (await trigger.post(f"/api/v1/clips/{clip.id}/verify", json={"views_verified": 1})).status_code == 409
    # Nothing waiting for a first check was left behind in the locked month
    assert not (await db.execute(select(Clip).where(Clip.period == NOW, Clip.status.in_(("submitted", "checking"))))).scalars().all()
    kwesi = await sign_in(app, "kwesi_cuts")
    r = await kwesi.post("/api/v1/clips", json={"url": "https://www.tiktok.com/@kwesi.cuts/video/after-submit", "platform": "tiktok",
                                                "title": "Posted after the run went in", "views_reported": 500})
    clip = await db.get(Clip, uuid.UUID(r.json()["id"]))
    assert clip.period == NEXT  # counts next month


async def test_month_end_view_shows_what_is_left_to_do(app, db):
    trigger = await sign_in(app, "trigger")
    state = (await trigger.get("/api/v1/clips/month-end")).json()
    assert state["period"] == NEXT and state["run_status"] is None  # the open month is next month now
    assert state["totals"]["first_checks"] >= 2  # the two YouTube clips, plus anything moved forward
    youtube = next(c for c in state["clips"] if "anthem" in c["title"])
    assert youtube["views_fetched"] == 11550 and youtube["gap_warning"]
    assert (await (await sign_in(app, "kwesi_cuts")).get("/api/v1/clips/month-end")).status_code == 403


async def test_once_sent_back_a_count_can_be_corrected_and_the_draft_follows(app, db):
    founder = await sign_in(app, "founder")
    run = await db.scalar(select(PayrollRun).where(PayrollRun.period == NOW))
    assert (await founder.post(f"/api/v1/payroll/runs/{run.id}/send-back", json={"note": "Recount clips"})).status_code == 200
    clip = await a_verified_clip(db, NOW)
    line = await db.scalar(select(PayrollLine).where(PayrollLine.run_id == run.id, PayrollLine.payee_user_id == clip.clipper_user_id,
                                                     PayrollLine.kind == "clipper_payout"))
    before_total, before_line = run.total_minor, line.amount_minor if line else 0
    trigger = await sign_in(app, "trigger")
    assert (await trigger.post(f"/api/v1/clips/{clip.id}/correct", json={"views_verified": 5, "reason": "x"})).status_code == 422
    assert (await trigger.post(f"/api/v1/clips/{clip.id}/verify", json={"views_verified": 5})).status_code == 409  # use correct
    new_count = clip.views_verified + 100_000
    r = await trigger.post(f"/api/v1/clips/{clip.id}/correct", json={"views_verified": new_count, "reason": "Month-end count"})
    assert r.status_code == 200 and r.json()["run_recalculated"] and r.json()["count_note"] == "Month-end count"
    await db.refresh(run)
    line = await db.scalar(select(PayrollLine).where(PayrollLine.run_id == run.id, PayrollLine.payee_user_id == clip.clipper_user_id,
                                                     PayrollLine.kind == "clipper_payout"))
    assert run.total_minor > before_total and line.amount_minor > before_line  # the run already pays the new count
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "clip.count_corrected", AuditLog.entity_id == str(clip.id)))
    assert log.before == {"views_verified": new_count - 100_000} and log.after == {"views_verified": new_count}
    assert (await (await sign_in(app, "kwesi_cuts")).post(f"/api/v1/clips/{clip.id}/correct",
                                                          json={"views_verified": 1, "reason": "Mine"})).status_code == 403


async def test_month_end_youtube_update_brings_verified_clips_up_to_date(app, db):
    trigger = await sign_in(app, "trigger")
    state = (await trigger.get("/api/v1/clips/month-end")).json()
    row = await db.scalar(select(Clip).where(Clip.title == "Signed the cap live for a viewer"))
    row.period = state["period"]  # whichever month is open now (the run above was sent back)
    await db.commit()
    clip = {"id": str(row.id)}
    assert (await trigger.post(f"/api/v1/clips/{clip['id']}/verify", json={"views_verified": 6000})).status_code == 200
    await db.refresh(row)
    row.views_fetched = 9400  # YouTube has moved on since the first check
    await db.commit()
    state = (await trigger.get("/api/v1/clips/month-end")).json()
    assert state["totals"]["youtube_ahead"] == 1
    r = (await trigger.post("/api/v1/clips/month-end/youtube")).json()
    assert r["updated"] == 1
    await db.refresh(row)
    assert row.views_verified == 9400 and row.count_note == "Month-end count from YouTube"
    clipper = (await (await sign_in(app, "nanaframes")).get("/api/v1/clipper/summary")).json()
    mine = next(c for c in clipper["clips"] if c["id"] == clip["id"])
    assert mine["count_note"] == "Month-end count from YouTube" and mine["views"] == 9400


async def test_submitting_a_run_moves_unchecked_clips_to_the_next_month(app, db):
    kwesi = await sign_in(app, "kwesi_cuts")
    r = await kwesi.post("/api/v1/clips", json={"url": "https://www.tiktok.com/@kwesi.cuts/video/late-in-month", "platform": "tiktok",
                                                "title": "Late in the month", "views_reported": 900})
    clip = await db.get(Clip, uuid.UUID(r.json()["id"]))
    assert clip.period == NOW  # the run was sent back, so this month is open again
    ahmed = await sign_in(app, "ahmed")
    run = await db.scalar(select(PayrollRun).where(PayrollRun.period == NOW))
    assert (await ahmed.post(f"/api/v1/payroll/runs/{run.id}/submit")).status_code == 200
    await db.refresh(clip)
    assert clip.period == NEXT and clip.status == "submitted"  # still checked and paid, just next month
    log = await db.scalar(select(AuditLog).where(AuditLog.action == "payroll.submitted").order_by(AuditLog.created_at.desc()).limit(1))
    assert "moved to next month" in log.summary
