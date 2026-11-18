"""Monthly performance scores (7.6): weighted goals completed, on-time delivery and a lead's rating.
Every score carries the inputs behind it; a missing input drops out and the other weights scale up."""

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.people import Goal, LeadRating, PerformanceScore, Task, User
from app.services.CompanyService import day_start, month_bounds
from app.services.SettingsService import get_setting


async def work_out(db: AsyncSession, user: User, period: str) -> dict:
    start, end = month_bounds(period)
    weights_all = await get_setting(db, "performance")
    weights = weights_all.get(user.role) or weights_all["default"]
    goals = (await db.execute(select(Goal).where(Goal.owner_user_id == user.id, Goal.period_start < end,
                                                 Goal.period_end >= start))).scalars().all()
    goals_done = sum(1 for g in goals if g.status == "done" or (g.target and g.current >= g.target))
    tasks = (await db.execute(select(Task).where(Task.owner_user_id == user.id, Task.status == "done",
                                                 Task.closed_at >= day_start(start), Task.closed_at < day_start(end)))).scalars().all()
    timed = [t for t in tasks if t.on_time is not None]
    on_time = sum(1 for t in timed if t.on_time)
    rating = (await db.execute(select(LeadRating).where(LeadRating.user_id == user.id, LeadRating.period == period))).scalar_one_or_none()
    parts = {
        "goals": (round(goals_done * 100 / len(goals)) if goals else None, f"{goals_done} of {len(goals)} goals met"),
        "on_time": (round(on_time * 100 / len(timed)) if timed else None, f"{on_time} of {len(timed)} tasks on time"),
        "lead": (rating.rating if rating else None, "Lead's rating" + (f": {rating.note}" if rating and rating.note else "")),
    }
    used = {k: v for k, v in parts.items() if v[0] is not None and weights.get(k)}
    total_w = sum(weights[k] for k in used)
    score: Optional[int] = round(sum(v[0] * weights[k] for k, v in used.items()) / total_w) if total_w else None
    return {"score": score, "period": period, "inputs": [
        {"key": k, "value": v[0], "weight": weights.get(k, 0), "detail": v[1]} for k, v in parts.items()],
        "tasks_closed": len(tasks)}


async def save(db: AsyncSession, user: User, period: str, actor: Optional[User]) -> dict:
    result = await work_out(db, user, period)
    if result["score"] is None:
        return result
    row = (await db.execute(select(PerformanceScore).where(PerformanceScore.user_id == user.id,
                                                           PerformanceScore.period == period))).scalar_one_or_none()
    if not row:
        row = PerformanceScore(user_id=user.id, period=period, score=result["score"])
        db.add(row)
    row.score, row.inputs, row.set_by = result["score"], {"inputs": result["inputs"]}, actor.id if actor else None
    await db.flush()
    return result


def status_for(score: Optional[int]) -> tuple[str, str]:
    if score is None:
        return "No data yet", ""
    if score >= 80:
        return "On track", "ok"
    if score >= 60:
        return "Watch", "warn"
    return "Action", "no"


def week_number(d: date) -> str:
    return f"W{d.isocalendar()[1]}"
