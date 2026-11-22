"""Planning streams with a streamer (what the streamer playbook points to in Hub OS): each streamer's setup (producer,
repeat segments, moderators, chat rules), a regular weekly schedule with an end time and chosen platforms, the run of
show for any planned stream, the streamer's "running late" / "can't stream" notice to their producer, and the
stream's title and thumbnail on the streamer's desk."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.models.posts import Post
from app.models.talent import Stream, Streamer
from app.services import SesEmailService
from app.services.CompanyService import today
from tests.conftest import sign_in


def next_weekday(wd: int):
    return today() + timedelta(days=((wd - today().weekday()) % 7) or 7)


async def galacta_id(db) -> str:
    return str((await db.execute(select(Streamer.id).where(Streamer.stage_name == "Galacta"))).scalar_one())


async def set_up_galacta(certified, db) -> dict:
    listing = (await certified.get("/api/v1/production/streamers")).json()
    producer = next(p for p in listing["producers"] if p["name"] == "Certified")
    r = await certified.put(f"/api/v1/production/streamers/{await galacta_id(db)}/setup",
                            json={"producer_user_id": producer["id"], "segments": ["Chat Dares", "  chat   dares ", "Rage Meter", ""],
                                  "moderators": "Kofi and Ama", "chat_rules": "Be kind. No spoilers in the first hour."})
    assert r.status_code == 200, r.text
    return r.json()


async def test_production_sets_each_streamers_setup_and_the_streamer_sees_it_on_their_desk(app, db):
    certified = await sign_in(app, "certified")
    listing = (await certified.get("/api/v1/production/streamers")).json()
    assert any(s["stage_name"] == "Galacta" for s in listing["streamers"])
    names = {p["name"] for p in listing["producers"]}
    assert "Certified" in names and "Galacta" not in names  # producers come from the team, never the streamers
    out = await set_up_galacta(certified, db)
    assert out["segments"] == ["Chat Dares", "Rage Meter"] and out["producer"]["name"] == "Certified"

    galacta = await sign_in(app, "galacta")
    desk = (await galacta.get("/api/v1/talent/summary")).json()
    assert desk["producer"]["name"] == "Certified" and desk["producer"]["email"] == "certified@streamershub.test"
    assert desk["setup"] == {"segments": ["Chat Dares", "Rage Meter"], "moderators": "Kofi and Ama",
                             "chat_rules": "Be kind. No spoilers in the first hour."}
    assert desk["can_notice"] is True

    # Only the Production desk changes it, and the producer has to be on the team
    sid = await galacta_id(db)
    assert (await galacta.put(f"/api/v1/production/streamers/{sid}/setup", json={})).status_code == 403
    trigger = await sign_in(app, "trigger")
    assert (await trigger.get("/api/v1/production/streamers")).status_code == 403
    star_user = (await db.execute(select(Streamer.user_id).where(Streamer.stage_name == "Galacta"))).scalar_one()
    r = await certified.put(f"/api/v1/production/streamers/{sid}/setup", json={"producer_user_id": str(star_user)})
    assert r.status_code == 400 and r.json()["detail"]["missing"] == ["producer_user_id"]


async def test_a_regular_weekly_schedule_with_an_end_time_and_chosen_platforms(app, db):
    certified = await sign_in(app, "certified")
    sid = await galacta_id(db)
    thursday = next_weekday(3)
    plan = {"streamer_id": sid, "title": "Weekend marathon", "category": "GTA 6", "starts_at": f"{thursday.isoformat()}T19:00",
            "end_time": "03:00", "platforms": ["youtube"], "weekdays": [3, 4, 5], "weeks": 2}
    r = await certified.post("/api/v1/streams", json=plan)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 6
    first = out["streams"][0]
    assert (first["day"], first["time"], first["end"], first["end_day"]) == ("Thu", "19:00", "03:00", "Fri")
    assert first["platforms"] == ["YouTube"] and first["notice"] is None
    assert [s["day"] for s in out["streams"]] == ["Thu", "Fri", "Sat"] * 2

    rows = (await db.execute(select(Stream).where(Stream.title == "Weekend marathon").order_by(Stream.scheduled_start))).scalars().all()
    assert len(rows) == 6 and all(r.scheduled_end - r.scheduled_start == timedelta(hours=8) for r in rows)

    # Planning the same weeks again adds nothing; a single stream still works; platforms are checked
    assert (await certified.post("/api/v1/streams", json=plan)).status_code == 409
    one = {**plan, "title": "Sunday chill", "weekdays": [], "weeks": 1, "end_time": None, "platforms": ["twitch", "youtube"],
           "starts_at": f"{(thursday + timedelta(days=3)).isoformat()}T16:00"}
    r = await certified.post("/api/v1/streams", json=one)
    assert r.json()["created"] == 1 and r.json()["streams"][0]["platforms"] == ["Twitch", "YouTube"]
    assert r.json()["streams"][0]["end"] is None
    bad = await certified.post("/api/v1/streams", json={**one, "platforms": ["myspace"]})
    assert bad.status_code == 400 and bad.json()["detail"]["missing"] == ["platforms"]
    galacta = await sign_in(app, "galacta")
    assert (await galacta.post("/api/v1/streams", json=one)).status_code == 403


async def test_run_of_show_and_the_streamers_late_or_cant_stream_notice(app, db):
    certified = await sign_in(app, "certified")
    await set_up_galacta(certified, db)
    sid = await galacta_id(db)
    tomorrow = today() + timedelta(days=1)
    made = (await certified.post("/api/v1/streams", json={"streamer_id": sid, "title": "Chapter 4 night", "category": "GTA 6",
                                                          "starts_at": f"{tomorrow.isoformat()}T21:15", "end_time": "23:30",
                                                          "platforms": ["youtube"]})).json()
    stream_id = made["streams"][0]["id"]

    # The producer writes the run of show for any planned stream, with the streamer's segments to hand
    r = await certified.put(f"/api/v1/streams/{stream_id}/run-of-show",
                            json=[{"time": "21:00", "title": "Chat Dares", "note": "Three dares from chat"},
                                  {"time": "20:00", "title": "Open: recap and tonight's goal"}])
    assert r.status_code == 200
    plan = (await certified.get(f"/api/v1/streams/{stream_id}/plan")).json()
    assert [i["time"] for i in plan["run"]] == ["20:00", "21:00"] and plan["segments"] == ["Chat Dares", "Rage Meter"]
    assert plan["stream"]["streamer"] == "Galacta" and plan["stream"]["end"] == "23:30"

    galacta = await sign_in(app, "galacta")
    path = f"/api/v1/talent/streams/{stream_id}/notice"
    assert (await galacta.post(path, json={"kind": "late", "minutes": 7})).status_code == 400
    assert (await galacta.post(path, json={"kind": "cant"})).json()["detail"]["missing"] == ["note"]
    before = len(SesEmailService.OUTBOX)
    r = await galacta.post(path, json={"kind": "late", "minutes": 30, "note": "Power cut in my area"})
    assert r.status_code == 200 and r.json()["stream"]["notice"] == "Running 30 min late"
    mail = [m for m in SesEmailService.OUTBOX[before:] if "certified@streamershub.test" in m["to"]]
    assert len(mail) == 1 and mail[0]["subject"] == "Galacta: Running 30 min late"
    plan = (await certified.get(f"/api/v1/streams/{stream_id}/plan")).json()
    assert plan["stream"]["notice"] == "Running 30 min late" and plan["stream"]["notice_note"] == "Power cut in my area"
    r = await galacta.post(path, json={"kind": "cant", "note": "Still no power"})
    assert r.json()["stream"]["notice"] == "Can't stream"

    # Only the streamer, only their own scheduled streams
    assert (await certified.post(path, json={"kind": "late", "minutes": 30})).status_code == 403
    assert (await galacta.post(f"/api/v1/talent/streams/{uuid.uuid4()}/notice", json={"kind": "late", "minutes": 30})).status_code == 404
    await certified.patch(f"/api/v1/streams/{stream_id}", json={"status": "cancelled"})
    assert (await galacta.post(path, json={"kind": "late", "minutes": 30})).status_code == 409


async def test_the_streamers_desk_shows_the_streams_title_and_thumbnail_from_the_website_post(app, db):
    galacta = await sign_in(app, "galacta")
    desk = (await galacta.get("/api/v1/talent/summary")).json()
    tonight = await db.get(Stream, uuid.UUID(desk["tonight"]["id"]))
    # Clear any sample post near this stream (whether one matches depends on when the sample data was made)
    for p in (await db.execute(select(Post).where(Post.kind == "stream", Post.streamer_id == tonight.streamer_id))).scalars():
        p.status = "archived"
    await db.commit()
    assert (await galacta.get("/api/v1/talent/summary")).json()["post"] is None  # no published post, nothing to show

    # A post three hours off is a different stream; one within two hours is this one
    def post(slug, title, shift):
        return Post(slug=slug, kind="stream", title=title, streamer_id=tonight.streamer_id, status="published",
                    starts_at=tonight.scheduled_start + shift, image_key="/brand/galacta-tonight.jpg",
                    link_url="https://www.youtube.com/@StreamersHub_Media/live")
    db.add(post("galacta-later", "A later stream", timedelta(hours=3)))
    await db.commit()
    assert (await galacta.get("/api/v1/talent/summary")).json()["post"] is None
    db.add(post("galacta-tonight", "Tonight's big one", timedelta(minutes=30)))
    await db.commit()
    desk = (await galacta.get("/api/v1/talent/summary")).json()
    assert desk["post"]["title"] == "Tonight's big one" and desk["post"]["image_url"] == "/brand/galacta-tonight.jpg"
    assert any(u["id"] == desk["tonight"]["id"] for u in desk["upcoming"])
