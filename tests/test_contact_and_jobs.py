"""The Contact page (reasons, the answers each one asks for, the conversation and replies by email), open
roles (a flyer is required, applications come in through the same inbox) and share counts."""

import io
import json
import re
from datetime import timedelta

from sqlalchemy import select

from app.core.security import now
from app.models.contact import ContactMessage, ContactReply
from app.models.people import StepInAction
from app.models.posts import LinkPage, Post
from app.services import SesEmailService
from tests.conftest import new_client, sign_in

PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def png(w=1080, h=1350) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (w, h), (20, 20, 20)).save(out, "PNG")
    return out.getvalue()


def mails_to(addr: str) -> list[dict]:
    return [m for m in SesEmailService.OUTBOX if addr in m["to"]]


async def send(client, reason: str, data: dict, files: dict | None = None):
    form = {"reason": reason, "data": json.dumps(data)}
    return await client.post("/api/v1/public/contact", data=form, files=files or {})


BRAND = {"name": "Esi Mensah", "email": "esi@brand.example", "agree": True, "company": "Afa Foods", "role": "Brand manager",
         "phone": "024 455 6677", "interests": ["Giveaway", "Sponsored stream segment", "Not an option"], "streamers": ["Galacta"],
         "goal": "Launch a product", "budget": "GHS 5,000 to 15,000",
         "message": "A weekly segment on Galacta's stream for our new chips.\n\nCan you send a rate card?"}


async def test_each_reason_asks_for_its_own_details_and_checks_them(app, db):
    guest = new_client(app)
    spec = (await guest.get("/api/v1/public/contact")).json()
    reasons = {r["key"]: r for r in spec["reasons"]}
    assert {"brand", "booking", "press", "investor", "job", "order", "tickets", "content", "support", "general"} <= set(reasons)
    brand = {f["key"]: f for f in reasons["brand"]["fields"]}
    assert brand["budget"]["required"] and brand["budget"]["options"]
    assert {"value": "Galacta", "label": "Galacta"} in brand["streamers"]["options"]  # the roster, from the database
    assert reasons["job"]["fields"][0]["source"] == "jobs" and reasons["job"]["open"] is True
    cv = next(f for f in reasons["job"]["fields"] if f["key"] == "cv")
    assert cv["type"] == "file" and cv["required"] and cv["accept"] == "document"

    # Missing answers come back by name, so the form can mark them
    r = await send(guest, "brand", {"name": "Esi", "email": "esi@brand.example", "agree": True, "company": "Afa"})
    assert r.status_code == 400
    assert {"role", "phone", "interests", "goal", "budget", "message"} <= set(r.json()["detail"]["missing"])
    r = await send(guest, "brand", {**BRAND, "budget": "A million"})
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["budget"]
    r = await send(guest, "brand", {**BRAND, "agree": False})
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["agree"]
    r = await send(guest, "general", {"name": "Bot", "email": "bot@example.com", "agree": True, "subject": "Hi", "message": "x",
                                      "company_site": "http://spam.example"})
    assert r.status_code == 200  # the bot trap: looks sent, nothing is kept
    assert not (await db.execute(select(ContactMessage).where(ContactMessage.email == "bot@example.com"))).scalars().first()


async def test_a_message_reaches_its_desk_and_the_reply_is_emailed(app, db):
    guest = new_client(app)
    SesEmailService.OUTBOX.clear()
    r = await send(guest, "brand", BRAND, files={"file:brief": ("deck.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    ref = r.json()["reference"]
    m = (await db.execute(select(ContactMessage).where(ContactMessage.reference == ref))).scalar_one()
    assert m.desk == "partnerships" and m.status == "new" and m.subject.startswith("Afa Foods:")
    assert m.payload["interests"] == ["Giveaway", "Sponsored stream segment"]  # an unknown choice is dropped
    assert "phone" not in m.payload and m.phone_enc and m.phone_last3 == "677"  # the number is encrypted
    assert len(m.files) == 1 and m.files[0]["field"] == "brief"
    # The sender gets a reference and a private link; the team gets told
    got = mails_to("esi@brand.example")[0]
    link = re.search(r"/contact/c/([\w-]+)", got["html"]).group(1)
    assert ref in got["subject"] and got["reply_to"] == "contact@streamershubmedia.com"
    assert any("New message" in x["subject"] and ref in x["subject"] for x in SesEmailService.OUTBOX)

    # Partnerships (Philip) sees it; Social (Trigger) and the store don't
    philip, trigger, store = await sign_in(app, "philip"), await sign_in(app, "trigger"), await sign_in(app, "storeops")
    assert ref in [x["reference"] for x in (await philip.get("/api/v1/inbox")).json()["rows"]]
    assert ref not in [x["reference"] for x in (await trigger.get("/api/v1/inbox")).json()["rows"]]
    assert ref not in [x["reference"] for x in (await store.get("/api/v1/inbox")).json()["rows"]]
    assert (await trigger.get(f"/api/v1/inbox/{m.id}")).status_code == 404
    me = (await philip.get("/api/v1/auth/me")).json()
    assert any(i["area"] == "inbox" and i["count"] for g in me["nav"] for i in g["items"])
    detail = (await philip.get(f"/api/v1/inbox/{m.id}")).json()
    assert detail["phone"] == "024 455 6677" and {"label": "Budget", "value": "GHS 5,000 to 15,000"}.items() <= next(
        a for a in detail["answers"] if a["key"] == "budget").items()
    assert detail["files"][0]["field"] == "Brief or deck (optional)" and detail["files"][0]["url"]

    # A note stays inside the team; a reply is emailed and moves the message on
    assert (await philip.post(f"/api/v1/inbox/{m.id}/note", json={"body": "Offer the December package."})).status_code == 200
    SesEmailService.OUTBOX.clear()
    r = await philip.post(f"/api/v1/inbox/{m.id}/reply", json={"body": "Thanks Esi.\n\nRate card attached in our next email."})
    assert r.status_code == 200 and r.json()["emailed"] is True and r.json()["status"] == "replied"
    mail = mails_to("esi@brand.example")[0]
    assert mail["subject"] == f"Re: {m.subject} [{ref}]" and "Rate card attached" in mail["html"] and link in mail["html"]
    assert "Philip" in mail["html"]

    # The sender's page shows the conversation without the team's note, and they can answer there
    page = (await guest.get(f"/api/v1/public/contact/c/{link}")).json()
    assert [t["direction"] for t in page["thread"]] == ["in", "out"] and page["thread"][1]["by"] == "Streamers Hub"
    assert "email" not in page and "phone" not in json.dumps(page["answers"])
    assert (await guest.get("/api/v1/public/contact/c/not-a-real-token")).status_code == 404
    SesEmailService.OUTBOX.clear()
    r = await guest.post(f"/api/v1/public/contact/c/{link}/reply", data={"body": "Great, send it over."})
    assert r.status_code == 200 and r.json()["status"] == "awaiting"
    assert any("Reply" in x["subject"] for x in SesEmailService.OUTBOX)
    # Closing it, then they write again: it comes back
    await philip.patch(f"/api/v1/inbox/{m.id}", json={"status": "closed"})
    await guest.post(f"/api/v1/public/contact/c/{link}/reply", data={"body": "One more thing."})
    await db.refresh(m)
    assert m.status == "awaiting"
    replies = (await db.execute(select(ContactReply).where(ContactReply.message_id == m.id))).scalars().all()
    assert sorted(r.direction for r in replies) == ["in", "in", "in", "note", "out"]


async def test_the_founder_answering_for_a_desk_is_shown_to_that_desk(app, db):
    guest = new_client(app)
    r = await send(guest, "order", {"name": "Kwesi Boateng", "email": "kwesi@shop.example", "agree": True, "order_number": "SH-1043",
                                    "issue": "It hasn't arrived", "phone": "0244112233", "message": "Still says packing."})
    m = (await db.execute(select(ContactMessage).where(ContactMessage.reference == r.json()["reference"]))).scalar_one()
    assert m.desk == "store"
    founder = await sign_in(app, "founder")
    assert (await founder.post(f"/api/v1/inbox/{m.id}/reply", json={"body": "It's out with the rider today.", "close": True})).status_code == 200
    act = (await db.execute(select(StepInAction).where(StepInAction.area == "store").order_by(StepInAction.created_at.desc()))).scalars().first()
    assert act and m.reference in act.action and "Founder" in act.actor_label
    seen = (await (await sign_in(app, "storeops")).get("/api/v1/step-ins?area=store")).json()["actions"]
    assert any(m.reference in a["action"] for a in seen)
    await db.refresh(m)
    assert m.status == "closed" and m.first_reply_at


async def test_an_open_role_needs_its_flyer_and_takes_applications(app, db):
    trigger = await sign_in(app, "trigger")
    job = {"department": "creative", "employment": "full_time", "workplace": "on_site", "location": "East Legon, Accra",
           "closes_on": (now() + timedelta(days=14)).date().isoformat(), "pay": "GHS 4,000", "show_pay": True,
           "responsibilities": ["• Cut shorts from streams", "", "- Edit the weekly highlight"], "requirements": ["A showreel"]}
    r = await trigger.post("/api/v1/posts", json={"kind": "job", "title": "Motion Designer", "summary": "Make the label move.", "job": job})
    assert r.status_code == 200, r.text
    post = r.json()
    assert post["job_raw"]["responsibilities"] == ["Cut shorts from streams", "Edit the weekly highlight"]
    # No flyer, no publishing
    r = await trigger.post(f"/api/v1/posts/{post['id']}/status", json={"status": "published"})
    assert r.status_code == 400 and "flyer" in r.json()["detail"]["message"]
    r = await trigger.post(f"/api/v1/posts/{post['id']}/image", data={"layout": "thumbnail"}, files={"file": ("flyer.png", png(), "image/png")})
    assert r.status_code == 200 and r.json()["layout"] == "flyer"  # a role is always shown as a whole flyer
    assert (await trigger.post(f"/api/v1/posts/{post['id']}/status", json={"status": "published"})).status_code == 200
    assert (await trigger.delete(f"/api/v1/posts/{post['id']}/image")).status_code == 409

    guest = new_client(app)
    jobs = (await guest.get("/api/v1/public/jobs")).json()
    mine = next(j for j in jobs["open"] if j["slug"] == post["slug"])
    assert mine["job"]["employment_label"] == "Full-time" and mine["job"]["pay"] == "GHS 4,000" and mine["state"] == "open"
    assert "camera-operator" in [j["slug"] for j in jobs["closed"]]  # closed by its date
    assert post["slug"] not in json.dumps((await guest.get("/api/v1/public/posts")).json())  # not on Live and events
    roles = next(r for r in (await guest.get("/api/v1/public/contact")).json()["reasons"] if r["key"] == "job")["fields"][0]["options"]
    assert post["slug"] in [o["value"] for o in roles] and "camera-operator" not in [o["value"] for o in roles]

    # Applying: the CV is required, and only once per role
    base = {"name": "Akua Sarpong", "email": "akua@cv.example", "agree": True, "role": post["slug"], "phone": "0209998887",
            "city": "Accra", "experience": "1 to 2 years", "availability": "Right away", "message": "I animate gaming intros.",
            "via": "whatsapp"}
    r = await send(guest, "job", base)
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["cv"]
    r = await send(guest, "job", base, files={"file:cv": ("cv.png", png(200, 200), "image/png")})
    assert r.status_code == 400  # a CV must be a PDF
    SesEmailService.OUTBOX.clear()
    r = await send(guest, "job", base, files={"file:cv": ("akua-cv.pdf", PDF, "application/pdf")})
    assert r.status_code == 200, r.text
    assert "Motion Designer" in mails_to("akua@cv.example")[0]["html"]
    assert (await send(guest, "job", base, files={"file:cv": ("cv.pdf", PDF, "application/pdf")})).status_code == 409
    m = (await db.execute(select(ContactMessage).where(ContactMessage.email == "akua@cv.example"))).scalar_one()
    assert m.desk == "people" and m.stage == "applied" and m.via == "whatsapp" and m.payload["department"] == "creative"

    # The posts desk sees how many applied; the people side sees who
    rows = (await trigger.get("/api/v1/posts")).json()
    row = next(p for p in rows["posts"] if p["id"] == post["id"])
    assert row["applicants"]["total"] == 1 and rows["job_totals"]["applicants"] >= 1
    assert m.reference not in json.dumps((await trigger.get("/api/v1/inbox?view=open")).json())
    kingmille = await sign_in(app, "kingmille")  # Creative Lead: applications for creative roles only
    seen = (await kingmille.get("/api/v1/inbox?reason=job&view=open")).json()["rows"]
    assert m.reference in [x["reference"] for x in seen]
    assert all(x["role_title"] != "Events and Community Intern" for x in seen)
    r = await kingmille.patch(f"/api/v1/inbox/{m.id}", json={"stage": "shortlisted"})
    assert r.status_code == 200 and r.json()["stage_label"] == "Shortlisted"
    stats = (await trigger.get(f"/api/v1/posts/{post['id']}/stats")).json()["stats"]
    assert stats["applications"] == 1 and stats["applications_via"] == {"whatsapp": 1}

    # Closing applications takes it off the form
    await trigger.put(f"/api/v1/posts/{post['id']}", json={"kind": "job", "title": "Motion Designer", "summary": "Make the label move.",
                                                           "job": {**job, "closed": True}})
    r = await send(guest, "job", {**base, "email": "late@cv.example"}, files={"file:cv": ("cv.pdf", PDF, "application/pdf")})
    assert r.status_code == 400 and "isn't open" in r.json()["detail"]["message"]


async def test_shares_are_counted_for_posts_roles_and_link_pages(app, db):
    guest = new_client(app)
    role = (await db.execute(select(Post).where(Post.slug == "events-and-community-intern"))).scalar_one()
    before = role.shares
    r = await guest.post(f"/api/v1/public/posts/{role.slug}/track", json={"kind": "share", "vid": "visitor-123", "via": "whatsapp"})
    assert r.json()["counted"] is True
    r = await guest.post(f"/api/v1/public/posts/{role.slug}/track", json={"kind": "click", "vid": "visitor-123", "target": "apply"})
    assert r.json()["counted"] is True
    # The team copying the link from Hub OS counts too, as "Team"
    philip = await sign_in(app, "philip")
    r = await philip.post(f"/api/v1/posts/{role.id}/shared", json={})
    assert r.status_code == 200 and r.json()["shares"] == before + 2
    via = {v["via"]: v for v in (await philip.get(f"/api/v1/posts/{role.id}/stats")).json()["stats"]["via"]}
    assert via["team"]["shares"] == 1 and via["whatsapp"]["shares"] == 1
    # Link pages
    r = await guest.post("/api/v1/public/links/galacta/share", json={"vid": "visitor-123", "via": "whatsapp"})
    assert r.json()["counted"] is True
    assert (await guest.post("/api/v1/public/links/galacta/share", json={"vid": "visitor-123", "via": "whatsapp"})).json()["counted"] is False
    page = (await db.execute(select(LinkPage).where(LinkPage.slug == "galacta"))).scalar_one()
    r = await philip.post(f"/api/v1/link-pages/{page.id}/shared")
    assert r.json()["shares"] == 2
    pages = (await philip.get("/api/v1/link-pages")).json()["pages"]
    assert next(p for p in pages if p["slug"] == "galacta")["shares"] == 2
