"""The OS review queue: merch designs and the three application lists, filtered to what this person reviews."""

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import Access, require_area
from app.core.database import aget_db
from app.models.applications import Application
from app.models.store import Design
from app.api.v1.endpoints.applications import _row as app_row
from app.api.v1.endpoints.designs import design_out

router = APIRouter(prefix="/review", tags=["review"])
TABS = [("design", "Merch designs"), ("designer", "Designer applications"), ("clipper", "Clipper applications"),
        ("streamer", "Streamer applications")]


def design_stages(access: Access) -> list[str]:
    stages = []
    if access.can("creative"):
        stages.append("creative_review")
    if access.is_founder:
        stages += ["founder_approval", "streamer_signoff"]
    return stages or ["creative_review"]


@router.get("")
async def review_summary(request: Request, access: Access = Depends(require_area("review")), db: AsyncSession = Depends(aget_db)):
    if not access.review_lists:
        raise HTTPException(403, "You don't review any lists.")
    base = str(request.base_url).rstrip("/")
    tabs, lists = [], {}
    for key, label in TABS:
        if key not in access.review_lists:
            continue
        if key == "design":
            rows = (await db.execute(select(Design).where(Design.status.in_(design_stages(access)))
                                     .order_by(Design.created_at))).scalars().all()
            items = [await design_out(db, d, base) for d in rows]
        else:
            rows = (await db.execute(select(Application).where(Application.track == key,
                                                               Application.status.in_(("new", "reviewing")))
                                     .order_by(Application.created_at))).scalars().all()
            items = [app_row(a) for a in rows]
        tabs.append({"key": key, "label": label, "count": len(items)})
        lists[key] = items
    week_ago = func.now() - func.make_interval(0, 0, 0, 7)
    decided = await db.scalar(select(func.count()).select_from(Application).where(
        Application.status.in_(("approved", "declined")), Application.reviewed_at >= week_ago)) or 0
    decided += await db.scalar(select(func.count()).select_from(Design).where(
        Design.status.in_(("live", "rejected")), Design.decided_at >= week_ago)) or 0
    oldest = min([i["submitted_at"] if "submitted_at" in i else i["created_at"] for v in lists.values() for i in v], default=None)
    recent_apps = (await db.execute(select(Application).where(Application.status.in_(("approved", "declined")),
                                                              Application.track.in_(access.review_lists))
                                    .order_by(Application.reviewed_at.desc()).limit(5))).scalars().all()
    recent_designs = (await db.execute(select(Design).where(Design.status.in_(("live", "rejected")))
                                       .order_by(Design.decided_at.desc()).limit(5))).scalars().all() if "design" in access.review_lists else []
    recent = [{"title": a.handle if a.track == "clipper" else a.name, "by": f"{a.track.title()} application",
               "at": a.reviewed_at.isoformat() if a.reviewed_at else None, "result": "Approved" if a.status == "approved" else "Declined",
               "tone": "ok" if a.status == "approved" else "no", "note": a.decision_note} for a in recent_apps]
    for d in recent_designs:
        out = await design_out(db, d, base, private=False)
        recent.append({"title": d.title, "by": f"{out['designer']} · merch design", "at": d.decided_at.isoformat() if d.decided_at else None,
                       "result": "Approved" if d.status == "live" else "Declined", "tone": "ok" if d.status == "live" else "no",
                       "note": d.decline_reason})
    recent.sort(key=lambda r: r["at"] or "", reverse=True)
    month_start = func.date_trunc("month", func.now())
    decided_apps = (await db.execute(select(Application).where(Application.track.in_(access.review_lists),
                                                               Application.status.in_(("approved", "declined")),
                                                               Application.reviewed_at >= month_start))).scalars().all()
    decided_designs = (await db.execute(select(Design).where(Design.status.in_(("live", "rejected")), Design.decided_at >= month_start))).scalars().all() \
        if "design" in access.review_lists else []

    def breakdown(apps, designs, status_app, status_design):
        parts = []
        for track, word in (("designer", "designer"), ("clipper", "clipper"), ("streamer", "streamer")):
            n = sum(1 for a in apps if a.track == track and a.status == status_app)
            if n:
                parts.append(f"{n} {word}{'s' if n != 1 else ''}")
        n = sum(1 for d in designs if d.status == status_design)
        if n:
            parts.append(f"{n} design{'s' if n != 1 else ''}")
        return parts

    durations = [(a.reviewed_at - a.created_at).total_seconds() / 86400 for a in decided_apps if a.reviewed_at]
    durations += [(d.decided_at - d.created_at).total_seconds() / 86400 for d in decided_designs if d.decided_at]
    waiting = " · ".join(f"{t['count']} {t['label'].split(' ')[0].lower().replace('merch', 'designs').rstrip('s')}{'s' if t['count'] != 1 else ''}"
                         for t in tabs if t["count"])
    kpis = {"waiting": sum(t["count"] for t in tabs), "waiting_line": waiting,
            "avg_days": round(sum(durations) / len(durations), 1) if durations else None,
            "approved": sum(1 for a in decided_apps if a.status == "approved") + sum(1 for d in decided_designs if d.status == "live"),
            "approved_line": " · ".join(breakdown(decided_apps, decided_designs, "approved", "live")),
            "declined": sum(1 for a in decided_apps if a.status == "declined") + sum(1 for d in decided_designs if d.status == "rejected")}
    return {"tabs": tabs, "lists": lists, "open": sum(t["count"] for t in tabs), "decided_week": decided, "kpis": kpis,
            "oldest_at": oldest, "recent": recent[:6], "can_final_approve": access.is_founder,
            "can_creative_check": access.is_founder or access.can("creative")}
