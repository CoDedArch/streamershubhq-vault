"""The Suggestion Bank (rules in SuggestionService).

Sending: everyone with a Hub OS account (staff and streamers), clippers and designers (one sign-in), and investors
(from the vault). Each sees their own ideas, our answers, and "You said, we did".

Answering: the "suggestions" area of Hub OS. People see only the ideas for their desks (the Founder sees them all,
private ones included). Changes made for a desk held only by stepping in are recorded for that desk."""

import contextlib
import json
import uuid
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.api.v1.endpoints.vault import current_investor
from app.core.access import Access, acting_for, current_access, require_area
from app.core.database import aget_db
from app.models.investors import Investor
from app.models.suggestions import Suggestion
from app.services import S3Service
from app.services import SuggestionService as SS
from app.services.AuditService import audit

router = APIRouter(tags=["suggestions"])
team_desk = require_area("suggestions")


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _sender(access: Access = Depends(current_access)) -> Access:
    if access.user.role not in SS.SENDER_ROLES or access.user.status != "active":
        raise HTTPException(403, "Suggestions are for the team, streamers, clippers, designers and investors.")
    return access


async def _form(request: Request) -> tuple[dict, list[dict]]:
    """A multipart form: `data` (the answers as JSON) and, optionally, one `file` (an image or a PDF, up to 5 MB)."""
    form = await request.form()
    try:
        raw = json.loads(str(form.get("data") or "{}"))
    except json.JSONDecodeError:
        raise HTTPException(400, "Something went wrong with the form. Refresh and try again.")
    answers = SS.clean(raw if isinstance(raw, dict) else {})
    up = form.get("file")
    files = []
    if isinstance(up, UploadFile) and up.filename:
        data = await up.read()
        try:
            S3Service.check_file(data, up.content_type or "", ("image", "document"))
        except S3Service.UploadRejected as exc:
            raise HTTPException(400, {"message": f"{up.filename}: {exc}", "missing": ["file"]})
        if len(data) > SS.MAX_FILE_BYTES:
            raise HTTPException(400, {"message": "Attachments can be up to 5 MB.", "missing": ["file"]})
        files.append({"key": S3Service.put("suggestions", up.filename, data, up.content_type or "application/octet-stream"),
                      "name": up.filename[:120], "size": len(data), "type": up.content_type})
    return answers, files


# Sending: Hub OS, the Clipper Desk and the Designer Studio ------------------------------------------------------

@router.get("/suggestions/mine")
async def my_suggestions(access: Access = Depends(_sender), db: AsyncSession = Depends(aget_db)):
    return await SS.mine(db, user=access.user)


@router.post("/suggestions")
async def send_suggestion(request: Request, access: Access = Depends(_sender), db: AsyncSession = Depends(aget_db)):
    answers, files = await _form(request)
    s, mail = await SS.create(db, answers, files, user=access.user)
    await audit(db, access.user, "suggestion.sent", "suggestion", s.reference, request=request,
                summary=f"Sent an idea ({SS.topic_label(s.topic)}){' with their name hidden' if s.hide_name else ''}")
    await db.commit()
    await SS.deliver(mail)
    return {"reference": s.reference, "id": str(s.id), **await SS.mine(db, user=access.user)}


class SenderReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


async def _own(db: AsyncSession, suggestion_id: uuid.UUID, *, user_id=None, investor_id=None) -> Suggestion:
    s = await db.get(Suggestion, suggestion_id)
    if not s or (user_id and s.author_user_id != user_id) or (investor_id and s.author_investor_id != investor_id):
        raise HTTPException(404, "Idea not found.")
    return s


@router.post("/suggestions/{suggestion_id}/reply")
async def answer_team(suggestion_id: uuid.UUID, body: SenderReplyIn, access: Access = Depends(_sender), db: AsyncSession = Depends(aget_db)):
    s = await _own(db, suggestion_id, user_id=access.user.id)
    users, invs = await SS.authors(db, [s])
    mail = await SS.sender_reply(db, s, body.body, SS.sender_label(s, users, invs))
    await db.commit()
    await SS.deliver(mail)
    return await SS.mine(db, user=access.user)


# Sending: the Investor Vault ----------------------------------------------------------------------------------

@router.get("/investor/suggestions")
async def investor_suggestions(investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    return await SS.mine(db, investor=investor)


@router.post("/investor/suggestions")
async def investor_send(request: Request, investor: Investor = Depends(current_investor), db: AsyncSession = Depends(aget_db)):
    answers, files = await _form(request)
    s, mail = await SS.create(db, answers, files, investor=investor)
    await audit(db, None, "suggestion.sent", "suggestion", s.reference, request=request, actor_label=f"Holder {investor.internal_number}",
                summary=f"An investor sent an idea ({SS.topic_label(s.topic)})")
    await db.commit()
    await SS.deliver(mail)
    return {"reference": s.reference, "id": str(s.id), **await SS.mine(db, investor=investor)}


@router.post("/investor/suggestions/{suggestion_id}/reply")
async def investor_answer(suggestion_id: uuid.UUID, body: SenderReplyIn, investor: Investor = Depends(current_investor),
                          db: AsyncSession = Depends(aget_db)):
    s = await _own(db, suggestion_id, investor_id=investor.id)
    users, invs = await SS.authors(db, [s])
    mail = await SS.sender_reply(db, s, body.body, SS.sender_label(s, users, invs))
    await db.commit()
    await SS.deliver(mail)
    return await SS.mine(db, investor=investor)


# Answering: Hub OS ---------------------------------------------------------------------------------------------

@router.get("/os/suggestions")
async def team_list(view: str = "todo", access: Access = Depends(team_desk), db: AsyncSession = Depends(aget_db)):
    if not SS.receives(access):
        return {"rows": [], "counts": {}, "late": 0, "receives": False, "is_founder": False, "reply_days": SS.REPLY_DAYS}
    return await SS.team_list(db, access, view)


async def _idea(db: AsyncSession, access: Access, suggestion_id: uuid.UUID, lock: bool = False) -> Suggestion:
    s = await db.get(Suggestion, suggestion_id, with_for_update=lock)
    if not s or not SS.can_see(access, s):
        raise HTTPException(404, "Idea not found.")
    return s


@contextlib.asynccontextmanager
async def _for_desk(access: Access, s: Suggestion, request: Request):
    desk = SS.acting_desk(access, s)
    if desk:
        async with acting_for(access, desk, request):
            yield
    else:
        yield


@router.get("/os/suggestions/{suggestion_id}")
async def open_idea(suggestion_id: uuid.UUID, request: Request, access: Access = Depends(team_desk), db: AsyncSession = Depends(aget_db)):
    s = await _idea(db, access, suggestion_id)
    return await SS.team_detail(db, s, access, _base(request))


async def _done(db: AsyncSession, access: Access, s: Suggestion, request: Request, mail: list[dict], action: str, summary: str) -> dict:
    await audit(db, access.user, f"suggestion.{action}", "suggestion", s.reference, request=request, summary=summary)
    await db.commit()
    await SS.deliver(mail)
    return await SS.team_detail(db, s, access, _base(request))


class TeamReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=4000)
    to_sender: bool = True


@router.post("/os/suggestions/{suggestion_id}/reply")
async def team_reply(suggestion_id: uuid.UUID, body: TeamReplyIn, request: Request, access: Access = Depends(team_desk),
                     db: AsyncSession = Depends(aget_db)):
    s = await _idea(db, access, suggestion_id, lock=True)
    async with _for_desk(access, s, request):
        mail = await SS.reply(db, s, access, body.body, body.to_sender)
        return await _done(db, access, s, request, mail, "replied" if body.to_sender else "note",
                           f"{'Answered' if body.to_sender else 'Note on'} idea {s.reference}")


@router.post("/os/suggestions/{suggestion_id}/looking")
async def mark_looking(suggestion_id: uuid.UUID, request: Request, access: Access = Depends(team_desk), db: AsyncSession = Depends(aget_db)):
    s = await _idea(db, access, suggestion_id, lock=True)
    async with _for_desk(access, s, request):
        SS.looking(s, access)
        return await _done(db, access, s, request, [], "looking", f"Looking at idea {s.reference}")


class MoveIn(BaseModel):
    department: str = Field(max_length=20)


@router.post("/os/suggestions/{suggestion_id}/move")
async def move(suggestion_id: uuid.UUID, body: MoveIn, request: Request, access: Access = Depends(team_desk), db: AsyncSession = Depends(aget_db)):
    s = await _idea(db, access, suggestion_id, lock=True)
    async with _for_desk(access, s, request):
        mail = await SS.move(db, s, access, body.department)
        out = await _done(db, access, s, request, mail, "moved", f"Moved idea {s.reference} to {SS.desks_label(s)}")
    # Moved away from this person's desks: they can't open it any more
    return out if SS.can_see(access, s) else {"id": str(s.id), "moved_away": True, "for": SS.desks_label(s)}


class MergeIn(BaseModel):
    into: str = Field(min_length=3, max_length=64)  # an idea's id or reference


@router.post("/os/suggestions/{suggestion_id}/merge")
async def merge(suggestion_id: uuid.UUID, body: MergeIn, request: Request, access: Access = Depends(team_desk), db: AsyncSession = Depends(aget_db)):
    s = await _idea(db, access, suggestion_id, lock=True)
    key = body.into.strip()
    try:
        into = await db.get(Suggestion, uuid.UUID(key))
    except ValueError:
        into = (await db.execute(select(Suggestion).where(Suggestion.reference == key.upper()))).scalar_one_or_none()
    if not into or not SS.can_see(access, into):
        raise HTTPException(404, "We couldn't find that idea.")
    async with _for_desk(access, s, request):
        mail = await SS.merge(db, s, into, access)
        return await _done(db, access, s, request, mail, "merged", f"Merged idea {s.reference} into {into.reference}")


class CloseIn(BaseModel):
    reason: str = Field(min_length=1, max_length=600)


@router.post("/os/suggestions/{suggestion_id}/close")
async def close(suggestion_id: uuid.UUID, body: CloseIn, request: Request, access: Access = Depends(team_desk), db: AsyncSession = Depends(aget_db)):
    s = await _idea(db, access, suggestion_id, lock=True)
    async with _for_desk(access, s, request):
        mail = await SS.close(db, s, access, body.reason)
        return await _done(db, access, s, request, mail, "closed", f"Closed idea {s.reference} as Not now")


@router.post("/os/suggestions/{suggestion_id}/reopen")
async def reopen(suggestion_id: uuid.UUID, request: Request, access: Access = Depends(team_desk), db: AsyncSession = Depends(aget_db)):
    s = await _idea(db, access, suggestion_id, lock=True)
    mail = await SS.reopen(db, s, access)
    return await _done(db, access, s, request, mail, "reopened", f"Reopened idea {s.reference}")


class RecommendIn(BaseModel):
    action: str = Field(min_length=1, max_length=1000)
    owner_user_id: str = Field(min_length=1, max_length=64)
    due_on: date
    cost: float = Field(default=0, ge=0, le=10_000_000)  # GHS
    effort: str = Field(max_length=10)
    benefit: Optional[str] = Field(default=None, max_length=600)


@router.post("/os/suggestions/{suggestion_id}/recommend")
async def recommend(suggestion_id: uuid.UUID, body: RecommendIn, request: Request, access: Access = Depends(team_desk),
                    db: AsyncSession = Depends(aget_db)):
    s = await _idea(db, access, suggestion_id, lock=True)
    async with _for_desk(access, s, request):
        mail = await SS.recommend(db, s, access, action=body.action, owner_user_id=body.owner_user_id, due_on=body.due_on,
                                  cost_minor=round(body.cost * 100), effort=body.effort, benefit=body.benefit)
        return await _done(db, access, s, request, mail, "recommended",
                           f"{'Planned' if s.status == 'planned' else 'Recommended'} idea {s.reference}: {body.action.strip()[:120]}")


class DecideIn(BaseModel):
    verdict: str = Field(pattern="^(approve|decline|ask)$")
    note: Optional[str] = Field(default=None, max_length=1000)
    owner_user_id: Optional[str] = Field(default=None, max_length=64)
    due_on: Optional[date] = None
    cost: Optional[float] = Field(default=None, ge=0, le=10_000_000)


@router.post("/os/suggestions/{suggestion_id}/decide")
async def decide(suggestion_id: uuid.UUID, body: DecideIn, request: Request, access: Access = Depends(team_desk),
                 db: AsyncSession = Depends(aget_db)):
    if not access.is_founder:
        raise HTTPException(403, "Only the Founder decides ideas.")
    s = await _idea(db, access, suggestion_id, lock=True)
    if body.verdict == "approve":
        mail = await SS.approve(db, s, access.user, owner_user_id=body.owner_user_id, due_on=body.due_on,
                                cost_minor=round(body.cost * 100) if body.cost is not None else None)
    elif body.verdict == "decline":
        mail = await SS.decline(db, s, access.user, body.note or "")
    else:
        mail = await SS.send_back(db, s, access.user, body.note or "")
    word = {"approve": "Approved", "decline": "Declined", "ask": "Sent back"}[body.verdict]
    return await _done(db, access, s, request, mail, f"decided_{body.verdict}", f"{word} idea {s.reference}")
