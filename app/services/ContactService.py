"""The Contact page: why people write to us, what each reason asks for, who answers it, and the
conversation that follows.

Each reason is one spec, used three ways: the website draws its form from it, the API checks a message
against it, and Hub OS shows the answers with its labels. Add or change a field here and all three follow.
"""

import hashlib
import re
import secrets
from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt, encrypt
from app.core.security import now
from app.models.contact import CONTACT_STATUSES, JOB_STAGES, ContactMessage, ContactReply
from app.models.posts import Post
from app.models.talent import Streamer
from app.services.MemberService import short_code

BUDGETS = ["Under GHS 2,000", "GHS 2,000 to 5,000", "GHS 5,000 to 15,000", "GHS 15,000 to 50,000", "Over GHS 50,000", "Not set yet"]


def f(key: str, label: str, type: str = "text", *, required: bool = True, options: Optional[list[str]] = None,
      source: Optional[str] = None, hint: Optional[str] = None, placeholder: Optional[str] = None, max: int = 200,
      wide: bool = False, accept: str = "image,document") -> dict:
    """One field. Types: text, email, tel, url, textarea, select, multi, date, time, file (`accept`: image,
    document, or both)."""
    if type == "textarea":
        max = max if max != 200 else 4000
        wide = True
    if type == "url":
        max = 500
    return {"key": key, "label": label, "type": type, "required": required, "options": options, "source": source,
            "hint": hint, "placeholder": placeholder, "max": max, "wide": wide or type in ("multi", "file"),
            "accept": accept if type == "file" else None}


PHONE = f("phone", "Phone or WhatsApp", "tel", placeholder="+233", hint="So we can call if it's quicker")
PHONE_OPT = {**PHONE, "required": False, "label": "Phone or WhatsApp (optional)"}

# reason: label, group, the desk that answers, a line for the picker, and the fields after name and email
REASONS: dict[str, dict] = {
    "brand": {
        "label": "Brand partnership or sponsorship", "group": "Work with us", "desk": "partnerships",
        "blurb": "Sponsored streams, product placement, branded clips, events and merch collabs.",
        "fields": [
            f("company", "Company or brand", placeholder="Rush+ Energy"),
            f("role", "Your role", placeholder="Brand manager"),
            PHONE,
            f("website", "Company website or page", "url", required=False, placeholder="https://"),
            f("interests", "What are you interested in?", "multi", options=[
                "Sponsored stream segment", "Product placement on stream", "Branded clips and content", "Event sponsorship",
                "Merch collaboration", "Ambassador deal", "Giveaway", "Something else"]),
            f("streamers", "Which streamers?", "multi", required=False, source="roster",
              hint="Leave it empty and we'll suggest the best fit"),
            f("goal", "Main goal", "select", options=["Awareness", "App installs or sign-ups", "Sales", "Launch a product",
                                                       "Grow a community", "Something else"]),
            f("audience", "Who do you want to reach?", required=False, placeholder="18 to 30, Accra and Kumasi, gamers"),
            f("budget", "Budget", "select", options=BUDGETS),
            f("start", "When would you like to start?", "date", required=False),
            f("duration", "How long?", "select", required=False, options=["One-off", "1 month", "3 months", "6 months", "12 months or more"]),
            f("message", "Tell us about the campaign", "textarea", placeholder="The product, the idea, any must-haves."),
            f("brief", "Brief or deck (optional)", "file", required=False, hint="PDF or image · up to 20 MB"),
        ],
    },
    "booking": {
        "label": "Book a streamer or an appearance", "group": "Work with us", "desk": "partnerships",
        "blurb": "Hosting, guest appearances, live streams from your event, meet and greets.",
        "fields": [
            f("organisation", "Organisation or organiser", placeholder="University of Ghana SRC"),
            PHONE,
            f("event_name", "Event name", placeholder="Legon Games Week"),
            f("event_type", "Type of event", "select", options=["Corporate event", "School or campus", "Product launch",
                                                                 "Festival or concert", "Tournament", "Private event", "Online only", "Something else"]),
            f("date", "Date", "date"),
            f("time", "Start time", "time", required=False),
            f("city", "City", placeholder="Accra"),
            f("venue", "Venue", required=False, placeholder="The Great Hall"),
            f("audience", "Expected audience", "select", options=["Under 100", "100 to 500", "500 to 2,000", "Over 2,000", "Online only"]),
            f("streamers", "Who would you like?", "multi", required=False, source="roster", hint="Leave it empty if you're not sure"),
            f("appearance", "What would they do?", "multi", options=["Host or MC", "Guest appearance", "Live stream from the event",
                                                                     "Play against fans", "Meet and greet", "Judge a competition", "Something else"]),
            f("budget", "Budget", "select", options=BUDGETS),
            f("travel", "Transport and accommodation", "select", required=False,
              options=["We cover transport and accommodation", "We cover transport", "Not discussed yet"]),
            f("message", "Anything else we should know", "textarea", placeholder="Schedule, dress code, what success looks like."),
        ],
    },
    "press": {
        "label": "Press and media", "group": "Work with us", "desk": "social",
        "blurb": "Interviews, features, quotes, photos and logos, podcast guests.",
        "fields": [
            f("outlet", "Publication, channel or show", placeholder="Joy Prime"),
            f("role", "Your role", placeholder="Journalist"),
            f("outlet_url", "Link to the outlet", "url", required=False, placeholder="https://"),
            f("request", "What do you need?", "select", options=["An interview", "A feature or story", "A quote or comment",
                                                                 "Photos or logos", "A podcast or show guest", "Filming or a documentary", "Something else"]),
            f("who", "Who would you like to speak to?", "select", required=False,
              options=["The Founder", "A streamer", "Someone from the label", "Anyone who can help"]),
            f("deadline", "Your deadline", "date", required=False),
            f("format", "How", "select", required=False, options=["In person", "Phone or video call", "By email"]),
            PHONE_OPT,
            f("message", "Tell us about the story", "textarea", placeholder="The angle, the questions, when it runs."),
        ],
    },
    "investor": {
        "label": "Investing in Streamers Hub", "group": "Work with us", "desk": "capital",
        "blurb": "For new investors. Existing holders use the Investor Vault.",
        "fields": [
            f("investor_type", "You are investing as", "select", options=["An individual or angel", "A fund or VC", "A company",
                                                                          "A family office", "Something else"]),
            f("organisation", "Organisation (if any)", required=False),
            PHONE,
            f("ticket", "Amount you have in mind", "select", options=["Under GHS 10,000", "GHS 10,000 to 50,000", "GHS 50,000 to 200,000",
                                                                      "Over GHS 200,000", "Prefer to discuss"]),
            f("linkedin", "LinkedIn or website", "url", required=False, placeholder="https://"),
            f("heard", "How did you hear about us?", required=False),
            f("message", "What would you like to know?", "textarea"),
        ],
    },
    "job": {
        "label": "Apply for an open role", "group": "Work with us", "desk": "people",
        "blurb": "Send your CV for a role we're hiring for.",
        "fields": [
            f("role", "The role", "select", source="jobs"),
            PHONE,
            f("city", "Where are you based?", placeholder="Accra, East Legon"),
            f("experience", "Experience in this kind of work", "select", options=["Starting out", "Under 1 year", "1 to 2 years",
                                                                                  "3 to 5 years", "Over 5 years"]),
            f("availability", "When could you start?", "select", options=["Right away", "In 2 weeks", "In a month", "Later than that"]),
            f("portfolio", "Portfolio, showreel or work samples", "url", required=False, placeholder="https://",
              hint="Behance, YouTube, Drive, TikTok: anywhere we can see your work"),
            f("linkedin", "LinkedIn (optional)", "url", required=False, placeholder="https://linkedin.com/in/"),
            f("pay", "Pay you expect (optional)", required=False, placeholder="GHS a month"),
            f("cv", "Your CV", "file", hint="PDF · up to 20 MB", accept="document"),
            f("message", "Why you, for this role?", "textarea", placeholder="What you've made, what you'd bring, why us."),
        ],
    },
    "order": {
        "label": "Help with a store order", "group": "Get help", "desk": "store",
        "blurb": "Delivery, sizes, exchanges, refunds and payments.",
        "fields": [
            f("order_number", "Order or list number", placeholder="SH-1042", hint="It's in your confirmation email", max=40),
            f("issue", "What's wrong?", "select", options=["It hasn't arrived", "Wrong item or size", "It's damaged or faulty",
                                                          "Change or cancel my order", "I'd like a refund", "A payment problem", "Something else"]),
            PHONE,
            f("message", "Tell us what happened", "textarea"),
            f("photo", "A photo (optional)", "file", required=False, hint="Helps if it's damaged or wrong", accept="image"),
        ],
    },
    "tickets": {
        "label": "Event tickets", "group": "Get help", "desk": "posts",
        "blurb": "Your ticket, changes, refunds, group and school bookings.",
        "fields": [
            f("event", "Which event?", "select", source="events"),
            f("ticket_code", "Ticket or reservation code", required=False, placeholder="SH-AB12CD", max=40),
            f("issue", "What do you need?", "select", options=["I didn't get my ticket", "Change the name or number of tickets",
                                                              "Cancel or refund", "A group or school booking", "Access needs", "Something else"]),
            PHONE,
            f("message", "Tell us more", "textarea"),
        ],
    },
    "content": {
        "label": "Content, clips and copyright", "group": "Get help", "desk": "production",
        "blurb": "Copyright and takedowns, using our clips, collabs with a streamer.",
        "fields": [
            f("topic", "What is it about?", "select", options=["Copyright or a takedown", "Using our clips or footage",
                                                              "Featuring my content on stream", "A collab with a streamer",
                                                              "A problem with a clip", "Something else"]),
            f("content_url", "Link to the content", "url", placeholder="https://"),
            f("rights", "Your connection to it", "select", required=False,
              options=["I own the rights", "I represent the owner", "I'm a viewer", "Doesn't apply"]),
            PHONE_OPT,
            f("message", "Tell us more", "textarea"),
        ],
    },
    "support": {
        "label": "Help with my account", "group": "Get help", "desk": "tech",
        "blurb": "Signing in to Hub OS, the Designer Studio, the Clipper Desk or the vault.",
        "fields": [
            f("account", "Which account?", "select", options=["Hub OS (the team)", "Designer Studio", "Clipper Desk", "Investor Vault",
                                                              "Order tracking"]),
            f("issue", "What's happening?", "select", options=["I can't sign in", "My code didn't arrive", "My invite link expired",
                                                              "Something is broken", "Something else"]),
            f("device", "Phone or computer, and browser", required=False, placeholder="iPhone, Safari"),
            f("message", "Tell us what you see", "textarea"),
            f("screenshot", "A screenshot (optional)", "file", required=False, accept="image"),
        ],
    },
    "general": {
        "label": "Something else", "group": "Something else", "desk": "command",
        "blurb": "Anything that doesn't fit above.",
        "fields": [
            f("subject", "Subject", max=160),
            PHONE_OPT,
            f("message", "Your message", "textarea"),
        ],
    },
}
GROUPS = ["Work with us", "Get help", "Something else"]
MAX_FILES = 3
OTHER_EVENT = "other"


def reasons_for(areas: list[str]) -> list[str]:
    """The reasons whose messages this person answers."""
    return [k for k, r in REASONS.items() if r["desk"] in areas]


def reason_label(key: str) -> str:
    return REASONS.get(key, {}).get("label", key)


# Choices that come from the database ---------------------------------------------------------------

async def open_jobs(db: AsyncSession) -> list[Post]:
    rows = (await db.execute(select(Post).where(Post.kind == "job", Post.status == "published")
                             .order_by(Post.pinned.desc(), Post.published_at.desc()))).scalars().all()
    return [p for p in rows if job_open(p)]


def job_open(p: Post) -> bool:
    from app.services.PostService import job_state

    return p.status == "published" and job_state(p) == "open"


async def sources(db: AsyncSession) -> dict[str, list[dict]]:
    from app.services import PostService

    roster = (await db.execute(select(Streamer).where(Streamer.status == "signed", Streamer.is_public.is_(True))
                               .order_by(Streamer.signing_number))).scalars().all()
    events = (await db.execute(select(Post).where(Post.kind == "event", Post.status == "published")
                               .order_by(Post.starts_at.desc()).limit(12))).scalars().all()
    return {
        "roster": [{"value": s.stage_name, "label": s.stage_name} for s in roster],
        "jobs": [{"value": p.slug, "label": p.title} for p in await open_jobs(db)],
        "events": [{"value": p.slug, "label": f"{p.title} · {PostService.when_text(p).split(' · ')[0]}"} for p in events]
                  + [{"value": OTHER_EVENT, "label": "Another event, or not listed"}],
    }


async def public_spec(db: AsyncSession) -> dict:
    src = await sources(db)
    out = []
    for key, r in REASONS.items():
        fields = []
        for fl in r["fields"]:
            opts = src[fl["source"]] if fl["source"] else [{"value": o, "label": o} for o in fl["options"] or []]
            fields.append({**fl, "options": opts if fl["type"] in ("select", "multi") else None})
        out.append({"key": key, "label": r["label"], "group": r["group"], "blurb": r["blurb"], "fields": fields,
                    "open": key != "job" or bool(src["jobs"])})
    return {"groups": GROUPS, "reasons": out}


# Checking a message -----------------------------------------------------------------------------------

def _digits(value: str) -> int:
    return sum(1 for c in value if c.isdigit())


async def clean(db: AsyncSession, reason: str, raw: dict) -> tuple[dict, list[dict]]:
    """Returns (cleaned answers, the file fields). Raises 400 with the missing or wrong fields."""
    from app.services import PostService

    spec = REASONS[reason]
    src = await sources(db)
    out: dict = {}
    missing: list[str] = []
    problems: list[str] = []
    for fl in spec["fields"]:
        key, kind = fl["key"], fl["type"]
        if kind == "file":
            continue
        value = raw.get(key)
        if kind == "multi":
            items = [str(v).strip() for v in (value if isinstance(value, list) else []) if str(v).strip()]
            allowed = {o["value"] for o in src[fl["source"]]} if fl["source"] else set(fl["options"] or [])
            items = [i for i in dict.fromkeys(items) if i in allowed][:12]
            if fl["required"] and not items:
                missing.append(key)
            if items:
                out[key] = items
            continue
        value = "" if value is None or isinstance(value, (list, dict)) else str(value).strip()
        if not value:
            if fl["required"]:
                missing.append(key)
            continue
        if len(value) > fl["max"]:
            problems.append(f"{fl['label']} is too long.")
            missing.append(key)
            continue
        if kind == "select":
            allowed = {o["value"] for o in src[fl["source"]]} if fl["source"] else set(fl["options"] or [])
            if value not in allowed:
                missing.append(key)
                if fl["source"] == "jobs":
                    problems.append("That role isn't open any more.")
                continue
        elif kind == "tel" and _digits(value) < 9:
            problems.append("Enter a phone number we can call.")
            missing.append(key)
            continue
        elif kind == "url":
            try:
                value = PostService.ok_url(value)
            except HTTPException:
                problems.append(f"{fl['label']}: paste a full link.")
                missing.append(key)
                continue
        elif kind == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                missing.append(key)
                continue
        elif kind == "time" and not re.match(r"^\d{2}:\d{2}$", value):
            missing.append(key)
            continue
        out[key] = value
    if missing:
        msg = problems[0] if problems else ("Fill in the highlighted fields." if len(missing) > 1 else "Fill in the highlighted field.")
        raise HTTPException(400, {"message": msg, "missing": missing})
    return out, [fl for fl in spec["fields"] if fl["type"] == "file"]


def subject_for(reason: str, a: dict, titles: dict[str, str]) -> str:
    pick = {
        "brand": lambda: f"{a.get('company')}: {', '.join(a.get('interests', [])[:2])}",
        "booking": lambda: f"{a.get('event_name')} · {a.get('date')} · {a.get('city')}",
        "press": lambda: f"{a.get('outlet')}: {a.get('request')}",
        "investor": lambda: f"{a.get('investor_type')} · {a.get('ticket')}",
        "job": lambda: f"Application: {titles.get(a.get('role', ''), a.get('role'))}",
        "order": lambda: f"{a.get('order_number')}: {a.get('issue')}",
        "tickets": lambda: f"{titles.get(a.get('event', ''), 'Tickets')}: {a.get('issue')}",
        "content": lambda: f"{a.get('topic')}",
        "support": lambda: f"{a.get('account')}: {a.get('issue')}",
        "general": lambda: a.get("subject") or "A message",
    }[reason]
    return pick()[:200]


def new_token() -> tuple[str, str, str]:
    raw = secrets.token_urlsafe(24)
    return raw, hash_token(raw), encrypt(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(f"contact:{raw}".encode()).hexdigest()


def token_of(m: ContactMessage) -> str:
    return decrypt(m.token_enc)


def conversation_path(m: ContactMessage) -> str:
    return f"/contact/c/{token_of(m)}"


async def new_reference(db: AsyncSession) -> str:
    for _ in range(6):
        ref = f"SH-MSG-{short_code(5)}"
        if not await db.scalar(select(func.count()).select_from(ContactMessage).where(ContactMessage.reference == ref)):
            return ref
    raise HTTPException(503, "Try again in a moment.")


# What Hub OS shows ------------------------------------------------------------------------------------

def answers(m: ContactMessage, titles: dict[str, str]) -> list[dict]:
    """The sender's answers with their labels, in the form's order (the message itself is in the thread)."""
    spec = REASONS.get(m.reason)
    if not spec:
        return []
    p = m.payload or {}
    out = []
    for fl in spec["fields"]:
        key = fl["key"]
        if fl["type"] == "file" or key in ("message", "phone") or key not in p:
            continue
        value = p[key]
        if fl["source"] in ("jobs", "events"):
            value = titles.get(value, "Another event" if value == OTHER_EVENT else value)
        out.append({"key": key, "label": fl["label"].replace(" (optional)", ""), "type": fl["type"],
                    "value": ", ".join(value) if isinstance(value, list) else value})
    return out


def phone_of(m: ContactMessage) -> Optional[str]:
    try:
        return decrypt(m.phone_enc) if m.phone_enc else None
    except Exception:
        return None


def needs_reply(m: ContactMessage) -> bool:
    return m.status in ("new", "awaiting")


def row(m: ContactMessage, titles: dict[str, str], names: dict) -> dict:
    p = m.payload or {}
    return {"id": str(m.id), "reference": m.reference, "reason": m.reason, "reason_label": reason_label(m.reason), "desk": m.desk,
            "subject": m.subject, "name": m.name, "email": m.email, "organisation": m.organisation, "status": m.status,
            "status_label": CONTACT_STATUSES.get(m.status, m.status), "stage": m.stage, "stage_label": JOB_STAGES.get(m.stage or "", None),
            "needs_reply": needs_reply(m), "assigned_to": str(m.assigned_to) if m.assigned_to else None,
            "assignee": names.get(m.assigned_to), "post_id": str(m.post_id) if m.post_id else None,
            "role_title": titles.get(p.get("role", "")) if m.reason == "job" else None,
            "files": len(m.files or []), "created_at": m.created_at.isoformat(),
            "last_at": max(x for x in (m.last_in_at, m.last_out_at, m.created_at) if x).isoformat(),
            "preview": (p.get("message") or "")[:160]}


async def thread(db: AsyncSession, m: ContactMessage, *, public: bool) -> list[ContactReply]:
    q = select(ContactReply).where(ContactReply.message_id == m.id).order_by(ContactReply.created_at)
    if public:
        q = q.where(ContactReply.direction != "note")
    return list((await db.execute(q)).scalars().all())


def first_name(name: str) -> str:
    return (name or "there").split(" ")[0]


def reply_out(r: ContactReply, base: str, *, public: bool) -> dict:
    from app.services import S3Service

    out = {"id": str(r.id), "direction": r.direction, "body": r.body, "at": r.created_at.isoformat(),
           "files": [{"name": x["name"], "url": S3Service.signed_url(x["key"], 600, base)} for x in r.files or []]}
    if r.direction == "out":
        out["by"] = "Streamers Hub" if public else (r.author_label or "The team")
        if not public:
            out["emailed"] = r.emailed
    elif r.direction == "note":
        out["by"] = r.author_label or "The team"
    return out


def touch_in(m: ContactMessage, at: Optional[datetime] = None) -> None:
    m.last_in_at = at or now()
    if m.status in ("replied", "closed"):
        m.status = "awaiting"
