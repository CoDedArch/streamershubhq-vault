"""People scores: leads set goals and the owner updates them, a task with no due date is neither on time nor late,
a lead can tick a team member's task off, and the Founder's score is goals and on-time delivery (nobody rates them)."""

from datetime import timedelta

from sqlalchemy import select

from app.models.people import LeadRating, User
from app.services.CompanyService import month_bounds, today
from app.services.money import month_key
from tests.conftest import sign_in


async def user_id(db, key: str) -> str:
    return str((await db.execute(select(User.id).where(User.email == f"{key}@streamershub.test"))).scalar_one())


def inputs(person: dict) -> dict:
    return {i["key"]: i for i in person["performance"]["inputs"]}


async def test_a_lead_sets_a_goal_the_owner_updates_it_and_it_counts_in_their_score(app, db):
    lead, c9 = await sign_in(app, "kingmille"), await sign_in(app, "c9per")
    c9_id, trigger_id = await user_id(db, "c9per"), await user_id(db, "trigger")
    start, end = month_bounds(month_key(today()))
    goal = {"title": "Thumbnails for every stream", "owner_user_id": c9_id, "target": 12, "unit": "thumbnails",
            "period_start": today().isoformat(), "period_end": (end - timedelta(days=1)).isoformat()}

    # The Creative Lead manages his own department only, and a goal needs a target above zero
    assert (await lead.post("/api/v1/goals", json={**goal, "owner_user_id": trigger_id})).status_code == 403
    assert (await lead.post("/api/v1/goals", json={**goal, "target": 0})).status_code == 422
    r = await lead.post("/api/v1/goals", json=goal)
    assert r.status_code == 200, r.text
    made = r.json()
    assert made["met"] is False and made["percent"] == 0

    page = (await lead.get(f"/api/v1/people/{c9_id}")).json()
    assert page["can_manage"] is True and page["can_rate"] is True
    assert inputs(page)["goals"]["detail"] == "0 of 1 goals met"

    # The owner sees it on My profile and pay and updates it
    mine = (await c9.get("/api/v1/me/overview")).json()
    assert [g["title"] for g in mine["goals"]] == ["Thumbnails for every stream"]
    r = await c9.patch(f"/api/v1/goals/{made['id']}", json={"current": 12})
    assert r.status_code == 200 and r.json()["met"] is True and r.json()["percent"] == 100
    page = (await lead.get(f"/api/v1/people/{c9_id}")).json()
    assert inputs(page)["goals"]["value"] == 100 and inputs(page)["goals"]["detail"] == "1 of 1 goals met"

    # Only a lead removes a goal
    assert (await c9.delete(f"/api/v1/goals/{made['id']}")).status_code == 403
    assert (await lead.delete(f"/api/v1/goals/{made['id']}")).status_code == 200
    assert (await c9.get("/api/v1/me/overview")).json()["goals"] == []


async def test_a_task_with_no_due_date_is_neither_on_time_nor_late_and_a_lead_can_tick_one_off(app, db):
    lead = await sign_in(app, "kingmille")
    c9_id = await user_id(db, "c9per")
    undated = (await lead.post("/api/v1/tasks", json={"title": "Sort the photo archive", "owner_user_id": c9_id})).json()
    late = (await lead.post("/api/v1/tasks", json={"title": "Poster for Friday", "owner_user_id": c9_id,
                                                    "due_on": (today() - timedelta(days=1)).isoformat()})).json()
    before = inputs((await lead.get(f"/api/v1/people/{c9_id}")).json())["on_time"]["detail"]

    # The lead ticks both off on the person page
    r = await lead.patch(f"/api/v1/tasks/{undated['id']}", json={"status": "done"})
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["on_time"] is None
    r = await lead.patch(f"/api/v1/tasks/{late['id']}", json={"status": "done"})
    assert r.json()["on_time"] is False

    # Only the dated one counts: one more task timed, and it was late
    done, timed = (int(x) for x in before.split(" tasks on time")[0].split(" of "))
    after = inputs((await lead.get(f"/api/v1/people/{c9_id}")).json())["on_time"]["detail"]
    assert after == f"{done} of {timed + 1} tasks on time"

    # Reopening clears it
    r = await lead.patch(f"/api/v1/tasks/{late['id']}", json={"status": "open"})
    assert r.json()["closed_at"] is None and r.json()["on_time"] is None


async def test_nobody_rates_the_founder(app, db):
    founder = await sign_in(app, "founder")
    me = await user_id(db, "founder")
    # Even a rating stored for the Founder doesn't count
    db.add(LeadRating(user_id=me, period=month_key(today()), rating=10))
    await db.commit()
    page = (await founder.get(f"/api/v1/people/{me}")).json()
    lead = inputs(page)["lead"]
    assert lead["weight"] == 0 and lead["value"] is None and lead["detail"] == "No lead rating for the Founder"
    assert page["can_manage"] is True and page["can_rate"] is False
    assert (await founder.post(f"/api/v1/people/{me}/rating", json={"rating": 90})).status_code == 400
