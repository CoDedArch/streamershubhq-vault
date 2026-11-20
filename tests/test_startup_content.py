"""A brand-new database (as in production) gets the public site's starting content on startup: the signed
streamer with his honours, the roster seats, the label's merch, and (until the real ones are ready) sample
partners and sample Designer Series pieces, which the team removes in one go. Never twice, and never again
after the team removes something. Inviting the streamer links his account to the profile already on the site."""

import pytest
from sqlalchemy import func, select, text

from app.core.database import load_models, session_manager, setup_schema
from app.core.security import hash_password
from app.models.base import Base
from app.models.partnerships import Brand
from app.models.people import AppSetting
from app.models.store import Product
from app.models.talent import Honour, RosterSeat, Stream, Streamer
from app.services import Bootstrap
from tests.conftest import DOMAIN, PASSWORD, new_client, sign_in


@pytest.fixture(scope="module")
async def empty(engine):
    """An empty database with only the Founder in it, the way a real launch starts."""
    from scripts.create_founder import create

    load_models()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        for seq in ("order_number_seq", "design_ref_seq", "interest_number_seq"):
            await conn.execute(text(f"DROP SEQUENCE IF EXISTS {seq}"))
        await setup_schema(conn)
    async with session_manager.session_factory() as db:
        user, _ = await create(db, "Founder & President", f"founder@{DOMAIN}", "Founder & President")
        user.password_hash, user.status = hash_password(PASSWORD), "active"
        await db.commit()
    from app.main import create_app

    return create_app(lifespan_on=False)


async def test_startup_fills_the_public_site_once(empty):
    guest = new_client(empty)
    before = (await guest.get("/api/v1/public/home")).json()
    assert before["roster"] == [] and before["products"] == [] and before["partners"] == []

    async with session_manager.session_factory() as db:
        done = await Bootstrap.run(db)
    assert set(done) == {"roster_v1", "galacta_gta6_v1", "label_line_v1", "sample_partners_v1", "sample_designer_line_v1",
                         "sample_clippers_v1", "org_link_bio_v1", "founder_v1", "galacta_gta6_live_v1", "galacta_gta6_day2_v1"}
    assert done["founder_v1"] == "there's a Founder already; left alone"
    home = (await guest.get("/api/v1/public/home")).json()
    star = home["roster"][0]
    assert star["stage_name"] == "Galacta" and star["number"] == "01" and star["content"].startswith("GTA 6")
    assert len(star["honours"]) == 5 and [s["n"] for s in home["seats"]] == ["02", "03", "04"]
    assert len(home["products"]) == 4 and len(home["partners"]) == 20
    store = (await guest.get("/api/v1/products")).json()["products"]
    assert len(store) == 16 and sum(1 for p in store if p["category"] == "designer") == 3
    assert all(p["images"] for p in store if p["category"] == "label")

    # Running again (every restart) changes nothing; a product the team removes stays removed
    async with session_manager.session_factory() as db:
        p = (await db.execute(select(Product).where(Product.slug == "gold-keychain"))).scalar_one()
        await db.delete(p)
        await db.commit()
        assert await Bootstrap.run(db) == {}
        assert await db.scalar(select(func.count()).select_from(Streamer)) == 1
        assert await db.scalar(select(func.count()).select_from(Honour)) == 5
        assert await db.scalar(select(func.count()).select_from(RosterSeat)) == 4
        assert await db.scalar(select(func.count()).select_from(Product)) == 15
        assert await db.scalar(select(func.count()).select_from(Brand)) == 20
        assert (await db.execute(select(AppSetting).where(AppSetting.key == "bootstrap"))).scalar_one().value["done"]


async def test_inviting_the_streamer_links_his_account_to_the_profile_on_the_site(empty):
    founder = await sign_in(empty, "founder")
    people = (await founder.get("/api/v1/people")).json()
    profile = next(p for p in people["profiles"] if p["stage_name"] == "Galacta")
    r = await founder.post("/api/v1/people/invite", json={
        "name": "Lamar", "email": f"lamar@{DOMAIN}", "role": "streamer", "department": "talent", "title": "Streamer · Signing No. 01",
        "desk": "talent", "pay_type": "salary", "streamer_id": profile["id"]})
    assert r.status_code == 200, r.text
    async with session_manager.session_factory() as db:
        s = (await db.execute(select(Streamer).where(Streamer.stage_name == "Galacta"))).scalar_one()
        assert str(s.user_id) == r.json()["id"] and s.is_public  # the same profile: still on the site
        assert await db.scalar(select(func.count()).select_from(Streamer)) == 1
    # He stays on the public site the whole time, and the profile can't be taken twice
    assert (await new_client(empty).get("/api/v1/public/home")).json()["roster"][0]["stage_name"] == "Galacta"
    again = await founder.post("/api/v1/people/invite", json={
        "name": "Someone", "email": f"someone@{DOMAIN}", "role": "streamer", "department": "talent", "title": "Streamer",
        "desk": "talent", "pay_type": "salary", "streamer_id": profile["id"]})
    assert again.status_code == 409
    assert profile["id"] not in [p["id"] for p in (await founder.get("/api/v1/people")).json()["profiles"]]


async def test_samples_come_off_in_one_go_and_real_partners_go_on(empty):
    founder = await sign_in(empty, "founder")
    guest = new_client(empty)
    summary = (await founder.get("/api/v1/partnerships/summary")).json()
    assert len(summary["website"]) == 20 and all(b["sample"] for b in summary["website"])
    products = (await founder.get("/api/v1/admin/store/products")).json()
    assert sum(1 for p in products if p["sample"]) == 3

    # A real partner, switched on beside the samples
    brand_id = (await founder.post("/api/v1/brands", json={"name": "Real Brand Co", "sector": "Gaming"})).json()["id"]
    r = await founder.post(f"/api/v1/brands/{brand_id}/website", json={"show": True, "wordmark": "c"})
    assert r.status_code == 200 and r.json()["public"] is True
    assert {"n": "Real Brand Co", "c": "c"} in (await guest.get("/api/v1/public/home")).json()["partners"]

    # Ready for launch: the samples go, the real partner and the label line stay
    r = await founder.post("/api/v1/brands/remove-samples")
    assert r.status_code == 200 and r.json() == {"removed": 20, "kept": 0}
    assert (await guest.get("/api/v1/public/home")).json()["partners"] == [{"n": "Real Brand Co", "c": "c"}]
    r = await founder.post("/api/v1/admin/store/products/remove-samples")
    assert r.status_code == 200 and r.json() == {"removed": 3, "kept": 0}
    store = (await guest.get("/api/v1/products")).json()["products"]
    assert len(store) == 12 and all(p["category"] == "label" for p in store)
    # and a restart doesn't bring them back
    async with session_manager.session_factory() as db:
        assert await Bootstrap.run(db) == {}
    assert len((await guest.get("/api/v1/public/home")).json()["partners"]) == 1


async def test_sample_clippers_fill_the_clip_table_but_cannot_sign_in_or_be_paid(empty):
    from datetime import timedelta

    from app.core.security import now
    from app.models.clips import Clip
    from app.models.people import User
    from app.services import ClipService
    from app.services.CompanyService import today
    from app.services.money import month_key

    guest = new_client(empty)
    table = (await guest.get("/api/v1/public/home")).json()["clip_table"]["rows"]
    assert len(table) == 6 and table[0]["handle"] == "@kwesi.cuts"
    # No sign-in, whatever the password
    r = await guest.post("/api/v1/auth/login", json={"email": "kwesi_cuts@sample.streamershub.test", "password": PASSWORD})
    assert r.status_code == 401
    async with session_manager.session_factory() as db:
        # Never paid, and never in the numbers investors see
        assert await ClipService.earnings_for_period(db, month_key(today())) == {}
        from app.services import CompanyService
        assert (await CompanyService.network(db))["clippers"] == 0
        # Still on the table weeks later
        await db.execute(Clip.__table__.update().values(posted_at=now() - timedelta(days=40)))
        await db.commit()
    assert len((await guest.get("/api/v1/public/home")).json()["clip_table"]["rows"]) == 6

    founder = await sign_in(empty, "founder")
    social = (await founder.get("/api/v1/social/summary")).json()
    assert social["sample_clippers"] == 8 and social["clipper_views_month"] == 0 and social["clips"] == []
    r = await founder.post("/api/v1/clippers/remove-samples")
    assert r.status_code == 200 and r.json() == {"removed": 8, "suspended": 0}
    assert (await guest.get("/api/v1/public/home")).json()["clip_table"]["rows"] == []
    async with session_manager.session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(User).where(User.is_sample.is_(True))) == 0
        assert await db.scalar(select(func.count()).select_from(Clip)) == 0
        assert await Bootstrap.run(db) == {}  # a restart doesn't bring them back


async def test_the_labels_link_page_bio_drops_the_old_city_line_only_if_nobody_changed_it(empty):
    from app.models.posts import LinkPage

    async with session_manager.session_factory() as db:
        db.add(LinkPage(slug="streamershub", title="Streamers Hub", bio=Bootstrap.OLD_ORG_BIO, theme="k"))
        await db.commit()
        assert await Bootstrap.org_link_bio(db) == "new bio"
        await db.commit()
        page = (await db.execute(select(LinkPage).where(LinkPage.slug == "streamershub"))).scalar_one()
        assert page.bio == "The content label for the internet's next big streamers."
        page.bio = "Written by the team."
        await db.commit()
        assert await Bootstrap.org_link_bio(db) == "nothing to change"
        assert (await db.execute(select(LinkPage.bio).where(LinkPage.slug == "streamershub"))).scalar_one() == "Written by the team."


async def test_galactas_gta6_stream_is_posted_on_his_profile_before_he_has_an_account(empty):
    from datetime import timedelta

    from app.models.posts import Post
    from app.services import PostService

    p = (await new_client(empty).get(f"/api/v1/public/posts/{Bootstrap.GTA6_LIVE['slug']}")).json()
    assert p["kind"] == "stream" and p["streamer"]["name"] == "Galacta" and p["platform_label"] == "Twitch"
    assert p["link_url"] == "https://www.twitch.tv/theonlygalacta"
    # The graphic ships with the website, so the preview image works without an upload
    assert p["image_url"] == "/brand/galacta-gta6-live.jpg" and p["share_image_url"] == "/brand/galacta-gta6-live-share.jpg"
    assert p["when"] == "Thu 19 Nov 2026 · 01:12 GMT"
    async with session_manager.session_factory() as db:
        star = (await db.execute(select(Streamer).where(Streamer.stage_name == "Galacta"))).scalar_one()
        post = (await db.execute(select(Post).where(Post.slug == Bootstrap.GTA6_LIVE["slug"]))).scalar_one()
        stream = (await db.execute(select(Stream).where(Stream.streamer_id == star.id, Stream.title == post.title))).scalar_one()
        # Both hang on his profile, the one his account takes over when he's invited
        assert post.streamer_id == star.id and post.status == "published"
        assert stream.platforms == ["twitch"] and stream.actual_start == Bootstrap.GTA6_LIVE_START
        start = Bootstrap.GTA6_LIVE_START
        assert PostService.state(post, start + timedelta(hours=1)) == "live"
        assert PostService.state(post, start + timedelta(hours=4)) == "ended"
        assert await Bootstrap.galacta_gta6_live(db) == "already there"


async def test_galactas_gta6_day2_stream_is_posted_for_youtube_with_its_thumbnail(empty):
    from datetime import timedelta

    from app.core.security import now
    from app.models.posts import Post
    from app.services import PostService

    p = (await new_client(empty).get(f"/api/v1/public/posts/{Bootstrap.GTA6_DAY2['slug']}")).json()
    assert p["kind"] == "stream" and p["streamer"]["name"] == "Galacta" and p["platform_label"] == "YouTube"
    assert p["title"].startswith("He Streamed GTA 6 for 20+ Hours Straight") and p["summary"].endswith("#GamingLive")
    assert p["link_url"] == "https://www.youtube.com/@StreamersHub_Media/live"
    assert p["image_url"] == "/brand/galacta-gta6-day2.jpg" and p["share_image_url"] == "/brand/galacta-gta6-day2-share.jpg"
    assert p["when"] == "Fri 20 Nov 2026 · 14:45 GMT"
    start = Bootstrap.GTA6_DAY2_START
    async with session_manager.session_factory() as db:
        star = (await db.execute(select(Streamer).where(Streamer.stage_name == "Galacta"))).scalar_one()
        post = (await db.execute(select(Post).where(Post.slug == Bootstrap.GTA6_DAY2["slug"]))).scalar_one()
        stream = (await db.execute(select(Stream).where(Stream.streamer_id == star.id, Stream.title == post.title))).scalar_one()
        assert post.streamer_id == star.id and post.status == "published" and stream.platforms == ["youtube"]
        # Upcoming until 14:45, then live: the stream on his profile matches the moment the database got it
        assert stream.scheduled_start == start
        assert (stream.status, stream.actual_start) == (("scheduled", None) if now() < start else ("live", start) if now() < start + PostService.LIVE_WINDOW else ("done", start))
        assert PostService.state(post, start - timedelta(minutes=10)) == "upcoming"
        assert PostService.state(post, start + timedelta(hours=1)) == "live"
        assert await Bootstrap.galacta_gta6_day2(db) == "already there"
