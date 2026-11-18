"""Clipper pay (7.3) and the Clip Table. Only verified views earn; flagged clips earn nothing."""

from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clips import Clip, ClipperEarning, ClipperProfile, LeaderboardSnapshot
from app.models.money import PayrollRun
from app.models.people import User
from app.services.SettingsService import get_setting


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_bounds(d: date) -> tuple[datetime, datetime]:
    """The Clip Table's week: the 7 days up to and including `d`, so it is never empty on a Monday."""
    end = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) + timedelta(days=1)
    return end - timedelta(days=7), end


def fixed_pay(views: int, rate_per_1000_minor: int, cap_minor: Optional[int]) -> int:
    amount = int((Decimal(views) * Decimal(rate_per_1000_minor) / Decimal(1000)).quantize(Decimal(1), ROUND_HALF_UP))
    return min(amount, cap_minor) if cap_minor else amount


def sample_clippers():
    """The sample accounts on the Clip Table. Their clips are shown but never paid or counted in the numbers."""
    return select(User.id).where(User.is_sample.is_(True))


async def verified_views_by_clipper(db: AsyncSession, period: str) -> dict:
    rows = (await db.execute(select(Clip.clipper_user_id, func.sum(Clip.views_verified))
                             .where(Clip.period == period, Clip.status == "verified", Clip.clipper_user_id.not_in(sample_clippers()))
                             .group_by(Clip.clipper_user_id))).all()
    return {uid: int(v or 0) for uid, v in rows}


async def earnings_for_period(db: AsyncSession, period: str) -> dict:
    """{clipper_user_id: (views, amount_minor)} under the current pay mode, budget and caps."""
    rules = await get_setting(db, "clipper_pay")
    views = await verified_views_by_clipper(db, period)
    out = {}
    if rules["mode"] == "pool":
        pool, total = int(rules.get("pool_minor") or 0), sum(views.values())
        for uid, v in views.items():
            out[uid] = (v, int(Decimal(pool) * v / total) if total else 0)
    else:
        for uid, v in views.items():
            out[uid] = (v, fixed_pay(v, int(rules["rate_per_1000_minor"]), rules.get("monthly_cap_minor")))
        budget = rules.get("monthly_budget_minor")
        total = sum(a for _, a in out.values())
        if budget and total > budget:  # scale everyone down to fit the month's budget
            out = {uid: (v, int(Decimal(a) * budget / total)) for uid, (v, a) in out.items()}
    return out


async def clipper_month(db: AsyncSession, user_id, period: str) -> dict:
    rules = await get_setting(db, "clipper_pay")
    earnings = await earnings_for_period(db, period)
    views, amount = earnings.get(user_id, (0, 0))
    checking = await db.scalar(select(func.coalesce(func.sum(Clip.views_reported), 0)).where(
        Clip.clipper_user_id == user_id, Clip.period == period, Clip.status.in_(("submitted", "checking")))) or 0
    potential = fixed_pay(int(checking), int(rules["rate_per_1000_minor"]), None) if rules["mode"] == "fixed" else None
    return {"verified_views": views, "earned_minor": amount, "checking_views": int(checking),
            "potential_minor": potential, "mode": rules["mode"], "rate_per_1000_minor": rules["rate_per_1000_minor"]}


async def leaderboard(db: AsyncSession, on: date, limit: int = 10, with_earnings: bool = False) -> list[dict]:
    """The weekly Clip Table: rank, handle, clips and verified views posted in the last 7 days, with movement
    against the ranks saved last Sunday. It never shows earnings unless `with_earnings` (Hub OS only).
    Sample clippers stay on the table every week until they're removed."""
    start, end = week_bounds(on)
    samples = select(User.id).where(User.is_sample.is_(True), User.status == "active")
    rows = (await db.execute(
        select(Clip.clipper_user_id, func.count(Clip.id), func.coalesce(func.sum(Clip.views_verified), 0))
        .where(Clip.status == "verified", ((Clip.posted_at >= start) & (Clip.posted_at < end)) | Clip.clipper_user_id.in_(samples))
        .group_by(Clip.clipper_user_id)
        .order_by(func.coalesce(func.sum(Clip.views_verified), 0).desc())
    )).all()
    last_week = iso_week(on - timedelta(days=7))
    prev = {r.clipper_user_id: r.rank for r in (await db.execute(
        select(LeaderboardSnapshot).where(LeaderboardSnapshot.week == last_week))).scalars()}
    period = f"{on.year:04d}-{on.month:02d}"
    earn = await earnings_for_period(db, period) if with_earnings else {}
    out = []
    for i, (uid, clips, views) in enumerate(rows[:limit], start=1):
        profile = (await db.execute(select(ClipperProfile).where(ClipperProfile.user_id == uid))).scalar_one_or_none()
        before = prev.get(uid)
        move = "New" if before is None else ("—" if before == i else (f"▲ {before - i}" if before > i else f"▼ {i - before}"))
        tone = "sub" if before is None or before == i else ("up" if before > i else "dn")
        item = {"rank": i, "user_id": str(uid), "handle": profile.handle if profile else "@clipper", "clips": clips,
                "views": int(views), "move": move, "move_tone": tone}
        if with_earnings:
            item["earned_minor"] = earn.get(uid, (0, 0))[1]
        out.append(item)
    return out


async def snapshot_week(db: AsyncSession, on: date) -> int:
    """Store this week's ranks (run on Sundays) so next week's table can show movement."""
    week = iso_week(on)
    board = await leaderboard(db, on, limit=100)
    existing = {r.clipper_user_id for r in (await db.execute(
        select(LeaderboardSnapshot).where(LeaderboardSnapshot.week == week))).scalars()}
    import uuid as _uuid

    for row in board:
        uid = _uuid.UUID(row["user_id"])
        if uid not in existing:
            db.add(LeaderboardSnapshot(week=week, clipper_user_id=uid, rank=row["rank"]))
    await db.flush()
    return len(board)


async def close_period(db: AsyncSession, period: str) -> list[ClipperEarning]:
    """Fix a month's clipper earnings (done when the payroll run is drafted)."""
    earnings = await earnings_for_period(db, period)
    rows = []
    for uid, (views, amount) in earnings.items():
        row = (await db.execute(select(ClipperEarning).where(ClipperEarning.clipper_user_id == uid,
                                                             ClipperEarning.period == period))).scalar_one_or_none()
        if row and row.status == "paid":
            rows.append(row)
            continue
        if not row:
            row = ClipperEarning(clipper_user_id=uid, period=period, verified_views=views, amount_minor=amount)
            db.add(row)
        else:
            row.verified_views, row.amount_minor = views, amount
        rows.append(row)
    await db.flush()
    return rows


async def add_strike(db: AsyncSession, user_id) -> ClipperProfile:
    profile = (await db.execute(select(ClipperProfile).where(ClipperProfile.user_id == user_id))).scalar_one()
    limit = (await get_setting(db, "clipper_pay"))["strikes_to_pause"]
    profile.strikes += 1
    if profile.strikes >= limit:
        profile.status = "paused"
    return profile


async def handle_of(db: AsyncSession, user_id) -> str:
    profile = (await db.execute(select(ClipperProfile).where(ClipperProfile.user_id == user_id))).scalar_one_or_none()
    if profile:
        return profile.handle
    user = await db.get(User, user_id)
    return user.handle or user.name if user else "@clipper"


# Month locks -----------------------------------------------------------------------------------------------

LOCKED_RUN = ("submitted", "approved", "paid")  # from submission on, a month's verified counts can't change


async def roll_pending(db: AsyncSession, period: str) -> int:
    """Clips still waiting for their first check when a month locks move to the next month, so they're still paid."""
    from app.services.money import next_month

    target = next_month(period)
    while await db.scalar(select(func.count()).select_from(PayrollRun).where(PayrollRun.period == target,
                                                                             PayrollRun.status.in_(LOCKED_RUN))):
        target = next_month(target)
    rows = (await db.execute(select(Clip).where(Clip.period == period, Clip.status.in_(("submitted", "checking"))))).scalars().all()
    for c in rows:
        c.period = target
    return len(rows)


# YouTube view counts --------------------------------------------------------------------------------------

async def refresh_youtube(db: AsyncSession, clips: list[Clip]) -> int:
    """Read YouTube's public count for these clips. The Social Media Manager still verifies; this is their guide."""
    from app.core.security import now
    from app.services import YouTubeService

    wanted = {}
    for c in clips:
        vid = YouTubeService.video_id(c.url) if c.platform == "youtube_shorts" else None
        if vid:
            wanted.setdefault(vid, []).append(c)
        elif c.platform == "youtube_shorts":
            c.fetch_note = "Couldn't find a video ID in this link."
    if not wanted or not YouTubeService.enabled():
        return 0
    try:
        stats = await YouTubeService.fetch(list(wanted))
    except YouTubeService.FetchFailed as exc:
        for group in wanted.values():
            for c in group:
                c.fetch_note = str(exc)
        return 0
    stamp, done = now(), 0
    for vid, group in wanted.items():
        for c in group:
            s = stats.get(vid)
            if s:
                c.views_fetched, c.views_fetched_at, c.platform_channel, c.fetch_note = s.views, stamp, (s.channel or "")[:120] or None, None
                done += 1
            else:
                c.fetch_note = "YouTube says this video is private, deleted or doesn't exist."
    return done


async def refresh_open_youtube_clips(db: AsyncSession) -> int:
    """Daily: keep counts fresh for every YouTube clip in a month whose payout run isn't approved yet."""
    closed = select(PayrollRun.period).where(PayrollRun.status.in_(("approved", "paid")))
    rows = (await db.execute(select(Clip).where(Clip.platform == "youtube_shorts", Clip.status.in_(("submitted", "checking", "verified")),
                                                Clip.period.not_in(closed)))).scalars().all()
    return await refresh_youtube(db, list(rows))
