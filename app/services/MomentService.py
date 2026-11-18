"""Marked moments: timestamps in a stream worth clipping.

The producer marks them live from the Production desk; streamers post their own from their desk after (or during) a
stream. Clippers see them all on their footage list, claim the ones they're cutting, and link the clip they post. The
Production, Social and Talent desks and the Founder see every moment, who claimed it, and what the clips did.
"""

import re
from datetime import timedelta
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import now
from app.models.clips import Clip
from app.models.people import User
from app.models.talent import MarkedMoment, MomentClaim, Stream, Streamer

KINDS = {"highlight": "Highlight", "clutch": "Clutch play", "funny": "Funny", "reaction": "Reaction", "chat": "Chat moment",
         "brand": "Brand moment", "other": "Other"}
WINDOW_DAYS = 21


def parse_at(value) -> int:
    """'1:23:45', '23:45', '83m', or plain seconds, into seconds from the start of the stream."""
    text = str(value).strip().lower()
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?", text)
    if m and any(m.groups()):
        h, mi, se = (int(x or 0) for x in m.groups())
        return h * 3600 + mi * 60 + se
    parts = text.split(":")
    if 2 <= len(parts) <= 3 and all(p.isdigit() for p in parts):
        nums = [int(p) for p in parts]
        if any(n >= 60 for n in nums[1:]):
            raise HTTPException(400, {"message": "Minutes and seconds go up to 59.", "missing": ["at"]})
        return nums[0] * 3600 + nums[1] * 60 + nums[2] if len(nums) == 3 else nums[0] * 60 + nums[1]
    raise HTTPException(400, {"message": "Write the time as 1:23:45 (hours:minutes:seconds) or 23:45.", "missing": ["at"]})


def fmt(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def stream_length(s: Stream) -> Optional[int]:
    if s.actual_start and s.ended_at:
        return int((s.ended_at - s.actual_start).total_seconds())
    if s.actual_start and s.status == "live":
        return int((now() - s.actual_start).total_seconds())
    return None


async def recent_streams(db: AsyncSession, streamer_id=None, days: int = WINDOW_DAYS) -> list[Stream]:
    q = select(Stream).where(Stream.scheduled_start >= now() - timedelta(days=days), Stream.status.in_(("live", "done")))
    if streamer_id:
        q = q.where(Stream.streamer_id == streamer_id)
    return list((await db.execute(q.order_by(Stream.scheduled_start.desc()).limit(30))).scalars())


async def board(db: AsyncSession, streams: list[Stream], viewer: Optional[User] = None, detail: bool = False,
                only_with_moments: bool = False) -> list[dict]:
    """Streams with their moments. `detail` adds who claimed each one and the clips posted from it (management)."""
    if not streams:
        return []
    ids = [s.id for s in streams]
    moments = list((await db.execute(select(MarkedMoment).where(MarkedMoment.stream_id.in_(ids))
                                     .order_by(MarkedMoment.at_seconds))).scalars())
    mids = [m.id for m in moments]
    claims = list((await db.execute(select(MomentClaim).where(MomentClaim.moment_id.in_(mids)))).scalars()) if mids else []
    clips = list((await db.execute(select(Clip).where(Clip.moment_id.in_(mids)))).scalars()) if mids else []
    people = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(
        {c.clipper_user_id for c in claims} | {c.clipper_user_id for c in clips} | {m.marked_by for m in moments if m.marked_by})))).scalars()}
    stars = {s.id: s for s in (await db.execute(select(Streamer).where(Streamer.id.in_({s.streamer_id for s in streams})))).scalars()}
    out = []
    for s in streams:
        star = stars.get(s.streamer_id)
        rows = []
        for m in (m for m in moments if m.stream_id == s.id):
            mine_claims = [c for c in claims if c.moment_id == m.id]
            mine_clips = [c for c in clips if c.moment_id == m.id]
            who = people.get(m.marked_by)
            row = {"id": str(m.id), "at": fmt(m.at_seconds), "at_seconds": m.at_seconds, "label": m.label, "note": m.note,
                   "kind": m.kind, "kind_label": KINDS.get(m.kind, "Highlight"), "source": m.source,
                   "by": (star.stage_name if m.source == "streamer" and star else (who.name if who else "Producer")),
                   "footage_url": m.footage_url, "claims": len(mine_claims), "clips": len(mine_clips),
                   "mine": bool(viewer and any(c.clipper_user_id == viewer.id for c in mine_claims)),
                   "can_delete": bool(viewer and m.marked_by == viewer.id and not mine_clips)}
            if detail:
                row["claimed_by"] = [(people[c.clipper_user_id].handle or people[c.clipper_user_id].name) for c in mine_claims
                                     if c.clipper_user_id in people]
                row["clip_list"] = [{"title": c.title, "url": c.url, "by": people[c.clipper_user_id].handle if c.clipper_user_id in people else "",
                                     "status": c.status, "views": c.views_verified if c.views_verified is not None else c.views_reported}
                                    for c in mine_clips]
            rows.append(row)
        if only_with_moments and not rows:
            continue
        length = stream_length(s)
        out.append({"stream_id": str(s.id), "title": s.title, "streamer": star.stage_name if star else "", "at": s.scheduled_start.isoformat(),
                    "status": s.status, "length": f"{length // 3600}h {length % 3600 // 60:02d}m" if length else None,
                    "length_seconds": length, "recording_url": s.recording_url, "moments": rows})
    return out


async def add(db: AsyncSession, stream: Stream, user: User, source: str, at, label: str, kind: str = "highlight",
              note: Optional[str] = None) -> MarkedMoment:
    seconds = parse_at(at)
    length = stream_length(stream)
    if length is not None and seconds > length + 60:
        raise HTTPException(400, {"message": f"That's past the end of the stream ({fmt(length)}).", "missing": ["at"]})
    if kind not in KINDS:
        kind = "other"
    dup = await db.scalar(select(func.count()).select_from(MarkedMoment).where(
        MarkedMoment.stream_id == stream.id, func.abs(MarkedMoment.at_seconds - seconds) <= 10))
    if dup:
        raise HTTPException(409, "There's already a moment marked within 10 seconds of that time.")
    m = MarkedMoment(stream_id=stream.id, at_seconds=seconds, label=label.strip(), kind=kind, note=(note or "").strip() or None,
                     source=source, marked_by=user.id)
    db.add(m)
    await db.flush()
    return m
