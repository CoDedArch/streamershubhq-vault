"""Creating accounts: staff invited from People, designers and clippers approved from the review queue,
and streamers signed to a seat. Nobody gets portal access before approval."""

import secrets
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.clips import ClipperProfile
from app.models.people import Department, OnboardingRun, StaffProfile, User

REF_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def short_code(n: int = 4) -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(n))


def clean_handle(handle: Optional[str]) -> Optional[str]:
    if not handle:
        return None
    h = handle.strip().lower().replace(" ", "")
    h = h if h.startswith("@") else f"@{h}"
    if len(h) < 3 or len(h) > 40 or not all(c.isalnum() or c in "@._-" for c in h):
        raise HTTPException(400, "Handles use letters, numbers, dots, dashes and underscores.")
    return h


async def department_id(db: AsyncSession, slug: Optional[str]):
    if not slug:
        return None
    dept = (await db.execute(select(Department).where(Department.slug == slug))).scalar_one_or_none()
    if not dept:
        raise HTTPException(400, "Unknown department.")
    return dept.id


async def create_member(db: AsyncSession, *, name: str, email: str, role: str, handle: Optional[str] = None,
                        department: Optional[str] = None, title: Optional[str] = None, desk: Optional[str] = None,
                        pay_type: str = "none", onboarding: Optional[str] = None) -> User:
    email = email.strip().lower()
    if await db.scalar(select(func.count()).select_from(User).where(User.email == email)):
        raise HTTPException(409, "Someone already has an account with this email.")
    handle = clean_handle(handle)
    if handle and await db.scalar(select(func.count()).select_from(User).where(User.handle == handle)):
        raise HTTPException(409, f"The handle {handle} is taken.")
    user = User(email=email, name=name.strip(), role=role, handle=handle, status="invited",
                department_id=await department_id(db, department))
    db.add(user)
    await db.flush()
    db.add(StaffProfile(user_id=user.id, title=title or {"designer": "Community designer", "clipper": "Clipper",
                                                         "streamer": "Streamer"}.get(role, "Team member"),
                        desk=desk, pay_type=pay_type))
    if role == "clipper":
        db.add(ClipperProfile(user_id=user.id, handle=handle or f"@{email.split('@')[0]}"))
    if onboarding:
        db.add(OnboardingRun(user_id=user.id, kind=onboarding, current_step=1, completed_steps=[], data={}))
    await db.flush()
    return user
