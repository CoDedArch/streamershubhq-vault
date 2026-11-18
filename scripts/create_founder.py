"""Create the first Founder account on a new database, once, and email them their invite.

Every other account starts from inside the system (People → Invite, an approved application, or Capital
for investors), so someone has to exist before anyone can be invited. That someone is the Founder.

    ./venv/bin/python -m scripts.create_founder --name "Kelvin Gbolo" --email founder@streamershubmedia.com
    ./venv/bin/python -m scripts.create_founder --resend      # the invite expired before it was used

The invite goes out through SES like every other email: a 7-day link to set a password, then the usual
sign-in with an emailed code and staff onboarding. Co-founders are invited by the Founder from People.

If SES isn't set up yet, add --print-link to show the link here as well. Treat it like a password.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.core.database import async_sessionmaker, session_manager
from app.models.people import Department, User
from app.services.AuditService import audit
from app.services.EmailService import link
from app.services.MemberService import create_member

LABEL = "Setup script"


async def create(db, name: str, email: str, title: str) -> tuple[User, str]:
    from app.api.v1.endpoints.auth import issue_invite

    if await db.scalar(select(User).where(User.role == "founder")):
        raise SystemExit("This database already has a Founder. Invite everyone else from Hub OS → People, "
                         "or use --resend if the Founder's invite expired.")
    user = await create_member(db, name=name, email=email, role="founder", department="leadership", title=title,
                               desk="command", pay_type="equity_vesting", onboarding="staff")
    for dept in (await db.execute(select(Department).where(Department.slug.in_(("leadership", "talent"))))).scalars():
        dept.lead_user_id = dept.lead_user_id or user.id
    await audit(db, None, "people.founder_created", "user", user.id, actor_label=LABEL, summary=f"{user.name} · {title}")
    return user, await issue_invite(db, user, "staff")


async def resend(db) -> tuple[User, str]:
    from app.api.v1.endpoints.auth import issue_invite

    user = await db.scalar(select(User).where(User.role == "founder"))
    if not user:
        raise SystemExit("There's no Founder yet. Run this with --name and --email first.")
    if user.status != "invited":
        raise SystemExit("The Founder has already set a password. Use \"Forgot password\" on the sign-in page instead.")
    await audit(db, None, "people.invite_resent", "user", user.id, actor_label=LABEL, summary="Founder invite sent again")
    return user, await issue_invite(db, user, "staff")


async def main(args: argparse.Namespace) -> None:
    if not args.resend and not (args.name and args.email):
        raise SystemExit("Give --name and --email, or --resend.")
    await session_manager.init()  # creates the database, tables and departments if they don't exist yet
    try:
        async with async_sessionmaker(bind=session_manager.engine, expire_on_commit=False, autoflush=False)() as db:
            user, token = await resend(db) if args.resend else await create(db, args.name, args.email, args.title)
            await db.commit()
    finally:
        await session_manager.close()
    if settings.SH_EMAIL_MODE == "ses":
        print(f"Invite emailed to {user.email}. The link works once and expires in 7 days.")
    else:
        print(f"Invite created for {user.email}, but email mode is '{settings.SH_EMAIL_MODE}', so nothing was sent."
              + ("" if args.print_link else " Run again with --resend --print-link."))
    if args.print_link:
        print(f"Link (treat it like a password): {link(f'/sign-in/set-password?token={token}&welcome=1')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create the first Founder account and email the invite.")
    parser.add_argument("--name")
    parser.add_argument("--email")
    parser.add_argument("--title", default="Founder & President")
    parser.add_argument("--resend", action="store_true", help="send a fresh invite to the Founder who hasn't signed in yet")
    parser.add_argument("--print-link", action="store_true", help="also show the invite link here")
    asyncio.run(main(parser.parse_args()))
