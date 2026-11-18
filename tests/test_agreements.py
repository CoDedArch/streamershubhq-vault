"""Agreements: the streamer's 6-month trial and the full contract after it, Founder-owned templates, custom terms per
person, investor agreements signed in the vault, and the Founder's tracker."""

import re
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from sqlalchemy import select

from app.models.agreements import Agreement, AgreementTemplate
from app.models.money import Decision
from app.models.people import EquityGrant, User
from app.models.talent import Streamer
from app.services import AgreementService, SesEmailService
from app.services.CompanyService import today
from tests.conftest import last_code, new_client, sign_in

PASSWORD = "New-Joiner-Pass-2026"


def token_in_mail(email: str, pattern: str) -> str:
    for mail in reversed(SesEmailService.OUTBOX):
        if email in mail["to"]:
            m = re.search(pattern, mail["html"])
            if m:
                return m.group(1)
    raise AssertionError(f"No link emailed to {email}")


async def join(app, email: str):
    """Accept the emailed invite and sign in."""
    client = new_client(app)
    token = token_in_mail(email, r"set-password\?token=([^&\"]+)")
    assert (await client.post("/api/v1/auth/invite/accept", json={"token": token, "password": PASSWORD})).status_code == 200
    r = (await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})).json()
    if r["step"] == "code":
        assert (await client.post("/api/v1/auth/otp/verify", json={"challenge_id": r["challenge_id"], "code": last_code(email)})).status_code == 200
    return client


async def agreement_of(db, email: str, stage: str = None) -> Agreement:
    user = await db.scalar(select(User).where(User.email == email))
    q = select(Agreement).where(Agreement.user_id == user.id)
    if stage:
        q = q.where(Agreement.stage == stage)
    return await db.scalar(q.order_by(Agreement.created_at.desc()).limit(1))


async def test_only_the_founder_sees_agreements_and_templates(app):
    for key in ("philip", "ahmed", "kingmille", "galacta"):
        c = await sign_in(app, key)
        assert (await c.get("/api/v1/agreements")).status_code == 403, key
        assert (await c.get("/api/v1/agreement-templates")).status_code == 403, key
        assert "agreements" not in (await c.get("/api/v1/auth/me")).json()["areas"]
    founder = await sign_in(app, "founder")
    data = (await founder.get("/api/v1/agreements")).json()
    assert data["kpis"]["trials"] == 1 and data["kpis"]["waiting"] == 2  # Galacta's trial; Community lead and SH-INV-004
    keys = {(t["audience"], t["stage"]) for t in (await founder.get("/api/v1/agreement-templates")).json()["templates"]}
    assert {("staff", "standard"), ("streamer", "trial"), ("streamer", "contract"), ("designer", "standard"),
            ("clipper", "standard"), ("investor", "standard")} <= keys


async def test_galacta_tracks_his_trial_from_the_day_he_signed(app):
    galacta = await sign_in(app, "galacta")
    mine = (await galacta.get("/api/v1/me/agreements")).json()
    trial = mine["current"][0]
    assert trial["stage"] == "trial" and trial["day"] == 76  # signed 75 days ago, so this is day 76
    start = today() - timedelta(days=75)
    assert trial["starts_on"] == start.isoformat()
    assert trial["ends_on"] == (start + relativedelta(months=6) - timedelta(days=1)).isoformat()
    assert "Galacta" in trial["text"] and "{{" not in trial["text"]
    assert (await galacta.get("/api/v1/auth/me")).json()["footer"].startswith("Trial · day 76 of ")


async def test_a_new_streamer_signs_the_trial_in_onboarding_and_it_counts_from_that_day(app, db):
    founder = await sign_in(app, "founder")
    r = await founder.post("/api/v1/people/invite", json={"name": "Kofi Live", "email": "kofi.live@example.test", "role": "streamer",
                                                          "department": "talent", "title": "Streamer", "desk": "talent"})
    assert r.status_code == 200, r.text
    kofi = await join(app, "kofi.live@example.test")
    state = (await kofi.get("/api/v1/onboarding/me")).json()
    assert state["agreement"]["stage"] == "trial" and state["agreement"]["status"] == "offered"
    assert (await kofi.get("/api/v1/auth/me")).json()["agreement_due"] is None  # signed in onboarding, not a pop-up
    await kofi.post("/api/v1/onboarding/me/steps/1", json={})
    r = await kofi.post("/api/v1/onboarding/me/steps/2", json={"signature": "Someone Else"})
    assert r.status_code == 400
    r = await kofi.post("/api/v1/onboarding/me/steps/2", json={"signature": "kofi live"})
    assert r.status_code == 200, r.text
    a = await agreement_of(db, "kofi.live@example.test", "trial")
    await db.refresh(a)
    assert a.status == "signed" and a.starts_on == today()
    assert a.ends_on == today() + relativedelta(months=6) - timedelta(days=1)
    assert "{{" not in a.body and "Kofi Live" in a.body  # the signed words are frozen


async def test_the_full_contract_follows_the_trial_and_shows_at_sign_in(app, db):
    founder = await sign_in(app, "founder")
    trial = await agreement_of(db, "galacta@streamershub.test", "trial")
    r = await founder.post(f"/api/v1/agreements/{trial.id}/follow-up")
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert (await founder.post(f"/api/v1/agreements/{trial.id}/follow-up")).status_code == 409  # only one at a time
    contract = (await founder.get(f"/api/v1/agreements/{cid}")).json()
    assert contract["stage"] == "contract" and contract["status"] == "draft" and contract["term_months"] == 36
    galacta = await sign_in(app, "galacta")
    assert (await galacta.get("/api/v1/auth/me")).json()["agreement_due"] is None  # drafts are the Founder's only
    assert (await founder.patch(f"/api/v1/agreements/{cid}", json={"term_months": 72})).status_code == 400  # 3 to 5 years
    terms = contract["terms"] + [{"kind": "bonus", "label": "Signing bonus", "value": "GHS 2,000", "detail": "Paid on signing"}]
    r = await founder.patch(f"/api/v1/agreements/{cid}", json={"term_months": 48, "terms": terms})
    assert r.status_code == 200 and r.json()["term"] == "4 years"
    assert (await founder.post(f"/api/v1/agreements/{cid}/send")).status_code == 200
    start = trial.ends_on + timedelta(days=1)
    text = next(w for w in (await galacta.get("/api/v1/me/agreements")).json()["waiting"] if w["id"] == cid)["text"]
    assert f"from {start.day} {start.strftime('%B %Y')}" in text  # the dates are known before signing
    due = (await galacta.get("/api/v1/auth/me")).json()["agreement_due"]
    assert due and due["id"] == cid and due["stage"] == "contract"
    assert (await galacta.post(f"/api/v1/me/agreements/{cid}/sign", json={"signature": "Galacta"})).status_code == 400
    r = await galacta.post(f"/api/v1/me/agreements/{cid}/sign", json={"signature": "Lamar (Galacta)"})
    assert r.status_code == 200, r.text
    await db.refresh(trial)
    c = await db.scalar(select(Agreement).where(Agreement.follows_id == trial.id))
    await db.refresh(c)
    assert str(c.id) == cid and c.status == "signed" and trial.status == "signed"
    assert c.starts_on == trial.ends_on + timedelta(days=1)  # picks up the day after the trial
    assert c.ends_on == c.starts_on + relativedelta(months=48) - timedelta(days=1)
    assert (await galacta.get("/api/v1/auth/me")).json()["agreement_due"] is None
    streamer = await db.scalar(select(Streamer).where(Streamer.stage_name == "Galacta"))
    await db.refresh(streamer)
    assert streamer.contract_end == c.ends_on
    assert (await founder.patch(f"/api/v1/agreements/{cid}", json={"title": "Changed"})).status_code == 409  # signed is final


async def test_trials_ending_soon_get_a_drafted_contract_for_the_founder(app, db):
    a = await agreement_of(db, "kofi.live@example.test", "trial")
    on = a.ends_on - timedelta(days=10)
    assert await AgreementService.draft_follow_ups(db, on) == 1
    await db.commit()
    assert await AgreementService.draft_follow_ups(db, on) == 0  # never twice
    draft = await db.scalar(select(Agreement).where(Agreement.follows_id == a.id))
    decision = await db.scalar(select(Decision).where(Decision.kind == "agreement", Decision.entity_id == str(draft.id)))
    assert draft.status == "draft" and "trial ends" in decision.title
    founder = await sign_in(app, "founder")
    command = (await founder.get("/api/v1/os/command")).json()
    item = next(d for d in command["decisions"] if d["id"] == str(decision.id))
    assert item["href"] == f"/os/agreements/{draft.id}"
    assert (await founder.post(f"/api/v1/decisions/{decision.id}", json={"approve": True})).status_code == 200
    await db.refresh(draft)
    assert draft.status == "offered"


async def test_each_invite_can_carry_its_own_terms(app, db):
    founder = await sign_in(app, "founder")
    templates = (await founder.get("/api/v1/agreement-templates")).json()["templates"]
    staff = next(t for t in templates if t["audience"] == "staff" and t["is_default"])
    terms = [{"kind": "equity", "label": "Vesting shares", "value": "0.75%", "detail": "Over 36 months with a 6-month cliff"},
             {"kind": "revenue_share", "label": "Producer share", "value": "4%", "detail": "Of the streamer's platform earnings"}]
    r = await founder.post("/api/v1/people/invite", json={"name": "Second Producer", "email": "producer2@example.test", "role": "staff",
                                                          "department": "production", "title": "Producer", "desk": "production",
                                                          "template_id": staff["id"], "terms": terms})
    assert r.status_code == 200, r.text
    a = await agreement_of(db, "producer2@example.test")
    assert [t["label"] for t in a.terms] == ["Vesting shares", "Producer share"] and a.status == "offered"
    producer = await join(app, "producer2@example.test")
    await producer.post("/api/v1/onboarding/me/steps/1", json={})
    assert (await producer.post("/api/v1/onboarding/me/steps/2", json={"signature": "Second Producer"})).status_code == 200
    grant = await db.scalar(select(EquityGrant).where(EquityGrant.holder_user_id == a.user_id))
    assert float(grant.percent) == 0.75 and grant.vest_months == 36 and grant.cliff_months == 6 and grant.agreement_id == a.id
    # A department head can invite, but only the Founder sets terms
    kingmille = await sign_in(app, "kingmille")
    r = await kingmille.post("/api/v1/people/invite", json={"name": "Junior", "email": "junior@example.test", "role": "staff",
                                                            "department": "creative", "title": "Junior", "terms": terms})
    assert r.status_code == 403


async def test_templates_are_the_founders_and_versioned(app, db):
    founder = await sign_in(app, "founder")
    data = (await founder.get("/api/v1/agreement-templates")).json()
    staff = next(t for t in data["templates"] if t["audience"] == "staff" and t["is_default"])
    body = {k: staff[k] for k in ("name", "audience", "stage", "summary", "term_months", "min_term_months", "max_term_months",
                                  "renewable", "default_terms", "is_default", "status")}
    signed = await agreement_of(db, "philip@streamershub.test")
    before = signed.body
    r = await founder.put(f"/api/v1/agreement-templates/{staff['id']}", json={**body, "body": staff["body"] + "\n\n## Studio access\nKeys stay in the studio."})
    assert r.status_code == 200 and r.json()["version"] == staff["version"] + 1
    await db.refresh(signed)
    assert signed.body == before and signed.template_version == staff["version"]  # signed agreements never change
    # A brand-new template, made the default for its kind
    r = await founder.post("/api/v1/agreement-templates", json={
        "name": "Streamer trial · 3 months", "audience": "streamer", "stage": "trial", "body": "## Trial\n{{name}} for {{term}}. {{terms}}",
        "term_months": 3, "default_terms": [{"kind": "salary", "label": "Stipend", "value": "GHS 1,000", "detail": ""}], "is_default": True})
    assert r.status_code == 200, r.text
    assert (await AgreementService.default_template(db, "streamer", "trial")).name == "Streamer trial · 3 months"
    old = await db.scalar(select(AgreementTemplate).where(AgreementTemplate.key == "streamer-trial"))
    await db.refresh(old)
    assert old.is_default is False
    copy = (await founder.post(f"/api/v1/agreement-templates/{old.id}/duplicate")).json()
    assert (await founder.get(f"/api/v1/agreement-templates/{copy['id']}")).json()["name"].endswith("(copy)")
    bad = await founder.post("/api/v1/agreement-templates", json={**body, "name": "Bad", "stage": "trial", "term_months": None, "body": "x" * 30})
    assert bad.status_code == 400  # a trial needs a length
    preview = (await founder.post("/api/v1/agreement-templates/preview", json={"body": "Hi {{name}}, {{mystery}}", "audience": "staff"})).json()
    assert preview["text"].startswith("Hi Ama Mensah") and preview["unknown"] == ["mystery"]


async def test_investor_agreements_are_signed_in_the_vault_and_seen_by_the_founder(app, db):
    founder = await sign_in(app, "founder")
    r = await founder.post("/api/v1/capital/investors", json={
        "legal_name": "Akua Darko", "email": "akua@example.test", "amount": 8000, "shares": 24000, "percent": 2.4, "tier_label": "Pilot holder",
        "extra_terms": [{"kind": "benefit", "label": "Studio visits", "value": "Twice a year", "detail": ""}]})
    assert r.status_code == 200, r.text
    rows = (await founder.get("/api/v1/agreements")).json()["agreements"]
    row = next(x for x in rows if x["party"]["name"] == "Akua Darko")
    assert row["status"] == "offered" and [t["label"] for t in row["terms"]] == ["Amount invested", "Stake", "Tier", "Studio visits"]
    token = token_in_mail("akua@example.test", r"vault/welcome\?token=([^&\"]+)")
    c = new_client(app)
    state = (await c.get(f"/api/v1/vault/welcome/{token}")).json()
    assert "Akua Darko" in state["agreement"]["text"] and "2.4%" in state["agreement"]["text"]
    await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 1})
    await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 2, "legal_name": "Akua Darko"})
    r = await c.post(f"/api/v1/vault/welcome/{token}/step", json={"step": 3, "signature": "Akua Darko", "agree": True})
    assert r.status_code == 200, r.text
    row = (await founder.get(f"/api/v1/agreements/{row['id']}")).json()
    assert row["status"] == "signed" and row["signed_name"] == "Akua Darko" and row["phase"] == "active"


async def test_ending_and_privacy(app, db):
    founder = await sign_in(app, "founder")
    trial = await agreement_of(db, "kofi.live@example.test", "trial")
    assert (await founder.post(f"/api/v1/agreements/{trial.id}/end", json={"reason": ""})).status_code == 422
    r = await founder.post(f"/api/v1/agreements/{trial.id}/end", json={"reason": "Not the right fit"})
    assert r.status_code == 200 and r.json()["status"] == "ended" and r.json()["ends_on"] == today().isoformat()
    galacta = await sign_in(app, "galacta")
    assert (await galacta.post(f"/api/v1/me/agreements/{trial.id}/sign", json={"signature": "Kofi Live"})).status_code == 404
    person = (await founder.get(f"/api/v1/people/{trial.user_id}")).json()
    statuses = {a["stage"]: a["status"] for a in person["agreements"]}
    assert statuses == {"trial": "ended", "contract": "withdrawn"}  # the contract drafted for it goes with it
    philip = await sign_in(app, "philip")
    assert "agreements" not in (await philip.get(f"/api/v1/people/{trial.user_id}")).json()
