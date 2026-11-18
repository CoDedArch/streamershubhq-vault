"""Design approval (6.2) to a live product, then a paid order to designer commission (6.3) and the payout run."""

from datetime import timedelta

from sqlalchemy import func, select, update

from app.core.security import now
from app.models.money import LedgerEntry
from app.models.store import CommissionEntry, Order, OrderItem, Product
from tests.conftest import new_client, sign_in

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


async def upload(client, title="Accra Skyline Tee", uses=None, ptype="tee"):
    data = {"title": title, "product_type": ptype, "asked_price": "200", "description": "Skyline at night"}
    if uses:
        data["uses_streamer_id"] = uses
    return await client.post("/api/v1/designs", data=data, files=[("files", ("art.png", PNG, "image/png")),
                                                                   ("files", ("mock.png", PNG, "image/png"))])


async def test_upload_checks_the_file_type(app):
    ama = await sign_in(app, "ama_draws")
    r = await ama.post("/api/v1/designs", data={"title": "Bad", "product_type": "tee"},
                       files=[("files", ("art.png", b"not really a png", "image/png"))])
    assert r.status_code == 400 and "doesn't match" in r.json()["detail"]
    r = await ama.post("/api/v1/designs", data={"title": "Bad", "product_type": "tee"},
                       files=[("files", ("art.exe", b"MZ....", "application/x-msdownload"))])
    assert r.status_code == 400


async def test_design_reaches_the_store_only_through_every_approval(app, db):
    ama = await sign_in(app, "ama_draws")
    kingmille = await sign_in(app, "kingmille")
    founder = await sign_in(app, "founder")
    galacta = await sign_in(app, "galacta")
    streamer_id = (await galacta.get("/api/v1/talent/summary")).json()["streamer"]["id"]
    d = (await upload(ama, "Galacta Orbit Tee", uses=streamer_id)).json()
    assert d["status"] == "creative_review" and d["stage"] == "Creative check"
    did = d["id"]
    # Skipping ahead is refused at every gate
    assert (await founder.post(f"/api/v1/designs/{did}/approve", json={"approve": True})).status_code == 409
    assert (await galacta.post(f"/api/v1/designs/{did}/streamer-signoff", json={"approve": True})).status_code == 409
    assert (await ama.post(f"/api/v1/designs/{did}/review", json={"action": "pass", "price": 200})).status_code == 403
    # Creative asks for changes; the designer uploads again
    assert (await kingmille.post(f"/api/v1/designs/{did}/review", json={"action": "changes"})).status_code == 400  # needs a note
    r = await kingmille.post(f"/api/v1/designs/{did}/review", json={"action": "changes", "note": "Make the orbit bolder"})
    assert r.json()["status"] == "changes_requested"
    r = await ama.post(f"/api/v1/designs/{did}/resubmit", data={"note": "Bolder orbit"}, files=[("files", ("v2.png", PNG, "image/png"))])
    assert r.json()["status"] == "creative_review" and r.json()["version"] == 2
    # Creative passes it with price and commission; it uses a streamer's name, so the streamer signs off
    r = await kingmille.post(f"/api/v1/designs/{did}/review", json={"action": "pass", "price": 200, "commission_bps": 1500, "production_cost": 60})
    assert r.json()["status"] == "streamer_signoff"
    other_streamer_check = await (await sign_in(app, "kofi_inks")).post(f"/api/v1/designs/{did}/streamer-signoff", json={"approve": True})
    assert other_streamer_check.status_code == 403
    r = await galacta.post(f"/api/v1/designs/{did}/streamer-signoff", json={"approve": True})
    assert r.json()["status"] == "founder_approval"
    assert (await kingmille.post(f"/api/v1/designs/{did}/approve", json={"approve": True})).status_code == 403  # only the Founder
    assert (await founder.post(f"/api/v1/designs/{did}/approve", json={"approve": False})).status_code == 400  # decline needs a reason
    r = await founder.post(f"/api/v1/designs/{did}/approve", json={"approve": True, "stock": 10})
    assert r.json()["status"] == "live" and r.json()["product_slug"] == "galacta-orbit-tee"
    product = (await new_client(app).get("/api/v1/products/galacta-orbit-tee")).json()
    assert product["price_minor"] == 20_000 and product["collection"] == "Roster Collection"


async def test_a_product_cannot_go_live_without_a_live_design(app, db):
    ama = await sign_in(app, "ama_draws")
    d = (await upload(ama, "Unapproved Hoodie", ptype="hoodie")).json()
    p = Product(slug="sneaky", name="Sneaky", price_minor=1000, category="designer", product_type="tee", status="draft",
                design_id=d["id"], stock=5)
    db.add(p)
    await db.commit()
    ops = await sign_in(app, "storeops")
    r = await ops.patch(f"/api/v1/admin/store/products/{p.id}", json={"status": "live"})
    assert r.status_code == 409
    assert (await new_client(app).get("/api/v1/products/sneaky")).status_code == 404


async def test_checkout_prices_come_from_the_database(app):
    c = new_client(app)
    q = (await c.post("/api/v1/cart/quote", json={"items": [{"slug": "signed-cap", "qty": 2, "price_minor": 1}], "zone": "accra"})).json()
    assert q["subtotal_minor"] == 28_000 and q["delivery_minor"] == 3_000 and q["total_minor"] == 31_000
    q = (await c.post("/api/v1/cart/quote", json={"items": [{"slug": "signed-cap", "qty": 9}]})).json()
    assert q["problems"] and "left" in q["problems"][0]["problem"]  # only 3 or 4 in stock
    q = (await c.post("/api/v1/cart/quote", json={"items": [{"slug": "play-head-hoodie", "qty": 1}]})).json()
    assert "size" in q["problems"][0]["problem"]


async def buy(c, slug="accra-after-dark-tee", size="M", qty=1, method="momo"):
    body = {"items": [{"slug": slug, "qty": qty, "size": size}], "zone": "accra", "method": method, "name": "Abena Owusu",
            "email": "abena@example.test", "phone": "024 555 0101", "address": "12 Ring Road", "city": "Accra"}
    return await c.post("/api/v1/orders", json=body)


async def test_paid_order_to_commission_to_payout_run(app, db):
    c = new_client(app)
    stock_before = (await db.execute(select(Product.stock).where(Product.slug == "accra-after-dark-tee"))).scalar_one()
    r = await buy(c, qty=2)
    assert r.status_code == 200, r.text
    order = r.json()
    assert order["test_mode"] and "/checkout/test-pay?reference=" in order["pay_url"]
    ref = order["pay_url"].split("reference=")[1].split("&")[0]
    # Not paid until the provider says so
    view = (await c.post(f"/api/v1/orders/{order['number']}/verify?code={order['code']}")).json()
    assert view["status"] == "pending"
    await c.post(f"/api/v1/payments/test/{ref}", json={"outcome": "success"})
    view = (await c.post(f"/api/v1/orders/{order['number']}/verify?code={order['code']}")).json()
    assert view["status"] == "paid" and view["total_minor"] == 41_000
    again = (await c.post(f"/api/v1/orders/{order['number']}/verify?code={order['code']}")).json()
    assert again["status"] == "paid"
    o = (await db.execute(select(Order).where(Order.number == order["number"]))).scalar_one()
    sales = (await db.execute(select(func.count()).select_from(LedgerEntry).where(LedgerEntry.source_id == str(o.id),
                                                                                  LedgerEntry.category == "store"))).scalar()
    assert sales == 1  # verifying twice doesn't book the sale twice
    await db.refresh(o)
    stock_after = (await db.execute(select(Product.stock).where(Product.slug == "accra-after-dark-tee"))).scalar_one()
    assert stock_after == stock_before - 2
    entry = (await db.execute(select(CommissionEntry).join(OrderItem, OrderItem.id == CommissionEntry.order_item_id)
                              .where(OrderItem.order_id == o.id))).scalar_one()
    assert entry.amount_minor == 5_700 and entry.status == "pending"
    # 2 x GHS 190 x 15% = GHS 57, pending until delivered and past the refund window
    # Store operations moves it along; the customer can track it without an account
    ops = await sign_in(app, "storeops")
    for expected in ("packing", "to_ship", "shipped", "delivered"):
        r = await ops.patch(f"/api/v1/admin/store/orders/{o.id}", json={"action": "advance", "courier_ref": "TRK-1"})
        assert r.json()["status"] == expected
    tracked = (await new_client(app).get(f"/api/v1/orders/{order['number']}?email=abena@example.test")).json()
    assert tracked["status"] == "delivered" and all(s["done"] for s in tracked["timeline"])
    assert (await new_client(app).get(f"/api/v1/orders/{order['number']}?email=wrong@example.test")).status_code == 404
    # After the refund window the commission is payable and lands in the next draft
    await db.execute(update(Order).where(Order.id == o.id).values(delivered_at=now() - timedelta(days=15)))
    await db.commit()
    from app.services.StoreService import make_commissions_payable

    await make_commissions_payable(db)
    await db.commit()
    await db.refresh(entry)
    assert entry.status == "payable"


async def test_refund_reverses_commission_and_restocks(app, db):
    c = new_client(app)
    order = (await buy(c, slug="signed-cap", size=None)).json()
    ref = order["pay_url"].split("reference=")[1].split("&")[0]
    await c.post(f"/api/v1/payments/test/{ref}", json={"outcome": "success"})
    await c.post(f"/api/v1/orders/{order['number']}/verify?code={order['code']}")
    r = await c.post(f"/api/v1/orders/{order['number']}/refund-request", json={"code": order["code"], "reason": "Wrong colour"})
    assert r.json()["status"] == "refund_requested"
    o = (await db.execute(select(Order).where(Order.number == order["number"]))).scalar_one()
    held = (await db.execute(select(CommissionEntry).where(CommissionEntry.amount_minor == 2_100, CommissionEntry.status == "on_hold"))).scalars().all()
    assert held
    ops = await sign_in(app, "storeops")
    assert (await ops.patch(f"/api/v1/admin/store/orders/{o.id}", json={"action": "refund"})).json()["status"] == "refunded"
    for h in held:
        await db.refresh(h)
    assert any(h.status == "reversed" for h in held)


async def test_failed_payment_leaves_the_order_unpaid(app):
    c = new_client(app)
    order = (await buy(c)).json()
    ref = order["pay_url"].split("reference=")[1].split("&")[0]
    await c.post(f"/api/v1/payments/test/{ref}", json={"outcome": "failed"})
    view = (await c.post(f"/api/v1/orders/{order['number']}/verify?code={order['code']}")).json()
    assert view["status"] == "pending"


async def test_webhook_needs_a_valid_signature(app):
    c = new_client(app)
    r = await c.post("/api/v1/payments/webhook", content=b'{"event":"charge.success","data":{"reference":"x"}}',
                     headers={"x-paystack-signature": "forged"})
    assert r.status_code == 401


async def test_store_desk_sees_where_each_cedi_goes(app):
    ops = await sign_in(app, "storeops")
    s = (await ops.get("/api/v1/admin/store/summary")).json()
    assert s["kpis"]["open_orders"] >= 4 and s["stock_alerts"]
    parts = s["unit_economics"]["parts"]
    assert [p["label"] for p in parts] == ["Production", "Designer commission", "Payment fees", "Net to Streamers Hub"]
    assert sum(p["minor"] for p in parts) == s["unit_economics"]["subtotal_minor"]
