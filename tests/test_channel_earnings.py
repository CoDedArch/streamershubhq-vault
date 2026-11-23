"""Channel earnings: a streamer's own channels pay the streamer, who passes the payouts to the label and reports each
month what every channel earned and paid, with its statement. Finance checks the report and confirms what the label
received; that becomes the streamer's platform income for the month it's confirmed, and payroll pays the streamer's
share of it (plus brand deals paid for their work), as set in Settings → Payroll."""

import json
from datetime import timedelta

from sqlalchemy import delete, select

from app.models.money import LedgerEntry, PayrollRun, SalaryTier
from app.models.partnerships import Deal, Deliverable, Invoice
from app.models.talent import PlatformIncome, Streamer
from app.services import ChannelService, PayrollService, SesEmailService
from app.services.CompanyService import month_bounds, today
from app.services.money import month_key, month_label, prev_month
from tests.conftest import sign_in

PNG = ("twitch.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, "image/png")
PDF = ("youtube.pdf", b"%PDF-1.4 statement", "application/pdf")


def send(client, report: dict, files=()):
    return client.post("/api/v1/talent/channel-reports", data={"report": json.dumps(report)},
                       files=[("files", f) for f in files])


async def galacta(db) -> Streamer:
    return (await db.execute(select(Streamer).where(Streamer.stage_name == "Galacta"))).scalar_one()


async def test_the_streamer_reports_each_channel_and_finance_confirms_what_arrived(app, db):
    star = await sign_in(app, "galacta")
    desk = (await star.get("/api/v1/talent/channel-reports")).json()
    last = prev_month(month_key(today()))
    assert desk["can_send"] is True and desk["due"] == {"month": last, "label": month_label(last)}
    assert desk["months"][0]["month"] == month_key(today()) and {"key": "twitch", "label": "Twitch"} in desk["platforms"]

    lines = [{"platform": "twitch", "earned": 120, "earned_currency": "USD", "paid": 1500, "paid_currency": "GHS", "file": 0},
             {"platform": "youtube", "earned": 30, "earned_currency": "USD", "paid": 0, "file": 1}]
    report = {"month": last, "lines": lines, "passed": 1500, "transfer_ref": "MP261202.1234.A1"}

    # Every channel needs its statement; a payout has to be passed on, with a reference
    r = await send(star, report, [PNG])
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["statement"]
    r = await send(star, {**report, "passed": 0}, [PNG, PDF])
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["passed"]
    r = await send(star, {**report, "transfer_ref": " "}, [PNG, PDF])
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["transfer_ref"]
    r = await send(star, {**report, "lines": [lines[0], {**lines[1], "platform": "twitch"}]}, [PNG, PDF])
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["platform"]
    r = await send(star, report, [PNG, ("fake.png", b"not an image", "image/png")])
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["statement"]
    r = await send(star, {**report, "month": "2020-01"}, [PNG, PDF])
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["month"]

    before = len(SesEmailService.OUTBOX)
    r = await send(star, report, [PNG, PDF])
    assert r.status_code == 200, r.text
    sent = r.json()
    assert sent["status"] == "submitted" and sent["passed_minor"] == 150000 and sent["paid_out"] is True
    assert [(x["platform_label"], x["earned"], x["paid"]) for x in sent["lines"]] == [("Twitch", "$120.00", "GHS 1,500"), ("YouTube", "$30.00", None)]
    assert all(x["statement_url"] for x in sent["lines"])
    mail = [m for m in SesEmailService.OUTBOX[before:] if m["subject"] == f"Galacta: {month_label(last)} channel earnings"]
    assert mail and "ahmed@streamershub.test" in mail[0]["to"]
    assert (await star.get("/api/v1/talent/channel-reports")).json()["due"] is None

    # Only Finance checks it
    certified = await sign_in(app, "certified")
    assert (await certified.get("/api/v1/finance/channel-reports")).status_code == 403
    cfo = await sign_in(app, "ahmed")
    waiting = (await cfo.get("/api/v1/finance/channel-reports")).json()["waiting"]
    mine = next(x for x in waiting if x["id"] == sent["id"])
    assert mine["streamer"]["name"] == "Galacta" and mine["transfer_ref"] == "MP261202.1234.A1"

    # A different amount needs a reason
    path = f"/api/v1/finance/channel-reports/{sent['id']}/confirm"
    r = await cfo.post(path, json={"received": 1490})
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["note"]
    before = len(SesEmailService.OUTBOX)
    r = await cfo.post(path, json={"received": 1490, "note": "GHS 10 MoMo charge"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed" and r.json()["income_month_label"] == month_label(month_key(today()))
    assert any(m["subject"] == f"{month_label(last)} channel earnings confirmed" for m in SesEmailService.OUTBOX[before:])

    # It's the streamer's platform income this month, the month the label has it, and it's in the books
    s = await galacta(db)
    own = (await db.execute(select(PlatformIncome).where(PlatformIncome.streamer_id == s.id, PlatformIncome.platform == "own",
                                                         PlatformIncome.month == month_key(today())))).scalar_one()
    assert own.amount_minor == 149000
    ledger = (await db.execute(select(LedgerEntry).where(LedgerEntry.source_table == "channel_reports",
                                                         LedgerEntry.source_id == sent["id"]))).scalar_one()
    assert (ledger.type, ledger.category, ledger.amount_minor) == ("revenue", "platform", 149000)

    # Confirmed is final
    assert (await cfo.post(path, json={"received": 1490, "note": "again"})).status_code == 409
    assert (await send(star, report, [PNG, PDF])).status_code == 409


async def test_finance_sends_a_report_back_and_the_streamer_corrects_it_keeping_the_statements(app, db):
    star = await sign_in(app, "galacta")
    month = month_key(today())
    r = await send(star, {"month": month, "lines": [{"platform": "kick", "earned": 0, "paid": 0, "file": 0}], "note": "Kick only"}, [PNG])
    assert r.status_code == 200, r.text
    sent = r.json()
    assert sent["paid_out"] is False and sent["passed_minor"] == 0  # a month with no payout is still reported

    founder = await sign_in(app, "founder")  # sees the desk, never sends for the streamer
    assert (await send(founder, {"month": month, "lines": [{"platform": "kick", "earned": 0, "file": 0}]}, [PNG])).status_code == 403

    cfo = await sign_in(app, "ahmed")
    assert (await cfo.post(f"/api/v1/finance/channel-reports/{sent['id']}/send-back", json={"note": ""})).status_code == 422
    before = len(SesEmailService.OUTBOX)
    r = await cfo.post(f"/api/v1/finance/channel-reports/{sent['id']}/send-back", json={"note": "TikTok is missing"})
    assert r.status_code == 200 and r.json()["status"] == "returned"
    assert any("TikTok is missing" in m["html"] for m in SesEmailService.OUTBOX[before:] if "galacta@streamershub.test" in m["to"])
    assert (await cfo.post(f"/api/v1/finance/channel-reports/{sent['id']}/confirm", json={"received": 0})).status_code == 409

    desk = (await star.get("/api/v1/talent/channel-reports")).json()
    back = next(x for x in desk["reports"] if x["id"] == sent["id"])
    assert back["status"] == "returned" and back["review_note"] == "TikTok is missing"

    # Sent again with the same Kick statement and a new TikTok one; a statement from elsewhere isn't accepted
    kick = back["lines"][0]["statement_key"]
    forged = {"month": month, "lines": [{"platform": "kick", "earned": 0, "statement_key": "sh/receipts/other.png"}]}
    assert (await send(star, forged)).json()["detail"]["missing"] == ["statement"]
    again = {"month": month, "lines": [{"platform": "kick", "earned": 0, "statement_key": kick},
                                       {"platform": "tiktok", "earned": 12.5, "earned_currency": "USD", "file": 0}]}
    r = await send(star, again, [PNG])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted" and r.json()["id"] == sent["id"] and r.json()["lines"][0]["statement_key"] == kick
    r = await cfo.post(f"/api/v1/finance/channel-reports/{sent['id']}/confirm", json={"received": 0})
    assert r.status_code == 200 and r.json()["received_minor"] == 0


async def test_payroll_pays_the_streamer_their_share_of_platform_income_and_brand_deals(app, db):
    founder = await sign_in(app, "founder")
    assert (await founder.put("/api/v1/settings/payroll", json={"streamer_share_bps": 20000})).status_code == 400
    assert (await founder.put("/api/v1/settings/payroll", json={"streamer_share_bps": 1500})).status_code == 200

    # A brand paid this month for work Galacta is on
    s = await galacta(db)
    deal = (await db.execute(select(Deal).join(Deliverable, Deliverable.deal_id == Deal.id)
                             .where(Deliverable.streamer_id == s.id).limit(1))).scalar_one()
    month = month_key(today())
    before = (await PayrollService.brand_revenue(db, month)).get(s.id, 0)
    db.add(Invoice(brand_id=deal.brand_id, deal_id=deal.id, number="SH-T-0901", amount_minor=400000, issued_at=today(),
                   due_at=today(), paid_at=today(), status="paid"))
    await db.commit()
    brand = (await PayrollService.brand_revenue(db, month)).get(s.id, 0)
    assert brand - before == 400000
    platform = await PayrollService.platform_income(db, s.id, month)
    assert platform >= 149000  # includes what Finance confirmed from his own channels

    # Next month's run pays 15% of this month's revenue
    nxt = month_key(month_bounds(month)[1])
    cfo = await sign_in(app, "ahmed")
    r = await cfo.post("/api/v1/payroll/runs/draft", json={"period": nxt})
    assert r.status_code == 200, r.text
    pr = (await db.execute(select(PayrollRun).where(PayrollRun.period == nxt))).scalar_one()
    rows = await PayrollService.run_rows(db, pr)
    share = next(x for x in rows if x["kind"] == "streamer_share")
    assert share["type"] == "Streamer share" and share["payee"] == "Lamar (Galacta)"
    assert share["amount_minor"] == round((platform + brand) * 0.15)
    assert "15% of" in share["basis"] and "brand deals" in share["basis"]

    star = await sign_in(app, "galacta")
    assert (await star.get("/api/v1/talent/summary")).json()["pay"]["share_percent"] == 15


async def test_reminders_and_no_salary_line_when_no_tiers_are_set(app, db):
    # On the 1st, a streamer who hasn't sent a month's report is reminded
    first_of_last = month_bounds(prev_month(month_key(today())))[0]
    month = prev_month(month_key(first_of_last))
    before = len(SesEmailService.OUTBOX)
    assert await ChannelService.remind(db, first_of_last) == 1
    assert [m["subject"] for m in SesEmailService.OUTBOX[before:]] == [f"Your {month_label(month)} channel earnings report"]
    # Last month's is in, so nobody is reminded about it
    assert await ChannelService.remind(db, month_bounds(month_key(today()))[0]) == 0

    # A pilot with no salary tiers: no GHS 0 salary line on hold, and no tier email on the 1st
    await db.execute(delete(SalaryTier))
    await db.commit()
    assert await PayrollService.set_tiers_for_month(db, month_key(today() + timedelta(days=40))) == []
    later = month_key(month_bounds(month_key(month_bounds(month_key(today()))[1]))[1])
    run = await PayrollService.draft_run(db, later, None)
    await db.commit()
    kinds = [x["kind"] for x in await PayrollService.run_rows(db, run) if x["payee"] == "Lamar (Galacta)"]
    assert "salary" not in kinds
