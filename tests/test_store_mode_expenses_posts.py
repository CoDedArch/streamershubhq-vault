"""Store modes (interest or payment), purchases and expenses (with the gear bought for a streamer), stepping
in on another desk, posts with tickets and their numbers, and link pages."""

import io
from datetime import timedelta

from sqlalchemy import select

from app.core.security import now
from app.models.money import Expense, LedgerEntry
from app.models.people import AuditLog, StepInAction
from app.models.posts import LinkItem, Post
from app.models.store import StoreInterest
from tests.conftest import new_client, sign_in


def png(w=900, h=1400) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (w, h), (253, 187, 36)).save(out, "PNG")
    return out.getvalue()


async def set_mode(founder, mode):
    return await founder.put("/api/v1/settings/store", json={"mode": mode})


async def test_the_store_takes_interest_until_payment_is_switched_on(app, db):
    founder = await sign_in(app, "founder")
    assert (await set_mode(founder, "interest")).status_code == 200
    guest = new_client(app)
    assert (await guest.get("/api/v1/store/config")).json()["mode"] == "interest"
    products = (await guest.get("/api/v1/products")).json()["products"]
    item = next(p for p in products if p["in_stock"] and not p["sizes"])
    # Paying is refused while the store takes interest
    r = await guest.post("/api/v1/orders", json={"items": [{"product_id": item["id"], "qty": 1}], "zone": "accra", "method": "momo",
                                                 "name": "Esi Mensah", "email": "esi@example.com", "phone": "0241234567",
                                                 "address": "12 Oxford St", "city": "Osu"})
    assert r.status_code == 409 and "by message" in r.json()["detail"]
    r = await guest.post("/api/v1/store/interest", json={"items": [{"product_id": item["id"], "qty": 2}], "name": "Esi Mensah",
                                                         "phone": "0241234567", "email": "esi@example.com", "city": "Osu",
                                                         "message": "Can I pick up on Saturday?"})
    assert r.status_code == 200, r.text
    number = r.json()["number"]
    assert number.startswith("SH-INT-") and r.json()["total_minor"] == item["price_minor"] * 2
    # The store desk sees it with the phone number, and works it through
    ops = await sign_in(app, "storeops")
    listing = (await ops.get("/api/v1/admin/store/interests")).json()
    row = next(x for x in listing["rows"] if x["number"] == number)
    assert row["phone"] == "0241234567" and row["status"] == "new" and listing["can_switch"] is False
    assert (await ops.get("/api/v1/admin/store/summary")).json()["mode"]["new_interest"] >= 1
    r = await ops.patch(f"/api/v1/admin/store/interests/{row['id']}", json={"status": "contacted", "note": "Called, paying by MoMo"})
    assert r.json()["status"] == "contacted" and r.json()["notes"][0]["note"] == "Called, paying by MoMo"
    # Only the Founder switches the mode; in test mode payments work, so checkout opens again
    assert (await ops.put("/api/v1/settings/store", json={"mode": "checkout"})).status_code == 403
    assert (await set_mode(founder, "checkout")).status_code == 200
    assert (await guest.get("/api/v1/store/config")).json()["mode"] == "checkout"


async def test_purchases_land_in_the_ledger_and_streamers_see_their_gear_without_prices(app, db):
    ahmed = await sign_in(app, "ahmed")
    galacta = await sign_in(app, "galacta")
    streamer_id = (await galacta.get("/api/v1/talent/summary")).json()["streamer"]["id"]
    today = now().date().isoformat()
    r = await ahmed.post("/api/v1/finance/expenses", json={
        "on_date": today, "category": "equipment", "description": "Ring light and mic arm for Galacta", "amount": 1250.5,
        "vendor": "Compu Ghana", "paid_with": "momo", "streamer_id": streamer_id,
        "items": [{"name": "Ring light"}, {"name": "Mic arm", "serial_number": "MA-77"}]})
    assert r.status_code == 200, r.text
    expense_id, gear = r.json()["id"], r.json()["equipment"]
    assert len(gear) == 2
    entry = (await db.execute(select(LedgerEntry).where(LedgerEntry.source_table == "expenses", LedgerEntry.source_id == expense_id))).scalar_one()
    assert entry.type == "expense" and entry.category == "equipment" and entry.amount_minor == 125050 and "for Galacta" in entry.note
    listing = (await ahmed.get("/api/v1/finance/expenses")).json()
    row = next(x for x in listing["rows"] if x["id"] == expense_id)
    assert row["streamer"]["name"] == "Galacta" and [g["name"] for g in row["equipment"]] == ["Ring light", "Mic arm"]
    assert next(s for s in listing["by_streamer"] if s["id"] == streamer_id)["month_minor"] >= 125050
    company = (await ahmed.get("/api/v1/finance/expenses?streamer=company")).json()
    assert all(x["streamer"] is None for x in company["rows"])
    # A company expense, not tied to anyone
    r = await ahmed.post("/api/v1/finance/expenses", json={"on_date": today, "category": "software", "description": "Canva Pro", "amount": 180})
    assert r.status_code == 200
    # Editing moves the ledger with it; deleting removes the entry and keeps the gear on the register
    await ahmed.patch(f"/api/v1/finance/expenses/{expense_id}", json={"amount": 1300})
    await db.refresh(entry)
    assert entry.amount_minor == 130000
    # The streamer sees their gear, never a price, and can report a problem
    import json

    mine = (await galacta.get("/api/v1/talent/summary")).json()["equipment"]
    gear_text = json.dumps(mine)
    assert "cost" not in gear_text and "65000" not in gear_text and "130000" not in gear_text
    ring = next(g for g in mine if g["name"] == "Ring light")
    r = await galacta.post(f"/api/v1/talent/equipment/{ring['id']}/issue", json={"detail": "It flickers after ten minutes."})
    assert r.status_code == 200 and next(g for g in r.json()["equipment"] if g["id"] == ring["id"])["condition"] == "fault"
    # Tech sees it and closes it; the item is good again
    bernard = await sign_in(app, "bernard")
    tech = (await bernard.get("/api/v1/tech/summary")).json()
    issue = next(i for i in tech["issues"] if "flickers" in i["detail"])
    assert next(e for e in tech["equipment"] if e["id"] == ring["id"])["assigned_to"] == "Galacta"
    assert (await bernard.post(f"/api/v1/equipment-issues/{issue['id']}/resolve", json={"resolution": "Swapped the power brick"})).status_code == 200
    assert next(e for e in (await bernard.get("/api/v1/tech/summary")).json()["equipment"] if e["id"] == ring["id"])["condition"] == "good"
    # Nobody else reports on a streamer's behalf, and a streamer can't touch Finance
    founder = await sign_in(app, "founder")
    assert (await founder.post(f"/api/v1/talent/equipment/{ring['id']}/issue", json={"detail": "Testing on his behalf"})).status_code == 403
    assert (await galacta.get("/api/v1/finance/expenses")).status_code == 403
    r = await ahmed.delete(f"/api/v1/finance/expenses/{expense_id}")
    assert r.json()["equipment_kept"] == 2
    assert not (await db.execute(select(LedgerEntry).where(LedgerEntry.source_id == expense_id))).scalar_one_or_none()
    assert not await db.get(Expense, __import__("uuid").UUID(expense_id))


async def test_tech_gear_with_a_cost_is_also_a_purchase(app, db):
    bernard = await sign_in(app, "bernard")
    r = await bernard.post("/api/v1/equipment", json={"name": "Backup inverter", "condition": "good", "cost": 2400})
    assert r.status_code == 200, r.text
    tag = r.json()["tag"]
    e = (await db.execute(select(Expense).where(Expense.description == "Backup inverter"))).scalar_one()
    assert e.category == "equipment" and e.amount_minor == 240000
    assert (await db.execute(select(LedgerEntry).where(LedgerEntry.source_id == str(e.id)))).scalar_one().amount_minor == 240000
    assert tag.startswith("SH-EQ-")


async def test_the_founder_steps_in_and_the_desk_sees_it(app, db):
    founder = await sign_in(app, "founder")
    bernard = await sign_in(app, "bernard")
    me = (await founder.get("/api/v1/auth/me")).json()
    assert {"tech", "finance", "store", "social", "posts"} <= set(me["stepped_in"])
    assert "command" not in me["stepped_in"] and "talent" not in me["stepped_in"]
    item = (await bernard.get("/api/v1/tech/summary")).json()["equipment"][0]
    r = await founder.patch(f"/api/v1/equipment/{item['id']}", json={"condition": "fault", "condition_note": "Loose HDMI"})
    assert r.status_code == 200
    seen = (await bernard.get("/api/v1/step-ins?area=tech")).json()
    assert seen["you_step_in"] is False
    act = seen["actions"][0]
    assert "Founder" in act["who"] and item["tag"] in act["action"]
    assert (await founder.get("/api/v1/step-ins?area=tech")).json()["you_step_in"] is True
    # Reading doesn't count as a change
    before = len((await db.execute(select(StepInAction))).scalars().all())
    await founder.get("/api/v1/tech/summary")
    assert len((await db.execute(select(StepInAction))).scalars().all()) == before
    # Bernard working on his own desk isn't flagged
    await bernard.patch(f"/api/v1/equipment/{item['id']}", json={"condition": "good"})
    assert len((await db.execute(select(StepInAction))).scalars().all()) == before


async def test_step_in_access_can_be_given_to_anyone_for_some_or_all_desks(app, db):
    founder = await sign_in(app, "founder")
    people = (await founder.get("/api/v1/people")).json()["team"]
    trigger = next(p for p in people if p["name"] == "Trigger")
    galacta = next(p for p in people if p["name"].startswith("Lamar"))
    t = await sign_in(app, "trigger")
    assert (await t.get("/api/v1/admin/store/summary")).status_code == 403
    assert (await founder.patch(f"/api/v1/people/{trigger['id']}", json={"step_in": ["store", "tech"]})).status_code == 200
    detail = (await founder.get(f"/api/v1/people/{trigger['id']}")).json()["step_in"]
    assert detail["grant"] == ["tech", "store"] and detail["can_edit"] is True
    assert (await t.get("/api/v1/admin/store/summary")).status_code == 200
    assert (await t.get("/api/v1/finance/summary")).status_code == 403
    nav = {i["area"]: i for g in (await t.get("/api/v1/auth/me")).json()["nav"] for i in g["items"]}
    assert nav["store"]["step_in"] is True and nav["social"]["step_in"] is False
    # Every desk
    await founder.patch(f"/api/v1/people/{trigger['id']}", json={"step_in": ["*"]})
    assert (await t.get("/api/v1/finance/summary")).status_code == 200
    r = await t.post("/api/v1/finance/expenses", json={"on_date": now().date().isoformat(), "category": "transport",
                                                       "description": "Courier to Kumasi", "amount": 90})
    assert r.status_code == 200  # Finance work, done stepping in
    acts = (await (await sign_in(app, "ahmed")).get("/api/v1/step-ins?area=finance")).json()["actions"]
    assert acts[0]["who"].startswith("Trigger") and "Courier to Kumasi" in acts[0]["action"]
    # Never the Founder's own desks or a streamer's account; and not for streamers
    assert (await t.get("/api/v1/agreements")).status_code == 403
    assert (await t.get("/api/v1/settings")).status_code == 403
    assert (await founder.patch(f"/api/v1/people/{galacta['id']}", json={"step_in": ["*"]})).status_code == 400
    assert (await founder.patch(f"/api/v1/people/{trigger['id']}", json={"step_in": ["command"]})).status_code == 400
    assert (await t.patch(f"/api/v1/people/{trigger['id']}", json={"step_in": ["*"]})).status_code in (403, 404)
    await founder.patch(f"/api/v1/people/{trigger['id']}", json={"step_in": []})
    assert (await t.get("/api/v1/finance/summary")).status_code == 403


async def test_a_stream_post_goes_up_with_a_share_image_and_counts_anonymously(app, db):
    trigger = await sign_in(app, "trigger")
    opts = (await trigger.get("/api/v1/posts")).json()["options"]
    galacta_id = next(s["id"] for s in opts["streamers"] if s["name"] == "Galacta")
    start = (now() + timedelta(days=1)).isoformat()
    body = {"kind": "stream", "title": "NBA 2K Road to 99 with Galacta", "topic": "Gaming", "theme": "y", "streamer_id": galacta_id,
            "platform": "youtube", "starts_at": start}
    r = await trigger.post("/api/v1/posts", json=body)
    assert r.status_code == 400 and "link" in r.json()["detail"]["message"]
    r = await trigger.post("/api/v1/posts", json={**body, "link_url": "youtube.com/@StreamsHub/live"})
    assert r.status_code == 200, r.text
    post = r.json()
    assert post["link_url"] == "https://youtube.com/@StreamsHub/live" and post["status"] == "draft"
    guest = new_client(app)
    assert (await guest.get(f"/api/v1/public/posts/{post['slug']}")).status_code == 404  # drafts aren't public
    r = await trigger.post(f"/api/v1/posts/{post['id']}/image", data={"layout": "flyer"}, files={"file": ("flyer.png", png(), "image/png")})
    assert r.status_code == 200 and r.json()["share_image_url"]
    await trigger.post(f"/api/v1/posts/{post['id']}/status", json={"status": "published"})
    pub = (await guest.get(f"/api/v1/public/posts/{post['slug']}")).json()
    assert pub["state"] == "upcoming" and pub["streamer"]["name"] == "Galacta"
    assert any(p["slug"] == post["slug"] for p in (await guest.get("/api/v1/public/posts")).json()["upcoming"])
    # The preview image: a 1200×630 JPEG, served publicly so WhatsApp can fetch it
    img = await guest.get(f"/api/v1/public/posts/{post['slug']}/image?size=share")
    from PIL import Image

    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert Image.open(io.BytesIO(img.content)).size == (1200, 630)
    # Views once per visitor a day, clicks per button, likes once and undoable, shares counted
    slug = post["slug"]
    for vid in ("visitor-aaaa", "visitor-aaaa", "visitor-bbbb"):
        await guest.post(f"/api/v1/public/posts/{slug}/track", json={"kind": "view", "vid": vid, "via": "whatsapp"})
    await guest.post(f"/api/v1/public/posts/{slug}/track", json={"kind": "click", "vid": "visitor-aaaa", "target": "watch", "via": "whatsapp"})
    await guest.post(f"/api/v1/public/posts/{slug}/track", json={"kind": "click", "vid": "visitor-aaaa", "target": "watch", "via": "whatsapp"})
    await guest.post(f"/api/v1/public/posts/{slug}/track", json={"kind": "share", "vid": "visitor-bbbb", "via": "whatsapp"})
    assert (await guest.post(f"/api/v1/public/posts/{slug}/like", json={"vid": "visitor-aaaa"})).json() == {"liked": True, "likes": 1}
    assert (await guest.post(f"/api/v1/public/posts/{slug}/like", json={"vid": "visitor-aaaa"})).json()["likes"] == 1
    assert (await guest.post(f"/api/v1/public/posts/{slug}/like", json={"vid": "visitor-bbbb"})).json()["likes"] == 2
    assert (await guest.post(f"/api/v1/public/posts/{slug}/like", json={"vid": "visitor-bbbb", "on": False})).json()["likes"] == 1
    assert (await guest.get(f"/api/v1/public/posts/{slug}?vid=visitor-aaaa")).json()["liked"] is True
    stats = (await trigger.get(f"/api/v1/posts/{post['id']}/stats")).json()["stats"]
    assert (stats["views"], stats["clicks"], stats["likes"], stats["shares"], stats["people"]) == (2, 1, 1, 1, 2)
    assert stats["click_rate"] == 50.0 and stats["via"][0]["via"] == "whatsapp" and stats["targets"] == {"watch": 1}
    assert sum(d["views"] for d in stats["daily"]) == 2
    # Nothing identifying is stored: visitors are hashes
    p = (await db.execute(select(Post).where(Post.slug == slug))).scalar_one()
    assert p.views == 2
    # A link that isn't a web link is refused
    r = await trigger.put(f"/api/v1/posts/{post['id']}", json={**body, "link_url": "javascript:alert(1)"})
    assert r.status_code == 400


async def test_a_post_with_a_long_title_saves_and_is_audited_by_its_full_slug(app, db):
    """A long title makes a slug longer than the audit log's old 64-character id, which failed the whole save."""
    trigger = await sign_in(app, "trigger")
    opts = (await trigger.get("/api/v1/posts")).json()["options"]
    galacta_id = next(s["id"] for s in opts["streamers"] if s["name"] == "Galacta")
    title = "35 Hours of GTA 6 in 2 Days… Now He's Taking On Chapter 3! 🔥 Day 3 (Galacta Live) and More"
    body = {"kind": "stream", "title": title, "topic": "Gaming", "theme": "y", "streamer_id": galacta_id, "platform": "youtube",
            "starts_at": (now() + timedelta(days=2)).isoformat(), "link_url": "https://www.youtube.com/@StreamersHub_Media/live"}
    r = await trigger.post("/api/v1/posts", json=body)
    assert r.status_code == 200, r.text
    post = r.json()
    assert len(post["slug"]) > 64
    assert (await trigger.put(f"/api/v1/posts/{post['id']}", json={**body, "summary": "Chapter 3 tonight"})).status_code == 200
    assert (await trigger.post(f"/api/v1/posts/{post['id']}/status", json={"status": "published"})).status_code == 200
    actions = (await db.execute(select(AuditLog.action).where(AuditLog.entity == "post", AuditLog.entity_id == post["slug"]))).scalars().all()
    assert {"post.created", "post.edited", "post.published"} <= set(actions)


async def test_event_tickets_follow_the_store_mode(app, db):
    founder = await sign_in(app, "founder")
    philip = await sign_in(app, "philip")
    start = (now() + timedelta(days=10)).isoformat()
    r = await philip.post("/api/v1/posts", json={"kind": "event", "title": "FC 27 Cup at the Studio", "topic": "Tournament", "theme": "k",
                                                 "starts_at": start, "venue": "East Legon", "ticket_price": 40, "ticket_capacity": 3,
                                                 "tickets_open": True})
    assert r.status_code == 200, r.text
    post = r.json()
    await philip.post(f"/api/v1/posts/{post['id']}/status", json={"status": "published"})
    guest = new_client(app)
    who = {"name": "Kofi Mensah", "email": "kofi@example.com", "phone": "0201234567", "qty": 2}
    # Taking interest: a reservation, followed up by the team
    await set_mode(founder, "interest")
    r = await guest.post(f"/api/v1/public/posts/{post['slug']}/tickets", json=who)
    assert r.status_code == 200 and r.json()["status"] == "interest" and r.json()["pay_url"] is None
    code = r.json()["code"]
    assert (await guest.post(f"/api/v1/public/posts/{post['slug']}/tickets", json=who)).json()["detail"] == "Only 1 ticket left."
    tickets = (await philip.get(f"/api/v1/posts/{post['id']}/tickets")).json()
    booking = next(b for b in tickets["rows"] if b["code"] == code)
    assert booking["phone"] == "0201234567" and tickets["left"] == 1
    # Money collected by MoMo: marked paid, and it reaches the ledger as event income
    assert (await philip.patch(f"/api/v1/tickets/{booking['id']}", json={"status": "paid", "note": "MoMo ref 88231"})).status_code == 200
    entry = (await db.execute(select(LedgerEntry).where(LedgerEntry.source_table == "ticket_bookings", LedgerEntry.source_id == booking["id"]))).scalar_one()
    assert entry.category == "events" and entry.amount_minor == 8000
    assert (await guest.get(f"/api/v1/public/tickets/{code}")).json()["status"] == "paid"
    # Taking payment: pay online, then the return page confirms it
    await set_mode(founder, "checkout")
    r = await guest.post(f"/api/v1/public/posts/{post['slug']}/tickets", json={**who, "qty": 1, "method": "card"})
    assert r.json()["status"] == "pending" and "test-pay" in r.json()["pay_url"]
    code2 = r.json()["code"]
    ref = r.json()["pay_url"].split("reference=")[1].split("&")[0]
    assert (await guest.post(f"/api/v1/payments/test/{ref}", json={"outcome": "success"})).json() == {"ticket": code2}
    assert (await guest.post(f"/api/v1/public/tickets/{code2}/verify")).json()["status"] == "paid"
    assert (await guest.post(f"/api/v1/public/tickets/{code2}/verify")).json()["status"] == "paid"  # once only
    paid = (await db.execute(select(LedgerEntry).where(LedgerEntry.source_table == "ticket_bookings", LedgerEntry.category == "events"))).scalars().all()
    assert sum(e.amount_minor for e in paid) == 12000
    # Sold out now
    r = await guest.post(f"/api/v1/public/posts/{post['slug']}/tickets", json={**who, "qty": 1})
    assert r.status_code == 409 and r.json()["detail"] == "Sold out."


async def test_link_pages_for_the_label_and_each_streamer(app, db):
    guest = new_client(app)
    org = (await guest.get("/api/v1/public/links/streamershub")).json()
    assert org["kind"] == "label" and org["items"] and "go" in org["items"][0] and "url" not in org["items"][0]
    assert any(r["slug"] == "galacta" for r in org["roster"])
    r = await guest.get(org["items"][0]["go"].replace("http://api.test", ""), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://")
    galacta = await sign_in(app, "galacta")
    mine = (await galacta.get("/api/v1/talent/links")).json()["page"]
    items = [{"id": i["id"], "kind": i["kind"], "label": i["label"], "url": i["url"]} for i in mine["items"]]
    items.append({"kind": "discord", "label": "Join the Discord", "url": "discord.gg/galacta"})
    r = await galacta.put("/api/v1/talent/links", json={"title": "Galacta", "bio": "Apex and 2K, every night.", "theme": "y", "items": items})
    assert r.status_code == 200, r.text
    pub = (await guest.get("/api/v1/public/links/galacta")).json()
    assert pub["bio"] == "Apex and 2K, every night." and pub["items"][-1]["label"] == "Join the Discord"
    assert pub["next"] and pub["next"]["streamer"]["name"] == "Galacta"
    bad = await galacta.put("/api/v1/talent/links", json={"title": "Galacta", "theme": "y", "items": [{"label": "x", "url": "javascript:alert(1)"}]})
    assert bad.status_code == 400
    # Social edits any page; a streamer can't edit through the posts desk
    trigger = await sign_in(app, "trigger")
    pages = (await trigger.get("/api/v1/link-pages")).json()["pages"]
    assert pages[0]["kind"] == "label" and any(p["slug"] == "galacta" for p in pages)
    assert (await galacta.get("/api/v1/link-pages")).status_code == 403
    clicks = (await db.execute(select(LinkItem).where(LinkItem.id == __import__("uuid").UUID(org["items"][0]["id"])))).scalar_one().clicks
    assert clicks >= 1
    assert (await db.execute(select(StoreInterest))).scalars().all() is not None
