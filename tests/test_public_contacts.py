"""Phone numbers never reach the public: no call, text or WhatsApp-chat links on link pages, jobs or posts.
Visitors' own messages (the contact form) are unchanged."""

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.security import now
from app.models.posts import LinkItem, LinkPage
from app.services import PostService
from tests.conftest import new_client, sign_in

PHONE_LINKS = ["tel:+233596157150", "sms:+233596157150", "whatsapp://send?phone=233596157150", "wa.me/233596157150",
               "https://wa.me/+233596157150", "https://api.whatsapp.com/send?phone=233596157150"]


def test_phone_links_are_refused_on_public_pages():
    for url in PHONE_LINKS:
        with pytest.raises(HTTPException) as err:
            PostService.ok_url(url, allow_email=True)
        assert "Phone numbers" in err.value.detail, url
    # Channels, groups, pages and email addresses are fine
    assert PostService.ok_url("https://whatsapp.com/channel/0029VaStreamersHub") == "https://whatsapp.com/channel/0029VaStreamersHub"
    assert PostService.ok_url("https://chat.whatsapp.com/AbCdEfGh") == "https://chat.whatsapp.com/AbCdEfGh"
    assert PostService.ok_url("youtube.com/@galacta") == "https://youtube.com/@galacta"
    assert PostService.ok_url("mailto:careers@streamershubmedia.com", allow_email=True) == "mailto:careers@streamershubmedia.com"


def test_the_contact_form_still_takes_links_as_before():
    with pytest.raises(HTTPException) as err:
        PostService.ok_url("tel:+233596157150", public_page=False)
    assert "https://" in err.value.detail
    assert PostService.ok_url("wa.me/233596157150", public_page=False) == "https://wa.me/233596157150"


async def test_link_pages_and_jobs_never_show_a_phone_number(app, db):
    galacta = await sign_in(app, "galacta")
    await galacta.get("/api/v1/talent/links")  # makes the page if it isn't there yet
    r = await galacta.put("/api/v1/talent/links", json={"title": "Galacta", "theme": "y", "items": [{"label": "Call me", "url": "tel:+233596157150"}]})
    assert r.status_code == 400 and "Phone numbers" in r.text

    # One saved before this rule: gone from the public page and its redirect, still in the editor to remove
    page = (await db.execute(select(LinkPage).where(LinkPage.slug == "galacta"))).scalar_one()
    item = LinkItem(page_id=page.id, kind="whatsapp", label="WhatsApp me", url="https://wa.me/233596157150", position=99)
    db.add(item)
    await db.commit()
    guest = new_client(app)
    public = (await guest.get("/api/v1/public/links/galacta")).json()
    assert all(i["label"] != "WhatsApp me" for i in public["items"])
    assert (await guest.get(f"/api/v1/public/links/go/{item.id}")).status_code == 404
    mine = (await galacta.get("/api/v1/talent/links")).json()["page"]
    assert any(i["label"] == "WhatsApp me" for i in mine["items"])

    # Jobs: apply by email, not by phone
    trigger = await sign_in(app, "trigger")
    job = {"department": "creative", "employment": "full_time", "workplace": "on_site", "location": "East Legon, Accra",
           "closes_on": (now() + timedelta(days=14)).date().isoformat(), "responsibilities": ["Cut shorts from streams"],
           "apply_via": "link", "apply_url": "tel:+233596157150"}
    r = await trigger.post("/api/v1/posts", json={"kind": "job", "title": "Clip Editor", "summary": "Cut the moments.", "job": job})
    assert r.status_code == 400 and "Phone numbers" in r.text
    job["apply_url"] = "mailto:careers@streamershubmedia.com"
    r = await trigger.post("/api/v1/posts", json={"kind": "job", "title": "Clip Editor", "summary": "Cut the moments.", "job": job})
    assert r.status_code == 200, r.text
