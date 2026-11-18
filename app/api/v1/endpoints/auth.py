"""Sign in, one-time codes, sign out, invites and password resets for staff, streamers, designers and
clippers. Investors sign in through /auth/investor/* (see vault_auth.py)."""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from app.core.types import Email
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AREAS, NAV_GROUPS, OS_ROLES, load_access
from app.core.database import aget_db
from app.core.security import (OS_COOKIE, clear_cookie, get_current_user, get_session, hash_password, hash_token,
                               new_token, now, read_session, start_session, user_by_email, verify_password)
from app.models.people import AuthSession, OnboardingRun, User
from app.services import AuthService, EmailService
from app.services.AuditService import audit
from app.services.NavService import ACCESS_LABEL, footer, nav_counts

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=200)


class CodeIn(BaseModel):
    challenge_id: str
    code: str = Field(min_length=6, max_length=6)


class ResendIn(BaseModel):
    challenge_id: str


class PasswordIn(BaseModel):
    token: str
    password: str = Field(min_length=10, max_length=200)


class ForgotIn(BaseModel):
    email: Email


def initials(name: str) -> str:
    """Two letters for an avatar. Brackets and punctuation are skipped: "Lamar (Galacta)" is LA, "Founder & President" FP."""
    import re

    parts = re.findall(r"[^\W_]+", re.sub(r"\(.*?\)", " ", name or ""))
    if not parts:
        return "SH"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


async def me_payload(db: AsyncSession, user: User) -> dict:
    access = await load_access(db, user)
    counts = await nav_counts(db, access) if user.role in OS_ROLES else {}
    nav = []
    for group in NAV_GROUPS:
        items = [{"area": a, "label": AREAS[a][0], "href": AREAS[a][1], "count": counts.get(a), "step_in": a in access.stepped_in}
                 for a in access.areas if AREAS[a][2] == group]
        if items:
            nav.append({"group": group, "items": items})
    onboarding = (await db.execute(select(OnboardingRun).where(OnboardingRun.user_id == user.id))).scalars().first()
    due = None
    if not onboarding or onboarding.completed_at:  # during onboarding it is signed in step 2 instead
        from app.services.AgreementService import waiting_for

        waiting = await waiting_for(db, user.id)
        due = {"id": str(waiting.id), "title": waiting.title, "stage": waiting.stage} if waiting else None
    return {
        "id": str(user.id), "name": user.name, "email": user.email, "handle": user.handle, "role": user.role,
        "initials": initials(user.handle or user.name), "avatar_url": user.avatar_url,
        "department": access.department, "desk": access.desk, "title": access.title, "areas": access.areas,
        "review_lists": access.review_lists, "home": access.home, "nav": nav, "stepped_in": access.stepped_in,
        "access_label": ACCESS_LABEL.get(access.desk or "", "Hub OS") if user.role in OS_ROLES else None,
        "footer": await footer(db, access) if user.role in OS_ROLES else None,
        "onboarding": None if not onboarding else {
            "kind": onboarding.kind, "step": onboarding.current_step, "done": onboarding.completed_at is not None},
        "agreement_due": due,
    }


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, db: AsyncSession = Depends(aget_db)):
    email = body.email.lower()
    await AuthService.check_rate(db, request, "os", email)
    user = await user_by_email(db, email)
    if user and user.locked_until and user.locked_until > now():
        raise HTTPException(423, "This account is locked for a few minutes after too many tries.")
    if not user or user.role == "investor" or user.is_sample or not verify_password(body.password, user.password_hash):
        await AuthService.record_attempt(db, request, "os", email, False)
        if user:
            user.failed_logins += 1
            if user.failed_logins >= 8:
                user.locked_until, user.failed_logins = now() + timedelta(minutes=15), 0
        await db.commit()
        raise HTTPException(401, "That email and password don't match.")
    if user.status != "active":
        raise HTTPException(403, "This account is not active yet. Use the link in your invite email.")
    user.failed_logins = 0
    if user.role in AuthService.TWO_STEP_ROLES:
        challenge = await AuthService.issue_code(db, request, purpose="login", email=user.email, name=user.name,
                                                 user_id=user.id)
        return {"step": "code", "challenge_id": str(challenge.id), "sent_to": AuthService.mask_email(user.email)}
    await AuthService.record_attempt(db, request, "os", email, True)
    await start_session(db, response, request, kind="portal", user_id=user.id)
    user.last_login_at = now()
    await db.commit()
    return {"step": "done", "user": await me_payload(db, user)}


@router.post("/otp/verify")
async def verify_code(body: CodeIn, request: Request, response: Response, db: AsyncSession = Depends(aget_db)):
    challenge = await AuthService.use_code(db, body.challenge_id, body.code, "login")
    user = await db.get(User, challenge.user_id)
    if not user or user.status != "active":
        raise HTTPException(403, "This account is not active.")
    await AuthService.record_attempt(db, request, "os", user.email, True)
    await start_session(db, response, request, kind="os" if user.role in OS_ROLES else "portal", user_id=user.id)
    user.last_login_at = now()
    await audit(db, user, "auth.sign_in", "user", user.id, request=request, summary="Signed in with a one-time code")
    await db.commit()
    return {"step": "done", "user": await me_payload(db, user)}


@router.post("/otp/resend")
async def resend_code(body: ResendIn, request: Request, db: AsyncSession = Depends(aget_db)):
    old = await AuthService.use_code_for_resend(db, body.challenge_id)
    user = await db.get(User, old.user_id)
    challenge = await AuthService.issue_code(db, request, purpose="login", email=user.email, name=user.name,
                                             user_id=user.id)
    return {"step": "code", "challenge_id": str(challenge.id), "sent_to": AuthService.mask_email(user.email)}


@router.post("/logout")
async def logout(request: Request, response: Response, db: AsyncSession = Depends(aget_db)):
    session = await read_session(request, db, OS_COOKIE)
    if session:
        session.revoked_at = now()
        await db.commit()
    clear_cookie(response, OS_COOKIE)
    return {"ok": True}


@router.get("/me")
async def me(user: User = Depends(get_current_user), db: AsyncSession = Depends(aget_db)):
    return await me_payload(db, user)


@router.post("/sessions/end-others")
async def end_other_sessions(session: AuthSession = Depends(get_session), db: AsyncSession = Depends(aget_db)):
    rows = (await db.execute(select(AuthSession).where(AuthSession.user_id == session.user_id,
                                                       AuthSession.id != session.id,
                                                       AuthSession.revoked_at.is_(None)))).scalars().all()
    for row in rows:
        row.revoked_at = now()
    await db.commit()
    return {"ended": len(rows)}


async def _user_for_token(db: AsyncSession, token: str) -> User:
    user = (await db.execute(select(User).where(User.invite_token_hash == hash_token(token)))).scalar_one_or_none()
    if not user or not user.invite_expires_at or user.invite_expires_at <= now():
        raise HTTPException(400, "This link has expired. Ask for a new one.")
    return user


@router.get("/invite/{token}")
async def read_invite(token: str, db: AsyncSession = Depends(aget_db)):
    user = await _user_for_token(db, token)
    return {"name": user.name, "email": user.email, "role": user.role, "first_time": user.password_hash is None}


@router.post("/invite/accept")
async def accept_invite(body: PasswordIn, request: Request, db: AsyncSession = Depends(aget_db)):
    user = await _user_for_token(db, body.token)
    user.password_hash = hash_password(body.password)
    if user.status == "invited":
        user.status = "active"
    user.invite_token_hash = user.invite_expires_at = None
    await audit(db, user, "auth.password_set", "user", user.id, request=request, summary="Set a password from a link")
    await db.commit()
    return {"ok": True, "email": user.email}


@router.post("/password/forgot")
async def forgot(body: ForgotIn, request: Request, db: AsyncSession = Depends(aget_db)):
    await AuthService.check_rate(db, request, "reset", body.email.lower())
    await AuthService.record_attempt(db, request, "reset", body.email.lower(), False)
    user = await user_by_email(db, body.email)
    if user and user.status == "active" and user.role != "investor":
        token = new_token()
        user.invite_token_hash, user.invite_expires_at = hash_token(token), now() + timedelta(hours=1)
        await db.commit()
        await EmailService.send_reset(user.email, user.name, f"/sign-in/set-password?token={token}")
    else:
        await db.commit()
    return {"ok": True}  # the same answer either way, so nobody can test which emails have accounts


async def issue_invite(db: AsyncSession, user: User, kind: str) -> str:
    """Give a user a 7-day link to set their password, and email it."""
    token = new_token()
    user.invite_token_hash, user.invite_expires_at = hash_token(token), now() + timedelta(days=7)
    await db.flush()
    await EmailService.send_invite(user.email, user.name, kind, f"/sign-in/set-password?token={token}&welcome=1")
    return token
