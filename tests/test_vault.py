"""The Investor Vault: Holder ID plus one-time code, lockout, step-up, shown-once, reissue, and isolation."""

from sqlalchemy import select

from app.models.investors import Investor
from app.models.people import AuditLog
from app.services import SesEmailService
from tests.conftest import last_code, new_client, sign_in, vault_sign_in


async def test_holder_id_alone_never_opens_the_vault(app, seeded):
    number, name, email, holder_id = seeded["holders"][0]
    c = new_client(app)
    r = await c.post("/api/v1/auth/investor/login", json={"holder_id": holder_id, "contact": email})
    assert r.status_code == 200 and "challenge_id" in r.json()
    assert (await c.get("/api/v1/investor/me")).status_code == 401
    assert "sh_vault" not in c.cookies


async def test_holder_sees_only_their_own_stake(app, seeded):
    number, name, email, holder_id = seeded["holders"][0]
    c = await vault_sign_in(app, holder_id, email)
    me = (await c.get("/api/v1/investor/me")).json()
    assert me["internal_number"] == number and me["stake"]["percent"] == 1.5
    assert me["stake"]["value_minor"] == 540_000 and me["stake"]["invested_minor"] == 500_000
    assert me["holder_id_hint"].endswith(holder_id[-4:]) and holder_id not in str(me)
    company = (await c.get("/api/v1/investor/company")).json()
    text = str(company)
    for other in seeded["holders"][1:]:
        assert other[1] not in text and other[2] not in text  # no other holder's name or email
    assert "salary" not in text.lower()  # no individual salaries
    assert company["round"]["percent"] == 78


async def test_exit_estimate_is_an_estimate_and_needs_a_fresh_code(app, seeded):
    _, _, email, holder_id = seeded["holders"][1]
    c = await vault_sign_in(app, holder_id, email)
    est = (await c.get("/api/v1/investor/exit-estimate")).json()
    assert est["exit"]["value_minor"] == 486_000  # GHS 5,400 less 10%
    assert "estimate, not an offer" in est["exit"]["note"]
    # A fresh sign-in counts as fresh, so age the step-up first
    from datetime import timedelta

    from app.core.database import session_manager
    from app.core.security import now
    from app.models.people import AuthSession

    async with session_manager.session_factory() as db:
        inv = (await db.execute(select(Investor).where(Investor.email == email))).scalar_one()
        for s in (await db.execute(select(AuthSession).where(AuthSession.investor_id == inv.id))).scalars():
            s.step_up_at = now() - timedelta(minutes=30)
        await db.commit()
    assert (await c.post("/api/v1/investor/exit-requests", json={"note": "Thinking about it"})).status_code == 428
    r = await c.post("/api/v1/investor/step-up")
    r = await c.post("/api/v1/investor/step-up/verify", json={"challenge_id": r.json()["challenge_id"], "code": last_code(email)})
    assert r.status_code == 200
    r = await c.post("/api/v1/investor/exit-requests", json={"note": "Thinking about it"})
    assert r.status_code == 200 and r.json()["estimate_minor"] == 486_000
    assert (await c.post("/api/v1/investor/exit-requests", json={})).status_code == 409  # one open request at a time
    founder = await sign_in(app, "founder")
    overview = (await founder.get("/api/v1/capital/overview")).json()
    assert any(e["status"] == "open" for e in overview["exit_requests"])
    # Staff never see a full Holder ID
    assert all(h[3] not in str(overview) for h in seeded["holders"])


async def test_five_wrong_tries_lock_the_vault_and_alert_strategy(app, seeded, db):
    _, _, email, holder_id = seeded["holders"][2]
    c = new_client(app)
    before = len(SesEmailService.OUTBOX)
    wrong = "SHAB-CDEF-GHJK"
    for i in range(4):
        assert (await c.post("/api/v1/auth/investor/login", json={"holder_id": wrong, "contact": email})).status_code == 401
    assert (await c.post("/api/v1/auth/investor/login", json={"holder_id": wrong, "contact": email})).status_code == 423
    # even the right ID is refused while locked
    assert (await c.post("/api/v1/auth/investor/login", json={"holder_id": holder_id, "contact": email})).status_code == 423
    alerts = [m for m in SesEmailService.OUTBOX[before:] if m["subject"].startswith("Vault locked")]
    assert alerts and "aaron@streamershub.test" in alerts[0]["to"]
    assert holder_id not in alerts[0]["html"]
    logs = (await db.execute(select(AuditLog).where(AuditLog.action.in_(("vault.sign_in_failed", "vault.locked"))))).scalars().all()
    assert len(logs) >= 5


async def test_lock_vault_ends_the_session(app, seeded):
    _, _, email, holder_id = seeded["holders"][0]
    c = await vault_sign_in(app, holder_id, email)
    token = c.cookies.get("sh_vault")
    await c.post("/api/v1/auth/investor/lock")
    replay = new_client(app)
    replay.cookies.set("sh_vault", token)
    assert (await replay.get("/api/v1/investor/me")).status_code == 401


async def test_vault_cookie_does_not_open_hub_os(app, seeded):
    _, _, email, holder_id = seeded["holders"][0]
    c = await vault_sign_in(app, holder_id, email)
    assert (await c.get("/api/v1/auth/me")).status_code == 401
    assert (await c.get("/api/v1/os/command")).status_code == 401


async def test_onboarding_shows_the_holder_id_once(app, db):
    c = new_client(app)
    token = "sample-invite-investor-four"
    state = (await c.get(f"/api/v1/vault/welcome/{token}")).json()
    assert state["step"] == 1 and not state["investment"]["payment_confirmed"]
    assert (await c.post(f"/api/v1/vault/welcome/{token}/holder-id")).status_code == 400  # not yet
    await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 1})
    assert (await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 2, "legal_name": "Someone Else"})).status_code == 400
    await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 2, "legal_name": "esi owusu"})
    assert (await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 3, "signature": "Esi Owusu"})).status_code == 400
    await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 3, "signature": "Esi Owusu", "agree": True})
    assert (await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 4})).status_code == 409  # Finance first
    finance = await sign_in(app, "ahmed")
    reg = (await finance.get("/api/v1/capital/overview")).json()["register"]
    four = next(r for r in reg if r["number"] == "SH-INV-004")
    r = await finance.post(f"/api/v1/capital/investments/{four['investment_id']}/confirm-payment", json={"reference": "BANK-NOV-004"})
    assert r.status_code == 200
    assert (await finance.post(f"/api/v1/capital/investments/{four['investment_id']}/confirm-payment", json={"reference": "x12"})).status_code == 409
    state = (await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 4})).json()
    assert state["step"] == 5
    r = await c.post(f"/api/v1/vault/welcome/{token}/holder-id")
    holder_id = r.json()["holder_id"]
    assert (await c.post(f"/api/v1/vault/welcome/{token}/holder-id")).status_code == 409  # shown once
    inv = (await db.execute(select(Investor).where(Investor.internal_number == "SH-INV-004"))).scalar_one()
    assert holder_id not in (inv.holder_id_hash or "") and inv.holder_id_last4 == holder_id[-4:]
    assert (await c.post(f"/api/v1/vault/welcome/{token}/finish", json={"saved": False})).status_code == 400
    assert (await c.post(f"/api/v1/vault/welcome/{token}/finish", json={"saved": True})).status_code == 200
    assert (await c.get(f"/api/v1/vault/welcome/{token}")).status_code == 400  # link used up
    v = await vault_sign_in(app, holder_id, "holder4@streamershub.test")
    assert (await v.get("/api/v1/investor/me")).json()["internal_number"] == "SH-INV-004"


async def test_reissue_revokes_the_old_id_and_sessions(app, seeded):
    _, _, email, holder_id = seeded["holders"][0]
    v = await vault_sign_in(app, holder_id, email)
    finance = await sign_in(app, "ahmed")
    overview = (await finance.get("/api/v1/capital/overview")).json()
    inv_id = next(r["id"] for r in overview["register"] if r["number"] == "SH-INV-001")
    assert (await finance.post(f"/api/v1/capital/investors/{inv_id}/reissue-holder-id", json={"checked_by": "Video call"})).status_code == 403
    aaron = await sign_in(app, "aaron")
    r = await aaron.post(f"/api/v1/capital/investors/{inv_id}/reissue-holder-id", json={"checked_by": "Video call with ID shown"})
    assert r.status_code == 200
    assert (await v.get("/api/v1/investor/me")).status_code == 401  # session ended
    c = new_client(app)
    assert (await c.post("/api/v1/auth/investor/login", json={"holder_id": holder_id, "contact": email})).status_code == 401


async def test_only_the_founder_sets_valuations(app):
    ahmed = await sign_in(app, "ahmed")
    body = {"amount": 400000, "effective_date": "2026-12-01", "basis_note": "After the first paid brand deal."}
    assert (await ahmed.post("/api/v1/capital/valuations", json=body)).status_code == 403
    founder = await sign_in(app, "founder")
    assert (await founder.post("/api/v1/capital/valuations", json={**body, "basis_note": "short"})).status_code == 422
    assert (await founder.post("/api/v1/capital/valuations", json=body)).status_code == 200
