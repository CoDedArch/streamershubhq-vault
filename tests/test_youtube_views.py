"""YouTube Shorts view counts: read automatically as a guide, while the Social Media Manager still verifies every clip.
YouTube itself is replaced by a stand-in, so the tests check our request and our handling of its answers."""

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.clips import Clip
from app.models.money import PayrollRun
from app.services import ClipService, YouTubeService
from tests.conftest import sign_in

KEY = "test-youtube-key"
VIDEOS = {"AbCdEfGhIj1": (48210, "Kwesi Cuts"), "ZyXwVuTsRq2": (1200, "Kwesi Cuts")}  # anything else is private or gone
calls: list[httpx.Request] = []
QUOTA = {"used_up": False}  # YouTube's daily allowance, when a test wants it gone


def youtube(request: httpx.Request) -> httpx.Response:
    calls.append(request)
    if request.url.params.get("key") != KEY:
        return httpx.Response(400, json={"error": {"message": "API key not valid."}})
    if QUOTA["used_up"]:
        return httpx.Response(403, json={"error": {"message": "quota exceeded"}})
    items = [{"id": i, "statistics": {"viewCount": str(VIDEOS[i][0])}, "snippet": {"channelTitle": VIDEOS[i][1], "title": "clip"}}
             for i in request.url.params["id"].split(",") if i in VIDEOS]
    return httpx.Response(200, json={"items": items})


@pytest.fixture
def yt(monkeypatch):
    monkeypatch.setattr(YouTubeService, "transport", httpx.MockTransport(youtube))
    monkeypatch.setattr(settings, "SH_YOUTUBE_API_KEY", KEY)
    calls.clear()
    return calls


def submit(clipper, url, reported, platform="youtube_shorts"):
    return clipper.post("/api/v1/clips", json={"url": url, "platform": platform, "title": "Watch this", "views_reported": reported})


def test_video_ids_come_out_of_every_kind_of_youtube_link():
    for url in ("https://www.youtube.com/shorts/AbCdEfGhIj1", "https://youtube.com/shorts/AbCdEfGhIj1?feature=share",
                "https://m.youtube.com/watch?v=AbCdEfGhIj1&t=4", "https://youtu.be/AbCdEfGhIj1", "https://www.youtube.com/live/AbCdEfGhIj1"):
        assert YouTubeService.video_id(url) == "AbCdEfGhIj1", url
    assert YouTubeService.video_id("https://www.youtube.com/@kwesi.cuts") is None


async def test_without_a_key_nothing_is_fetched_and_the_old_flow_works(app, db, monkeypatch):
    monkeypatch.setattr(settings, "SH_YOUTUBE_API_KEY", "")
    kwesi = await sign_in(app, "kwesi_cuts")
    r = await submit(kwesi, "https://www.youtube.com/shorts/AbCdEfGhIj1", 50000)
    assert r.status_code == 200 and r.json()["views_fetched"] is None
    social = await sign_in(app, "trigger")
    assert (await social.post(f"/api/v1/clips/{r.json()['id']}/refresh")).status_code == 409
    v = await social.post(f"/api/v1/clips/{r.json()['id']}/verify", json={"views_verified": 47000})
    assert v.json()["views_verified"] == 47000  # typed by hand, as before


async def test_a_youtube_clip_gets_its_count_on_submission_and_a_gap_is_flagged(app, db, yt):
    kwesi = await sign_in(app, "kwesi_cuts")
    r = await submit(kwesi, "https://youtube.com/shorts/ZyXwVuTsRq2?feature=share", 9000)
    row = r.json()
    assert r.status_code == 200 and row["views_fetched"] == 1200 and row["platform_channel"] == "Kwesi Cuts"
    assert row["gap_warning"] and row["reported_gap_percent"] == 650 and row["status"] == "submitted"  # never auto-verified
    assert yt and yt[0].url.params["part"] == "statistics,snippet" and yt[0].url.params["id"] == "ZyXwVuTsRq2"
    social = await sign_in(app, "trigger")
    summary = (await social.get("/api/v1/social/summary")).json()
    listed = next(c for c in summary["clips"] if c["id"] == row["id"])
    assert listed["views_fetched"] == 1200 and listed["gap_warning"]
    v = await social.post(f"/api/v1/clips/{row['id']}/verify", json={"views_verified": 1180})
    assert v.json()["views_verified"] == 1180  # the Social Media Manager's number is what's paid


async def test_private_videos_and_youtube_errors_never_block_a_submission(app, db, yt):
    kwesi = await sign_in(app, "kwesi_cuts")
    r = await submit(kwesi, "https://www.youtube.com/shorts/PrivateVid01", 300)
    assert r.status_code == 200 and r.json()["views_fetched"] is None and "private" in r.json()["fetch_note"]
    QUOTA["used_up"] = True
    try:
        r = await submit(kwesi, "https://www.youtube.com/shorts/AbCdEfGhIj1?quota=1", 300)
    finally:
        QUOTA["used_up"] = False
    assert r.status_code == 200 and "quota" in r.json()["fetch_note"] and KEY not in r.json()["fetch_note"]
    r = await submit(kwesi, "https://www.tiktok.com/@kwesi.cuts/video/77777", 300, platform="tiktok")
    assert r.json()["auto_counted"] is False and r.json()["views_fetched"] is None
    social = await sign_in(app, "trigger")
    assert (await social.post(f"/api/v1/clips/{r.json()['id']}/refresh")).status_code == 400  # TikTok stays manual


async def test_refresh_is_for_the_social_desk_and_updates_the_guide(app, db, yt):
    kwesi = await sign_in(app, "kwesi_cuts")
    clip_id = (await submit(kwesi, "https://youtu.be/AbCdEfGhIj1", 48000)).json()["id"]
    assert (await kwesi.post(f"/api/v1/clips/{clip_id}/refresh")).status_code == 403
    VIDEOS["AbCdEfGhIj1"] = (51000, "Kwesi Cuts")
    social = await sign_in(app, "trigger")
    row = (await social.post(f"/api/v1/clips/{clip_id}/refresh")).json()
    assert row["views_fetched"] == 51000 and not row["gap_warning"] and row["handle"] == "@kwesi.cuts"


async def test_the_daily_job_refreshes_open_months_only(app, db, yt):
    open_clip = await db.scalar(select(Clip).where(Clip.url == "https://youtu.be/AbCdEfGhIj1"))
    closed = await db.scalar(select(PayrollRun).where(PayrollRun.status.in_(("approved", "paid"))).limit(1))
    old = Clip(clipper_user_id=open_clip.clipper_user_id, url="https://www.youtube.com/shorts/ZyXwVuTsRq2?old=1", platform="youtube_shorts",
               title="Old month", posted_at=open_clip.posted_at, views_reported=10, status="verified", views_verified=10,
               period=closed.period)
    db.add(old)
    await db.commit()
    VIDEOS["AbCdEfGhIj1"] = (60000, "Kwesi Cuts")
    yt.clear()
    assert await ClipService.refresh_open_youtube_clips(db) >= 1
    await db.commit()
    await db.refresh(open_clip)
    await db.refresh(old)
    assert open_clip.views_fetched == 60000 and old.views_fetched is None  # a paid month is left alone
    assert len(yt) == 1  # every open clip in one request
