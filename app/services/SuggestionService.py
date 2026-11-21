"""The Suggestion Bank: anyone who works with the label sends an idea from their own portal; only the desks it
concerns see it; they answer within a week; anything worth doing is recommended to the Founder, and the sender
always sees what happened.

Who sees an idea: the desks of its topic (TOPICS), the Founder always. Private topics (pay and contracts, a
concern about a person) go to the Founder alone, and an investor's idea goes to Strategy & Investors. A desk can
move an idea to another department; only the Founder can move one out of "the Founder only".

Hidden names: with "hide my name" the team, the Founder included, sees the group ("A clipper") and never the
person. The link to the sender is kept only so they can follow their idea and get our answers.

What happens to an idea: new -> looking (a desk answered or picked it up) -> recommended (the desk proposed what
to do; it waits in the Founder's "Needs your decision") -> planned (approved: a task for the owner) -> done (the
task is done). A desk can close an idea as "Not now" with a reason (the Founder gets a weekly list and can reopen
any), or merge it into a similar one. The sender sees: Received, Being looked at, Planned, Done, Not now.
"""

import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import OS_ROLES, Access, load_access
from app.core.security import now
from app.models.investors import Investor
from app.models.money import Decision
from app.models.people import Department, Task, User
from app.models.suggestions import Suggestion, SuggestionReply
from app.services.CompanyService import day_start, today
from app.services.MemberService import short_code
from app.services.money import ghs

# topic: what the sender picks, and the Hub OS desks (areas) that see it. Private ones: the Founder alone.
TOPICS: dict[str, dict] = {
    "content": {"label": "Streams and content", "desks": ["production"]},
    "clips": {"label": "Clips and the Clip Table", "desks": ["production", "social"]},
    "merch": {"label": "Merch and the store", "desks": ["creative", "store"]},
    "marketing": {"label": "Marketing, social, partnerships and events", "desks": ["partnerships", "social"]},
    "tech": {"label": "The website and portals", "desks": ["tech"]},
    "talent": {"label": "Talent and new signings", "desks": ["command"]},
    "work": {"label": "How we work: tools, processes, culture", "desks": ["command"]},
    "pay": {"label": "Pay, contracts or agreements", "desks": [], "private": True},
    "concern": {"label": "A concern about a person", "desks": [], "private": True},
}
INVESTOR_DESKS = ["capital"]  # an investor's idea goes to Strategy & Investors, whatever the topic
# Where a desk can move an idea: (key, label, desks); "founder" makes it private
DEPARTMENTS = [("production", "Content & Production", ["production"]), ("marketing", "Marketing & Partnerships", ["partnerships", "social"]),
               ("creative", "Creative", ["creative"]), ("store", "Store operations", ["store"]), ("tech", "Tech & Dev", ["tech"]),
               ("leadership", "Leadership", ["command"]), ("strategy", "Strategy & Investors", ["capital"]), ("founder", "The Founder only", [])]
DESK_LABEL = {"production": "Content & Production", "social": "Social", "partnerships": "Partnerships", "creative": "Creative",
              "store": "Store operations", "tech": "Tech & Dev", "command": "Leadership", "capital": "Strategy & Investors"}
DESK_DEPARTMENT = {"production": "production", "social": "marketing", "partnerships": "marketing", "creative": "creative",
                   "store": "finance", "tech": "tech", "command": "leadership", "capital": "strategy"}
RECEIVING_AREAS = set(DESK_LABEL)

GROUP_LABEL = {"staff": "A staff member", "streamer": "A streamer", "clipper": "A clipper", "designer": "A designer", "investor": "An investor"}
TEAM_STATUS = {"new": "New", "looking": "Being looked at", "recommended": "Waiting for the Founder", "planned": "Planned",
               "done": "Done", "closed": "Not now", "merged": "Merged"}
SENDER_STATUS = {"new": "Received", "looking": "Being looked at", "recommended": "Being looked at", "planned": "Planned",
                 "done": "Done", "closed": "Not now", "merged": "Merged"}
EFFORTS = {"small": "Small", "medium": "Medium", "large": "Large"}
SENDER_ROLES = OS_ROLES | {"clipper", "designer"}

WEEKLY_LIMIT = 5
REPLY_DAYS = 7
REMIND_DAYS = 5
MAX_FILE_BYTES = 5 * 1024 * 1024
OPEN = ("new", "looking")  # the desk still has to answer or act


def topic_label(key: str) -> str:
    return TOPICS.get(key, {}).get("label", key)


def group_of(role: str) -> str:
    return role if role in ("streamer", "clipper", "designer") else "staff"


def desks_label(s: Suggestion) -> str:
    if s.private:
        return "The Founder only"
    names = list(dict.fromkeys(DESK_LABEL.get(d, d) for d in s.desks or []))
    return " and ".join(names) or "The Founder only"


# Who sees what ---------------------------------------------------------------------------------------------

def receives(access: Access) -> bool:
    """Does this person answer ideas at all (rather than only send them)?"""
    return access.is_founder or (access.user.role in OS_ROLES and access.user.role != "streamer"
                                 and bool(RECEIVING_AREAS & set(access.areas)))


def can_see(access: Access, s: Suggestion) -> bool:
    if access.is_founder:
        return True
    if s.private or access.user.role not in OS_ROLES or access.user.role == "streamer":
        return False
    return any(access.can(d) for d in s.desks or [])


def acting_desk(access: Access, s: Suggestion) -> Optional[str]:
    """The desk a change is recorded against when this person holds the idea's desks only by stepping in."""
    if access.is_founder or any(d in access.areas and d not in access.stepped_in for d in s.desks or []):
        return None
    return next((d for d in s.desks or [] if d in access.stepped_in), None)


def is_late(s: Suggestion, at: Optional[datetime] = None) -> bool:
    return s.status == "new" and not s.first_response_at and (at or now()) - s.created_at > timedelta(days=REPLY_DAYS)


# The sender --------------------------------------------------------------------------------------------------

async def authors(db: AsyncSession, rows: list[Suggestion]) -> tuple[dict, dict]:
    uids = {s.author_user_id for s in rows if s.author_user_id}
    iids = {s.author_investor_id for s in rows if s.author_investor_id}
    users = {u.id: u for u in (await db.execute(select(User).where(User.id.in_(uids)))).scalars()} if uids else {}
    invs = {i.id: i for i in (await db.execute(select(Investor).where(Investor.id.in_(iids)))).scalars()} if iids else {}
    return users, invs


def sender_label(s: Suggestion, users: dict, invs: dict) -> str:
    """What the team sees as the sender. A hidden name shows the group only, to everyone."""
    if s.hide_name:
        return GROUP_LABEL[s.sender_group]
    if s.author_investor_id:
        inv = invs.get(s.author_investor_id)
        return f"{inv.legal_name} · investor" if inv else GROUP_LABEL["investor"]
    u = users.get(s.author_user_id)
    if not u:
        return GROUP_LABEL[s.sender_group]
    if s.sender_group == "clipper" and u.handle:
        return f"{u.handle} · clipper"
    return f"{u.name} · {s.sender_group}" if s.sender_group in ("streamer", "designer") else u.name


def credit(s: Suggestion, users: dict) -> str:
    """The name on "You said, we did", which every sender sees. Investors are never named there."""
    if s.hide_name or s.author_investor_id:
        return GROUP_LABEL[s.sender_group]
    u = users.get(s.author_user_id)
    if not u:
        return GROUP_LABEL[s.sender_group]
    return u.handle if s.sender_group == "clipper" and u.handle else u.name


def sender_path(s: Suggestion, role: Optional[str]) -> str:
    if s.author_investor_id:
        return "/vault/home#ideas"
    return {"clipper": "/desk#ideas", "designer": "/studio#ideas"}.get(role or "", f"/os/suggestions?view=mine&id={s.id}")


async def sent_this_week(db: AsyncSession, *, user_id=None, investor_id=None) -> int:
    since = now() - timedelta(days=7)
    who = Suggestion.author_user_id == user_id if user_id else Suggestion.author_investor_id == investor_id
    return await db.scalar(select(func.count()).select_from(Suggestion).where(who, Suggestion.created_at >= since)) or 0


async def new_reference(db: AsyncSession) -> str:
    for _ in range(6):
        ref = f"SH-IDEA-{short_code(5)}"
        if not await db.scalar(select(func.count()).select_from(Suggestion).where(Suggestion.reference == ref)):
            return ref
    raise HTTPException(503, "Try again in a moment.")


def clean(raw: dict) -> dict:
    """The form's answers, checked. Raises 400 with the fields to fix."""
    from app.services import PostService

    topic = str(raw.get("topic") or "")
    title = " ".join(str(raw.get("title") or "").split())
    body = str(raw.get("body") or "").strip()
    benefit = str(raw.get("benefit") or "").strip()
    link = str(raw.get("link") or "").strip()
    missing, problem = [], None
    if topic not in TOPICS:
        missing.append("topic")
    if not 3 <= len(title) <= 100:
        missing.append("title")
        problem = problem or ("Give it a short title (up to 100 characters)." if title else None)
    if not 10 <= len(body) <= 1500:
        missing.append("body")
        problem = problem or ("Say a little more about the idea." if len(body) < 10 and body else
                              "Keep the idea to about 250 words (1,500 characters)." if body else None)
    if len(benefit) > 600:
        missing.append("benefit")
        problem = problem or "Keep what it fixes to 600 characters."
    if link:
        try:
            link = PostService.ok_url(link, public_page=False)
        except HTTPException:
            missing.append("link")
            problem = problem or "Paste a full link, starting with https://"
    if missing:
        raise HTTPException(400, {"message": problem or ("Fill in the highlighted fields." if len(missing) > 1 else "Fill in the highlighted field."),
                                  "missing": missing})
    return {"topic": topic, "title": title, "body": body, "benefit": benefit or None, "link": link or None,
            "hide_name": raw.get("hide_name") in (True, "true", "on", "1")}


async def create(db: AsyncSession, answers: dict, files: list[dict], *, user: Optional[User] = None,
                 investor: Optional[Investor] = None) -> tuple[Suggestion, list[dict]]:
    """A new idea from a team member, streamer, clipper or designer (user) or an investor. Returns it and the
    emails to send once it's saved."""
    sent = await sent_this_week(db, user_id=user.id if user else None, investor_id=investor.id if investor else None)
    if sent >= WEEKLY_LIMIT:
        raise HTTPException(429, f"You've sent {WEEKLY_LIMIT} ideas this week. Send more next week, or add to one you've sent.")
    topic = TOPICS[answers["topic"]]
    private = bool(topic.get("private"))
    desks = [] if private else (INVESTOR_DESKS if investor else list(topic["desks"]))
    s = Suggestion(reference=await new_reference(db), topic=answers["topic"], desks=desks, private=private, title=answers["title"],
                   body=answers["body"], benefit=answers["benefit"], link=answers["link"], files=files,
                   sender_group="investor" if investor else group_of(user.role), author_user_id=user.id if user else None,
                   author_investor_id=investor.id if investor else None, hide_name=answers["hide_name"], status="new")
    db.add(s)
    await db.flush()
    users, invs = ({user.id: user} if user else {}), ({investor.id: investor} if investor else {})
    mail = [{"kind": "received", "to": user.email if user else investor.email, "name": user.name if user else investor.legal_name,
             "reference": s.reference, "title": s.title, "path": sender_path(s, user.role if user else None)}]
    mail += await _new_for_team(db, s, sender_label(s, users, invs), exclude=user.id if user else None)
    return s, mail


async def _new_for_team(db: AsyncSession, s: Suggestion, sender: str, exclude=None) -> list[dict]:
    to = await team_emails(db, s, exclude=exclude)
    return [{"kind": "team", "to": to, "subject": f"New idea · {s.title} [{s.reference}]", "title": "A new idea for your team",
             "lines": [f"{sender} suggested: {s.title}", f"Topic: {topic_label(s.topic)} · for {desks_label(s)}",
                       f"Answer within {REPLY_DAYS} days: reply to them, close it with a reason, or recommend what to do."],
             "cta": ("Open in Hub OS", f"/os/suggestions?id={s.id}")}] if to else []


# The team ----------------------------------------------------------------------------------------------------

async def team_emails(db: AsyncSession, s: Suggestion, exclude=None) -> list[str]:
    """Everyone whose own desks see this idea (not stepped-in desks: the Founder isn't emailed every idea).
    Private ideas: the Founder."""
    rows = (await db.execute(select(User).where(User.role.in_(OS_ROLES - {"streamer"}), User.status == "active"))).scalars().all()
    out = []
    for u in rows:
        if u.id == exclude:
            continue
        if s.private:
            if u.role == "founder":
                out.append(u.email)
            continue
        access = await load_access(db, u)
        if any(d in access.areas and d not in access.stepped_in for d in s.desks or []):
            out.append(u.email)
    return out


async def lead_emails(db: AsyncSession, s: Suggestion) -> list[str]:
    """The heads of the idea's departments (the Founder for private ideas, or when a department has no lead)."""
    slugs = {DESK_DEPARTMENT[d] for d in s.desks or [] if d in DESK_DEPARTMENT}
    ids = set()
    if slugs and not s.private:
        ids = {d.lead_user_id for d in (await db.execute(select(Department).where(Department.slug.in_(slugs)))).scalars() if d.lead_user_id}
    q = select(User.email).where(User.status == "active")
    q = q.where(User.id.in_(ids)) if ids else q.where(User.role == "founder")
    return list((await db.execute(q)).scalars())


def _who(access: Access) -> str:
    title = access.title or ("Founder" if access.is_founder else None)
    name = access.user.name
    return name if not title or title.lower() in name.lower() else f"{name}, {title}"


def _event(db: AsyncSession, s: Suggestion, text: str, access: Optional[Access] = None) -> None:
    db.add(SuggestionReply(suggestion_id=s.id, direction="event", body=text,
                           author_user_id=access.user.id if access else None, author_label=_who(access)[:160] if access else "Hub OS"))


def _answered(s: Suggestion) -> None:
    s.first_response_at = s.first_response_at or now()


async def followers(db: AsyncSession, s: Suggestion) -> list[Suggestion]:
    """The idea and the ones merged into it: everyone who suggested it hears what happens."""
    merged = (await db.execute(select(Suggestion).where(Suggestion.merged_into_id == s.id))).scalars().all()
    return [s, *merged]


async def tell_senders(db: AsyncSession, s: Suggestion, heading: str, text: str) -> list[dict]:
    rows = await followers(db, s)
    users, invs = await authors(db, rows)
    out = []
    for r in rows:
        u, inv = users.get(r.author_user_id), invs.get(r.author_investor_id)
        if not (u or inv) or (u and u.status != "active"):
            continue
        out.append({"kind": "update", "to": u.email if u else inv.email, "name": u.name if u else inv.legal_name,
                    "reference": r.reference, "title": r.title if r.id == s.id else f"{r.title} (joined {s.reference})",
                    "heading": heading, "text": text, "path": sender_path(r, u.role if u else None)})
    return out


def _require(s: Suggestion, *statuses: str, what: str = "do that") -> None:
    if s.status not in statuses:
        raise HTTPException(409, f"This idea is {TEAM_STATUS.get(s.status, s.status).lower()}, so you can't {what} now.")


async def reply(db: AsyncSession, s: Suggestion, access: Access, body: str, to_sender: bool) -> list[dict]:
    text = body.strip()
    if not 1 <= len(text) <= 4000:
        raise HTTPException(400, {"message": "Write your message (up to 4,000 characters).", "missing": ["body"]})
    if not to_sender:
        db.add(SuggestionReply(suggestion_id=s.id, direction="note", body=text, author_user_id=access.user.id, author_label=_who(access)[:160]))
        return []
    _require(s, "new", "looking", "recommended", "planned", "done", "closed", what="answer it")
    db.add(SuggestionReply(suggestion_id=s.id, direction="out", body=text, author_user_id=access.user.id, author_label=_who(access)[:160]))
    if s.status == "new":
        s.status = "looking"
    _answered(s)
    users, invs = await authors(db, [s])
    u, inv = users.get(s.author_user_id), invs.get(s.author_investor_id)
    if not (u or inv):
        return []
    return [{"kind": "update", "to": u.email if u else inv.email, "name": u.name if u else inv.legal_name, "reference": s.reference,
             "title": s.title, "heading": "We answered your idea", "text": text, "signed": _who(access),
             "path": sender_path(s, u.role if u else None)}]


def looking(s: Suggestion, access: Access) -> None:
    _require(s, "new", what="mark it")
    s.status = "looking"
    _answered(s)


async def move(db: AsyncSession, s: Suggestion, access: Access, department: str) -> list[dict]:
    target = next((d for d in DEPARTMENTS if d[0] == department), None)
    if not target:
        raise HTTPException(400, "Pick a department.")
    _require(s, "new", "looking", what="move it")
    key, label, desks = target
    if s.private and not access.is_founder:
        raise HTTPException(403, "Only the Founder can move a private idea.")
    before = desks_label(s)
    s.private = key == "founder"
    s.desks = list(desks)
    s.reminded_at = None  # the new desk gets its own reminder
    if desks_label(s) == before:
        raise HTTPException(409, f"It's already with {before}.")
    _event(db, s, f"Moved from {before} to {label}", access)
    users, invs = await authors(db, [s])
    return await _new_for_team(db, s, sender_label(s, users, invs), exclude=access.user.id)


async def merge(db: AsyncSession, s: Suggestion, into: Suggestion, access: Access) -> list[dict]:
    if into.id == s.id:
        raise HTTPException(400, "Pick a different idea to merge it into.")
    _require(s, "new", "looking", what="merge it")
    if into.status in ("merged", "closed"):
        raise HTTPException(409, f"{into.reference} is {TEAM_STATUS[into.status].lower()}. Merge into an open idea instead.")
    children = (await db.execute(select(Suggestion).where(Suggestion.merged_into_id == s.id))).scalars().all()
    for c in children:
        c.merged_into_id = into.id
    s.status, s.merged_into_id = "merged", into.id
    _answered(s)
    _event(db, s, f"Merged into {into.reference}: {into.title}", access)
    _event(db, into, f"{s.reference} merged into this: {s.title}", access)
    return await tell_senders(db, s, "Your idea joined a similar one",
                              f"Someone suggested something very close, so we put the two together: “{into.title}” ({into.reference}). "
                              "You'll hear from us when it moves.")


async def close(db: AsyncSession, s: Suggestion, access: Access, reason: str) -> list[dict]:
    reason = reason.strip()
    if not 5 <= len(reason) <= 600:
        raise HTTPException(400, {"message": "Say why, in a sentence or two. They see this.", "missing": ["reason"]})
    _require(s, "new", "looking", what="close it")
    s.status, s.closed_reason, s.closed_by, s.closed_at = "closed", reason, access.user.id, now()
    _answered(s)
    _event(db, s, f"Closed as Not now: {reason}", access)
    return await tell_senders(db, s, "Not now, and why", reason)


async def reopen(db: AsyncSession, s: Suggestion, access: Access) -> list[dict]:
    if not access.is_founder:
        raise HTTPException(403, "Only the Founder reopens ideas.")
    _require(s, "closed", what="reopen it")
    s.status, s.closed_reason, s.closed_by, s.closed_at = "looking", None, None, None
    _event(db, s, "Reopened by the Founder", access)
    return await tell_senders(db, s, "We're taking another look", "Your idea is open again. We'll let you know what we decide.")


async def _owner(db: AsyncSession, owner_id) -> User:
    try:
        u = await db.get(User, uuid.UUID(str(owner_id)))
    except (ValueError, TypeError):
        u = None
    if not u or u.role not in OS_ROLES or u.role == "streamer" or u.status != "active":
        raise HTTPException(400, {"message": "Pick who owns it from the team.", "missing": ["owner_user_id"]})
    return u


async def recommend(db: AsyncSession, s: Suggestion, access: Access, *, action: str, owner_user_id: Any, due_on: date,
                    cost_minor: int, effort: str, benefit: Optional[str]) -> list[dict]:
    """The desk proposes what to do. It waits in the Founder's "Needs your decision"; the Founder's own
    recommendation is approved straight away."""
    _require(s, "new", "looking", what="recommend it")
    action = action.strip()
    if not 5 <= len(action) <= 1000:
        raise HTTPException(400, {"message": "Say what to do, in a sentence or two.", "missing": ["action"]})
    if effort not in EFFORTS:
        raise HTTPException(400, {"message": "Pick the effort.", "missing": ["effort"]})
    if due_on < today():
        raise HTTPException(400, {"message": "Pick a due date from today on.", "missing": ["due_on"]})
    owner = await _owner(db, owner_user_id)
    s.recommendation = {"action": action, "owner_user_id": str(owner.id), "owner": owner.name, "due_on": due_on.isoformat(),
                        "cost_minor": int(cost_minor), "effort": effort, "benefit": (benefit or "").strip() or None,
                        "by": _who(access), "by_user_id": str(access.user.id), "at": now().isoformat()}
    _answered(s)
    if access.is_founder:
        s.status = "recommended"
        return await approve(db, s, access.user)
    s.status = "recommended"
    d = Decision(title=f"Idea {s.reference}: {s.title}"[:200], source=f"Suggestion Bank · {desks_label(s)} · {_who(access)}"[:200],
                 kind="suggestion", entity_id=str(s.id), amount_minor=int(cost_minor) or None, requested_by=access.user.id,
                 note=action[:1000])
    db.add(d)
    await db.flush()
    s.decision_id = d.id
    _event(db, s, f"Recommended to the Founder: {action} (owner {owner.name}, due {due_on.strftime('%d %b %Y')}"
                  f"{', ' + ghs(int(cost_minor)) if cost_minor else ''}, {EFFORTS[effort].lower()} effort)", access)
    founders = list((await db.execute(select(User.email).where(User.role == "founder", User.status == "active"))).scalars())
    return [{"kind": "team", "to": founders, "subject": f"An idea needs your decision · {s.title} [{s.reference}]",
             "title": "An idea needs your decision",
             "lines": [f"{desks_label(s)} recommends: {action}", f"Owner {owner.name} · due {due_on.strftime('%d %b %Y')}"
                       f"{' · ' + ghs(int(cost_minor)) if cost_minor else ''} · {EFFORTS[effort].lower()} effort"],
             "cta": ("Decide in Hub OS", f"/os/suggestions?id={s.id}")}] if founders else []


async def _close_decision(db: AsyncSession, s: Suggestion, actor: User, status: str, note: Optional[str]) -> None:
    d = await db.get(Decision, s.decision_id) if s.decision_id else None
    if d and d.status == "open":
        d.status, d.decided_by, d.decided_at, d.note = status, actor.id, now(), (note or d.note)


async def approve(db: AsyncSession, s: Suggestion, actor: User, *, owner_user_id: Any = None, due_on: Optional[date] = None,
                  cost_minor: Optional[int] = None) -> list[dict]:
    """The Founder approves (optionally with another owner, date or cost): the owner gets a task."""
    _require(s, "recommended", what="approve it")
    rec = dict(s.recommendation or {})
    owner = await _owner(db, owner_user_id or rec.get("owner_user_id"))
    due = due_on or (date.fromisoformat(rec["due_on"]) if rec.get("due_on") else None)
    if due and due < today():
        raise HTTPException(400, {"message": "Pick a due date from today on.", "missing": ["due_on"]})
    rec.update(owner_user_id=str(owner.id), owner=owner.name, due_on=due.isoformat() if due else None,
               cost_minor=int(cost_minor) if cost_minor is not None else rec.get("cost_minor", 0))
    t = Task(title=f"Idea {s.reference}: {rec.get('action') or s.title}"[:200], owner_user_id=owner.id,
             department_id=owner.department_id, due_on=due)
    db.add(t)
    await db.flush()
    s.recommendation, s.task_id, s.status, s.decided_by, s.decided_at = rec, t.id, "planned", actor.id, now()
    await _close_decision(db, s, actor, "approved", None)
    access = await load_access(db, actor)
    _event(db, s, f"Approved by the Founder · task for {owner.name}{', due ' + due.strftime('%d %b %Y') if due else ''}", access)
    mail = [{"kind": "team", "to": [owner.email], "subject": f"A task from an idea · {s.title} [{s.reference}]",
             "title": "You own an idea the Founder approved",
             "lines": [f"What to do: {rec.get('action')}", f"Due {due.strftime('%d %b %Y')}" if due else "No due date",
                       "Mark the task done in Hub OS when it's live, and the person who suggested it is told."],
             "cta": ("Open the idea", f"/os/suggestions?id={s.id}")}]
    return mail + await tell_senders(db, s, "We're doing it", f"Your idea is planned: {rec.get('action')}")


async def decline(db: AsyncSession, s: Suggestion, actor: User, reason: str) -> list[dict]:
    reason = (reason or "").strip()
    if not 5 <= len(reason) <= 600:
        raise HTTPException(400, {"message": "Say why, in a sentence or two. They see this.", "missing": ["note"]})
    _require(s, "recommended", what="decline it")
    s.status, s.closed_reason, s.closed_by, s.closed_at, s.decided_by, s.decided_at = "closed", reason, actor.id, now(), actor.id, now()
    await _close_decision(db, s, actor, "declined", reason)
    _event(db, s, f"Declined by the Founder: {reason}", await load_access(db, actor))
    return await tell_senders(db, s, "Not now, and why", reason)


async def send_back(db: AsyncSession, s: Suggestion, actor: User, question: str) -> list[dict]:
    question = (question or "").strip()
    if not 5 <= len(question) <= 1000:
        raise HTTPException(400, {"message": "Write your question for the team.", "missing": ["note"]})
    _require(s, "recommended", what="send it back")
    s.status = "looking"
    await _close_decision(db, s, actor, "returned", f"Sent back: {question}")
    access = await load_access(db, actor)
    db.add(SuggestionReply(suggestion_id=s.id, direction="note", body=f"Sent back by the Founder: {question}",
                           author_user_id=actor.id, author_label=_who(access)[:160]))
    to = await team_emails(db, s, exclude=actor.id)
    return [{"kind": "team", "to": to, "subject": f"The Founder sent an idea back · {s.title} [{s.reference}]",
             "title": "An idea came back with a question", "lines": [question, "Answer it in the idea, then recommend again."],
             "cta": ("Open the idea", f"/os/suggestions?id={s.id}")}] if to else []


async def task_changed(db: AsyncSession, t: Task) -> list[dict]:
    """A task from an approved idea is done (the idea is done and its senders hear it), or reopened."""
    s = (await db.execute(select(Suggestion).where(Suggestion.task_id == t.id))).scalar_one_or_none()
    if not s:
        return []
    if t.status == "done" and s.status == "planned":
        s.status, s.done_at = "done", now()
        _event(db, s, "Done: the task is finished")
        return await tell_senders(db, s, "Your idea is live", f"“{s.title}” is done. Thank you for suggesting it.")
    if t.status != "done" and s.status == "done":
        s.status, s.done_at = "planned", None
        _event(db, s, "The task was reopened, so the idea is planned again")
    return []


async def sender_reply(db: AsyncSession, s: Suggestion, body: str, label: str) -> list[dict]:
    text = body.strip()
    if not 1 <= len(text) <= 4000:
        raise HTTPException(400, {"message": "Write your message (up to 4,000 characters).", "missing": ["body"]})
    if s.status == "merged":
        raise HTTPException(409, "This idea joined another one. We'll keep you posted on that one.")
    db.add(SuggestionReply(suggestion_id=s.id, direction="in", body=text))
    to = await team_emails(db, s)
    return [{"kind": "team", "to": to, "subject": f"Reply on an idea · {s.title} [{s.reference}]", "title": f"{label} replied",
             "lines": [text[:600]], "cta": ("Open in Hub OS", f"/os/suggestions?id={s.id}")}] if to else []


# What the screens show ------------------------------------------------------------------------------------------

async def merged_counts(db: AsyncSession, ids: list) -> dict:
    if not ids:
        return {}
    rows = await db.execute(select(Suggestion.merged_into_id, func.count()).where(Suggestion.merged_into_id.in_(ids))
                            .group_by(Suggestion.merged_into_id))
    return dict(rows.all())


def team_row(s: Suggestion, users: dict, invs: dict, merged: dict) -> dict:
    return {"id": str(s.id), "reference": s.reference, "title": s.title, "topic": s.topic, "topic_label": topic_label(s.topic),
            "status": s.status, "status_label": TEAM_STATUS[s.status], "from": sender_label(s, users, invs), "group": s.sender_group,
            "private": s.private, "for": desks_label(s), "created_at": s.created_at.isoformat(), "late": is_late(s),
            "suggested_by": 1 + merged.get(s.id, 0), "preview": s.body[:160]}


async def team_detail(db: AsyncSession, s: Suggestion, access: Access, base: str) -> dict:
    from app.services import S3Service

    users, invs = await authors(db, [s])
    merged = await merged_counts(db, [s.id])
    out = team_row(s, users, invs, merged)
    thread = (await db.execute(select(SuggestionReply).where(SuggestionReply.suggestion_id == s.id)
                               .order_by(SuggestionReply.created_at))).scalars().all()
    into = await db.get(Suggestion, s.merged_into_id) if s.merged_into_id else None
    joined = (await db.execute(select(Suggestion).where(Suggestion.merged_into_id == s.id).order_by(Suggestion.created_at))).scalars().all()
    task = await db.get(Task, s.task_id) if s.task_id else None
    j_users, j_invs = await authors(db, list(joined))
    acting = receives(access) and can_see(access, s)
    team = []
    if acting and s.status in ("new", "looking", "recommended"):
        rows = (await db.execute(select(User).where(User.role.in_(OS_ROLES - {"streamer"}), User.status == "active").order_by(User.name))).scalars()
        team = [{"id": str(u.id), "name": u.name} for u in rows]
    targets = []
    if acting and s.status in OPEN:
        cand = (await db.execute(select(Suggestion).where(Suggestion.id != s.id, Suggestion.status.in_(("new", "looking", "recommended", "planned", "done")))
                                 .order_by(Suggestion.created_at.desc()).limit(200))).scalars().all()
        targets = [{"id": str(c.id), "reference": c.reference, "title": c.title} for c in cand if can_see(access, c)][:60]
    out.update({
        "body": s.body, "benefit": s.benefit, "link": s.link, "hide_name": s.hide_name,
        "files": [{"name": f["name"], "url": S3Service.signed_url(f["key"], 600, base), "size": f.get("size")} for f in s.files or []],
        "thread": [{"id": str(r.id), "direction": r.direction, "body": r.body, "at": r.created_at.isoformat(),
                    "by": out["from"] if r.direction == "in" else (r.author_label or "The team")} for r in thread],
        "first_response_at": s.first_response_at.isoformat() if s.first_response_at else None,
        "closed_reason": s.closed_reason, "recommendation": s.recommendation,
        "merged_into": {"id": str(into.id), "reference": into.reference, "title": into.title} if into else None,
        "joined": [{"reference": j.reference, "title": j.title, "from": sender_label(j, j_users, j_invs)} for j in joined],
        "task": {"title": task.title, "status": task.status, "due_on": task.due_on.isoformat() if task.due_on else None,
                 "on_time": task.on_time} if task else None,
        "can_act": acting, "can_decide": access.is_founder and s.status == "recommended", "can_reopen": access.is_founder and s.status == "closed",
        "stepping_in": acting_desk(access, s) is not None,
        "departments": [{"key": k, "label": label} for k, label, _ in DEPARTMENTS],
        "team": team, "merge_targets": targets, "efforts": [{"key": k, "label": v} for k, v in EFFORTS.items()],
        "reply_days": REPLY_DAYS,
    })
    return out


VIEWS = {"todo": ("new", "looking"), "waiting": ("recommended",), "planned": ("planned",), "done": ("done",), "closed": ("closed", "merged")}


async def team_list(db: AsyncSession, access: Access, view: str) -> dict:
    rows = [s for s in (await db.execute(select(Suggestion).order_by(Suggestion.created_at.desc()).limit(1000))).scalars().all()
            if can_see(access, s)]
    week_ago = now() - timedelta(days=7)
    closed_week = [s for s in rows if s.status == "closed" and s.closed_at and s.closed_at >= week_ago]
    counts = {k: sum(1 for s in rows if s.status in v) for k, v in VIEWS.items()}
    counts["closed_week"] = len(closed_week)
    pick = closed_week if view == "closed_week" else [s for s in rows if s.status in VIEWS.get(view, VIEWS["todo"])]
    if view in ("todo", "waiting"):
        pick.sort(key=lambda s: s.created_at)  # oldest first: answer in order
    users, invs = await authors(db, pick)
    merged = await merged_counts(db, [s.id for s in pick])
    return {"rows": [team_row(s, users, invs, merged) for s in pick[:300]], "counts": counts, "late": sum(1 for s in rows if is_late(s)),
            "receives": receives(access), "is_founder": access.is_founder, "reply_days": REPLY_DAYS}


async def said_did(db: AsyncSession) -> list[dict]:
    """You said, we did: ideas done in the last two months, credited to who suggested them."""
    since = now() - timedelta(days=62)
    rows = (await db.execute(select(Suggestion).where(Suggestion.status == "done", Suggestion.done_at >= since)
                             .order_by(Suggestion.done_at.desc()).limit(12))).scalars().all()
    users, _ = await authors(db, rows)
    merged = await merged_counts(db, [s.id for s in rows])
    return [{"title": s.title, "by": credit(s, users), "topic_label": topic_label(s.topic), "done_at": s.done_at.isoformat(),
             "suggested_by": 1 + merged.get(s.id, 0)} for s in rows]


async def mine(db: AsyncSession, *, user: Optional[User] = None, investor: Optional[Investor] = None) -> dict:
    who = Suggestion.author_user_id == user.id if user else Suggestion.author_investor_id == investor.id
    rows = (await db.execute(select(Suggestion).where(who).order_by(Suggestion.created_at.desc()).limit(100))).scalars().all()
    ids = [s.id for s in rows]
    replies = (await db.execute(select(SuggestionReply).where(SuggestionReply.suggestion_id.in_(ids), SuggestionReply.direction.in_(("in", "out")))
                                .order_by(SuggestionReply.created_at))).scalars().all() if ids else []
    by: dict = {}
    for r in replies:
        by.setdefault(r.suggestion_id, []).append(r)
    mains = {m.id: m for m in (await db.execute(select(Suggestion).where(Suggestion.id.in_({s.merged_into_id for s in rows if s.merged_into_id})))).scalars()} \
        if any(s.merged_into_id for s in rows) else {}
    merged = await merged_counts(db, ids + list(mains))
    items = []
    for s in rows:
        main = mains.get(s.merged_into_id)
        items.append({"id": str(s.id), "reference": s.reference, "title": s.title, "body": s.body, "topic_label": topic_label(s.topic),
                      "status": s.status, "status_label": SENDER_STATUS[s.status], "closed_reason": s.closed_reason,
                      "created_at": s.created_at.isoformat(), "hide_name": s.hide_name, "suggested_by": 1 + merged.get(s.id, 0),
                      "merged_into": {"reference": main.reference, "title": main.title, "status_label": SENDER_STATUS[main.status],
                                      "suggested_by": 1 + merged.get(main.id, 0)} if main else None,
                      "can_reply": s.status != "merged",
                      "replies": [{"id": str(r.id), "mine": r.direction == "in", "body": r.body, "at": r.created_at.isoformat(),
                                   "by": "You" if r.direction == "in" else "Streamers Hub"} for r in by.get(s.id, [])]})
    sent = await sent_this_week(db, user_id=user.id if user else None, investor_id=investor.id if investor else None)
    return {"items": items, "said_did": await said_did(db), "sent_this_week": sent, "weekly_limit": WEEKLY_LIMIT, "reply_days": REPLY_DAYS,
            "topics": [{"key": k, "label": t["label"], "private": bool(t.get("private")),
                        "goes_to": "The Founder only" if t.get("private") else ("Strategy & Investors" if investor else
                                                                                " and ".join(dict.fromkeys(DESK_LABEL[d] for d in t["desks"])))}
                       for k, t in TOPICS.items()],
            "group_label": GROUP_LABEL["investor" if investor else group_of(user.role)]}


# Counts, numbers and the scheduler --------------------------------------------------------------------------------

async def nav_count(db: AsyncSession, access: Access) -> int:
    """New ideas waiting for a first answer on this person's desks, plus (the Founder) ideas waiting for a decision."""
    if not receives(access):
        return 0
    rows = (await db.execute(select(Suggestion).where(Suggestion.status.in_(("new", "recommended"))))).scalars().all()
    return sum(1 for s in rows if can_see(access, s) and (s.status == "new" or access.is_founder))


async def metrics(db: AsyncSession) -> dict:
    """For Command: this month's ideas, how fast they were answered, how many were adopted, and what's late now."""
    start = day_start(today().replace(day=1))
    at = now()
    rows = (await db.execute(select(Suggestion).where(Suggestion.created_at >= start))).scalars().all()
    due = [s for s in rows if s.first_response_at or at - s.created_at > timedelta(days=REPLY_DAYS)]
    on_time = [s for s in due if s.first_response_at and s.first_response_at - s.created_at <= timedelta(days=REPLY_DAYS)]
    firsts = [(s.first_response_at - s.created_at).total_seconds() / 86400 for s in rows if s.first_response_at]
    adopted = sum(1 for s in rows if s.status in ("planned", "done"))
    late = (await db.execute(select(Suggestion).where(Suggestion.status == "new", Suggestion.first_response_at.is_(None),
                                                      Suggestion.created_at < at - timedelta(days=REPLY_DAYS)))).scalars().all()
    done = await db.scalar(select(func.count()).select_from(Suggestion).where(Suggestion.status == "done", Suggestion.done_at >= start)) or 0
    groups = {g: sum(1 for s in rows if s.sender_group == g) for g in GROUP_LABEL}
    return {"received": len(rows), "by_group": {g: n for g, n in groups.items() if n},
            "answered_on_time_pct": round(len(on_time) * 100 / len(due)) if due else None,
            "adopted": adopted, "adopted_pct": round(adopted * 100 / len(rows)) if rows else None, "done": done,
            "avg_days_to_answer": round(sum(firsts) / len(firsts), 1) if firsts else None, "late": len(late),
            "waiting_for_you": await db.scalar(select(func.count()).select_from(Suggestion).where(Suggestion.status == "recommended")) or 0,
            "reply_days": REPLY_DAYS}


async def remind(db: AsyncSession) -> list[dict]:
    """Daily: ideas with no answer after REMIND_DAYS days. Their department heads are told once."""
    rows = (await db.execute(select(Suggestion).where(Suggestion.status == "new", Suggestion.first_response_at.is_(None),
                                                      Suggestion.reminded_at.is_(None),
                                                      Suggestion.created_at < now() - timedelta(days=REMIND_DAYS)))).scalars().all()
    mail = []
    for s in rows:
        s.reminded_at = now()
        to = await lead_emails(db, s)
        if to:
            mail.append({"kind": "team", "to": to, "subject": f"An idea is waiting for an answer · {s.title} [{s.reference}]",
                         "title": "An idea is still waiting",
                         "lines": [f"“{s.title}” ({topic_label(s.topic)}) came in {(now() - s.created_at).days} days ago and hasn't been answered.",
                                   f"Answer it within {REPLY_DAYS} days of arriving: reply, close it with a reason, or recommend what to do."],
                         "cta": ("Answer it", f"/os/suggestions?id={s.id}")})
    return mail


async def weekly_closed(db: AsyncSession) -> list[dict]:
    """Mondays: the ideas the departments closed as "Not now" this past week, for the Founder to check."""
    since = now() - timedelta(days=7)
    founders = (await db.execute(select(User).where(User.role == "founder", User.status == "active"))).scalars().all()
    rows = (await db.execute(select(Suggestion).where(Suggestion.status == "closed", Suggestion.closed_at >= since,
                                                      or_(Suggestion.closed_by.is_(None), Suggestion.closed_by.notin_([f.id for f in founders] or [uuid.uuid4()])))
                             .order_by(Suggestion.closed_at))).scalars().all()
    if not rows or not founders:
        return []
    lines = [f"{s.reference} · {s.title} — {desks_label(s)}: {s.closed_reason}" for s in rows[:20]]
    return [{"kind": "team", "to": [f.email for f in founders], "subject": f"Ideas closed this week: {len(rows)}",
             "title": "Ideas the departments closed this week",
             "lines": lines + ["Reopen any of them from Suggestions in Hub OS."], "cta": ("Review them", "/os/suggestions?view=closed_week")}]


async def deliver(mail: list[dict]) -> None:
    """Send what an action queued, after it's saved."""
    from app.services import EmailService

    for m in mail:
        try:
            if m["kind"] == "received":
                await EmailService.suggestion_received(m["to"], m["name"], m["reference"], m["title"], m["path"])
            elif m["kind"] == "update":
                await EmailService.suggestion_update(m["to"], m["name"], m["reference"], m["title"], m["heading"], m["text"],
                                                     m["path"], m.get("signed"))
            elif m["kind"] == "team" and m["to"]:
                await EmailService.notify(m["to"], m["subject"], m["title"], m["lines"], m.get("cta"))
        except Exception:  # an email that didn't go never undoes the change
            import logging

            logging.getLogger(__name__).exception("Suggestion email not sent")
