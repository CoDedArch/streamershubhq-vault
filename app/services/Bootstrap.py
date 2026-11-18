"""What the public website needs before anyone has entered anything: the signed streamer and his honours,
the roster seats, and the label's own merch line. Runs at every start, in every environment, including
production.

Each step runs once per database and is then recorded in the "bootstrap" setting, so anything the team
later edits, archives or deletes stays that way. A step also skips anything that already exists, so the
sample database (which has all of this already) is left alone.

Also, at the Founder's request (18 Nov 2026), sample content shown until the real thing replaces it: the
design's 20 placeholder brand partners, three Designer Series pieces, and eight clippers on the Clip Table.
These are marked `is_sample`, so Hub OS tags them and removes them all in one go (Partnerships, Store
operations and Social). Sample clippers can't sign in, are never paid and never count in the numbers. Everything else that
is sample data (people, orders, money) stays in scripts/seed.py and never reaches production.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.partnerships import Brand
from app.models.people import AppSetting
from app.models.posts import LinkPage
from app.models.store import Product
from app.models.talent import Honour, RosterSeat, Streamer

logger = logging.getLogger(__name__)

GALACTA = {"stage_name": "Galacta", "known_as": "Lamar", "category": "gaming", "signing_number": 1,
           "content": "GTA 6 · NBA 2K · EA FC · Apex Legends", "image_url": "/brand/galacta.jpg",
           "bio": "Known off-stream as Lamar. A competitor first, an entertainer always. This is the record he walked in with."}
OLD_CONTENT = "Apex Legends · NBA 2K · EA FC"
HONOURS = [("Back-to-back champion", "NBA 2K24 and 2K25 at DKG Hub"), ("Runner-up", "NBA 2K24 tournament by Ghana Esports"),
           ("Former #1 Wraith in Ghana", "By kills, Apex Legends"), ("Former top 11", "Ghana NBA 2K26 leaderboard"),
           ("Elite in Division Rivals", "FIFA 25 and FC 26")]
OPEN_SEATS = [(2, "The Competitor", "Ranked play, tournaments, clutch moments."),
              (3, "The Entertainer", "Big personality. The chat never goes quiet."),
              (4, "The Wildcard", "Sports, music, comedy, lifestyle. Surprise us.")]

# The label's own line, with the real product shots in the website's public/merch folder:
# slug, name, type, tag, price (GHS), production cost (GHS), starting stock, photo, description
LABEL_LINE = [
    ("pro-jersey", "Streamers Hub Pro Jersey", "jersey", "Drop 001", 350, 120, 30, "pro-jersey",
     "Breathable esports jersey in label black, with yellow and teal piping. Crest on the chest, the full play-head on the back."),
    ("classic-logo-tee", "Classic Logo Tee", "tee", "Bestseller", 180, 55, 40, "classic-logo-tee",
     "Heavyweight cotton tee. Small crest on the chest, the big play-head across the back."),
    ("play-head-hoodie", "Play-Head Hoodie", "hoodie", "Drop 001", 380, 140, 18, "play-head-hoodie",
     "Brushed-fleece hoodie with the crest on the chest and the play-head across the back."),
    ("washed-oversized-tee", "Washed Oversized Tee", "tee", "New", 220, 70, 25, "washed-oversized-tee",
     "Oversized, garment-washed tee for a lived-in black. Crest front, play-head back."),
    ("stream-desk-mat-xl", "Stream Desk Mat XL", "mat", "New", 220, 70, 25, "desk-mat-xl",
     "Extra-large stitched-edge desk mat. Room for keyboard and mouse, crest in the corner."),
    ("piped-snapback", "Piped Snapback", "cap", None, 160, 50, 15, "piped-snapback",
     "Flat-brim snapback with raised embroidery and yellow and teal piping."),
    ("tonal-back-tee", "Tonal Back Tee", "tee", None, 190, 60, 20, "tonal-back-tee",
     "Black on black: a quiet crest up front and a tonal play-head across the back."),
    ("gold-rim-mug", "Gold Rim Mug", "mug", None, 120, 35, 30, "gold-rim-mug", "Matte black mug with a gold rim and handle."),
    ("gold-tag-tote", "Gold Tag Tote", "tote", None, 200, 60, 20, "gold-tag-tote",
     "Heavy canvas tote with piped handles and a gold play-head tag."),
    ("cream-logo-tee", "Cream Logo Tee", "tee", None, 180, 55, 20, "cream-logo-tee",
     "The classic in cream. Crest on the chest, oversized play-head across the back."),
    ("steel-bottle", "Steel Bottle", "bottle", None, 170, 55, 20, "steel-bottle",
     "Insulated steel bottle. Cold for a full stream, hot for the morning after."),
    ("gold-keychain", "Gold Play-Head Keychain", "keychain", None, 60, 15, 50, "gold-keychain",
     "Solid gold-tone keychain cut in the shape of the play-head, with enamel inlay."),
    ("studio-gift-set", "Studio Gift Set", "set", "Gift set", 290, 90, 10, "studio-gift-set",
     "The gold rim mug and the gold tag tote together, boxed as a gift."),
]


# Placeholder brand partners from the design: fictional names, shown until real partners are added.
# name, lettering style (the grid's a, b, c, e, f), sector
SAMPLE_PARTNERS = [("Voltacel", "a", "Telecoms and fintech"), ("Kasa Mobile", "b", "Telecoms and fintech"), ("nuvo phones", "c", "Devices and gear"),
                   ("Tembo", "e", "Food and drink"), ("Rush+ Energy", "f", "Food and drink"), ("Zuri Malt", "e", "Food and drink"),
                   ("adom pay", "c", "Telecoms and fintech"), ("Sankofa Bank", "a", "Banks and insurance"), ("Harmattan", "b", "Food and drink"),
                   ("Okra Games", "a", "Devices and gear"), ("Pixel Lagoon", "f", "Devices and gear"), ("gridline fibre", "c", "Telecoms and fintech"),
                   ("Asante Air", "b", "Banks and insurance"), ("Jollof King", "a", "Food and drink"), ("Kumasi Kicks", "e", "Devices and gear"),
                   ("Baobab Audio", "f", "Devices and gear"), ("Savanna", "b", "Food and drink"), ("lumen displays", "c", "Devices and gear"),
                   ("Ridgeway", "a", "Banks and insurance"), ("Akwaaba Eats", "e", "Food and drink")]

# Designer Series pieces shown until real designers' approved designs replace them (no designer is credited):
# slug, name, type, tile colour, tag, price (GHS), production cost (GHS), stock, description
SAMPLE_DESIGNER_LINE = [
    ("signed-cap", "Signed Cap", "cap", "t", "Designer", 140, 40, 4, "Six-panel cap with the label's play-head stitched on the front."),
    ("accra-after-dark-tee", "Accra After Dark Tee", "tee", "k", "Designer", 190, 55, 30, "Night colours, the city and the stream, printed on heavyweight cotton."),
    ("gg-wo-ho-hoodie", "GG Wo Ho Hoodie", "hoodie", "t", "Limited · 50", 390, 145, 47, "A limited run of fifty heavyweight hoodies."),
]


# Sample clippers for the public Clip Table: handle, then their clips (title, platform, verified views)
SAMPLE_CLIPPERS = [
    ("@kwesi.cuts", [("He really missed that", "tiktok", 48300), ("Wig forfeit, live", "tiktok", 31200), ("Clutch 1v3", "youtube_shorts", 22400)]),
    ("@nanaframes", [("Chat chose violence", "instagram", 21900), ("Signed the cap live", "youtube_shorts", 18400), ("The anthem", "tiktok", 9800)]),
    ("@accra.afterdark", [("Galacta sings the anthem", "youtube_shorts", 21000), ("Late-night lobbies", "tiktok", 12600)]),
    ("@yaw_replays", [("Free phone??", "tiktok", 16400), ("Ranked rage", "tiktok", 8900)]),
    ("@zongoedits", [("Football night chaos", "instagram", 14200), ("The pass", "tiktok", 6100)]),
    ("@dansoman.clips", [("Viewer games, round 3", "tiktok", 11800)]),
    ("@tema.trims", [("He saw it coming", "youtube_shorts", 7400), ("Q&A moments", "instagram", 3900)]),
    ("@kasoa.kuts", [("New release first look", "tiktok", 6300)]),
]


def _cedis(n: float) -> int:
    return round(n * 100)


async def _galacta(db: AsyncSession) -> Streamer | None:
    return (await db.execute(select(Streamer).where(func.lower(Streamer.stage_name) == "galacta"))).scalars().first()


async def roster(db: AsyncSession) -> str:
    """Galacta on the public roster, with his honours, and the open seats."""
    made = []
    s = await _galacta(db)
    if not s:
        if await db.scalar(select(func.count()).select_from(Streamer).where(Streamer.signing_number == GALACTA["signing_number"])):
            return "signing number 1 is taken by another streamer; left alone"
        s = Streamer(**GALACTA, status="signed", is_public=True)  # no account yet: invite him from People
        db.add(s)
        await db.flush()
        made.append("Galacta")
    if not await db.scalar(select(func.count()).select_from(Honour).where(Honour.streamer_id == s.id)):
        for i, (title, detail) in enumerate(HONOURS, 1):
            db.add(Honour(streamer_id=s.id, position=i, title=title, detail=detail))
        made.append(f"{len(HONOURS)} honours")
    seats = {r.number: r for r in (await db.execute(select(RosterSeat))).scalars()}
    if 1 not in seats:
        db.add(RosterSeat(number=1, title=s.stage_name, description=s.content or "", streamer_id=s.id))
        made.append("seat 01")
    for number, title, description in OPEN_SEATS:
        if number not in seats:
            db.add(RosterSeat(number=number, title=title, description=description))
            made.append(f"seat {number:02d}")
    await db.flush()
    return ", ".join(made) or "already there"


async def gta6_first(db: AsyncSession) -> str:
    """Galacta's games, with GTA 6 first. Only replaces the old line, never something the team wrote."""
    s = await _galacta(db)
    if not s or s.content != OLD_CONTENT:
        return "nothing to change"
    s.content = GALACTA["content"]
    seat = (await db.execute(select(RosterSeat).where(RosterSeat.streamer_id == s.id))).scalars().first()
    if seat and seat.description == OLD_CONTENT:
        seat.description = GALACTA["content"]
    await db.flush()
    return "GTA 6 first"


OLD_ORG_BIO = "Africa's first content label for streamers. Accra, Ghana."


async def org_link_bio(db: AsyncSession) -> str:
    """The label's link page: the internet, not one city. Only replaces the old default, never a bio the team wrote."""
    page = (await db.execute(select(LinkPage).where(LinkPage.slug == "streamershub", LinkPage.streamer_id.is_(None)))).scalar_one_or_none()
    if not page or page.bio != OLD_ORG_BIO:
        return "nothing to change"
    page.bio = "The content label for the internet's next big streamers."
    await db.flush()
    return "new bio"


async def label_line(db: AsyncSession) -> str:
    """The label's own merch, live in the store. Stock starts at the planned first run: set the real counts in
    Store operations."""
    have = set((await db.execute(select(Product.slug))).scalars())
    position = (await db.scalar(select(func.max(Product.position)))) or 0
    added = 0
    for slug, name, ptype, tag, price, cost, stock, photo, description in LABEL_LINE:
        if slug in have:
            continue
        position += 1
        sizes = ["S", "M", "L", "XL", "XXL"] if ptype in ("tee", "hoodie", "jersey") else []
        db.add(Product(slug=slug, name=name, price_minor=_cedis(price), category="label", product_type=ptype, tone="", tag=tag,
                       sizes=sizes, status="live", commission_rate_bps=0, production_cost_minor=_cedis(cost), stock=stock,
                       reorder_at=5, position=position, images=[f"/merch/{photo}.jpg"], description=description))
        added += 1
    await db.flush()
    return f"{added} products" if added else "already there"


async def sample_partners(db: AsyncSession) -> str:
    """The placeholder Brand Partners grid. Skipped when the site already shows partners."""
    if await db.scalar(select(func.count()).select_from(Brand).where(Brand.is_public.is_(True))):
        return "partners already on the website"
    have = {n.lower() for n in (await db.execute(select(Brand.name))).scalars()}
    added = 0
    for i, (name, mark, sector) in enumerate(SAMPLE_PARTNERS):
        if name.lower() in have:
            continue
        db.add(Brand(name=name, sector=sector, wordmark=mark, is_public=True, public_order=i, status="listed", is_sample=True,
                     note="Sample partner: remove before launch"))
        added += 1
    await db.flush()
    return f"{added} sample partners"


async def sample_designer_line(db: AsyncSession) -> str:
    have = set((await db.execute(select(Product.slug))).scalars())
    position = (await db.scalar(select(func.max(Product.position)))) or 0
    added = 0
    for slug, name, ptype, tone, tag, price, cost, stock, description in SAMPLE_DESIGNER_LINE:
        if slug in have:
            continue
        position += 1
        sizes = ["S", "M", "L", "XL", "XXL"] if ptype in ("tee", "hoodie", "jersey") else []
        db.add(Product(slug=slug, name=name, price_minor=_cedis(price), category="designer", product_type=ptype, tone=tone, tag=tag,
                       sizes=sizes, status="live", commission_rate_bps=0, production_cost_minor=_cedis(cost), stock=stock, reorder_at=5,
                       position=position, images=[], description=description, is_sample=True))
        added += 1
    await db.flush()
    return f"{added} sample designer products" if added else "already there"


async def sample_clippers(db: AsyncSession) -> str:
    """Placeholder clippers so the Clip Table isn't empty. Skipped when there are clippers already."""
    import uuid as _uuid
    from datetime import timedelta

    from app.core.security import now
    from app.models.clips import Clip, ClipperProfile
    from app.models.people import User
    from app.services.CompanyService import today
    from app.services.money import month_key

    if await db.scalar(select(func.count()).select_from(User).where(User.role == "clipper")):
        return "clippers already there"
    period, at = month_key(today()), now()
    clips = 0
    for n, (handle, made) in enumerate(SAMPLE_CLIPPERS):
        key = handle[1:].replace(".", "_")
        # no password and an address that never receives mail: nobody can sign in as a sample
        u = User(email=f"{key}@sample.streamershub.test", name=handle, role="clipper", handle=handle, status="active",
                 password_hash=None, is_sample=True)
        db.add(u)
        await db.flush()
        db.add(ClipperProfile(user_id=u.id, handle=handle, platforms={}, status="active"))
        for i, (title, platform, views) in enumerate(made):
            when = at - timedelta(hours=6 + n * 5 + i * 19)
            db.add(Clip(clipper_user_id=u.id, url=f"https://sample.invalid/clips/{_uuid.uuid4().hex}", platform=platform, title=title,
                        posted_at=when, views_reported=views, views_verified=views, status="verified", verified_at=at, period=period))
            clips += 1
    await db.flush()
    return f"{len(SAMPLE_CLIPPERS)} sample clippers, {clips} clips"


STEPS = [("roster_v1", roster), ("galacta_gta6_v1", gta6_first), ("label_line_v1", label_line),
         ("sample_partners_v1", sample_partners), ("sample_designer_line_v1", sample_designer_line),
         ("sample_clippers_v1", sample_clippers), ("org_link_bio_v1", org_link_bio)]


async def run(db: AsyncSession) -> dict[str, str]:
    """Run the steps this database hasn't had yet. Returns what each new step did."""
    row = (await db.execute(select(AppSetting).where(AppSetting.key == "bootstrap").with_for_update())).scalar_one_or_none()
    done = list((row.value or {}).get("done", [])) if row else []
    out = {}
    for key, step in STEPS:
        if key in done:
            continue
        out[key] = await step(db)
        done.append(key)
    if out:
        if row:
            row.value = {**(row.value or {}), "done": done}
        else:
            db.add(AppSetting(key="bootstrap", value={"done": done}))
        await db.commit()
        logger.info("Website content on startup: %s", "; ".join(f"{k}: {v}" for k, v in out.items()))
    return out
