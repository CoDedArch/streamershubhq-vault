"""Prices in cedis or dollars: the Founder sets the dollar rate; until then the website shows cedis only.
Payment is always in cedis."""

from tests.conftest import new_client, sign_in


async def test_dollars_appear_once_the_founder_sets_a_rate(app):
    guest = new_client(app)
    cur = (await guest.get("/api/v1/store/config")).json()["currency"]
    assert cur["charge"] == "GHS" and [o["code"] for o in cur["options"]] == ["GHS"] and cur["rate_note"] is None

    founder = await sign_in(app, "founder")
    # Dollars first needs a rate; a silly rate is refused
    r = await founder.put("/api/v1/settings/store", json={"default_currency": "USD"})
    assert r.status_code == 400 and "rate" in r.json()["detail"]
    assert (await founder.put("/api/v1/settings/store", json={"ghs_per_usd": 0})).status_code == 400
    r = await founder.put("/api/v1/settings/store", json={"ghs_per_usd": 15.5, "default_currency": "GHS"})
    assert r.status_code == 200 and r.json()["ghs_per_usd"] == 15.5 and r.json()["rate_updated_on"]

    cur = (await guest.get("/api/v1/store/config")).json()["currency"]
    assert [o["code"] for o in cur["options"]] == ["GHS", "USD"] and cur["options"][1]["ghs_per_unit"] == 15.5
    assert cur["default"] == "GHS" and cur["rate_note"].startswith("1 USD = GHS 15.50")
    r = await founder.put("/api/v1/settings/store", json={"ghs_per_usd": 15.5, "default_currency": "USD"})
    assert r.status_code == 200
    assert (await guest.get("/api/v1/store/config")).json()["currency"]["default"] == "USD"

    # Clearing the rate goes back to cedis only
    await founder.put("/api/v1/settings/store", json={"ghs_per_usd": None, "default_currency": "GHS"})
    cur = (await guest.get("/api/v1/store/config")).json()["currency"]
    assert [o["code"] for o in cur["options"]] == ["GHS"] and cur["default"] == "GHS"
    # Only the Founder sets it
    trigger = await sign_in(app, "trigger")
    assert (await trigger.put("/api/v1/settings/store", json={"ghs_per_usd": 20})).status_code == 403
