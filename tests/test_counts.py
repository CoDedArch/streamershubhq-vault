"""The counts beside each Hub OS desk and in the Investor Vault: what is waiting on that page."""

from sqlalchemy import func, select

from app.models.investors import InvestorDocument, InvestorReport
from app.models.money import Decision
from app.models.posts import TicketBooking
from app.models.tech import EquipmentIssue, Incident
from tests.conftest import new_client, sign_in, vault_sign_in


def counts(me: dict) -> dict:
    return {i["area"]: i["count"] for g in me["nav"] for i in g["items"]}


async def test_each_desk_shows_what_is_waiting_on_it(app, db):
    founder = counts((await (await sign_in(app, "founder")).get("/api/v1/auth/me")).json())
    open_decisions = await db.scalar(select(func.count()).select_from(Decision).where(Decision.status == "open"))
    assert founder["command"] == (open_decisions or None)
    issues = await db.scalar(select(func.count()).select_from(EquipmentIssue).where(EquipmentIssue.status == "open"))
    incidents = await db.scalar(select(func.count()).select_from(Incident).where(Incident.resolved_at.is_(None)))
    assert founder["tech"] == issues + incidents and issues >= 1
    reservations = await db.scalar(select(func.count()).select_from(TicketBooking).where(TicketBooking.status == "interest"))
    assert founder["posts"] == reservations == 3  # the sample event's reservations to call about
    for area in ("production", "partnerships", "creative", "store", "inbox", "people"):
        assert founder[area], area
    assert founder["settings"] is None  # nothing to act on there

    # A streamer sees their own brand work and their open tasks
    galacta = counts((await (await sign_in(app, "galacta")).get("/api/v1/auth/me")).json())
    assert galacta["talent"] and galacta["me"]
    assert set(galacta) == {"talent", "me"}

    # Resolving a reported problem takes it off Tech's count
    bernard = await sign_in(app, "bernard")
    before = counts((await bernard.get("/api/v1/auth/me")).json())["tech"]
    issue = (await db.execute(select(EquipmentIssue).where(EquipmentIssue.status == "open"))).scalars().first()
    r = await bernard.post(f"/api/v1/equipment-issues/{issue.id}/resolve", json={"resolution": "Swapped the controller"})
    assert r.status_code == 200, r.text
    assert (counts((await bernard.get("/api/v1/auth/me")).json())["tech"] or 0) == before - 1


async def test_the_vault_counts_unread_reports_and_new_documents(app, db, seeded):
    from app.models.investors import Investor

    _, _, email, holder_id = seeded["holders"][0]
    published = await db.scalar(select(func.count()).select_from(InvestorReport).where(InvestorReport.published_at.is_not(None)))
    c = await vault_sign_in(app, holder_id, email)
    me = (await c.get("/api/v1/investor/me")).json()
    assert me["reports_unread"] == published and published >= 1
    first = next(r for r in (await c.get("/api/v1/investor/reports")).json() if r["published_at"])
    assert first["read"] is False
    assert (await c.get(f"/api/v1/investor/reports/{first['id']}")).status_code == 200
    assert (await c.get(f"/api/v1/investor/reports/{first['id']}")).status_code == 200  # reading twice counts once
    assert (await c.get("/api/v1/investor/me")).json()["reports_unread"] == published - 1
    assert next(r for r in (await c.get("/api/v1/investor/reports")).json() if r["id"] == first["id"])["read"] is True

    # A document is new once its file is uploaded, until the holder downloads it
    investor = (await db.execute(select(Investor).where(Investor.email == email))).scalar_one()
    doc = (await db.execute(select(InvestorDocument).where(InvestorDocument.investor_id == investor.id,
                                                           InvestorDocument.kind == "certificate"))).scalar_one()
    before = (await c.get("/api/v1/investor/me")).json()["documents_new"]
    doc.file = "sh/investors/certificate.pdf"
    await db.commit()
    assert (await c.get("/api/v1/investor/me")).json()["documents_new"] == before + 1
    assert next(d for d in (await c.get("/api/v1/investor/documents")).json() if d["id"] == str(doc.id))["new"] is True


async def test_the_label_link_page_uses_the_official_mark(app):
    page = (await new_client(app).get("/api/v1/public/links/streamershub")).json()
    assert page["avatar_url"] == "/brand/sh-mark-avatar.png"
