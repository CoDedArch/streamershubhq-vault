"""Passwords, one-time codes, session cookies and the current-user dependencies.

A session is a row in auth_sessions; the cookie holds a signed token naming it. Signing out (or
"Lock vault") revokes the row, so a copied cookie stops working at once.
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import aget_db
from app.models.people import AuthSession, User

OS_COOKIE = "sh_session"
VAULT_COOKIE = "sh_vault"
CODE_MINUTES = 10


def now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=settings.SH_BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: Optional[str]) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), code.strip().encode(), hashlib.sha256).hexdigest()


def codes_match(code: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_code(code), hashed)


def new_token() -> str:
    """An unguessable link token (invites, password resets). Store only hash_token(token)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request: Request) -> str:
    """The caller's IP. Behind a load balancer set SH_PROXY_HOPS (usually 1): the address that many hops from
    the right of X-Forwarded-For was added by our own proxy, so it can't be faked to dodge rate limits."""
    hops = settings.SH_PROXY_HOPS
    forwarded = request.headers.get("x-forwarded-for")
    if hops > 0 and forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= hops:
            return parts[-hops][:64]
    return (request.client.host if request.client else "")[:64]


def _encode(session: AuthSession) -> str:
    return jwt.encode(
        {"sid": str(session.id), "k": session.kind, "exp": session.expires_at, "iat": now()},
        settings.SECRET_KEY, algorithm=settings.ALGORITHM,
    )


def _cookie_kwargs(max_age: int) -> dict:
    return dict(
        httponly=True, secure=settings.is_production, samesite="lax", max_age=max_age, path="/",
        domain=settings.SH_COOKIE_DOMAIN if settings.is_production else None,
    )


async def start_session(db: AsyncSession, response: Response, request: Request, *, kind: str,
                        user_id: Optional[uuid.UUID] = None, investor_id: Optional[uuid.UUID] = None) -> AuthSession:
    minutes = settings.SH_VAULT_SESSION_MINUTES if kind == "vault" else settings.SH_SESSION_HOURS * 60
    session = AuthSession(
        kind=kind, user_id=user_id, investor_id=investor_id, expires_at=now() + timedelta(minutes=minutes),
        ip=client_ip(request), user_agent=(request.headers.get("user-agent") or "")[:300],
        step_up_at=now() if kind == "vault" else None,
    )
    db.add(session)
    await db.flush()
    cookie = VAULT_COOKIE if kind == "vault" else OS_COOKIE
    response.set_cookie(cookie, _encode(session), **_cookie_kwargs(minutes * 60))
    return session


def clear_cookie(response: Response, cookie: str) -> None:
    response.delete_cookie(cookie, path="/", domain=settings.SH_COOKIE_DOMAIN if settings.is_production else None)


async def read_session(request: Request, db: AsyncSession, cookie: str) -> Optional[AuthSession]:
    token = request.cookies.get(cookie)
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        sid = uuid.UUID(payload["sid"])
    except (jwt.PyJWTError, KeyError, ValueError):
        return None
    session = await db.get(AuthSession, sid)
    if not session or session.revoked_at or session.expires_at <= now():
        return None
    return session


async def get_session(request: Request, db: AsyncSession = Depends(aget_db)) -> AuthSession:
    session = await read_session(request, db, OS_COOKIE)
    if not session or not session.user_id:
        raise HTTPException(401, "Please sign in.")
    return session


async def get_current_user(session: AuthSession = Depends(get_session), db: AsyncSession = Depends(aget_db)) -> User:
    user = await db.get(User, session.user_id)
    if not user:
        raise HTTPException(401, "Please sign in.")
    if user.status != "active":
        raise HTTPException(403, "This account is not active. Contact the Founder.")
    return user


async def get_optional_user(request: Request, db: AsyncSession = Depends(aget_db)) -> Optional[User]:
    session = await read_session(request, db, OS_COOKIE)
    if not session or not session.user_id:
        return None
    user = await db.get(User, session.user_id)
    return user if user and user.status == "active" else None


async def user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    return (await db.execute(select(User).where(User.email == email.strip().lower()))).scalar_one_or_none()
