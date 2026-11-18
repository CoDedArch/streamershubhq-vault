"""Staff onboarding (6.7), the department desks' summaries, and the audit trail for role changes."""

from sqlalchemy import select

from app.models.people import AuditLog
from tests.conftest import sign_in


async def test_staff_onboarding_saves_each_step(app):
    editor = await sign_in(app, "editor")
    state = (await editor.get("/api/v1/onboarding/me")).json()
    assert state["step"] == 3 and state["steps"][2] == "Your profile"
    assert (await editor.post("/api/v1/onboarding/me/steps/4", json={})).status_code == 400  # can't skip
    r = await editor.post("/api/v1/onboarding/me/steps/3", json={"payout_method": "momo", "momo_number": "12"})
    assert r.status_code == 400
    r = await editor.post("/api/v1/onboarding/me/steps/3", json={"payout_method": "momo", "momo_number": "024 333 4444",
                                                                 "momo_network": "telecel", "emergency_contact": "Mum · 020 111 2222"})
    assert r.json()["step"] == 4 and r.json()["profile"]["momo_hint"] == "MoMo ··· 444"
    tools = r.json()["tools"]
    assert (await editor.post("/api/v1/onboarding/me/steps/4", json={"tools": tools[:1]})).status_code == 400
    r = await editor.post("/api/v1/onboarding/me/steps/4", json={"tools": tools})
    r = await editor.post("/api/v1/onboarding/me/steps/5", json={})
    assert r.json()["done"] is True
    founder = await sign_in(app, "founder")
    pipeline = (await founder.get("/api/v1/people")).json()["pipeline"]
    assert all("Video editor" not in p["people"] for p in pipeline)


async def test_every_desk_summary_has_what_its_screen_needs(app):
    founder = await sign_in(app, "founder")
    cmd = (await founder.get("/api/v1/os/command")).json()
    assert len(cmd["kpis"]) == 6 and len(cmd["targets"]) == 4 and len(cmd["departments"]) == 8
    kpis = {k["key"]: k for k in cmd["kpis"]}
    assert kpis["revenue"]["money"] == 382_000 and kpis["revenue"]["note"] == "▲ 41% vs last month"
    assert kpis["runway"]["value"] == "7.4 months" and kpis["subs"]["value"] == "212" and kpis["subs"]["note"] == "▲ 68 this month"
    assert kpis["pipeline"]["money"] == 4_600_000 and kpis["store"]["money"] == 148_000
    assert cmd["round"]["committed_minor"] == 12_500_000 and cmd["round"]["percent"] == 78
    assert {d["kind"] for d in cmd["decisions"] if d["status"] == "open"} >= {"payroll", "deal", "budget", "seat_offer", "release"}
    prod = (await founder.get("/api/v1/production/summary")).json()
    assert prod["current"]["live"] and len(prod["run_of_show"]) == 7 and len(prod["health"]) == 5 and len(prod["last7"]) == 7
    part = (await founder.get("/api/v1/partnerships/summary")).json()
    stages = {s["key"]: s for s in part["stages"]}
    assert len(stages["contacted"]["cards"]) == 11 and stages["at_contract"]["cards"][0]["name"] == "Rush+ Energy"
    social = (await founder.get("/api/v1/social/summary")).json()
    assert social["platforms"][0]["value"] == 1120 and social["platforms"][0]["change"] == 164
    creative = (await founder.get("/api/v1/creative/summary")).json()
    assert [c["key"] for c in creative["board"]] == ["requested", "in_progress", "in_review", "delivered"]
    tech = (await founder.get("/api/v1/tech/summary")).json()
    assert len(tech["equipment"]) == 9 and tech["kpis"]["open_incidents"] == 1
    talent = (await founder.get("/api/v1/talent/summary")).json()
    assert talent["pay"]["tier"] == "Milestone tier 2" and talent["pay"]["salary_minor"] == 350_000
    fin = (await founder.get("/api/v1/finance/summary")).json()
    assert fin["money"]["income_minor"] == 382_000 and fin["money"]["spend_minor"] == 491_000 and fin["money"]["cash_minor"] == 4_120_000
    assert [r["label"] for r in fin["pnl"] if r.get("total")] == ["Total income", "Net · month to date"]


async def test_role_changes_are_audited(app, db):
    founder = await sign_in(app, "founder")
    people = (await founder.get("/api/v1/people")).json()["team"]
    c9 = next(p for p in people if p["name"] == "C9PER Multimedia")
    r = await founder.patch(f"/api/v1/people/{c9['id']}", json={"desk": "social"})
    assert r.status_code == 200
    log = (await db.execute(select(AuditLog).where(AuditLog.action == "people.role_changed"))).scalars().first()
    assert log and log.before["desk"] == "creative" and log.after["desk"] == "social"
    kingmille = await sign_in(app, "kingmille")
    assert (await kingmille.patch(f"/api/v1/people/{c9['id']}", json={"role": "founder"})).status_code == 403


async def test_staff_invite_respects_department(app):
    kingmille = await sign_in(app, "kingmille")
    r = await kingmille.post("/api/v1/people/invite", json={"name": "New Designer", "email": "newdesigner@example.test", "role": "staff",
                                                           "department": "creative", "title": "Junior designer", "desk": "creative"})
    assert r.status_code == 200
    r = await kingmille.post("/api/v1/people/invite", json={"name": "Outsider", "email": "out@example.test", "role": "staff",
                                                           "department": "finance", "title": "Accountant"})
    assert r.status_code == 403
