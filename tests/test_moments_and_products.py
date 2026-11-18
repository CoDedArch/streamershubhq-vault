"""Stream timestamps (streamers post, clippers claim and cut, management watches) and running the store's products
(publish, unpublish, delete or hide, restore, photos)."""

import base64

from sqlalchemy import select

from app.models.store import Product
from app.models.talent import MarkedMoment, Stream, Streamer
from app.services.MomentService import parse_at
from tests.conftest import new_client, sign_in

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


async def galacta_stream(db, status="done") -> Stream:
    star = await db.scalar(select(Streamer).where(Streamer.stage_name == "Galacta"))
    return await db.scalar(select(Stream).where(Stream.streamer_id == star.id, Stream.status == status)
                           .order_by(Stream.scheduled_start.desc()).limit(1))


def test_timestamps_read_like_people_write_them():
    assert parse_at("1:02:03") == 3723 and parse_at("23:45") == 1425 and parse_at("90") == 90 and parse_at("1h 5m") == 3900


async def test_a_streamer_posts_a_moment_and_clippers_see_it(app, db):
    galacta = await sign_in(app, "galacta")
    stream = await galacta_stream(db)
    body = {"stream_id": str(stream.id), "at": "0:12:30", "label": "Nutmeg then the chip", "kind": "clutch",
            "note": "Start at 12:20 for the build-up, keep my reaction cam."}
    r = await galacta.post("/api/v1/talent/moments", json=body)
    assert r.status_code == 200 and r.json()["at"] == "0:12:30", r.text
    assert (await galacta.post("/api/v1/talent/moments", json={**body, "at": "0:12:35"})).status_code == 409  # within 10 seconds
    assert (await galacta.post("/api/v1/talent/moments", json={**body, "at": "1:75:00"})).status_code == 400
    assert (await galacta.post("/api/v1/talent/moments", json={**body, "at": "9:00:00"})).status_code == 400  # past the end
    upcoming = await galacta_stream(db, "scheduled")
    assert (await galacta.post("/api/v1/talent/moments", json={**body, "stream_id": str(upcoming.id)})).status_code == 409
    kwesi = await sign_in(app, "kwesi_cuts")
    assert (await kwesi.post("/api/v1/talent/moments", json=body)).status_code == 403  # only streamers post here
    footage = (await kwesi.get("/api/v1/clipper/footage")).json()
    moment = next(m for s in footage for m in s["moments"] if m["label"] == "Nutmeg then the chip")
    assert moment["source"] == "streamer" and moment["by"] == "Galacta" and moment["note"].startswith("Start at 12:20")
    assert moment["claims"] == 0 and not moment["mine"]


async def test_clippers_claim_cut_and_link_and_management_sees_it(app, db):
    kwesi = await sign_in(app, "kwesi_cuts")
    footage = (await kwesi.get("/api/v1/clipper/footage")).json()
    moment = next(m for s in footage for m in s["moments"] if m["label"] == "Nutmeg then the chip")
    r = (await kwesi.post(f"/api/v1/clipper/moments/{moment['id']}/claim")).json()
    assert r == {"claimed": True, "claims": 1}
    nana = await sign_in(app, "nanaframes")
    assert (await nana.post(f"/api/v1/clipper/moments/{moment['id']}/claim")).json()["claims"] == 2
    assert (await nana.post(f"/api/v1/clipper/moments/{moment['id']}/claim")).json() == {"claimed": False, "claims": 1}  # un-claim
    r = await kwesi.post("/api/v1/clips", json={"url": "https://www.tiktok.com/@kwesi.cuts/video/nutmeg-chip-1", "platform": "tiktok",
                                                 "title": "He really chipped him", "moment_id": moment["id"], "views_reported": 1200})
    assert r.status_code == 200, r.text
    galacta = await sign_in(app, "galacta")
    assert (await galacta.delete(f"/api/v1/talent/moments/{moment['id']}")).status_code == 409  # a clip came from it
    for key in ("founder", "certified", "trigger", "philip"):
        c = await sign_in(app, key)
        board = (await c.get("/api/v1/moments")).json()
        row = next(m for s in board["streams"] for m in s["moments"] if m["id"] == moment["id"])
        assert row["claimed_by"] == ["@kwesi.cuts"] and row["clip_list"][0]["title"] == "He really chipped him", key
        assert board["totals"]["by_streamer"] >= 1
    for key in ("galacta", "kwesi_cuts", "ama_draws", "storeops"):
        assert (await (await sign_in(app, key)).get("/api/v1/moments")).status_code == 403, key


async def test_streamers_remove_only_their_own_unclipped_moments(app, db):
    galacta = await sign_in(app, "galacta")
    stream = await galacta_stream(db)
    r = await galacta.post("/api/v1/talent/moments", json={"stream_id": str(stream.id), "at": "0:40:00", "label": "Typo moment"})
    mid = r.json()["id"]
    producer_mark = await db.scalar(select(MarkedMoment).where(MarkedMoment.source == "producer").limit(1))
    assert (await galacta.delete(f"/api/v1/talent/moments/{producer_mark.id}")).status_code == 404
    assert (await galacta.delete(f"/api/v1/talent/moments/{mid}")).status_code == 200
    mine = (await galacta.get("/api/v1/talent/moments")).json()
    assert all(m["id"] != mid for s in mine["streams"] for m in s["moments"])


async def test_the_label_line_uses_the_real_product_photos(app):
    c = new_client(app)
    products = (await c.get("/api/v1/products")).json()["products"]
    label = [p for p in products if p["category"] == "label"]
    assert len(label) == 13 and all(p["images"][0].startswith("/merch/") for p in label)


async def test_unpublish_takes_it_off_sale_and_publish_brings_it_back(app, db):
    store = await sign_in(app, "storeops")
    guest = new_client(app)
    p = await db.scalar(select(Product).where(Product.slug == "gold-rim-mug"))
    r = await store.patch(f"/api/v1/admin/store/products/{p.id}", json={"status": "draft"})
    assert r.status_code == 200 and r.json()["status_label"] == "Unpublished"
    assert all(x["slug"] != "gold-rim-mug" for x in (await guest.get("/api/v1/products")).json()["products"])
    assert (await guest.get("/api/v1/products/gold-rim-mug")).status_code == 404
    quote = (await guest.post("/api/v1/cart/quote", json={"items": [{"slug": "gold-rim-mug", "qty": 1}]})).json()
    assert quote["problems"]  # can't be bought while unpublished
    assert (await store.patch(f"/api/v1/admin/store/products/{p.id}", json={"status": "live"})).status_code == 200
    assert (await guest.get("/api/v1/products/gold-rim-mug")).status_code == 200


async def test_delete_erases_unordered_products_and_hides_ordered_ones(app, db):
    founder = await sign_in(app, "founder")
    keychain = await db.scalar(select(Product).where(Product.slug == "gold-keychain"))
    r = (await founder.delete(f"/api/v1/admin/store/products/{keychain.id}")).json()
    assert r == {"deleted": True, "archived": False, "orders": 0}
    assert await db.scalar(select(Product).where(Product.slug == "gold-keychain")) is None
    cap = await db.scalar(select(Product).where(Product.slug == "signed-cap"))
    r = (await founder.delete(f"/api/v1/admin/store/products/{cap.id}")).json()
    assert r["deleted"] is False and r["archived"] is True and r["orders"] > 0
    listed = {x["slug"]: x for x in (await founder.get("/api/v1/admin/store/products")).json()}
    assert listed["signed-cap"]["status_label"] == "Deleted"
    assert (await new_client(app).get("/api/v1/products/signed-cap")).status_code == 404
    r = await founder.patch(f"/api/v1/admin/store/products/{cap.id}", json={"status": "draft"})
    assert r.json()["status_label"] == "Unpublished"  # restored, ready to publish again
    await founder.patch(f"/api/v1/admin/store/products/{cap.id}", json={"status": "live"})


async def test_product_photos_can_be_added_reordered_and_removed(app, db):
    store = await sign_in(app, "storeops")
    p = await db.scalar(select(Product).where(Product.slug == "steel-bottle"))
    r = await store.post(f"/api/v1/admin/store/products/{p.id}/images", files={"file": ("side.png", PNG, "image/png")})
    assert r.status_code == 200 and len(r.json()["images"]) == 2, r.text
    assert (await store.post(f"/api/v1/admin/store/products/{p.id}/images", files={"file": ("x.png", b"not an image", "image/png")})).status_code == 400
    r = (await store.post(f"/api/v1/admin/store/products/{p.id}/images/arrange", json={"index": 1, "action": "cover"})).json()
    assert not r["images"][0].startswith("/merch/")  # the upload is the cover now
    r = (await store.post(f"/api/v1/admin/store/products/{p.id}/images/arrange", json={"index": 0, "action": "remove"})).json()
    assert r["images"] == ["/merch/steel-bottle.jpg"]
    for key in ("trigger", "ama_draws", "galacta"):
        c = await sign_in(app, key)
        assert (await c.delete(f"/api/v1/admin/store/products/{p.id}")).status_code == 403, key
