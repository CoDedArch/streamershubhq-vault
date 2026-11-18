"""The Contact page and the Messages inbox.

Public: the reasons and their forms, sending a message (or an application for an open role), and the
sender's own conversation page, reached from a private link in every email we send them.

Hub OS: the "inbox" area. Each message belongs to the desk that answers its reason, and people see only
their desks' messages. A reply is emailed to the sender. Answering for a desk held only by stepping in is
recorded for that desk."""

import json
import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.datastructures import UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AREAS, OS_ROLES, Access, acting_for, require_area
from app.core.crypto import digits_tail, encrypt
from app.core.database import aget_db
from app.core.security import client_ip, now
from app.models.contact import CONTACT_STATUSES, JOB_STAGES, ContactMessage, ContactReply
from app.models.people import SignInAttempt, StaffProfile, User
from app.models.posts import Post
from app.services import ContactService as CS
from app.services import EmailService, S3Service
from app.services.AuditService import audit

router = APIRouter(tags=["contact"])
inbox_desk = require_area("inbox")
HOUR = timedelta(hours=1)


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _recent(db: AsyncSession, kind: str, *, ip: Optional[str] = None, contact: Optional[str] = None) -> int:
    q = select(func.count()).select_from(SignInAttempt).where(SignInAttempt.kind == kind, SignInAttempt.created_at >= now() - HOUR)
    q = q.where(SignInAttempt.ip == ip) if ip else q.where(SignInAttempt.contact == contact)
    return await db.scalar(q) or 0


async def _store_files(items: list[tuple[str, UploadFile]], accepts: dict[str, str], folder: str) -> list[dict]:
    stored = []
    for key, up in items:
        data = await up.read()
        kinds = tuple(k for k in (accepts.get(key) or "image,document").split(",") if k)
        try:
            S3Service.check_file(data, up.content_type or "", kinds)
        except S3Service.UploadRejected as exc:
            raise HTTPException(400, {"message": f"{up.filename or 'That file'}: {exc}", "missing": [key]})
        stored.append({"key": S3Service.put(folder, up.filename or "file", data, up.content_type or "application/octet-stream"),
                       "name": (up.filename or "file")[:120], "size": len(data), "type": up.content_type, "field": key})
    return stored


async def _titles(db: AsyncSession, msgs: list[ContactMessage]) -> dict[str, str]:
    slugs = {v for m in msgs for k, v in (m.payload or {}).items() if k in ("role", "event") and isinstance(v, str)}
    if not slugs:
        return {}
    return {p.slug: p.title for p in (await db.execute(select(Post).where(Post.slug.in_(slugs)))).scalars()}


# Public ------------------------------------------------------------------------------------------------

@router.get("/public/contact")
async def contact_form(db: AsyncSession = Depends(aget_db)):
    from app.core.config import settings

    return {**await CS.public_spec(db), "email": settings.SH_CONTACT_EMAIL}


@router.post("/public/contact")
async def send_message(request: Request, db: AsyncSession = Depends(aget_db)):
    """A multipart form: `reason`, `data` (the answers as JSON, with name, email and agree) and one file
    per file field, named `file:<field key>`."""
    form = await request.form()
    reason = str(form.get("reason") or "")
    if reason not in CS.REASONS:
        raise HTTPException(400, "Pick what you're getting in touch about.")
    ip = client_ip(request)
    try:
        raw = json.loads(str(form.get("data") or "{}"))
    except json.JSONDecodeError:
        raise HTTPException(400, "Something went wrong with the form. Refresh and try again.")
    raw = raw if isinstance(raw, dict) else {}
    if str(raw.get("company_site") or "").strip():  # the hidden field only bots fill in
        return {"reference": f"SH-MSG-{CS.short_code(5)}", "reason": reason}
    if await _recent(db, "contact", ip=ip) >= 8:
        raise HTTPException(429, "Too many messages from this connection. Try again in an hour, or email us.")
    name = str(raw.get("name") or "").strip()[:120]
    email = str(raw.get("email") or "").strip().lower()[:255]
    missing = [k for k, v in (("name", len(name) >= 2), ("email", "@" in email and "." in email.split("@")[-1])) if not v]
    answers: dict = {}
    files_spec: list[dict] = []
    try:
        answers, files_spec = await CS.clean(db, reason, raw)
    except HTTPException as exc:
        if not missing:
            raise
        missing += exc.detail.get("missing", []) if isinstance(exc.detail, dict) else []
    if missing:
        raise HTTPException(400, {"message": "Enter your name and an email we can reply to." if missing[0] in ("name", "email")
                                  else "Fill in the highlighted fields.", "missing": missing})
    if raw.get("agree") not in (True, "true", "on", "1"):
        raise HTTPException(400, {"message": "Tick the box so we can reply to you.", "missing": ["agree"]})
    if await _recent(db, "contact", contact=email) >= 5:
        raise HTTPException(429, "You've sent several messages in the last hour. We'll reply to those first.")

    uploads = [(k.split(":", 1)[1], v) for k, v in form.multi_items() if k.startswith("file:") and isinstance(v, UploadFile) and v.filename]
    keys = {fl["key"] for fl in files_spec}
    uploads = [(k, v) for k, v in uploads if k in keys]
    seen: set[str] = set()
    uploads = [(k, v) for k, v in uploads if not (k in seen or seen.add(k))][:CS.MAX_FILES]
    need = [fl["key"] for fl in files_spec if fl["required"] and fl["key"] not in {k for k, _ in uploads}]
    if need:
        label = next(fl["label"] for fl in files_spec if fl["key"] == need[0])
        raise HTTPException(400, {"message": f"Attach {label[0].lower() + label[1:]}.", "missing": need})

    post: Optional[Post] = None
    if reason == "job":
        post = (await db.execute(select(Post).where(Post.slug == answers["role"], Post.kind == "job"))).scalar_one_or_none()
        if not post or not CS.job_open(post):
            raise HTTPException(409, {"message": "That role isn't taking applications any more.", "missing": ["role"]})
        dup = (await db.execute(select(ContactMessage).where(ContactMessage.post_id == post.id, ContactMessage.email == email,
                                                             ContactMessage.status != "spam"))).scalars().first()
        if dup:
            raise HTTPException(409, {"message": f"You've already applied for this role ({dup.reference}). We'll reply by email.",
                                      "reference": dup.reference})
        answers["department"] = (post.job or {}).get("department")
    elif reason == "tickets" and answers.get("event") != CS.OTHER_EVENT:
        post = (await db.execute(select(Post).where(Post.slug == answers.get("event")))).scalar_one_or_none()

    stored = await _store_files(uploads, {fl["key"]: fl["accept"] for fl in files_spec}, "contact")
    titles = {post.slug: post.title} if post else {}
    phone = answers.pop("phone", None)
    raw_token, token_hash, token_enc = CS.new_token()
    m = ContactMessage(reference=await CS.new_reference(db), reason=reason, desk=CS.REASONS[reason]["desk"],
                       subject=CS.subject_for(reason, answers, titles), name=name, email=email,
                       phone_enc=encrypt(phone) if phone else None, phone_last3=digits_tail(phone, 3) if phone else None,
                       organisation=(answers.get("company") or answers.get("organisation") or answers.get("outlet") or None),
                       payload=answers, files=stored, post_id=post.id if post else None, status="new",
                       stage="applied" if reason == "job" else None, token_hash=token_hash, token_enc=token_enc,
                       last_in_at=now(), via=str(raw.get("via") or "")[:20] or None, ip=ip)
    db.add(m)
    await db.flush()
    db.add(ContactReply(message_id=m.id, direction="in", body=answers.get("message", ""), files=stored))
    db.add(SignInAttempt(kind="contact", contact=email, ip=ip, ok=True))
    await db.commit()
    path = f"/contact/c/{raw_token}"
    role = post.title if reason == "job" and post else None
    await EmailService.contact_received(email, name, m.reference, CS.reason_label(reason), m.subject, path, role)
    await EmailService.contact_to_team(m.reference, CS.reason_label(reason), m.subject, name, answers.get("message", ""), f"/os/inbox?id={m.id}")
    return {"reference": m.reference, "reason": reason, "conversation": path}


async def _by_token(db: AsyncSession, token: str, lock: bool = False) -> ContactMessage:
    q = select(ContactMessage).where(ContactMessage.token_hash == CS.hash_token(token.strip()))
    m = (await db.execute(q.with_for_update() if lock else q)).scalar_one_or_none()
    if not m or m.status == "spam":
        raise HTTPException(404, "We couldn't find this conversation. Use the link in your latest email from us.")
    return m


PUBLIC_STATUS = {"new": "With our team", "replied": "We replied", "awaiting": "With our team", "closed": "Closed"}


@router.get("/public/contact/c/{token}")
async def conversation(token: str, request: Request, db: AsyncSession = Depends(aget_db)):
    m = await _by_token(db, token)
    titles = await _titles(db, [m])
    return {"reference": m.reference, "reason": m.reason, "reason_label": CS.reason_label(m.reason), "subject": m.subject,
            "status": m.status, "status_label": PUBLIC_STATUS.get(m.status, ""), "name": CS.first_name(m.name),
            "created_at": m.created_at.isoformat(), "role": titles.get((m.payload or {}).get("role", "")) if m.reason == "job" else None,
            "answers": CS.answers(m, titles),
            "thread": [CS.reply_out(r, _base(request), public=True) for r in await CS.thread(db, m, public=True)]}


@router.post("/public/contact/c/{token}/reply")
async def answer_us(token: str, request: Request, db: AsyncSession = Depends(aget_db)):
    form = await request.form()
    body = str(form.get("body") or "").strip()
    if not 1 <= len(body) <= 4000:
        raise HTTPException(400, {"message": "Write your reply (up to 4,000 characters).", "missing": ["body"]})
    ip = client_ip(request)
    if await _recent(db, "msg_reply", ip=ip) >= 20:
        raise HTTPException(429, "Too many replies from this connection. Try again in an hour.")
    m = await _by_token(db, token, lock=True)
    ups = [("files", v) for k, v in form.multi_items() if k == "files" and isinstance(v, UploadFile) and v.filename][:CS.MAX_FILES]
    stored = await _store_files(ups, {"files": "image,document"}, "contact")
    db.add(ContactReply(message_id=m.id, direction="in", body=body, files=stored))
    CS.touch_in(m)
    db.add(SignInAttempt(kind="msg_reply", contact=m.email, ip=ip, ok=True))
    await db.commit()
    await EmailService.contact_to_team(m.reference, CS.reason_label(m.reason), m.subject, m.name, body, f"/os/inbox?id={m.id}", again=True)
    return {"ok": True, "status": m.status}


# Hub OS: the Messages inbox ------------------------------------------------------------------------------

def _visible(access: Access, m: ContactMessage) -> bool:
    if m.desk not in access.areas:
        return False
    if m.reason == "job" and access.user.role == "dept_head" and "people" not in access.stepped_in:
        return (m.payload or {}).get("department") == access.department  # a lead sees applications for their team
    return True


def _scope(access: Access):
    desks = sorted({CS.REASONS[r]["desk"] for r in CS.reasons_for(access.areas)})
    q = select(ContactMessage).where(ContactMessage.desk.in_(desks))
    if access.user.role == "dept_head" and "people" not in access.stepped_in:
        q = q.where(or_(ContactMessage.reason != "job", ContactMessage.payload["department"].as_string() == (access.department or "")))
    return q


async def inbox_count(db: AsyncSession, access: Access) -> int:
    q = _scope(access).where(ContactMessage.status.in_(("new", "awaiting")))
    return await db.scalar(select(func.count()).select_from(q.subquery())) or 0


async def _names(db: AsyncSession, ids) -> dict:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {u.id: u.name for u in (await db.execute(select(User).where(User.id.in_(ids)))).scalars()}


VIEWS = {"todo": ("new", "awaiting"), "replied": ("replied",), "open": ("new", "awaiting", "replied"), "closed": ("closed",), "spam": ("spam",)}


@router.get("/inbox")
async def inbox(view: str = "todo", reason: Optional[str] = None, job: Optional[uuid.UUID] = None, stage: Optional[str] = None,
                q: Optional[str] = None, mine: bool = False, access: Access = Depends(inbox_desk), db: AsyncSession = Depends(aget_db)):
    base = _scope(access)
    mine_reasons = CS.reasons_for(access.areas)
    counts = {k: await db.scalar(select(func.count()).select_from(base.where(ContactMessage.status.in_(v)).subquery())) or 0
              for k, v in VIEWS.items()}
    query = base.where(ContactMessage.status.in_(VIEWS.get(view, VIEWS["todo"])))
    if reason in mine_reasons:
        query = query.where(ContactMessage.reason == reason)
    if job:
        query = query.where(ContactMessage.post_id == job)
    if stage in JOB_STAGES:
        query = query.where(ContactMessage.stage == stage)
    if mine:
        query = query.where(ContactMessage.assigned_to == access.user.id)
    if q and q.strip():
        like = f"%{q.strip()[:60]}%"
        query = query.where(or_(ContactMessage.name.ilike(like), ContactMessage.email.ilike(like), ContactMessage.subject.ilike(like),
                                ContactMessage.reference.ilike(like), ContactMessage.organisation.ilike(like)))
    rows = (await db.execute(query.order_by(func.coalesce(ContactMessage.last_in_at, ContactMessage.created_at).desc()).limit(200))).scalars().all()
    waiting = base.where(ContactMessage.status.in_(("new", "awaiting"))).subquery()
    by_reason = dict((await db.execute(select(waiting.c.reason, func.count()).group_by(waiting.c.reason))).all())
    titles = await _titles(db, rows)
    names = await _names(db, [m.assigned_to for m in rows])
    jobs = (await db.execute(select(Post).where(Post.kind == "job", Post.status != "archived").order_by(Post.created_at.desc()))).scalars().all() \
        if "job" in mine_reasons else []
    return {
        "rows": [CS.row(m, titles, names) for m in rows], "counts": counts,
        "reasons": [{"key": k, "label": CS.reason_label(k), "desk": CS.REASONS[k]["desk"], "desk_label": AREAS[CS.REASONS[k]["desk"]][0],
                     "waiting": by_reason.get(k, 0), "stepping_in": CS.REASONS[k]["desk"] in access.stepped_in} for k in mine_reasons],
        "stages": [{"key": k, "label": v} for k, v in JOB_STAGES.items()],
        "jobs": [{"id": str(p.id), "title": p.title, "status": p.status} for p in jobs],
    }


async def _message(db: AsyncSession, access: Access, message_id: uuid.UUID, lock: bool = False) -> ContactMessage:
    m = await db.get(ContactMessage, message_id, with_for_update=lock)
    if not m or not _visible(access, m):
        raise HTTPException(404, "Message not found.")
    return m


async def _team(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(select(User, StaffProfile.title).outerjoin(StaffProfile, StaffProfile.user_id == User.id)
                             .where(User.role.in_(OS_ROLES - {"streamer"}), User.status == "active").order_by(User.name))).all()
    return [{"id": str(u.id), "name": u.name, "title": t} for u, t in rows]


async def detail(db: AsyncSession, access: Access, m: ContactMessage, base: str) -> dict:
    titles = await _titles(db, [m])
    names = await _names(db, [m.assigned_to])
    post = await db.get(Post, m.post_id) if m.post_id else None
    out = CS.row(m, titles, names)
    out.update({
        "phone": CS.phone_of(m), "answers": CS.answers(m, titles), "via": m.via,
        "files": [{"name": x["name"], "field": next((fl["label"] for fl in CS.REASONS.get(m.reason, {}).get("fields", []) if fl["key"] == x.get("field")), "File"),
                   "url": S3Service.signed_url(x["key"], 600, base), "size": x.get("size")} for x in m.files or []],
        "thread": [CS.reply_out(r, base, public=False) for r in await CS.thread(db, m, public=False)],
        "post": {"id": str(post.id), "title": post.title, "slug": post.slug, "kind": post.kind} if post else None,
        "stepping_in": m.desk in access.stepped_in, "desk_label": AREAS[m.desk][0],
        "first_reply_at": m.first_reply_at.isoformat() if m.first_reply_at else None,
        "team": await _team(db), "statuses": [{"key": k, "label": v} for k, v in CONTACT_STATUSES.items()],
    })
    if m.reason == "job":
        out["stages"] = [{"key": k, "label": v} for k, v in JOB_STAGES.items()]
    return out


@router.get("/inbox/{message_id}")
async def open_message(message_id: uuid.UUID, request: Request, access: Access = Depends(inbox_desk), db: AsyncSession = Depends(aget_db)):
    m = await _message(db, access, message_id)
    return await detail(db, access, m, _base(request))


def _who(access: Access) -> str:
    title = access.title or ("Founder" if access.is_founder else None)
    name = access.user.name
    return name if not title or title.lower() in name.lower() else f"{name}, {title}"


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=6000)
    close: bool = False


@router.post("/inbox/{message_id}/reply")
async def reply(message_id: uuid.UUID, body: ReplyIn, request: Request, access: Access = Depends(inbox_desk),
                db: AsyncSession = Depends(aget_db)):
    """Answer the sender: saved in the conversation and emailed to them."""
    m = await _message(db, access, message_id, lock=True)
    if m.status == "spam":
        raise HTTPException(409, "This is marked as spam. Move it back to the inbox first.")
    text = body.body.strip()
    if not text:
        raise HTTPException(400, {"message": "Write your reply.", "missing": ["body"]})
    who = _who(access)
    async with acting_for(access, m.desk, request):
        r = ContactReply(message_id=m.id, direction="out", body=text, author_user_id=access.user.id, author_label=who[:160], emailed=None)
        db.add(r)
        m.last_out_at = now()
        m.first_reply_at = m.first_reply_at or m.last_out_at
        m.status = "closed" if body.close else "replied"
        m.assigned_to = m.assigned_to or access.user.id
        await audit(db, access.user, "contact.replied", "contact_message", m.reference, request=request,
                    summary=f"Replied to {m.reference} ({CS.reason_label(m.reason)}: {m.name})" + (" and closed it" if body.close else ""))
        await db.commit()
    r.emailed = await EmailService.contact_reply(m.email, m.name, m.reference, m.subject, text, who, CS.conversation_path(m))
    await db.commit()
    return {**await detail(db, access, m, _base(request)), "emailed": r.emailed}


class NoteIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


@router.post("/inbox/{message_id}/note")
async def note(message_id: uuid.UUID, body: NoteIn, request: Request, access: Access = Depends(inbox_desk), db: AsyncSession = Depends(aget_db)):
    """A note for the team only. The sender never sees it."""
    m = await _message(db, access, message_id)
    async with acting_for(access, m.desk, request):
        db.add(ContactReply(message_id=m.id, direction="note", body=body.body.strip(), author_user_id=access.user.id, author_label=_who(access)[:160]))
        await audit(db, access.user, "contact.note", "contact_message", m.reference, request=request, summary=f"Note on {m.reference}")
        await db.commit()
    return await detail(db, access, m, _base(request))


@router.post("/inbox/{message_id}/resend/{reply_id}")
async def resend(message_id: uuid.UUID, reply_id: uuid.UUID, request: Request, access: Access = Depends(inbox_desk),
                 db: AsyncSession = Depends(aget_db)):
    """Send a reply's email again (when it didn't go the first time)."""
    m = await _message(db, access, message_id)
    r = await db.get(ContactReply, reply_id)
    if not r or r.message_id != m.id or r.direction != "out":
        raise HTTPException(404, "Reply not found.")
    r.emailed = await EmailService.contact_reply(m.email, m.name, m.reference, m.subject, r.body, r.author_label or "The team", CS.conversation_path(m))
    await db.commit()
    if not r.emailed:
        raise HTTPException(503, "The email still didn't go. Check the email settings, or call them.")
    return await detail(db, access, m, _base(request))


class MessageUpdate(BaseModel):
    status: Optional[str] = Field(default=None, pattern="^(new|replied|awaiting|closed|spam)$")
    stage: Optional[str] = Field(default=None, pattern="^(applied|shortlisted|interview|offer|hired|declined)$")
    assigned_to: Optional[str] = None  # a user id, or "" for nobody


@router.patch("/inbox/{message_id}")
async def update_message(message_id: uuid.UUID, body: MessageUpdate, request: Request, access: Access = Depends(inbox_desk),
                         db: AsyncSession = Depends(aget_db)):
    m = await _message(db, access, message_id, lock=True)
    changes = []
    async with acting_for(access, m.desk, request):
        if body.status and body.status != m.status:
            m.status = body.status
            changes.append(f"marked {CONTACT_STATUSES[body.status].lower()}")
        if body.stage:
            if m.reason != "job":
                raise HTTPException(400, "Only applications have stages.")
            if body.stage != m.stage:
                m.stage = body.stage
                changes.append(f"moved to {JOB_STAGES[body.stage].lower()}")
        if body.assigned_to is not None:
            uid = None
            if body.assigned_to:
                try:
                    uid = uuid.UUID(body.assigned_to)
                except ValueError:
                    raise HTTPException(400, "Pick someone from the team.")
                u = await db.get(User, uid)
                if not u or u.role not in OS_ROLES or u.role == "streamer" or u.status != "active":
                    raise HTTPException(400, "Pick someone from the team.")
                changes.append(f"given to {u.name}")
            else:
                changes.append("unassigned")
            m.assigned_to = uid
        if changes:
            await audit(db, access.user, "contact.updated", "contact_message", m.reference, request=request,
                        summary=f"{m.reference} {', '.join(changes)}")
        await db.commit()
    return await detail(db, access, m, _base(request))
