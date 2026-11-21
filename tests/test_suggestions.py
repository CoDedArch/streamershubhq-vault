"""The Suggestion Bank: who sees an idea, hidden names, the weekly limit, answering, closing (and the Founder's
weekly list), merging, moving, recommending, the Founder's decision, the owner's task, "You said, we did", the
reminder, and the numbers on Command."""

import json
from datetime import date, timedelta

from sqlalchemy import select, update

from app.models.money import Decision
from app.models.people import Task
from app.models.suggestions import Suggestion
from app.services import SesEmailService
from app.services import SuggestionService as SS
from tests.conftest import sign_in, vault_sign_in

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


async def send(c, topic: str, title: str, *, body: str = "Here is the idea, with enough detail to act on.", hide: bool = False,
               file: bool = False, path: str = "/api/v1/suggestions", benefit: str = ""):
    data = {"data": json.dumps({"topic": topic, "title": title, "body": body, "benefit": benefit, "hide_name": hide})}
    return await c.post(path, data=data, files={"file": ("screen.png", PNG, "image/png")} if file else None)


def mail_to(email: str, subject_part: str = "") -> list[dict]:
    return [m for m in SesEmailService.OUTBOX if email in m["to"] and subject_part in m["subject"]]


async def rows(c, view: str = "todo") -> list[dict]:
    r = await c.get(f"/api/v1/os/suggestions?view={view}")
    assert r.status_code == 200, r.text
    return r.json()["rows"]


async def idea_id(c, title: str) -> str:
    mine = (await c.get("/api/v1/suggestions/mine")).json()["items"]
    return next(i["id"] for i in mine if i["title"] == title)


def test_customers_cant_send_ideas():
    assert "customer" not in SS.SENDER_ROLES and {"clipper", "designer", "staff", "streamer"} <= SS.SENDER_ROLES


async def test_an_idea_reaches_only_the_desks_it_concerns(app):
    SesEmailService.OUTBOX.clear()
    clipper = await sign_in(app, "kwesi_cuts")
    r = await send(clipper, "merch", "Glow-in-the-dark clipper tee", file=True)
    assert r.status_code == 200, r.text
    ref = r.json()["reference"]
    assert ref.startswith("SH-IDEA-")
    assert mail_to("kwesi_cuts@streamershub.test", "We have your idea")
    # Merch goes to Creative and Store operations, and only they are emailed
    assert mail_to("kingmille@streamershub.test", "New idea") and mail_to("storeops@streamershub.test", "New idea")
    assert not mail_to("trigger@streamershub.test", "New idea") and not mail_to("founder@streamershub.test", "New idea")
    lead = await sign_in(app, "kingmille")
    row = next(x for x in await rows(lead) if x["reference"] == ref)
    assert row["from"] == "@kwesi.cuts · clipper" and row["for"] == "Creative and Store operations" and row["status_label"] == "New"
    detail = (await lead.get(f"/api/v1/os/suggestions/{row['id']}")).json()
    assert detail["files"][0]["name"] == "screen.png" and detail["can_act"] and not detail["can_decide"]
    assert any(x["reference"] == ref for x in await rows(await sign_in(app, "storeops")))
    assert any(x["reference"] == ref for x in await rows(await sign_in(app, "founder")))
    for key in ("trigger", "certified"):  # Social and Production don't see merch ideas
        other = await sign_in(app, key)
        assert ref not in json.dumps(await rows(other))
        assert (await other.get(f"/api/v1/os/suggestions/{row['id']}")).status_code == 404
    streamer = (await (await sign_in(app, "galacta")).get("/api/v1/os/suggestions")).json()
    assert streamer["receives"] is False and streamer["rows"] == []
    mine = (await clipper.get("/api/v1/suggestions/mine")).json()
    item = next(i for i in mine["items"] if i["reference"] == ref)
    assert item["status_label"] == "Received" and mine["weekly_limit"] == 5
    nav = (await lead.get("/api/v1/auth/me")).json()["nav"]
    assert {i["area"]: i["count"] for g in nav for i in g["items"]}["suggestions"] >= 1


async def test_private_topics_go_to_the_founder_alone_and_hidden_names_stay_hidden(app):
    staff = await sign_in(app, "certified")
    r = await send(staff, "pay", "Pay dates clash with rent day", hide=True)
    ref = r.json()["reference"]
    founder = await sign_in(app, "founder")
    row = next(x for x in await rows(founder) if x["reference"] == ref)
    assert row["private"] and row["for"] == "The Founder only" and row["from"] == "A staff member"
    me = (await staff.get("/api/v1/auth/me")).json()
    detail = (await founder.get(f"/api/v1/os/suggestions/{row['id']}")).json()
    shown = json.dumps({k: v for k, v in detail.items() if k != "team"})  # "team" is everyone who could own it
    assert me["id"] not in shown and me["email"] not in shown and "Certified" not in shown  # hidden from the Founder too
    cofounder = await sign_in(app, "philip")
    assert ref not in json.dumps(await rows(cofounder))
    designer = await sign_in(app, "ama_draws")
    r = await send(designer, "merch", "Pastel colourway for the hoodie", hide=True)
    lead = await sign_in(app, "kingmille")
    seen = next(x for x in await rows(lead) if x["reference"] == r.json()["reference"])
    assert seen["from"] == "A designer"
    me = (await designer.get("/api/v1/auth/me")).json()
    detail = json.dumps((await lead.get(f"/api/v1/os/suggestions/{seen['id']}")).json())
    assert me["id"] not in detail and me["email"] not in detail and "ama.draws" not in detail and "Ama" not in detail


async def test_investor_ideas_go_to_strategy_and_investors(app, seeded):
    number, name, email, holder_id = seeded["holders"][0]
    vault = await vault_sign_in(app, holder_id, email)
    r = await send(vault, "content", "A behind-the-scenes series", path="/api/v1/investor/suggestions")
    assert r.status_code == 200, r.text
    ref = r.json()["reference"]
    assert any(i["reference"] == ref for i in (await vault.get("/api/v1/investor/suggestions")).json()["items"])
    strategy = await sign_in(app, "aaron")
    row = next(x for x in await rows(strategy) if x["reference"] == ref)
    assert row["for"] == "Strategy & Investors" and row["from"].endswith("· investor")
    assert ref not in json.dumps(await rows(await sign_in(app, "certified")))  # not Production, though it's about content


async def test_five_ideas_a_week(app):
    designer = await sign_in(app, "kofi_inks")
    for i in range(5):
        assert (await send(designer, "merch", f"Idea number {i + 1}")).status_code == 200
    r = await send(designer, "merch", "One too many")
    assert r.status_code == 429 and "5 ideas this week" in r.json()["detail"]


async def test_answering_closing_and_the_founders_weekly_list(app, db):
    SesEmailService.OUTBOX.clear()
    clipper = await sign_in(app, "nanaframes")
    await send(clipper, "clips", "Weekly clip challenge")
    iid = await idea_id(clipper, "Weekly clip challenge")
    producer = await sign_in(app, "certified")
    r = await producer.post(f"/api/v1/os/suggestions/{iid}/reply", json={"body": "Love it. Which day works best for you?"})
    assert r.json()["status"] == "looking" and r.json()["first_response_at"]
    assert mail_to("nanaframes@streamershub.test", "We answered your idea")
    await producer.post(f"/api/v1/os/suggestions/{iid}/reply", json={"body": "Check the budget first.", "to_sender": False})
    item = next(i for i in (await clipper.get("/api/v1/suggestions/mine")).json()["items"] if i["id"] == iid)
    assert item["status_label"] == "Being looked at" and [x["by"] for x in item["replies"]] == ["Streamers Hub"]  # no team note
    r = await clipper.post(f"/api/v1/suggestions/{iid}/reply", json={"body": "Fridays, after the stream."})
    assert any(x["body"] == "Fridays, after the stream." for x in r.json()["items"][0]["replies"] + sum([i["replies"] for i in r.json()["items"]], []))
    assert (await producer.post(f"/api/v1/os/suggestions/{iid}/close", json={"reason": "No"})).status_code == 400  # needs a real reason
    r = await producer.post(f"/api/v1/os/suggestions/{iid}/close", json={"reason": "We're not adding new challenges until January."})
    assert r.json()["status"] == "closed"
    item = next(i for i in (await clipper.get("/api/v1/suggestions/mine")).json()["items"] if i["id"] == iid)
    assert item["status_label"] == "Not now" and item["closed_reason"].startswith("We're not adding")
    founder = await sign_in(app, "founder")
    assert any(x["id"] == iid for x in await rows(founder, "closed_week"))
    assert (await producer.post(f"/api/v1/os/suggestions/{iid}/reopen")).status_code == 403
    mail = await SS.weekly_closed(db)
    assert mail and "founder@streamershub.test" in mail[0]["to"] and any("Weekly clip challenge" in x for x in mail[0]["lines"])
    r = await founder.post(f"/api/v1/os/suggestions/{iid}/reopen")
    assert r.json()["status"] == "looking" and mail_to("nanaframes@streamershub.test", "another look")


async def test_recommend_approve_task_done_and_you_said_we_did(app, db):
    SesEmailService.OUTBOX.clear()
    clipper = await sign_in(app, "kwesi_cuts")
    await send(clipper, "content", "Late-night chill streams on Sundays")
    iid = await idea_id(clipper, "Late-night chill streams on Sundays")
    producer = await sign_in(app, "certified")
    staff = (await producer.get(f"/api/v1/os/suggestions/{iid}")).json()["team"]
    owner = next(u["id"] for u in staff if u["name"] == "Certified")
    trigger = next(u["id"] for u in staff if u["name"] == "Trigger")
    due = (date.today() + timedelta(days=10)).isoformat()
    r = await producer.post(f"/api/v1/os/suggestions/{iid}/recommend", json={"action": "Pilot two Sunday streams in December",
                                                                            "owner_user_id": owner, "due_on": due, "cost": 300, "effort": "small"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "recommended" and r.json()["recommendation"]["cost_minor"] == 30000
    founder = await sign_in(app, "founder")
    assert mail_to("founder@streamershub.test", "needs your decision")
    command = (await founder.get("/api/v1/os/command")).json()
    line = next(d for d in command["decisions"] if d["kind"] == "suggestion" and d["status"] == "open" and "Late-night" in d["title"])
    assert line["href"] == f"/os/suggestions?id={iid}" and command["ideas"]["waiting_for_you"] >= 1
    item = next(i for i in (await clipper.get("/api/v1/suggestions/mine")).json()["items"] if i["id"] == iid)
    assert item["status_label"] == "Being looked at"  # the sender doesn't see the internal step
    assert (await producer.post(f"/api/v1/os/suggestions/{iid}/decide", json={"verdict": "approve"})).status_code == 403
    r = await founder.post(f"/api/v1/os/suggestions/{iid}/decide", json={"verdict": "approve", "owner_user_id": trigger})
    assert r.json()["status"] == "planned" and r.json()["task"]["status"] == "open"
    s = await db.get(Suggestion, __import__("uuid").UUID(iid))
    task = await db.get(Task, s.task_id)
    assert task.title.startswith(f"Idea {s.reference}: Pilot two Sunday") and str(task.owner_user_id) == trigger
    assert (await db.get(Decision, s.decision_id)).status == "approved"
    assert mail_to("trigger@streamershub.test", "A task from an idea") and mail_to("kwesi_cuts@streamershub.test", "We're doing it")
    social = await sign_in(app, "trigger")
    r = await social.patch(f"/api/v1/tasks/{task.id}", json={"status": "done"})
    assert r.status_code == 200, r.text
    await db.refresh(s)
    assert s.status == "done" and mail_to("kwesi_cuts@streamershub.test", "Your idea is live")
    said = (await (await sign_in(app, "ama_draws")).get("/api/v1/suggestions/mine")).json()["said_did"]
    assert {"title": "Late-night chill streams on Sundays", "by": "@kwesi.cuts"}.items() <= next(x for x in said if x["title"].startswith("Late-night")).items()
    ideas = (await founder.get("/api/v1/os/command")).json()["ideas"]
    assert ideas["received"] >= 1 and ideas["adopted"] >= 1 and ideas["done"] >= 1


async def test_founder_declines_from_command_or_sends_back_with_a_question(app):
    clipper = await sign_in(app, "yaw_replays")
    await send(clipper, "clips", "Pay clippers weekly")
    await send(clipper, "clips", "A clipper of the month award")
    first, second = await idea_id(clipper, "Pay clippers weekly"), await idea_id(clipper, "A clipper of the month award")
    producer = await sign_in(app, "certified")
    staff = (await producer.get(f"/api/v1/os/suggestions/{first}")).json()["team"]
    owner = staff[0]["id"]
    due = (date.today() + timedelta(days=5)).isoformat()
    for iid in (first, second):
        r = await producer.post(f"/api/v1/os/suggestions/{iid}/recommend", json={"action": "Try it for a month", "owner_user_id": owner,
                                                                                "due_on": due, "effort": "medium"})
        assert r.status_code == 200, r.text
    founder = await sign_in(app, "founder")
    decisions = (await founder.get("/api/v1/os/command")).json()["decisions"]
    d = next(x for x in decisions if x["status"] == "open" and "Pay clippers weekly" in x["title"])
    assert (await founder.post(f"/api/v1/decisions/{d['id']}", json={"approve": False})).status_code == 400  # a decline needs a reason
    r = await founder.post(f"/api/v1/decisions/{d['id']}", json={"approve": False, "note": "Payouts stay monthly while we're small."})
    assert r.status_code == 200, r.text
    item = next(i for i in (await clipper.get("/api/v1/suggestions/mine")).json()["items"] if i["id"] == first)
    assert item["status_label"] == "Not now" and item["closed_reason"] == "Payouts stay monthly while we're small."
    r = await founder.post(f"/api/v1/os/suggestions/{second}/decide", json={"verdict": "ask", "note": "What would the award be?"})
    assert r.json()["status"] == "looking" and any("What would the award be?" in t["body"] for t in r.json()["thread"] if t["direction"] == "note")
    back = next(x for x in (await founder.get("/api/v1/os/command")).json()["decisions"] if "clipper of the month" in x["title"])
    assert back["status"] == "returned"


async def test_merging_and_moving(app):
    one, two = await sign_in(app, "tema_trims"), await sign_in(app, "clipsbyefya")
    await send(one, "merch", "Clipper desk mats")
    await send(two, "merch", "Desk mats for clippers")
    main, dup = await idea_id(one, "Clipper desk mats"), await idea_id(two, "Desk mats for clippers")
    lead = await sign_in(app, "kingmille")
    r = await lead.post(f"/api/v1/os/suggestions/{dup}/merge", json={"into": main})
    assert r.json()["status"] == "merged" and r.json()["merged_into"]["id"] == main
    assert (await lead.get(f"/api/v1/os/suggestions/{main}")).json()["suggested_by"] == 2
    item = next(i for i in (await two.get("/api/v1/suggestions/mine")).json()["items"] if i["id"] == dup)
    assert item["merged_into"]["title"] == "Clipper desk mats" and not item["can_reply"]
    r = await lead.post(f"/api/v1/os/suggestions/{main}/move", json={"department": "tech"})
    assert r.json() == {"id": main, "moved_away": True, "for": "Tech & Dev"}
    assert (await lead.get(f"/api/v1/os/suggestions/{main}")).status_code == 404
    assert any(x["id"] == main for x in await rows(await sign_in(app, "bernard")))
    tech = await sign_in(app, "bernard")
    r = await tech.post(f"/api/v1/os/suggestions/{main}/move", json={"department": "founder"})
    assert r.json()["moved_away"] and r.json()["for"] == "The Founder only"
    founder = await sign_in(app, "founder")
    assert (await founder.get(f"/api/v1/os/suggestions/{main}")).json()["private"] is True


async def test_unanswered_ideas_remind_the_department_head_once(app, db):
    clipper = await sign_in(app, "kwesi_cuts")
    await send(clipper, "content", "Watch-along nights")
    iid = await idea_id(clipper, "Watch-along nights")
    s = await db.get(Suggestion, __import__("uuid").UUID(iid))
    await db.execute(update(Suggestion).where(Suggestion.id == s.id).values(created_at=s.created_at - timedelta(days=6)))
    await db.commit()
    mail = await SS.remind(db)
    await db.commit()
    assert any("certified@streamershub.test" in m["to"] and "Watch-along" in m["subject"] for m in mail)  # Production's lead
    again = await SS.remind(db)
    assert not any("Watch-along" in m["subject"] for m in again)
    await db.execute(update(Suggestion).where(Suggestion.id == s.id).values(created_at=s.created_at - timedelta(days=9)))
    await db.commit()
    founder = await sign_in(app, "founder")
    assert (await founder.get("/api/v1/os/command")).json()["ideas"]["late"] >= 1
    assert any(x["id"] == iid and x["late"] for x in await rows(founder))
    assert (await db.execute(select(Suggestion).where(Suggestion.id == s.id))).scalar_one().reminded_at is not None
