"""Every role is refused outside its scope (section 4). The API is the guard; the sidebar only mirrors it."""

import pytest

from tests.conftest import new_client, sign_in

DESKS = {
    "command": "/api/v1/os/command",
    "people": "/api/v1/people",
    "review": "/api/v1/review",
    "capital": "/api/v1/capital/overview",
    "production": "/api/v1/production/summary",
    "partnerships": "/api/v1/partnerships/summary",
    "social": "/api/v1/social/summary",
    "creative": "/api/v1/creative/summary",
    "talent": "/api/v1/talent/summary",
    "tech": "/api/v1/tech/summary",
    "finance": "/api/v1/finance/summary",
    "store": "/api/v1/admin/store/summary",
    "settings": "/api/v1/settings",
}

# Who may open which desk. Everything not listed must be refused.
ALLOWED = {
    "founder": set(DESKS),
    "philip": {"command", "people", "partnerships", "social", "review"},          # co-founder, Marketing & Partnerships
    "bernard": {"command", "people", "tech"},                                       # co-founder, CTO
    "aaron": {"command", "people", "capital"},                                      # co-founder, Strategy & Investors
    "ahmed": {"finance", "store", "capital"},                                       # CFO
    "kingmille": {"people", "creative", "review"},                                  # Creative Lead (department head)
    "certified": {"production", "review"},                                          # Producer
    "trigger": {"social", "review"},                                                # Social Media Manager
    "storeops": {"store"},
    "galacta": {"talent"},                                                          # streamer
    "ama_draws": set(),                                                             # designer
    "kwesi_cuts": set(),                                                            # clipper
}


@pytest.mark.parametrize("who", list(ALLOWED))
async def test_desk_access_matrix(app, who):
    c = await sign_in(app, who)
    me = (await c.get("/api/v1/auth/me")).json()
    for area, url in DESKS.items():
        r = await c.get(url)
        if area in ALLOWED[who]:
            assert r.status_code == 200, f"{who} should open {area}: {r.status_code} {r.text[:200]}"
            if area != "settings":
                assert area in me["areas"], f"{who}'s sidebar is missing {area}"
        else:
            assert r.status_code == 403, f"{who} must be refused {area}, got {r.status_code}"
            assert area not in me["areas"], f"{who}'s sidebar shows {area} but the API refuses it"


async def test_signed_out_is_refused_everywhere(app):
    c = new_client(app)
    for url in list(DESKS.values()) + ["/api/v1/designer/summary", "/api/v1/clipper/summary", "/api/v1/me/overview"]:
        assert (await c.get(url)).status_code == 401, url


async def test_review_lists_follow_the_reviewer(app):
    expect = {"founder": ["design", "designer", "clipper", "streamer"], "kingmille": ["design", "designer"],
              "trigger": ["clipper"], "certified": ["streamer"]}
    for who, lists in expect.items():
        c = await sign_in(app, who)
        r = (await c.get("/api/v1/review")).json()
        assert [t["key"] for t in r["tabs"]] == lists, who
        for track in ("designer", "clipper", "streamer"):
            code = (await c.get(f"/api/v1/applications?track={track}")).status_code
            assert code == (200 if track in lists else 403), (who, track)


async def test_portal_users_cannot_reach_hub_os_or_each_other(app):
    designer = await sign_in(app, "ama_draws")
    clipper = await sign_in(app, "kwesi_cuts")
    assert (await designer.get("/api/v1/designer/summary")).status_code == 200
    assert (await designer.get("/api/v1/clipper/summary")).status_code == 403
    assert (await clipper.get("/api/v1/designer/summary")).status_code == 403
    assert (await clipper.get("/api/v1/clips")).status_code == 403  # verification is the Social Manager's
    me = (await designer.get("/api/v1/auth/me")).json()
    assert me["home"] == "/studio" and me["nav"] == [] and me["areas"] == []


async def test_designer_only_loads_own_designs(app):
    ama = await sign_in(app, "ama_draws")
    kofi = await sign_in(app, "kofi_inks")
    mine = (await kofi.get("/api/v1/designs/mine")).json()
    assert mine and all(d["designer"] == "@kofi.inks" for d in mine)
    other = mine[0]["id"]
    assert (await ama.get(f"/api/v1/designs/{other}")).status_code == 404
    assert (await ama.get("/api/v1/designs")).status_code == 403  # the review list is staff-only


async def test_only_finance_and_founder_see_vesting(app):
    founder = await sign_in(app, "founder")
    kingmille = await sign_in(app, "kingmille")
    people = (await founder.get("/api/v1/people")).json()["team"]
    trigger_id = next(p["id"] for p in people if p["name"] == "Trigger")
    assert "vesting" in (await founder.get(f"/api/v1/people/{trigger_id}")).json()
    km_people = (await kingmille.get("/api/v1/people")).json()
    assert km_people["scoped"] and all(p["department"] == "Creative" for p in km_people["team"])
    assert (await kingmille.get(f"/api/v1/people/{trigger_id}")).status_code == 404  # outside their department
    own = (await kingmille.get("/api/v1/me/overview")).json()
    assert own["vesting"] and own["vesting"][0]["percent"] == 1.5


async def test_only_the_founder_changes_settings_and_roles(app):
    ahmed = await sign_in(app, "ahmed")
    assert (await ahmed.put("/api/v1/settings/commission", json={"default_rate_bps": 2000})).status_code == 403
    founder = await sign_in(app, "founder")
    r = await founder.put("/api/v1/settings/commission", json={"default_rate_bps": 9000})
    assert r.status_code == 400
    r = await founder.put("/api/v1/settings/commission", json={"default_rate_bps": 1500})
    assert r.status_code == 200 and r.json()["default_rate_bps"] == 1500
