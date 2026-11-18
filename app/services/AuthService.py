"""Sign-in: password plus a one-time code for everyone with a desk or a portal, Holder ID plus a
one-time code for investors, invites, password resets and rate limits."""

import secrets
import uuid
from datetime import timedelta
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import lookup_hash, normalise_contact
from app.core.security import CODE_MINUTES, client_ip, codes_match, hash_code, new_code, now
from app.models.investors import Investor
from app.models.people import Department, LoginChallenge, SignInAttempt, User
from app.services import EmailService
from app.services.AuditService import audit

ph = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
HOLDER_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O or 1/I
_DUMMY_HASH = ph.hash("SHXX-XXXX-XXXX")

TWO_STEP_ROLES = {"founder", "cofounder", "dept_head", "finance", "staff", "streamer", "designer", "clipper"}
IP_LIMIT, CONTACT_LIMIT, WINDOW_MINUTES = 30, 8, 15
VAULT_LOCK_AFTER, VAULT_LOCK_MINUTES = 5, 15


def new_holder_id() -> str:
    """12 characters from a 32-letter alphabet (60 bits) from the OS's CSPRNG, shown as XXXX-XXXX-XXXX."""
    raw = "".join(secrets.choice(HOLDER_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def clean_holder_id(value: str) -> str:
    raw = "".join(c for c in (value or "").upper() if c.isalnum())
    raw = raw.replace("O", "0").replace("I", "1")  # never valid, so a typo of these fails cleanly
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:12]}" if len(raw) == 12 else raw


def hash_holder_id(holder_id: str) -> str:
    return ph.hash(clean_holder_id(holder_id))


def holder_id_matches(holder_id: str, hashed: Optional[str]) -> bool:
    try:
        return ph.verify(hashed or _DUMMY_HASH, clean_holder_id(holder_id)) and bool(hashed)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return f"{name[:2]}{'•' * max(1, len(name) - 2)}@{domain}"


async def check_rate(db: AsyncSession, request: Request, kind: str, contact: str) -> None:
    since = now() - timedelta(minutes=WINDOW_MINUTES)
    ip = client_ip(request)
    by_ip = await db.scalar(select(func.count()).select_from(SignInAttempt).where(
        SignInAttempt.ip == ip, SignInAttempt.ok.is_(False), SignInAttempt.created_at >= since))
    by_contact = await db.scalar(select(func.count()).select_from(SignInAttempt).where(
        SignInAttempt.contact == contact, SignInAttempt.kind == kind, SignInAttempt.ok.is_(False),
        SignInAttempt.created_at >= since))
    if (by_ip or 0) >= IP_LIMIT or (by_contact or 0) >= CONTACT_LIMIT:
        raise HTTPException(429, "Too many tries. Wait 15 minutes and try again.")


async def record_attempt(db: AsyncSession, request: Request, kind: str, contact: str, ok: bool) -> None:
    db.add(SignInAttempt(kind=kind, contact=contact, ip=client_ip(request), ok=ok))
    await db.flush()


async def issue_code(db: AsyncSession, request: Request, *, purpose: str, email: str, name: str,
                     user_id: Optional[uuid.UUID] = None, investor_id: Optional[uuid.UUID] = None,
                     session_id: Optional[uuid.UUID] = None) -> LoginChallenge:
    code = new_code()
    challenge = LoginChallenge(user_id=user_id, investor_id=investor_id, session_id=session_id, purpose=purpose,
                               code_hash=hash_code(code), expires_at=now() + timedelta(minutes=CODE_MINUTES),
                               ip=client_ip(request))
    db.add(challenge)
    await db.flush()
    await db.commit()
    if not await EmailService.send_code(email, name, code, purpose):
        raise HTTPException(503, "We couldn't send your code just now. Try again in a minute.")
    return challenge


async def use_code(db: AsyncSession, challenge_id: str, code: str, purpose: str) -> LoginChallenge:
    try:
        challenge = await db.get(LoginChallenge, uuid.UUID(str(challenge_id)))
    except ValueError:
        challenge = None
    if not challenge or challenge.purpose != purpose or challenge.used_at:
        raise HTTPException(400, "That code has expired. Ask for a new one.")
    if challenge.expires_at <= now():
        raise HTTPException(400, "That code has expired. Ask for a new one.")
    if challenge.attempts >= 5:
        raise HTTPException(429, "Too many wrong codes. Ask for a new one.")
    if not codes_match(code, challenge.code_hash):
        challenge.attempts += 1
        await db.commit()
        left = 5 - challenge.attempts
        raise HTTPException(400, f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left." if left
                            else "Too many wrong codes. Ask for a new one.")
    challenge.used_at = now()
    return challenge


async def find_investor_by_contact(db: AsyncSession, contact: str) -> Optional[Investor]:
    value = normalise_contact(contact)
    if "@" in value:
        return (await db.execute(select(Investor).where(func.lower(Investor.email) == value))).scalar_one_or_none()
    if not value:
        return None
    return (await db.execute(select(Investor).where(Investor.phone_hash == lookup_hash(value)))).scalar_one_or_none()


async def lock_vault(db: AsyncSession, investor: Investor, request: Request) -> None:
    investor.locked_until = now() + timedelta(minutes=VAULT_LOCK_MINUTES)
    investor.failed_attempts = 0
    await audit(db, None, "vault.locked", "investor", investor.id, request=request,
                summary=f"{investor.internal_number}: vault locked for {VAULT_LOCK_MINUTES} minutes after "
                        f"{VAULT_LOCK_AFTER} wrong tries", actor_label="system")
    strategy = (await db.execute(select(Department).where(Department.slug == "strategy"))).scalar_one_or_none()
    to = []
    if strategy:
        to = list((await db.execute(select(User.email).where(User.department_id == strategy.id,
                                                             User.status == "active"))).scalars())
    founders = list((await db.execute(select(User.email).where(User.role == "founder", User.status == "active"))).scalars())
    await db.commit()
    await EmailService.notify(sorted(set(to + founders)), f"Vault locked · {investor.internal_number}",
                              "An investor vault was locked",
                              [f"Holder {investor.internal_number} (ID ending {investor.holder_id_last4 or '????'}) "
                               f"had {VAULT_LOCK_AFTER} wrong sign-in tries. The vault is locked for "
                               f"{VAULT_LOCK_MINUTES} minutes.",
                               "If the holder didn't do this, call them and consider issuing a new Holder ID."],
                              ("Open Investor relations", "/os/capital"))


async def use_code_for_resend(db: AsyncSession, challenge_id: str, purpose: str = "login") -> LoginChallenge:
    """Retire a pending code so a new one can be sent. At most one resend every 30 seconds."""
    try:
        challenge = await db.get(LoginChallenge, uuid.UUID(str(challenge_id)))
    except ValueError:
        challenge = None
    if not challenge or challenge.purpose != purpose or challenge.used_at:
        raise HTTPException(400, "Start again from the sign-in page.")
    if challenge.created_at > now() - timedelta(seconds=30):
        raise HTTPException(429, "Wait a few seconds before asking for another code.")
    challenge.used_at = now()
    await db.flush()
    return challenge
