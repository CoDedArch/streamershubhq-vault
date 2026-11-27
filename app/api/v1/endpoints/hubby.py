"""Hubby, the helper in Hub OS, the Designer Studio, the Clipper Desk and the Investor Vault.

GET /hubby/guide is everything the website needs for the offline guide, plus the current mode. POST /hubby/ask
streams the AI's answer as it's written when AI is available; when it isn't, or it fails, it says why and the
website answers from the guide. Either way the question is kept (its text for 30 days) for the Founder's view of
what people ask.
"""

import json
import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import OS_ROLES, Access, load_access, require_founder
from app.core.database import aget_db, session_manager
from app.core.security import OS_COOKIE, VAULT_COOKIE, read_session
from app.models.investors import Investor
from app.models.people import User
from app.services import HubbyService
from app.services.hubby_guide import DEFAULT_STARTERS, STARTERS

router = APIRouter(prefix="/hubby", tags=["hubby"])
logger = logging.getLogger(__name__)


async def _os_asker(request: Request, db: AsyncSession) -> Optional[HubbyService.Asker]:
    session = await read_session(request, db, OS_COOKIE)
    if not session or not session.user_id:
        return None
    user = await db.get(User, session.user_id)
    if not user or user.status != "active":
        return None
    if user.role in OS_ROLES:
        access = await load_access(db, user)
        return HubbyService.Asker(role=user.role, name=user.name, title=access.title, department=access.department,
                                  user_id=user.id, access=access)
    if user.role in ("designer", "clipper"):
        return HubbyService.Asker(role=user.role, name=user.handle or user.name, user_id=user.id, portal=user.role)
    return None


async def _vault_asker(request: Request, db: AsyncSession) -> Optional[HubbyService.Asker]:
    session = await read_session(request, db, VAULT_COOKIE)
    if not session or session.kind != "vault" or not session.investor_id:
        return None
    investor = await db.get(Investor, session.investor_id)
    if not investor or investor.status != "active":
        return None
    return HubbyService.Asker(role="investor", name=investor.legal_name, investor_id=investor.id, portal="investor")


async def current_asker(request: Request, db: AsyncSession = Depends(aget_db)) -> HubbyService.Asker:
    """Whoever is signed in where the question is asked: the vault's own session on the Investor Vault pages."""
    order = (_vault_asker, _os_asker) if request.query_params.get("in") == "vault" else (_os_asker, _vault_asker)
    for find in order:
        asker = await find(request, db)
        if asker:
            return asker
    raise HTTPException(401, "Please sign in.")


def _starters(page: str) -> list[str]:
    page = (page or "").split("?")[0].rstrip("/") or "/"
    return STARTERS.get(page) or STARTERS.get("/" + "/".join(page.strip("/").split("/")[:2])) or DEFAULT_STARTERS


@router.get("/status")
async def mode(asker: HubbyService.Asker = Depends(current_asker), db: AsyncSession = Depends(aget_db)):
    mode, reason = await HubbyService.status(db, asker)
    return {"mode": mode, "reason": reason}


@router.get("/guide")
async def guide(page: str = "", asker: HubbyService.Asker = Depends(current_asker), db: AsyncSession = Depends(aget_db)):
    mode, reason = await HubbyService.status(db, asker)
    out = await HubbyService.guide_for(db, asker)
    return {"mode": mode, "reason": reason, "first_name": asker.first_name, "starters": _starters(page), **out,
            "fallback": "Messages" if asker.portal == "investor" else ("Ideas" if asker.portal else "Suggestions")}


class Turn(BaseModel):
    q: str = Field(default="", max_length=300)
    a: str = Field(default="", max_length=1200)


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=300)
    page: str = Field(default="", max_length=200)
    history: list[Turn] = Field(default_factory=list, max_length=6)


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


@router.post("/ask")
async def ask(body: AskIn, asker: HubbyService.Asker = Depends(current_asker), db: AsyncSession = Depends(aget_db)):
    """The answer as a stream of server-sent events: start, then either guide (answer from the guide, with why) or
    delta pieces of the AI's answer followed by done, or error if it broke off (answer from the guide)."""
    if await HubbyService.too_many_today(db, asker):
        raise HTTPException(429, "That's a lot of questions for one day. Hubby's guide still works: search it below.")
    question = body.question.strip()
    start = await HubbyService.begin(db, asker, question, body.page)
    await db.commit()
    history = [t.model_dump() for t in body.history]

    async def events():
        qid = str(start["id"])
        yield _event("start", {"id": qid, "mode": start["mode"]})
        if start["mode"] != "ai":
            yield _event("guide", {"id": qid, "reason": start["reason"]})
            return
        row: dict = {}
        try:
            async for kind, data in HubbyService.answer_stream(start, asker, question, body.page, history):
                if kind == "delta":
                    yield _event("delta", {"t": data})
                    continue
                row = data.pop("row", {})
                data.pop("summary", None)
                yield _event(kind, {"id": qid, **data})
        finally:
            if row:  # the question's numbers: written in a session of its own, as the request's has closed
                try:
                    async with session_manager.session_factory() as s:
                        await HubbyService.record(s, start["id"], row)
                        await s.commit()
                except Exception:
                    logger.exception("Could not record Hubby's answer")

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


class FeedbackIn(BaseModel):
    helpful: Optional[bool] = None


@router.post("/questions/{question_id}/feedback")
async def feedback(question_id: uuid.UUID, body: FeedbackIn, asker: HubbyService.Asker = Depends(current_asker),
                   db: AsyncSession = Depends(aget_db)):
    if not await HubbyService.feedback(db, asker, question_id, body.helpful):
        raise HTTPException(404, "That answer isn't there any more.")
    await db.commit()
    return {"ok": True}


@router.get("/overview")
async def overview(access: Access = Depends(require_founder), db: AsyncSession = Depends(aget_db)):
    return await HubbyService.overview(db)
