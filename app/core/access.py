"""Who can open what. One source for the API's checks and for the sidebar the frontend draws.

A user has one role and at most one department; staff also have a desk (their landing screen).
Areas are the screens of Hub OS. The API refuses any area a user does not have, and the sidebar
is built from the same list, so the frontend never shows a link the API would refuse.

Stepping in: the Founder can work on every operational desk, and can give the same to anyone (every
desk, or chosen ones). A desk someone has only by stepping in is marked, and every change they make
there is recorded as a StepInAction so the desk's own team sees who did it. Nobody steps into a
streamer's own account: the Talent desk is the streamer's.
"""

import contextlib
import contextvars
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import aget_db, session_manager
from app.core.security import get_current_user
from app.models.people import Department, StaffProfile, User

OS_ROLES = {"founder", "cofounder", "dept_head", "finance", "staff", "streamer"}
PORTAL_ROLES = {"designer", "clipper", "customer"}
ALL_ROLES = OS_ROLES | PORTAL_ROLES | {"investor"}

DEPARTMENTS = [
    ("leadership", "Leadership"),
    ("strategy", "Strategy & Investors"),
    ("finance", "Finance"),
    ("marketing", "Marketing & Partnerships"),
    ("creative", "Creative"),
    ("production", "Content & Production"),
    ("tech", "Tech & Dev"),
    ("talent", "Talent"),
]

# area: (label, route, nav group)
AREAS = {
    "command": ("Command", "/os", "Executive"),
    "people": ("People and teams", "/os/people", "Executive"),
    "review": ("Review queue", "/os/review", "Executive"),
    "inbox": ("Messages", "/os/inbox", "Executive"),
    "capital": ("Investor relations", "/os/capital", "Executive"),
    "agreements": ("Agreements", "/os/agreements", "Executive"),
    "production": ("Production", "/os/production", "Departments"),
    "partnerships": ("Partnerships", "/os/partnerships", "Departments"),
    "social": ("Social", "/os/social", "Departments"),
    "creative": ("Creative", "/os/creative", "Departments"),
    "talent": ("Talent", "/os/talent", "Departments"),
    "tech": ("Tech and Dev", "/os/tech", "Departments"),
    "finance": ("Finance and payroll", "/os/finance", "Money"),
    "store": ("Store operations", "/os/store", "Money"),
    "posts": ("Live, events and links", "/os/posts", "Departments"),
    "me": ("My profile and pay", "/os/me", "You"),
    "settings": ("Settings", "/os/settings", "You"),
}
NAV_GROUPS = ["Executive", "Departments", "Money", "You"]

DEPARTMENT_AREAS = {
    "leadership": ["command"],
    "strategy": ["capital"],
    "finance": ["finance", "store"],
    "marketing": ["partnerships", "social", "posts"],
    "creative": ["creative"],
    "production": ["production", "posts"],
    "tech": ["tech"],
    "talent": ["talent"],
}

# Desks someone can step into. Not Command, Agreements or Settings (the Founder's own), not Talent (a
# streamer's own account) and not their personal pages.
OPERATIONAL_AREAS = ["people", "review", "production", "partnerships", "social", "posts", "creative", "tech", "finance",
                     "store", "capital"]
FOUNDER_OWN = ["command", "people", "review", "agreements", "settings", "me"]
# Messages from the Contact page go to the desk that answers them (ContactService.REASONS). Anyone with
# one of these desks has the Messages inbox, and sees only the messages for their desks.
INBOX_DESKS = {"command", "partnerships", "social", "capital", "people", "store", "posts", "production", "tech"}

PORTAL_HOME = {"designer": "/studio", "clipper": "/desk", "customer": "/store", "investor": "/vault/home"}


@dataclass
class Access:
    user: User
    department: Optional[str] = None  # slug
    desk: Optional[str] = None
    title: Optional[str] = None
    areas: list[str] = field(default_factory=list)
    review_lists: list[str] = field(default_factory=list)
    stepped_in: list[str] = field(default_factory=list)  # areas held only by stepping in
    acting_in: Optional[str] = None  # set on a request made through a stepped-in area

    @property
    def is_founder(self) -> bool:
        return self.user.role == "founder"

    @property
    def home(self) -> str:
        if self.user.role in PORTAL_HOME:
            return PORTAL_HOME[self.user.role]
        if self.desk and self.desk in self.areas:
            return AREAS[self.desk][1]
        return AREAS[self.areas[0]][1] if self.areas else "/os/me"

    def can(self, area: str) -> bool:
        return area in self.areas

    @property
    def does_finance(self) -> bool:
        """Finance work (payout runs, the ledger, purchases): Finance, the Founder, or someone stepping in."""
        return self.user.role in ("finance", "founder") or "finance" in self.stepped_in


def step_in_areas(user: User) -> list[str]:
    """The desks this person may step into, from their grant. The Founder always has every one."""
    if user.role == "founder":
        return list(OPERATIONAL_AREAS)
    if user.role not in OS_ROLES or user.role == "streamer" or user.status != "active":
        return []
    grant = list(user.step_in or [])
    return list(OPERATIONAL_AREAS) if "*" in grant else [a for a in OPERATIONAL_AREAS if a in grant]


def work_out(user: User, department: Optional[str], desk: Optional[str]) -> tuple[list[str], list[str], list[str]]:
    """(areas, review lists, the areas held only by stepping in)."""
    role = user.role
    areas: list[str] = []
    if role == "founder":
        # The Founder's own desks, plus Talent to look over a streamer's desk (never to act as them)
        areas = FOUNDER_OWN + DEPARTMENT_AREAS.get(department or "leadership", []) + ["talent", "capital"]
    elif role in OS_ROLES:
        if role == "cofounder":
            areas += ["command", "people"]
        if role == "dept_head":
            areas += ["people"]
        if role == "finance":
            areas += ["finance", "store", "capital"]
        if role in ("cofounder", "dept_head") and department:
            areas += DEPARTMENT_AREAS.get(department, [])
        if role == "streamer":
            areas += ["talent"]
        if desk and desk in AREAS and desk not in ("settings", "command"):
            areas.append(desk)
        if role != "streamer" and {"social", "production"} & set(areas):
            areas.append("posts")  # Social and Production post the streams and events they run
        areas.append("me")
    if role == "founder":
        own = set(areas)
    else:
        own = set(areas) | ({"review"} if role in OS_ROLES and _review_lists(role, set(areas)) else set())
    stepped = [a for a in step_in_areas(user) if a not in own]
    every = own | set(stepped)
    lists = ["design", "designer", "clipper", "streamer"] if role == "founder" else _review_lists(role, every)
    if lists:
        every.add("review")
    if role in OS_ROLES and role != "streamer" and INBOX_DESKS & every:
        every.add("inbox")
        own.add("inbox")  # a message answered for a stepped-in desk is recorded for that desk, not here
    ordered = [a for a in AREAS if a in every]
    return ordered, lists, [a for a in AREAS if a in every and a not in own]


def _review_lists(role: str, areas: set[str]) -> list[str]:
    if role not in OS_ROLES or role == "streamer":
        return []
    lists: list[str] = []
    if "creative" in areas:
        lists += ["design", "designer"]
    if "social" in areas:
        lists += ["clipper"]
    if "production" in areas:
        lists += ["streamer"]
    return lists


async def load_access(db: AsyncSession, user: User) -> Access:
    dept = (await db.get(Department, user.department_id)) if user.department_id else None
    profile = (await db.execute(select(StaffProfile).where(StaffProfile.user_id == user.id))).scalar_one_or_none()
    desk = profile.desk if profile else None
    if not desk and dept and user.role in OS_ROLES:
        desk = (DEPARTMENT_AREAS.get(dept.slug) or [None])[0]
    if user.role == "founder":
        desk = "command"
    areas, lists, stepped = work_out(user, dept.slug if dept else None, desk)
    return Access(user=user, department=dept.slug if dept else None, desk=desk,
                  title=profile.title if profile else None, areas=areas, review_lists=lists, stepped_in=stepped)


async def current_access(user: User = Depends(get_current_user), db: AsyncSession = Depends(aget_db)) -> Access:
    return await load_access(db, user)


# What a stepped-in request did, gathered from audit() calls while it runs
_acting: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("acting", default=None)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
logger = logging.getLogger(__name__)


def acting_note(summary: str) -> None:
    """Called by audit(): the summary joins the step-in record of this request, if it is one."""
    state = _acting.get()
    if state is not None and summary:
        state["notes"].append(summary)


def acting_area() -> Optional[str]:
    state = _acting.get()
    return state["area"] if state else None


def _humanise(name: str) -> str:
    words = re.sub(r"_+", " ", name).strip()
    return words[:1].upper() + words[1:] if words else "Made a change"


async def record_step_in(access: Access, area: str, request: Request, notes: list[str]) -> None:
    from app.models.people import StepInAction

    endpoint = request.scope.get("endpoint")
    action = "; ".join(dict.fromkeys(notes)) if notes else _humanise(getattr(endpoint, "__name__", "") or "change")
    title = access.title or ("Founder" if access.is_founder else None)
    who = access.user.name if not title or title.lower() in access.user.name.lower() else f"{access.user.name} ({title})"
    try:
        async with session_manager.session_factory() as db:
            db.add(StepInAction(area=area, actor_user_id=access.user.id, actor_label=who[:160], action=action[:200],
                                method=request.method, path=request.url.path[:300]))
            await db.commit()
    except Exception:  # the change itself already went through; never fail it over the note
        logger.exception("Could not record a step-in action")


def require_area(*areas: str):
    """Allow the request when the user has any of these areas. A change made through an area held only by
    stepping in is recorded for that desk."""

    async def dep(request: Request, access: Access = Depends(current_access)):
        if access.user.role not in OS_ROLES or not any(access.can(a) for a in areas):
            raise HTTPException(403, "You don't have access to this part of Hub OS.")
        own = [a for a in areas if access.can(a) and a not in access.stepped_in]
        if own or request.method in SAFE_METHODS:
            yield access
            return
        area = next(a for a in areas if a in access.stepped_in)
        access.acting_in = area
        state = {"area": area, "notes": []}
        token = _acting.set(state)
        try:
            yield access
        except Exception:
            raise
        else:
            await record_step_in(access, area, request, state["notes"])
        finally:
            _acting.reset(token)

    return dep


@contextlib.asynccontextmanager
async def acting_for(access: Access, area: str, request: Request):
    """A change to another desk's item made from a shared screen (the Messages inbox). If this person
    holds that desk only by stepping in, it is recorded for that desk, as require_area does."""
    if area not in access.stepped_in:
        yield
        return
    access.acting_in = area
    state = {"area": area, "notes": []}
    token = _acting.set(state)
    try:
        yield
    finally:
        _acting.reset(token)
    await record_step_in(access, area, request, state["notes"])


def require_role(*roles: str):
    async def dep(access: Access = Depends(current_access)) -> Access:
        if access.user.role not in roles:
            raise HTTPException(403, "You don't have access to this.")
        return access

    return dep


require_founder = require_role("founder")
