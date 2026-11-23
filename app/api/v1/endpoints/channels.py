"""Channel earnings: the streamer's monthly report on their own channels (from the Talent desk) and Finance's check.

See ChannelService for why: the streamer's own channels pay the streamer, who passes the payouts to the label.
"""

import json
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints.desks import _streamer_for
from app.core.access import Access, require_area
from app.core.database import aget_db
from app.core.security import now
from app.models.talent import ChannelReport, Streamer
from app.services import ChannelService as C
from app.services import S3Service
from app.services.AuditService import audit
from app.services.money import ghs, month_label

router = APIRouter(tags=["channels"])
talent_desk = require_area("talent")
finance_desk = require_area("finance")


class LineIn(BaseModel):
    platform: str
    earned: float = Field(ge=0, le=10_000_000)
    earned_currency: str = "USD"
    paid: float = Field(default=0, ge=0, le=10_000_000)
    paid_currency: str = "GHS"
    file: Optional[int] = Field(default=None, ge=0)  # which of the files sent with the report is this line's statement
    statement_key: Optional[str] = Field(default=None, max_length=300)  # or keep the statement already on this report


class ReportIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    lines: list[LineIn] = Field(min_length=1, max_length=8)
    passed: float = Field(default=0, ge=0, le=10_000_000)
    transfer_ref: Optional[str] = Field(default=None, max_length=80)
    note: Optional[str] = Field(default=None, max_length=500)


def _bad(message: str, field: str):
    raise HTTPException(400, {"message": message, "missing": [field]})


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


@router.get("/talent/channel-reports")
async def my_reports(request: Request, streamer_id: Optional[uuid.UUID] = None, access: Access = Depends(talent_desk),
                     db: AsyncSession = Depends(aget_db)):
    s = await _streamer_for(db, access, streamer_id)
    rows = (await db.execute(select(ChannelReport).where(ChannelReport.streamer_id == s.id)
                             .order_by(ChannelReport.month.desc()).limit(12))).scalars().all()
    due = C.due_month(s, {r.month for r in rows})
    return {"can_send": access.user.role == "streamer",
            "due": {"month": due, "label": month_label(due)} if due else None,
            "months": [{"month": m, "label": month_label(m)} for m in C.months_open(s)],
            "platforms": [{"key": k, "label": v} for k, v in C.PLATFORMS.items()],
            "reports": [await C.out(db, r, _base(request), s) for r in rows]}


@router.post("/talent/channel-reports")
async def send_report(request: Request, report: str = Form(...), files: list[UploadFile] = File(default=[]),
                      access: Access = Depends(talent_desk), db: AsyncSession = Depends(aget_db)):
    """Send (or correct and send again) a month's report. Each channel needs its statement or a screenshot."""
    if access.user.role != "streamer":
        raise HTTPException(403, "Only the streamer sends the report for their own channels.")
    try:
        body = ReportIn.model_validate(json.loads(report))
    except ValueError:  # bad JSON or a field out of range
        raise HTTPException(400, "Check the amounts on the report and try again.")
    s = await _streamer_for(db, access, None)
    if body.month not in C.months_open(s):
        _bad("Pick a month from the list.", "month")
    existing = (await db.execute(select(ChannelReport).where(ChannelReport.streamer_id == s.id, ChannelReport.month == body.month)
                                 .with_for_update())).scalar_one_or_none()
    if existing and existing.status == "confirmed":
        raise HTTPException(409, f"Finance has already confirmed your {month_label(body.month)} report.")
    kept = {ln["statement_key"]: ln.get("statement_name") for ln in (existing.lines if existing else []) if ln.get("statement_key")}

    # Check everything before storing any file
    seen, used, checked = set(), set(), []
    for ln in body.lines:
        if ln.platform not in C.PLATFORMS:
            _bad("Pick each channel's platform from the list.", "platform")
        name = C.PLATFORMS[ln.platform]
        if ln.platform in seen and ln.platform != "other":
            _bad(f"{name} is on the report twice. Put each channel on one line.", "platform")
        seen.add(ln.platform)
        if ln.earned_currency not in C.CURRENCIES or ln.paid_currency not in C.CURRENCIES:
            _bad("Amounts are in dollars or cedis.", "currency")
        upload = None
        if ln.file is not None:
            if ln.file >= len(files) or ln.file in used:
                _bad(f"Add the {name} statement again.", "statement")
            used.add(ln.file)
            f = files[ln.file]
            data = await f.read()
            try:
                S3Service.check_file(data, f.content_type or "", ("image", "document"))
            except S3Service.UploadRejected as exc:
                raise HTTPException(400, {"message": f"{name} statement: {exc}", "missing": ["statement"]})
            upload = (f, data)
        elif not (ln.statement_key and ln.statement_key in kept):
            _bad(f"Add the {name} statement, or a screenshot of its earnings page.", "statement")
        checked.append((ln, upload))
    paid_any = any(ln.paid for ln in body.lines)
    passed_minor = round(body.passed * 100)
    ref = (body.transfer_ref or "").strip() or None
    if paid_any and not passed_minor:
        _bad("A channel paid you this month. Say how much you passed to the label.", "passed")
    if passed_minor and not ref:
        _bad("Add the mobile money or bank reference for what you passed to the label.", "transfer_ref")

    lines = []
    for ln, upload in checked:
        if upload:
            f, data = upload
            key = S3Service.put("channel-statements", f.filename or "statement", data, f.content_type or "application/octet-stream")
            filename = (f.filename or "statement")[:120]
        else:
            key, filename = ln.statement_key, kept[ln.statement_key]
        lines.append({"platform": ln.platform, "earned_minor": round(ln.earned * 100), "earned_currency": ln.earned_currency,
                      "paid_minor": round(ln.paid * 100), "paid_currency": ln.paid_currency,
                      "statement_key": key, "statement_name": filename})
    resent = existing is not None
    r = existing or ChannelReport(streamer_id=s.id, month=body.month)
    r.lines, r.passed_minor, r.transfer_ref, r.note = lines, passed_minor, ref, (body.note or "").strip() or None
    r.status, r.submitted_by, r.submitted_at = "submitted", access.user.id, now()
    if not existing:
        db.add(r)
    await db.flush()
    await audit(db, access.user, "channel_report.sent", "channel_report", r.id, request=request,
                summary=f"{s.stage_name} · {month_label(r.month)} · passed on {ghs(passed_minor)}")
    await db.commit()
    await C.tell_finance(db, r, s, resent)
    return await C.out(db, r, _base(request), s)


@router.get("/finance/channel-reports")
async def for_finance(request: Request, access: Access = Depends(finance_desk), db: AsyncSession = Depends(aget_db)):
    waiting = (await db.execute(select(ChannelReport).where(ChannelReport.status == "submitted")
                                .order_by(ChannelReport.submitted_at))).scalars().all()
    recent = (await db.execute(select(ChannelReport).where(ChannelReport.status != "submitted")
                               .order_by(ChannelReport.updated_at.desc()).limit(12))).scalars().all()
    base = _base(request)
    return {"can_check": access.does_finance, "waiting": [await C.out(db, r, base) for r in waiting],
            "recent": [await C.out(db, r, base) for r in recent]}


async def _waiting(db: AsyncSession, access: Access, report_id: uuid.UUID) -> ChannelReport:
    if not access.does_finance:
        raise HTTPException(403, "Only Finance checks channel earnings.")
    r = await db.get(ChannelReport, report_id, with_for_update=True)
    if not r:
        raise HTTPException(404, "Report not found.")
    if r.status != "submitted":
        raise HTTPException(409, "This report isn't waiting for a check.")
    return r


class ConfirmIn(BaseModel):
    received: float = Field(ge=0, le=10_000_000)
    note: Optional[str] = Field(default=None, max_length=500)


@router.post("/finance/channel-reports/{report_id}/confirm")
async def confirm(report_id: uuid.UUID, body: ConfirmIn, request: Request, access: Access = Depends(finance_desk),
                  db: AsyncSession = Depends(aget_db)):
    """Finance confirms what the label received. It counts as the streamer's platform income this month."""
    r = await _waiting(db, access, report_id)
    received = round(body.received * 100)
    if received != r.passed_minor and not (body.note or "").strip():
        _bad(f"That's different from the {ghs(r.passed_minor)} the streamer says they passed on. Say why in a note.", "note")
    s = await db.get(Streamer, r.streamer_id)
    await C.confirm(db, r, received, access.user, body.note)
    await audit(db, access.user, "channel_report.confirmed", "channel_report", r.id, request=request,
                summary=f"{s.stage_name} · {month_label(r.month)} · the label received {ghs(received)}")
    await db.commit()
    await C.tell_streamer(db, r, s)
    return await C.out(db, r, _base(request), s)


class BackIn(BaseModel):
    note: str = Field(min_length=3, max_length=500)


@router.post("/finance/channel-reports/{report_id}/send-back")
async def send_back(report_id: uuid.UUID, body: BackIn, request: Request, access: Access = Depends(finance_desk),
                    db: AsyncSession = Depends(aget_db)):
    r = await _waiting(db, access, report_id)
    s = await db.get(Streamer, r.streamer_id)
    r.status, r.review_note, r.reviewed_by, r.reviewed_at = "returned", body.note.strip(), access.user.id, now()
    await audit(db, access.user, "channel_report.sent_back", "channel_report", r.id, request=request,
                summary=f"{s.stage_name} · {month_label(r.month)}: {body.note.strip()}")
    await db.commit()
    await C.tell_streamer(db, r, s)
    return await C.out(db, r, _base(request), s)
