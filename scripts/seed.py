"""SAMPLE DATA ONLY. Never run this in production.

Fills a fresh Streamers Hub database with the sample data from the designs (section 12 of the brief):
departments, one user per role, Galacta and three open seats, a week of streams, the Clip Table,
20 placeholder brand partners (fictional: replace before launch), the store, designers and clippers,
the review queue, a payout run of GHS 6,420 for 14 payees waiting for the Founder, November finance,
the pilot round, three holders and their reports.

Dates are relative to today, so the screens look current whenever it is run.

    ./venv/bin/python -m scripts.seed            # refuses if the database already has users
    ./venv/bin/python -m scripts.seed --reset    # drops every table first

Every sample account uses an @streamershub.test address (no email is ever sent to it) and the
password in SH_SEED_PASSWORD (default below). One-time codes for sample accounts appear in the API log.
"""

import argparse
import asyncio
import os
import random
import uuid
from datetime import date, datetime, time, timedelta, timezone

from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select, text

from app.core.config import settings
from app.core.crypto import encrypt, lookup_hash
from app.core.database import load_models, session_manager, setup_schema
from app.core.security import hash_password, hash_token
from app.models.agreements import Agreement, AgreementTemplate
from app.models.applications import Application
from app.models.base import Base
from app.models.clips import Clip, ClipperEarning, ClipperProfile, LeaderboardSnapshot
from app.models.investors import (FundingRound, Investment, Investor, InvestorDocument, InvestorMessage, InvestorReport,
                                  Milestone, Valuation)
from app.models.money import Budget, CashBalance, Decision, LedgerEntry, PayrollLine, PayrollRun, SalaryTier
from app.models.partnerships import Brand, Deal, Deliverable, RateCardItem
from app.models.people import (Department, EquityGrant, Goal, KpiSnapshot, LeadRating, OnboardingRun, StaffProfile, Task,
                               User)
from app.models.store import CommissionEntry, Design, DesignBrief, Order, OrderItem, Product
from app.models.talent import (CreativeRequest, EditJob, Honour, MarkedMoment, MomentClaim, PlatformIncome, RosterSeat, RunOfShowItem,
                               SocialPost, Stream, StreamHealth, Streamer)
from app.models.tech import AccessAccount, Equipment, Incident
from app.services import AgreementService, AuthService, PayrollService
from app.services.CompanyService import ACCRA, today
from app.services.money import cedis, month_key, prev_month

PASSWORD = os.getenv("SH_SEED_PASSWORD", "Sample-Pass-2026")
DOMAIN = "streamershub.test"
rng = random.Random(46)
_HASH: list[str] = []


def pw() -> str:
    """One bcrypt hash for the shared sample password (hashing it per account is slow)."""
    if not _HASH:
        _HASH.append(hash_password(PASSWORD))
    return _HASH[0]


def at(d: date, hh: int = 12, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), ACCRA).astimezone(timezone.utc)


async def seed(db, reset: bool) -> dict:
    T = today()
    M0 = T.replace(day=1)
    now_month = month_key(T)
    oct_key = prev_month(now_month)
    sep_key = prev_month(oct_key)
    oct0 = date(int(oct_key[:4]), int(oct_key[5:]), 1)
    sep0 = date(int(sep_key[:4]), int(sep_key[5:]), 1)
    out = {"password": PASSWORD, "holders": [], "invites": []}

    # Departments and people -------------------------------------------------------------------------
    from app.core.access import DEPARTMENTS

    depts = {d.slug: d for d in (await db.execute(select(Department))).scalars()}  # created with the schema
    for i, (slug, name) in enumerate(DEPARTMENTS):
        if slug not in depts:
            depts[slug] = Department(slug=slug, name=name, sort=i)
            db.add(depts[slug])
    await db.flush()

    users = {}

    def person(key, name, role, dept, title, desk=None, handle=None, pay_type="none", payout="momo", status="active"):
        u = User(email=f"{key}@{DOMAIN}", name=name, role=role, handle=handle, status=status,
                 department_id=depts[dept].id if dept else None, password_hash=pw() if status == "active" else None,
                 phone_enc=encrypt("0241234567"), phone_last4="4567")
        db.add(u)
        users[key] = u
        return u

    staff = [
        ("founder", "Founder & President", "founder", "leadership", "Founder & President", "command", "equity_vesting"),
        ("philip", "Philip", "cofounder", "marketing", "Co-Founder · Partnerships and Growth", "partnerships", "equity_vesting"),
        ("bernard", "Bernard", "cofounder", "tech", "Co-Founder · CTO", "tech", "equity_vesting"),
        ("aaron", "Aaron", "cofounder", "strategy", "Co-Founder · Strategy and Investors", "capital", "equity_vesting"),
        ("ahmed", "Ahmed", "finance", "finance", "Chief Financial Officer", "finance", "equity_vesting"),
        ("certified", "Certified", "staff", "production", "Producer", "production", "revenue_share"),
        ("kingmille", "King Mille", "dept_head", "creative", "Creative Lead", "creative", "equity_vesting"),
        ("c9per", "C9PER Multimedia", "staff", "creative", "Photo, design and video", "creative", "equity_vesting"),
        ("trigger", "Trigger", "staff", "marketing", "Social Media Manager", "social", "equity_vesting"),
        ("storeops", "Store Operations", "staff", "leadership", "Store Operations", "store", "none"),
        ("galacta", "Lamar (Galacta)", "streamer", "talent", "Streamer · Signing No. 01", "talent", "salary"),
    ]
    for key, name, role, dept, title, desk, pay in staff:
        person(key, name, role, dept, title, desk, pay_type=pay)
    person("editor", "Video editor", "staff", "creative", "Video editor", "creative", pay_type="equity_vesting")
    person("community", "Community lead", "staff", "marketing", "Community lead", "social", pay_type="equity_vesting", status="invited")
    await db.flush()
    for key, name, role, dept, title, desk, pay in staff + [("editor", "", "staff", "creative", "Video editor", "creative", "equity_vesting"),
                                                            ("community", "", "staff", "marketing", "Community lead", "social", "equity_vesting")]:
        db.add(StaffProfile(user_id=users[key].id, title=title, desk=desk, pay_type=pay, payout_method="momo", momo_network="mtn",
                            momo_number_enc=encrypt("0241234412"), momo_last3="412", start_date=date(2026, 9, 1),
                            reports_to_id=None, emergency_contact="Family · 020 000 0000"))
    for slug, key in [("leadership", "founder"), ("strategy", "aaron"), ("finance", "ahmed"), ("marketing", "philip"),
                      ("creative", "kingmille"), ("production", "certified"), ("tech", "bernard"), ("talent", "founder")]:
        depts[slug].lead_user_id = users[key].id
    for key, pct in [("founder", 40), ("philip", 8), ("bernard", 8), ("aaron", 8), ("ahmed", 2), ("kingmille", 1.5),
                     ("c9per", 1), ("trigger", 1), ("certified", 1)]:
        db.add(EquityGrant(holder_user_id=users[key].id, percent=pct, vest_months=48, cliff_months=12, start_date=date(2026, 9, 1)))
    # Onboarding: the new video editor is on step 3, the community lead hasn't accepted the invite yet
    for key in [k for k, *_ in staff]:
        db.add(OnboardingRun(user_id=users[key].id, kind="staff", current_step=5, completed_steps=[1, 2, 3, 4, 5],
                             data={}, completed_at=at(date(2026, 9, 3))))
    db.add(OnboardingRun(user_id=users["editor"].id, kind="staff", current_step=3, completed_steps=[1, 2],
                         data={"signed": {k: {"name": "Video editor", "at": at(T - timedelta(days=2)).isoformat()}
                                          for k in ("role", "vesting", "confidentiality", "conduct")}}))
    token = "sample-invite-community-lead"
    users["community"].invite_token_hash, users["community"].invite_expires_at = hash_token(token), at(T + timedelta(days=7))
    db.add(OnboardingRun(user_id=users["community"].id, kind="staff", current_step=1, completed_steps=[], data={}))
    out["invites"].append(("Staff invite (Community lead)", f"/sign-in/set-password?token={token}&welcome=1"))

    # Designers and clippers
    designers = {}
    for handle, name, method in [("@ama.draws", "Ama Draws", "momo"), ("@kofi.inks", "Kofi Inks", "bank"),
                                 ("@efua.lines", "Efua Lines", "momo"), ("@esi.studio", "Esi Studio", "momo")]:
        key = handle[1:].replace(".", "_")
        u = User(email=f"{key}@{DOMAIN}", name=name, role="designer", handle=handle, status="active", password_hash=pw())
        db.add(u)
        await db.flush()
        db.add(StaffProfile(user_id=u.id, title="Community designer", payout_method=method, momo_network="mtn" if method == "momo" else None,
                            momo_number_enc=encrypt("0551112223") if method == "momo" else None, momo_last3="223" if method == "momo" else None,
                            bank_name="GCB Bank" if method == "bank" else None, bank_account_enc=encrypt("1021300045678") if method == "bank" else None,
                            bank_last4="5678" if method == "bank" else None))
        db.add(OnboardingRun(user_id=u.id, kind="designer", current_step=4, completed_steps=[1, 2, 3, 4], data={}, completed_at=at(date(2026, 9, 20))))
        designers[handle] = u
    clippers = {}
    clipper_list = [("@kwesi.cuts", True), ("@nanaframes", True), ("@clipsbyefya", False), ("@accra.afterdark", True),
                    ("@yaw_replays", True), ("@zongoedits", True), ("@dansoman.clips", True), ("@tema.trims", True), ("@kasoa.kuts", True)]
    for handle, has_payout in clipper_list:
        key = handle[1:].replace(".", "_")
        u = User(email=f"{key}@{DOMAIN}", name=handle, role="clipper", handle=handle, status="active", password_hash=pw())
        db.add(u)
        await db.flush()
        db.add(ClipperProfile(user_id=u.id, handle=handle, platforms={"tiktok": f"tiktok.com/{handle}"}, strikes=1 if handle == "@yaw_replays" else 0))
        db.add(StaffProfile(user_id=u.id, title="Clipper", payout_method="momo" if has_payout else None, momo_network="mtn" if has_payout else None,
                            momo_number_enc=encrypt("0201234321") if has_payout else None, momo_last3="321" if has_payout else None))
        db.add(OnboardingRun(user_id=u.id, kind="clipper", current_step=4, completed_steps=[1, 2, 3, 4], data={}, completed_at=at(date(2026, 10, 1))))
        clippers[handle] = u
    await db.flush()

    # Talent ------------------------------------------------------------------------------------------
    galacta = Streamer(user_id=users["galacta"].id, stage_name="Galacta", known_as="Lamar", category="gaming",
                       content="GTA 6 · NBA 2K · EA FC · Apex Legends", signing_number=1, image_url="/brand/galacta.jpg",
                       contract_start=date(2026, 9, 1), contract_end=date(2027, 8, 31), status="signed",
                       bio="Known off-stream as Lamar. A competitor first, an entertainer always. This is the record he walked in with.")
    db.add(galacta)
    await db.flush()
    db.add(RosterSeat(number=1, title="Galacta", description="GTA 6 · NBA 2K · EA FC · Apex Legends", streamer_id=galacta.id))
    for n, t, d in [(2, "The Competitor", "Ranked play, tournaments, clutch moments."),
                    (3, "The Entertainer", "Big personality. The chat never goes quiet."),
                    (4, "The Wildcard", "Sports, music, comedy, lifestyle. Surprise us.")]:
        db.add(RosterSeat(number=n, title=t, description=d))
    for i, (t, d) in enumerate([("Back-to-back champion", "NBA 2K24 and 2K25 at DKG Hub"), ("Runner-up", "NBA 2K24 tournament by Ghana Esports"),
                                ("Former #1 Wraith in Ghana", "By kills, Apex Legends"), ("Former top 11", "Ghana NBA 2K26 leaderboard"),
                                ("Elite in Division Rivals", "FIFA 25 and FC 26")], 1):
        db.add(Honour(streamer_id=galacta.id, position=i, title=t, detail=d))
    for month, amount in [(sep_key, 800), (oct_key, 1640), (now_month, 1640)]:
        db.add(PlatformIncome(streamer_id=galacta.id, month=month, platform="youtube", amount_minor=cedis(amount * 0.55)))
        db.add(PlatformIncome(streamer_id=galacta.id, month=month, platform="twitch", amount_minor=cedis(amount) - cedis(amount * 0.55)))
    for i, (label, lo, hi, pay) in enumerate([("Base tier", 0, 999.99, 3000), ("Milestone tier 2", 1000, 4999.99, 3500),
                                              ("Milestone tier 3", 5000, 9999.99, 4500), ("Milestone tier 4", 10000, None, None)]):
        db.add(SalaryTier(label=label, min_platform_income_minor=cedis(lo), max_platform_income_minor=cedis(hi) if hi is not None else None,
                          salary_minor=cedis(pay) if pay else None, position=i))

    # Streams: this month's 26 (two drops), with the last seven days as in the designs
    week = [(6, "20:00", "Ranked grind", "Gaming", None, 11, 0), (5, "20:00", "Football night", "Sport and gaming", None, 15, 0),
            (4, "20:00", "Chill stream, Q&A", "Just chatting", None, 9, 0), (3, "20:00", "Ranked night", "Gaming", "Rush+ Energy trial read", 17, 0),
            (2, "21:00", "New release first look", "Gaming", None, 13, 6), (1, "18:00", "Weekend cup", "Sport and gaming", None, 16, 0)]
    streams = []
    days_in = (T - M0).days
    extra = max(0, min(26 - 7, days_in * 2))
    for i in range(extra):  # earlier this month, before the last week
        day = M0 + timedelta(days=min(i // 2, max(0, days_in - 7)))
        if day >= T - timedelta(days=6):
            break
        late = 0 if i % 9 else 7
        s = Stream(streamer_id=galacta.id, title=rng.choice(["Ranked grind", "Viewer games", "Football night", "Late-night lobbies", "Weekend cup"]),
                   category="Gaming", scheduled_start=at(day, 14 if i % 2 else 20), actual_start=at(day, 14 if i % 2 else 20, late),
                   ended_at=at(day, 16 if i % 2 else 22, 15), platforms=["youtube", "twitch", "kick", "tiktok"], avg_viewers=rng.randint(8, 14),
                   peak_viewers=rng.randint(15, 24), status="done", uptime_percent=96.0 if i in (5, 11) else 100.0)
        db.add(s)
        streams.append(s)
    for back, hhmm, title, cat, brand, viewers, late in week:
        day = T - timedelta(days=back)
        hh, mm = map(int, hhmm.split(":"))
        s = Stream(streamer_id=galacta.id, title=title, category=cat, brand_moment=brand, scheduled_start=at(day, hh, mm),
                   actual_start=at(day, hh, mm) + timedelta(minutes=late), ended_at=at(day, hh, mm) + timedelta(hours=2, minutes=20),
                   platforms=["youtube", "twitch", "kick", "tiktok"], avg_viewers=viewers, peak_viewers=viewers + 6, status="done",
                   uptime_percent=100.0, recording_url=None)
        db.add(s)
        streams.append(s)
    started = datetime.now(timezone.utc) - timedelta(hours=1, minutes=12)  # live for 1h12m whenever the seed runs
    live = Stream(streamer_id=galacta.id, title="Viewer games", category="Variety", brand_moment="Store mention", scheduled_start=started,
                  actual_start=started, platforms=["youtube", "twitch", "kick", "tiktok"], avg_viewers=19, peak_viewers=24, watching_now=19,
                  chat_per_min=11, new_followers=9, status="live", uptime_percent=100.0)
    db.add(live)
    nxt = Stream(streamer_id=galacta.id, title="Ranked grind", category="Gaming", scheduled_start=at(T + timedelta(days=1), 20),
                 platforms=["youtube", "twitch", "kick", "tiktok"], status="scheduled")
    db.add(nxt)
    await db.flush()
    for i, (ts, t, note, st) in enumerate([("19:00", "Go live, intro and tonight’s goal", "Started on time", "done"),
                                           ("19:10", "Viewer games, block 1", "Chat picks the opponents", "done"),
                                           ("20:00", "Viewer challenge", "Loser wears the wig", "done"),
                                           ("20:30", "Merch mention and store link", "Drop 001 · Signed Cap is low on stock", "next"),
                                           ("20:35", "Viewer games, block 2", None, "queued"),
                                           ("21:20", "Week recap and shout-outs", "Top clippers of the week", "queued"),
                                           ("21:30", "Outro and tomorrow’s schedule", None, "queued")]):
        db.add(RunOfShowItem(stream_id=live.id, at_time=ts, title=t, note=note, status=st, position=i))
    db.add(StreamHealth(stream_id=live.id, bitrate_kbps=6000, dropped_frames_pct=0.1, upload_mbps=24, power="mains", backup_ready=True, audio_db=-9))
    moment_rows = []
    for s, labels in [(live, ["Clutch 1v2 in block 1", "Wig forfeit", "Chat picks the worst team", "Galacta rage-quits (joke)", "Merch shout-out"]),
                      (streams[-1], ["Penalty shootout win", "Final whistle", "Crowd chant", "Cup lift"]),
                      (streams[-2], ["First boss reaction", "Glitch moment", "Trailer reaction", "Ending theory"])]:
        for j, label in enumerate(labels):
            kind = "clutch" if "Clutch" in label or "win" in label else "funny" if "Wig" in label or "rage" in label or "Glitch" in label \
                else "brand" if "Merch" in label else "reaction" if "reaction" in label or "chant" in label else "highlight"
            moment_rows.append(MarkedMoment(stream_id=s.id, at_seconds=600 + j * 1500, label=label, kind=kind, source="producer",
                                            marked_by=users["certified"].id))
    # Galacta's own timestamps, posted from his desk after the streams, with context for clippers
    for s, at_s, label, kind, note in [
            (streams[-1], 2280, "Last-minute free kick", "clutch", "Watch from 38:00 for the build-up. Cut it with the commentary audio."),
            (streams[-1], 5460, "Chat roasts my keeper", "chat", "Best with the chat overlay visible. Keep it under 30s."),
            (streams[-2], 3125, "I jumped so hard I knocked the mic", "funny", "Starts at 52:05. My reaction is the clip, not the game."),
            (live, 1980, "Signed the cap live for a viewer", "brand", "Keep the store link and the cap in frame.")]:
        moment_rows.append(MarkedMoment(stream_id=s.id, at_seconds=at_s, label=label, kind=kind, note=note, source="streamer",
                                        marked_by=users["galacta"].id))
    db.add_all(moment_rows)
    db.add_all([EditJob(source_stream_id=streams[-1].id, title="Weekend cup highlight edit", format="highlight", assignee_id=users["c9per"].id,
                        due_at=at(T, 22), status="editing"),
                EditJob(source_stream_id=live.id, title="Clip: Wig forfeit", format="short", assignee_id=users["editor"].id, due_at=at(T + timedelta(days=1), 12), status="todo")])

    # KPIs and pilot targets
    def snap(key, values):
        for d, v in values:
            db.add(KpiSnapshot(metric_key=key, value=v, captured_at=at(d, 23), source="manual", entered_by=users["trigger"].id))
    snap("yt_subscribers", [(M0 - timedelta(days=1), 144), (T - timedelta(days=7), 191), (T, 212)])
    snap("avg_live_viewers", [(M0 - timedelta(days=1), 9), (T - timedelta(days=7), 12), (T, 14)])
    snap("tiktok_followers", [(T - timedelta(days=8), 956), (T, 1120)])
    snap("instagram_followers", [(T - timedelta(days=8), 319), (T, 348)])
    snap("twitch_kick_followers", [(T - timedelta(days=8), 179), (T, 196)])
    snap("posting_streak_days", [(T, 41)])
    pilot_end = date(2027, 3, 31)
    for i, (title, key, target, unit) in enumerate([("500 YouTube subscribers", "yt_subscribers", 500, "subscribers"),
                                                   ("30+ average live viewers", "avg_live_viewers", 30, "viewers"),
                                                   ("Daily posts on core platforms", "posting_streak_days", 1, "days"),
                                                   ("1+ paid brand deal", "paid_brand_deals", 1, "deals")]):
        db.add(Goal(title=title, metric_key=key, target=target, current=0, unit=unit, period_start=date(2026, 10, 1), period_end=pilot_end,
                    is_pilot_target=True, sort=i))

    # Galacta's weekly milestones
    for title, status, progress in [("Go live on time, every stream", "done", "6 of 7"), ("Stream at least 2 hours", "done", "7 of 7"),
                                    ("Rush+ Energy trial read", "done", "1 of 1"), ("Reply to chat questions on stream", "doing", "Daily"),
                                    ("Record 2 pieces to camera for social", "open", "0 of 2"), ("Weekly review with producer", "open", "Tonight")]:
        db.add(Task(owner_user_id=users["galacta"].id, department_id=depts["talent"].id, title=title, status=status, progress_label=progress,
                    is_weekly_milestone=True, closed_at=at(T - timedelta(days=1)) if status == "done" else None, on_time=True if status == "done" else None,
                    due_on=T + timedelta(days=6 - T.weekday())))

    # Tasks closed per week (W-7 .. this week) and each person's headline
    per_week = [31, 38, 44, 41, 52, 49, 53, 64]
    team = ["philip", "bernard", "aaron", "ahmed", "certified", "kingmille", "c9per", "trigger"]
    headlines = {"philip": "Rush+ Energy contract sent", "bernard": "Backup internet line installed", "aaron": "Investor report sent",
                 "ahmed": "Payout run prepared", "certified": "6 of 6 streams produced", "kingmille": "5 merch designs reviewed",
                 "c9per": "14 clips and 6 thumbnails", "trigger": "44 posts · 41-day streak"}
    week_start = T - timedelta(days=T.weekday())
    for w, count in enumerate(per_week):
        ws = week_start - timedelta(days=7 * (7 - w))
        for k in range(count):
            owner = team[k % len(team)]
            day = min(ws + timedelta(days=k % 7), T)
            db.add(Task(owner_user_id=users[owner].id, department_id=users[owner].department_id, title=f"Weekly task {k + 1}",
                        status="done", closed_at=at(day, 9 + k % 8), on_time=(k % 9 != 0), due_on=day))
    for key, title in headlines.items():
        db.add(Task(owner_user_id=users[key].id, department_id=users[key].department_id, title=title, status="done",
                    closed_at=at(T, 18), on_time=True, due_on=T))
    for key, rating in [("philip", 70), ("bernard", 95), ("aaron", 95), ("ahmed", 85), ("certified", 100), ("kingmille", 88),
                        ("c9per", 90), ("trigger", 100), ("galacta", 96)]:
        db.add(LeadRating(user_id=users[key].id, period=now_month, rating=rating, rated_by=users["founder"].id))
    db.add(Goal(owner_user_id=users["philip"].id, department_id=depts["marketing"].id, title="Sign the first paid brand deal",
                target=1, current=0, period_start=M0, period_end=T + timedelta(days=14)))

    # Partnerships ----------------------------------------------------------------------------------
    public = [("Voltacel", "a", "Telecoms and fintech"), ("Kasa Mobile", "b", "Telecoms and fintech"), ("nuvo phones", "c", "Devices and gear"),
              ("Tembo", "e", "Food and drink"), ("Rush+ Energy", "f", "Food and drink"), ("Zuri Malt", "e", "Food and drink"),
              ("adom pay", "c", "Telecoms and fintech"), ("Sankofa Bank", "a", "Banks and insurance"), ("Harmattan", "b", "Food and drink"),
              ("Okra Games", "a", "Devices and gear"), ("Pixel Lagoon", "f", "Devices and gear"), ("gridline fibre", "c", "Telecoms and fintech"),
              ("Asante Air", "b", "Banks and insurance"), ("Jollof King", "a", "Food and drink"), ("Kumasi Kicks", "e", "Devices and gear"),
              ("Baobab Audio", "f", "Devices and gear"), ("Savanna", "b", "Food and drink"), ("lumen displays", "c", "Devices and gear"),
              ("Ridgeway", "a", "Banks and insurance"), ("Akwaaba Eats", "e", "Food and drink")]
    brands = {}
    for i, (name, mark, sector) in enumerate(public):
        b = Brand(name=name, sector=sector, wordmark=mark, is_public=True, public_order=i, status="listed", is_sample=True,
                  note="Placeholder partner: replace before launch")
        db.add(b)
        brands[name] = b
    rush = brands["Rush+ Energy"]
    rush.status, rush.note, rush.next_step, rush.owner_user_id = "at_contract", "3 months · reads + logo", "Founder to sign", users["philip"].id
    board = [("PayCedi", "Telecoms and fintech", "contacted", "intro email sent", None, 4), ("Lumen Phones", "Devices and gear", "contacted", "media kit sent", None, 2),
             ("Jollof Express", "Food and drink", "replied", "wants numbers", "Call Tue", 1), ("Volta Arcade", "Devices and gear", "replied", "barter offer", "Reply due", 1),
             ("Kente Telecom", "Telecoms and fintech", "in_talks", "Data bundle for viewers", "Proposal out", 3),
             ("Ember Chairs", "Devices and gear", "in_talks", "Chair on stream + code", "Sample sent", 2)]
    more_contacted = [("Asoba Fintech", "Telecoms and fintech"), ("Bolt Riders", "Telecoms and fintech"), ("Cocoa Crunch", "Food and drink"),
                      ("Densu Water", "Food and drink"), ("Ekow Insure", "Banks and insurance"), ("Fafa Foods", "Food and drink"),
                      ("Gold Coast Bank", "Banks and insurance"), ("Hilltop Telecom", "Telecoms and fintech"), ("Ivory Snacks", "Food and drink")]
    more_replied = [("Juapong Juice", "Food and drink"), ("Kpong Pay", "Telecoms and fintech"), ("Labadi Wear", "Devices and gear")]
    for name, sector, stage, note, step, days in board:
        brands[name] = Brand(name=name, sector=sector, status=stage, note=note, next_step=step, owner_user_id=users["philip"].id,
                             last_contact_at=at(T - timedelta(days=days)))
        db.add(brands[name])
    for name, sector in more_contacted:
        db.add(Brand(name=name, sector=sector, status="contacted", owner_user_id=users["philip"].id, last_contact_at=at(T - timedelta(days=rng.randint(1, 9)))))
    for name, sector in more_replied:
        db.add(Brand(name=name, sector=sector, status="replied", owner_user_id=users["philip"].id, last_contact_at=at(T - timedelta(days=rng.randint(1, 5)))))
    await db.flush()
    rush_deal = Deal(brand_id=rush.id, title="Rush+ Energy 3-month deal", value_minor=cedis(18000), status="at_contract")
    db.add_all([rush_deal, Deal(brand_id=brands["Kente Telecom"].id, title="Kente Telecom data bundle", value_minor=cedis(20000), status="draft"),
                Deal(brand_id=brands["Ember Chairs"].id, title="Ember Chairs on stream", value_minor=cedis(8000), status="draft"),
                Deal(brand_id=rush.id, title="Rush+ Energy trial read", value_minor=cedis(700), status="trial", start=T - timedelta(days=4), end=T - timedelta(days=4))])
    await db.flush()
    db.add_all([Deliverable(brand_id=rush.id, deal_id=rush_deal.id, streamer_id=galacta.id, title="trial read report",
                            detail="Views, clips and chat mentions from Thursday", due_at=at(T, 18), status="due"),
                Deliverable(brand_id=brands["Ember Chairs"].id, streamer_id=galacta.id, title="unboxing on stream",
                            detail="Sample arrived · planned for Friday", due_at=at(T + timedelta(days=(4 - T.weekday()) % 7 or 7), 20), status="planned"),
                Deliverable(brand_id=brands["Kente Telecom"].id, title="audience sheet", detail="Age, city and device split for their team",
                            due_at=at(T - timedelta(days=2)), status="sent")])
    for i, (t, d, p, u) in enumerate([("Live read", "60 seconds on stream, with chat call-to-action", 450, "per stream"),
                                      ("Logo on overlay", "Bottom-right bug for every stream", 2500, "per month"),
                                      ("Dedicated segment", "15-minute branded challenge", 1200, "per stream"),
                                      ("Social pack", "1 YouTube Short, 2 TikToks, 1 Instagram Reel", 900, "per pack")]):
        db.add(RateCardItem(title=t, detail=d, price_minor=cedis(p), unit=u, position=i))

    # Store -------------------------------------------------------------------------------------------
    def inhouse(title, ptype, collection, price, cost, streamer=None):
        n = len(designs_made) + 1
        d = Design(reference=f"SH-DSN-{n:04d}", designer_user_id=None, title=title, product_type=ptype, status="live",
                   price_minor=cedis(price), commission_rate_bps=0, collection=collection, production_cost_minor=cedis(cost),
                   uses_streamer_id=streamer, decided_at=at(date(2026, 10, 1)), files=[])
        designs_made.append(d)
        return d

    designs_made: list[Design] = []
    products = {}
    # The label's own line uses the real product shots in the Studio's public/merch folder
    catalogue = [
        ("pro-jersey", "Streamers Hub Pro Jersey", "label", "jersey", "", "Drop 001", 350, 120, None, 30),
        ("classic-logo-tee", "Classic Logo Tee", "label", "tee", "", "Bestseller", 180, 55, None, 40),
        ("play-head-hoodie", "Play-Head Hoodie", "label", "hoodie", "", "Drop 001", 380, 140, None, 18),
        ("galacta-season-one-jersey", "Galacta Season One Jersey", "roster", "jersey", "", "Roster", 320, 110, None, 22),
        ("signed-cap", "Signed Cap", "designer", "cap", "t", "Designer", 140, 40, "@ama.draws", 4),
        ("washed-oversized-tee", "Washed Oversized Tee", "label", "tee", "", "New", 220, 70, None, 25),
        ("stream-desk-mat-xl", "Stream Desk Mat XL", "label", "mat", "", "New", 220, 70, None, 25),
        ("accra-after-dark-tee", "Accra After Dark Tee", "designer", "tee", "k", "Designer", 190, 55, "@kofi.inks", 30),
        ("piped-snapback", "Piped Snapback", "label", "cap", "", None, 160, 50, None, 15),
        ("tonal-back-tee", "Tonal Back Tee", "label", "tee", "", None, 190, 60, None, 20),
        ("galacta-orbit-poster-a2", "Galacta Orbit Poster A2", "roster", "poster", "y", "Roster", 90, 20, None, 3),
        ("gold-rim-mug", "Gold Rim Mug", "label", "mug", "", None, 120, 35, None, 30),
        ("gold-tag-tote", "Gold Tag Tote", "label", "tote", "", None, 200, 60, None, 20),
        ("cream-logo-tee", "Cream Logo Tee", "label", "tee", "", None, 180, 55, None, 20),
        ("steel-bottle", "Steel Bottle", "label", "bottle", "", None, 170, 55, None, 20),
        ("gold-keychain", "Gold Play-Head Keychain", "label", "keychain", "", None, 60, 15, None, 50),
        ("studio-gift-set", "Studio Gift Set", "label", "set", "", "Gift set", 290, 90, None, 10),
        ("gg-wo-ho-hoodie", "GG Wo Ho Hoodie", "designer", "hoodie", "t", "Limited · 50", 390, 145, "@esi.studio", 47),
    ]
    photos = {"pro-jersey": "pro-jersey", "classic-logo-tee": "classic-logo-tee", "play-head-hoodie": "play-head-hoodie",
              "washed-oversized-tee": "washed-oversized-tee", "stream-desk-mat-xl": "desk-mat-xl", "piped-snapback": "piped-snapback",
              "tonal-back-tee": "tonal-back-tee", "gold-rim-mug": "gold-rim-mug", "gold-tag-tote": "gold-tag-tote",
              "cream-logo-tee": "cream-logo-tee", "steel-bottle": "steel-bottle", "gold-keychain": "gold-keychain",
              "studio-gift-set": "studio-gift-set"}
    blurbs = {"signed-cap": "Six-panel cap with the label's play-head stitched on the front. Designed by @ama.draws.",
              "pro-jersey": "Breathable esports jersey in label black, with yellow and teal piping. Crest on the chest, the full play-head on the back.",
              "classic-logo-tee": "Heavyweight cotton tee. Small crest on the chest, the big play-head across the back.",
              "play-head-hoodie": "Brushed-fleece hoodie with the crest on the chest and the play-head across the back.",
              "washed-oversized-tee": "Oversized, garment-washed tee for a lived-in black. Crest front, play-head back.",
              "tonal-back-tee": "Black on black: a quiet crest up front and a tonal play-head across the back.",
              "cream-logo-tee": "The classic in cream. Crest on the chest, oversized play-head across the back.",
              "piped-snapback": "Flat-brim snapback with raised embroidery and yellow and teal piping.",
              "stream-desk-mat-xl": "Extra-large stitched-edge desk mat. Room for keyboard and mouse, crest in the corner.",
              "gold-rim-mug": "Matte black mug with a gold rim and handle.",
              "gold-tag-tote": "Heavy canvas tote with piped handles and a gold play-head tag.",
              "steel-bottle": "Insulated steel bottle. Cold for a full stream, hot for the morning after.",
              "gold-keychain": "Solid gold-tone keychain cut in the shape of the play-head, with enamel inlay.",
              "studio-gift-set": "The gold rim mug and the gold tag tote together, boxed as a gift."}
    for i, (slug, name, cat, ptype, tone, tag, price, cost, by, stock) in enumerate(catalogue):
        if by:
            n = len(designs_made) + 1
            d = Design(reference=f"SH-DSN-{n:04d}", designer_user_id=designers[by].id, title=name, product_type=ptype, status="live",
                       price_minor=cedis(price), commission_rate_bps=1500, collection="designer", production_cost_minor=cedis(cost),
                       decided_at=at(T - timedelta(days={"signed-cap": 12, "accra-after-dark-tee": 7}.get(slug, 40))), files=[],
                       review_notes=[{"at": at(T - timedelta(days=12)).isoformat(), "by": "Founder & President", "stage": "approved", "note": "Approved for the store"}])
            designs_made.append(d)
        else:
            d = inhouse(name, ptype, cat, price, cost, galacta.id if cat == "roster" else None)
        db.add(d)
        await db.flush()
        sizes = ["S", "M", "L", "XL", "XXL"] if ptype in ("tee", "hoodie", "jersey") else ([] if ptype != "poster" else ["A2"])
        images = [f"/merch/{photos[slug]}.jpg"] if slug in photos else []
        p = Product(slug=slug, name=name, price_minor=cedis(price), category=cat, product_type=ptype, tone=tone, tag=tag, sizes=sizes,
                    status="live", design_id=d.id, streamer_id=galacta.id if cat == "roster" else None,
                    commission_rate_bps=1500 if by else 0, production_cost_minor=cedis(cost), stock=stock, reorder_at=5, position=i,
                    images=images, description=blurbs.get(slug), is_sample=slug in ("signed-cap", "accra-after-dark-tee", "gg-wo-ho-hoodie"))
        db.add(p)
        products[slug] = p
    await db.flush()
    # Designs in the review queue, and a declined one
    queue = [("Kente Circuit Tee", "@ama.draws", "tee", 180, "creative_review", None, "Print file passes at 300 dpi · 2 colours · original", 0),
             ("Trotro Speedrun Hoodie", "@kofi.inks", "hoodie", 380, "founder_approval", None, "Creative Lead: approved · back print large", 0),
             ("Galacta Wraith Jersey", "@efua.lines", "jersey", 320, "founder_approval", galacta.id, "Uses Galacta’s name · streamer sign-off done", 0),
             ("Night Shift Desk Mat", "@ama.draws", "mat", 220, "founder_approval", None, "Second version after feedback", 1),
             ("Logo Flip Cap", "@ama.draws", "cap", 140, "rejected", None, None, 0)]
    for title, by, ptype, price, status, uses, note, extra_v in queue:
        n = len(designs_made) + 1
        d = Design(reference=f"SH-DSN-{n:04d}", designer_user_id=designers[by].id, title=title, product_type=ptype, asked_price_minor=cedis(price),
                   status=status, uses_streamer_id=uses, version=1 + extra_v, files=[],
                   price_minor=cedis(price) if status == "founder_approval" else None,
                   commission_rate_bps=1500 if status == "founder_approval" else None,
                   collection=("roster" if uses else "designer") if status == "founder_approval" else None,
                   production_cost_minor=cedis(price * 0.35) if status == "founder_approval" else None,
                   decline_reason="Too close to another brand's mark" if status == "rejected" else None,
                   decided_at=at(T - timedelta(days=9)) if status == "rejected" else None,
                   review_notes=[{"at": at(T - timedelta(days=3)).isoformat(), "by": "King Mille", "stage": "passed" if status == "founder_approval" else "note", "note": note}] if note else
                   [{"at": at(T - timedelta(days=9)).isoformat(), "by": "Founder & President", "stage": "declined", "note": "Too close to another brand's mark"}])
        d.created_at = at(T - timedelta(days=4 - len(designs_made) % 4))
        designs_made.append(d)
        db.add(d)
    await db.flush()
    await db.execute(text(f"SELECT setval('design_ref_seq', {len(designs_made)})"))
    db.add(DesignBrief(title="Drop 002 · Accra nights", detail="Two pieces for the December drop. Night colours, the city, the stream.",
                       due_on=at(T + timedelta(days=18))))
    db.add(DesignBrief(title="Galacta Season One merch", detail="A cap or tee for Galacta's fans. Uses his name, so he signs it off.",
                       due_on=at(T + timedelta(days=25))))

    # Orders: September and October (delivered, so designer commission is payable), and this month's SH-1003 to SH-1009
    seq = [990]

    def order(d: date, items: list[tuple[str, int, str | None]], status: str, name: str, city: str, method="momo", refunded=False):
        seq[0] += 1
        number = f"SH-{seq[0]:04d}"
        subtotal = sum(products[s].price_minor * q for s, q, _ in items)
        delivery = cedis(30)
        o = Order(number=number, customer_name=name, customer_email=f"customer{seq[0]}@{DOMAIN}", customer_phone_enc=encrypt("0249876543"),
                  customer_phone_last3="543", address="House 12, Example Street", city=city, delivery_zone="accra",
                  subtotal_minor=subtotal, delivery_minor=delivery, total_minor=subtotal + delivery, payment_method=method,
                  payment_ref=f"SHP-SEED-{seq[0]}", tracking_code=f"seed{seq[0]}code", status=status, paid_at=at(d, 13),
                  shipped_at=at(d + timedelta(days=1)) if status in ("shipped", "delivered", "refunded") else None,
                  delivered_at=at(d + timedelta(days=3)) if status in ("delivered", "refunded") else None,
                  history=[{"at": at(d, 13).isoformat(), "status": "paid", "by": "payment provider"}])
        db.add(o)
        return o, items, refunded

    plan = [
        (sep0 + timedelta(days=4), [("signed-cap", 1, None)], "refunded", "Kwabena T.", "Tema", "momo", True),
        (sep0 + timedelta(days=6), [("signed-cap", 2, None), ("accra-after-dark-tee", 2, "M")], "delivered", "Akosua M.", "Accra"),
        (sep0 + timedelta(days=9), [("accra-after-dark-tee", 2, "L")], "delivered", "Fiifi A.", "Kumasi"),
        (sep0 + timedelta(days=12), [("signed-cap", 2, None), ("gg-wo-ho-hoodie", 1, "L")], "delivered", "Afua B.", "Accra"),
        (sep0 + timedelta(days=15), [("accra-after-dark-tee", 2, "S")], "delivered", "Edem K.", "Ho"),
        (sep0 + timedelta(days=20), [("signed-cap", 1, None)], "delivered", "Yaa S.", "Accra"),
        (oct0 + timedelta(days=3), [("signed-cap", 3, None)], "delivered", "Kojo A.", "Accra"),
        (oct0 + timedelta(days=8), [("signed-cap", 1, None)], "delivered", "Ama K.", "Kumasi"),
        (oct0 + timedelta(days=12), [("accra-after-dark-tee", 1, "M")], "delivered", "Selasi D.", "Accra"),
        (oct0 + timedelta(days=14), [("signed-cap", 2, None)], "delivered", "Nii O.", "Accra"),
        (M0 + timedelta(days=min(1, days_in)), [("galacta-orbit-poster-a2", 1, "A2")], "delivered", "Mawuli E.", "Accra"),
    ]
    orders_rows = [order(*p) for p in plan]
    seq[0] = 1003
    nov = [  # SH-1004 .. SH-1009, as on the Store operations screen
        (max(M0, T - timedelta(days=8)), [("stream-desk-mat-xl", 1, None)], "delivered", "Esi M.", "Cape Coast"),
        (max(M0, T - timedelta(days=5)), [("accra-after-dark-tee", 1, "M")], "shipped", "Kojo B.", "Madina, Accra", "card"),
        (max(M0, T - timedelta(days=2)), [("signed-cap", 1, None)], "paid", "Nana A.", "Takoradi"),
        (max(M0, T - timedelta(days=1)), [("galacta-orbit-poster-a2", 1, "A2")], "packing", "Selorm K.", "Tema", "card"),
        (T, [("classic-logo-tee", 2, "L")], "to_ship", "Yaw D.", "Kumasi"),
        (T, [("play-head-hoodie", 1, "M")], "to_ship", "Abena O.", "East Legon, Accra"),
    ]
    orders_rows += [order(*p) for p in nov]
    await db.flush()
    await db.execute(text(f"SELECT setval('order_number_seq', {seq[0]})"))
    for o, items, refunded in orders_rows:
        for slug, qty, size in items:
            p = products[slug]
            design = next(d for d in designs_made if d.id == p.design_id)
            commission = round(p.price_minor * qty * p.commission_rate_bps / 10000)
            item = OrderItem(order_id=o.id, product_id=p.id, product_name=p.name, size=size, qty=qty, unit_price_minor=p.price_minor,
                             commission_minor=commission, production_cost_minor=p.production_cost_minor * qty)
            db.add(item)
            await db.flush()
            if design.designer_user_id and commission:
                payable = o.status == "delivered" and o.delivered_at and o.delivered_at <= at(T - timedelta(days=14))
                status = "reversed" if refunded else ("payable" if payable else "pending")
                db.add(CommissionEntry(designer_user_id=design.designer_user_id, order_item_id=item.id, amount_minor=commission, status=status))

    # Ledger: September, October and this month (income 3,820 and spend 4,910 as in the designs)
    def ledger(d: date, t: str, cat: str, amount: float, note: str):
        db.add(LedgerEntry(on_date=d, type=t, category=cat, amount_minor=cedis(amount), source_table="seed", source_id=str(uuid.uuid4()), note=note))

    for key, start, income in [(sep_key, sep0, {"platform": 800, "store": 2180, "brand": 0}),
                               (oct_key, oct0, {"platform": 1640, "store": 1060, "brand": 0}),
                               (now_month, M0, {"platform": 1640, "store": 1480, "brand": 700})]:
        d = min(start + timedelta(days=10), T)
        for cat, amt in income.items():
            if amt:
                ledger(d, "revenue", cat, amt, f"Sample {cat} income")
    for key, start, costs in [(sep_key, sep0, {"salary": 3000, "internet_power": 1200, "marketing": 1100, "store_cogs": 500, "operations": 400}),
                              (oct_key, oct0, {"salary": 3000, "internet_power": 1150, "marketing": 653, "store_cogs": 300, "operations": 200, "payouts": 288}),
                              (now_month, M0, {"salary": 1500, "internet_power": 1150, "marketing": 1220, "store_cogs": 640, "operations": 400})]:
        d = min(start + timedelta(days=12), T)
        for cat, amt in costs.items():
            ledger(d, "expense", cat, amt, f"Sample {cat}")
    db.add(CashBalance(on_date=T, amount_minor=cedis(41200), entered_by=users["ahmed"].id))
    for cat, amt in {"platform": 1200, "store": 1000, "brand": 2000, "salary": 3000, "internet_power": 1200, "marketing": 1500,
                     "store_cogs": 500, "operations": 800}.items():
        db.add(Budget(month=now_month, type="revenue" if cat in ("platform", "store", "brand") else "expense", category=cat, planned_minor=cedis(amt)))
    k = now_month
    from app.services.money import next_month
    for brand_amt, other_amt in [(6000, 3000), (7000, 4000), (8000, 4000), (10000, 5000)]:
        k = next_month(k)
        db.add(Budget(month=k, type="revenue", category="brand", planned_minor=cedis(brand_amt)))
        db.add(Budget(month=k, type="revenue", category="platform", planned_minor=cedis(other_amt * 0.6)))
        db.add(Budget(month=k, type="revenue", category="store", planned_minor=cedis(other_amt * 0.4)))

    # Clips: this month's verified views per clipper, so the payout run comes to GHS 6,420
    monthly = {"@kwesi.cuts": 212400, "@nanaframes": 164000, "@clipsbyefya": 139800, "@accra.afterdark": 97200, "@yaw_replays": 81500,
               "@zongoedits": 64100, "@dansoman.clips": 23700, "@tema.trims": 12000, "@kasoa.kuts": 8000}
    counts = {"@kwesi.cuts": 14, "@nanaframes": 11, "@clipsbyefya": 17, "@accra.afterdark": 9, "@yaw_replays": 12, "@zongoedits": 8,
              "@dansoman.clips": 5, "@tema.trims": 4, "@kasoa.kuts": 3}
    kwesi_named = [("Loser wears the wig", "tiktok", 4, 41200), ("Chat picked the worst team", "instagram", 2, 33600),
                   ("1v3 clutch", "youtube_shorts", 3, 27900), ("First look reaction", "tiktok", 5, 22400), ("Rush+ taste test", "tiktok", 3, 18700)]
    recent = [s for s in streams if s.status == "done"][-6:]
    for handle, total in monthly.items():
        u = clippers[handle]
        n = counts[handle]
        parts = []
        if handle == "@kwesi.cuts":
            parts = list(kwesi_named)
            rest = total - sum(p[3] for p in parts)
            for i in range(n - len(parts)):
                parts.append((f"Stream moment {i + 1}", "tiktok", i % 6, rest // (n - 5) + (rest % (n - 5) if i == 0 else 0)))
        else:
            for i in range(n):
                parts.append((f"{['Clutch', 'Reaction', 'Fail', 'Chat moment', 'Win'][i % 5]} #{i + 1}", "tiktok", i % 6,
                              total // n + (total % n if i == 0 else 0)))
        for i, (title, platform, back, views) in enumerate(parts):
            day = max(M0, T - timedelta(days=min(back, 6)))
            src = recent[i % len(recent)] if recent else None
            db.add(Clip(clipper_user_id=u.id, source_stream_id=src.id if src else None, url=f"https://www.{ {'tiktok': 'tiktok.com', 'instagram': 'instagram.com', 'youtube_shorts': 'youtube.com'}[platform]}/{handle}/video/{uuid.uuid4().hex[:10]}",
                        platform=platform, title=title, posted_at=at(day, 21), views_reported=views + rng.randint(0, 900), views_verified=views,
                        status="verified", verified_by=users["trigger"].id, verified_at=at(day, 23), period=now_month))
    pending = [("He really missed that", "@kwesi.cuts", 48300, "tiktok", 2), ("Chat chose violence", "@nanaframes", 21900, "instagram", 1),
               ("Free phone??", "@yaw_replays", 9400, "tiktok", 3)]
    for title, handle, views, platform, back in pending:
        db.add(Clip(clipper_user_id=clippers[handle].id, source_stream_id=recent[-1].id if recent else None,
                    url=f"https://www.{'tiktok.com' if platform == 'tiktok' else 'instagram.com'}/{handle}/video/{uuid.uuid4().hex[:10]}",
                    platform=platform, title=title, posted_at=at(max(M0, T - timedelta(days=back)), 20), views_reported=views,
                    status="flagged" if handle == "@yaw_replays" else "submitted", period=now_month,
                    flag_reason="Misleading caption" if handle == "@yaw_replays" else None))
    last_week = T - timedelta(days=7)
    from app.services.ClipService import iso_week
    for rank, handle in enumerate(["@nanaframes", "@kwesi.cuts", "@yaw_replays", "@accra.afterdark", "@dansoman.clips", "@tema.trims", "@clipsbyefya"], 1):
        db.add(LeaderboardSnapshot(week=iso_week(last_week), clipper_user_id=clippers[handle].id, rank=rank))
    # Last month's run, already paid (Galacta's base tier and @kwesi.cuts' GHS 288)
    oct_run = PayrollRun(period=oct_key, status="paid", total_minor=cedis(3288), payee_count=2, drafted_at=at(oct0 + timedelta(days=24)),
                         submitted_by=users["ahmed"].id, submitted_at=at(oct0 + timedelta(days=25)), approved_by=users["founder"].id,
                         approved_at=at(oct0 + timedelta(days=26)))
    db.add(oct_run)
    await db.flush()
    db.add(PayrollLine(run_id=oct_run.id, payee_user_id=users["galacta"].id, kind="salary", amount_minor=cedis(3000), method="momo",
                       basis={"text": "October · Base tier"}, status="paid", paid_ref="MOMO-OCT-0001", paid_at=at(oct0 + timedelta(days=27))))
    kl = PayrollLine(run_id=oct_run.id, payee_user_id=clippers["@kwesi.cuts"].id, kind="clipper_payout", amount_minor=cedis(288), method="momo",
                     basis={"text": "96.0K verified views"}, status="paid", paid_ref="MOMO-OCT-0002", paid_at=at(oct0 + timedelta(days=27)))
    db.add(kl)
    await db.flush()
    db.add(ClipperEarning(clipper_user_id=clippers["@kwesi.cuts"].id, period=oct_key, verified_views=96000, amount_minor=cedis(288),
                          status="paid", payroll_line_id=kl.id))
    await db.flush()

    # This month's run: drafted by the rules, then sent to the Founder by Finance
    run = await PayrollService.draft_run(db, now_month, users["ahmed"])
    await PayrollService.submit(db, run, users["ahmed"])
    out["run"] = (run.total_minor, run.payee_count)

    # Other decisions waiting for the Founder
    for title, source, kind, amount, entity in [
        ("Sign Rush+ Energy 3-month deal · GHS 18,000", "Partnerships · contract ready for signature", "deal", 18000, str(rush_deal.id)),
        ("Budget request: second camera · GHS 1,500", "Production · Producer", "budget", 1500, None),
        ("Release Drop 002 (5 designs) to the store", "Creative · all designs approved", "release", None, None)]:
        db.add(Decision(title=title, source=source, kind=kind, amount_minor=cedis(amount) if amount else None, entity_id=entity,
                        requested_by=users["philip"].id))

    # Applications -----------------------------------------------------------------------------------
    def app(track, ref, name, email_key, handle, payload, tag=None, tone=None, status="new", days=2, decided=None):
        a = Application(reference=ref, track=track, name=name, email=f"{email_key}@{DOMAIN}", handle=handle, payload=payload, tag=tag,
                        tag_tone=tone, status=status)
        a.created_at = at(T - timedelta(days=days))
        if decided:
            a.reviewed_at, a.reviewed_by = at(T - timedelta(days=decided)), users["trigger"].id
        db.add(a)
        return a

    for ref, name, tools, city, pieces, tag, tone in [("SH-APP-DSN-7M2K", "Yaa T.", "Illustrator", "Kumasi", 12, "Portfolio 4.5 / 5", "ok"),
                                                      ("SH-APP-DSN-3W9C", "Papa K.", "Graphic designer", "Accra", 6, "Portfolio 4 / 5", "ok"),
                                                      ("SH-APP-DSN-8R1D", "Sena A.", "Student", "Ho", 4, "Portfolio 3 / 5", "warn"),
                                                      ("SH-APP-DSN-2H5P", "Mensah O.", "Agency designer", "Tema", 9, "Check originality", "no")]:
        app("designer", ref, name, ref.lower().replace("-", "_"), f"@{name.split()[0].lower()}.designs",
            {"full_name": name, "handle": f"@{name.split()[0].lower()}.designs", "tools": f"{tools} · {city}",
             "portfolio_url": f"behance.net/{name.split()[0].lower()}", "first_design": f"A {city} streetwear piece · {pieces} pieces in portfolio"},
            tag, tone, "reviewing", days=3)
    for ref, handle, sw, tag, tone in [("SH-APP-CLP-5T8N", "@kwame.reels", "TikTok 8.2K · 3 sample clips · CapCut", "Samples 4.5 / 5", "ok"),
                                       ("SH-APP-CLP-1J6V", "@adwoa.cuts", "TikTok 3.1K · Instagram 2.4K · Premiere", "Samples 4 / 5", "ok"),
                                       ("SH-APP-CLP-9B3X", "@fiifi.gg", "YouTube Shorts 12K · gaming only", "Samples 4 / 5", "ok"),
                                       ("SH-APP-CLP-4D7L", "@esi.edits", "TikTok 950 · new account, strong samples", "Small audience", "warn"),
                                       ("SH-APP-CLP-6Q2Z", "@bigviewsgh", "TikTok 41K · follower jump of 30K in one week", "Check for bought views", "no"),
                                       ("SH-APP-CLP-3K5R", "@osu.shorts", "TikTok 2.2K · CapCut", None, None),
                                       ("SH-APP-CLP-7N2W", "@legon.clips", "Instagram 1.8K · VN", None, None),
                                       ("SH-APP-CLP-2P8Q", "@kaneshie.cuts", "TikTok 5.4K · CapCut", None, None),
                                       ("SH-APP-CLP-8M3T", "@ho.highlights", "YouTube Shorts 900 · Premiere", None, None)]:
        app("clipper", ref, handle, ref.lower().replace("-", "_"), handle,
            {"handle": handle, "software": sw, "tiktok_url": f"tiktok.com/{handle}", "best_clip_url": f"tiktok.com/{handle}/video/1",
             "what_travels": "The first three seconds."}, tag, tone, "reviewing" if tag else "new", days=rng.randint(1, 4))
    streamer_apps = [("SH-APP-STR-4K7Q", "Applicant 4K7Q", "Sport and gaming", "Accra", "2 years streaming · assessment score 86", "Seat 02 shortlist", "ok"),
                     ("SH-APP-STR-9P2M", "Applicant 9P2M", "Music and talk", "Kumasi", "8 months streaming · score 78", "Seat 03 shortlist", "ok"),
                     ("SH-APP-STR-7C5T", "Applicant 7C5T", "Comedy", "Takoradi", "New to streaming · strong on camera", "Needs tryout", "warn")]
    for i in range(20):
        code = f"{rng.choice('23456789')}{rng.choice('ABCDEFGHJKMNPQRSTUVWXYZ')}{rng.choice('23456789')}{rng.choice('ABCDEFGHJKMNPQRSTUVWXYZ')}"
        streamer_apps.append((f"SH-APP-STR-{code}", f"Applicant {code}", rng.choice(["Gaming", "Sport and gaming", "Music", "Lifestyle", "Tech", "Comedy"]),
                              rng.choice(["Accra", "Kumasi", "Tema", "Cape Coast", "Tamale"]), f"{rng.randint(1, 30)} months streaming", None, None))
    seat_app = None
    for ref, name, content, city, note, tag, tone in streamer_apps:
        a = app("streamer", ref, name, ref.lower().replace("-", "_"), None,
                {"stage_name": name, "full_name": name, "city": city, "content": content, "channel_url": "youtube.com/@applicant",
                 "weekly_hours": "20", "why": note}, tag, tone, "reviewing" if tag else "new", days=rng.randint(1, 10))
        if ref == "SH-APP-STR-4K7Q":
            seat_app = a
    await db.flush()
    db.add(Decision(title="Offer Seat 02 to applicant SH-APP-STR-4K7Q", source="Talent · assessment score 86", kind="seat_offer",
                    entity_id=str(seat_app.id), requested_by=users["certified"].id))
    for handle, days in [("@nanaframes", 8), ("@clipsbyefya", 10)]:
        a = app("clipper", f"SH-APP-CLP-{handle[1:5].upper()}", handle, f"done_{handle[1:]}", handle, {"handle": handle}, status="approved",
                days=days + 2, decided=days)
        a.created_user_id = clippers[handle].id
    for d in designs_made:
        if d.title in ("Accra After Dark Tee", "Signed Cap"):
            d.decided_at = at(T - timedelta(days={"Accra After Dark Tee": 7, "Signed Cap": 12}[d.title]))

    # Social, creative and tech ------------------------------------------------------------------------
    ws = T - timedelta(days=T.weekday())
    cal = [["YouTube highlight", "TikTok ×2"], ["Football night promo|live_promo", "Instagram Reel"], ["YouTube Short ×2", "TikTok ×2"],
           ["Rush+ trial clip|brand", "Stream promo|live_promo"], ["First-look teaser|live_promo", "Merch post"],
           ["Weekend cup live|live_promo", "TikTok ×3"], ["Week recap", "Top clipper shout-out"]]
    for i, posts in enumerate(cal):
        d = ws + timedelta(days=i)
        for p in posts:
            title, _, kind = p.partition("|")
            db.add(SocialPost(on_date=d, title=title, kind=kind or "post", status="posted" if d <= T else "planned", owner_id=users["trigger"].id))
    posted_target = 184 - sum(len(p) for i, p in enumerate(cal) if ws + timedelta(days=i) <= T and ws + timedelta(days=i) >= M0)
    month_days = max(1, (min(T, ws - timedelta(days=1)) - M0).days + 1) if ws > M0 else 0
    for i in range(max(0, posted_target) if month_days else 0):
        d = M0 + timedelta(days=i % month_days)
        db.add(SocialPost(on_date=d, title=rng.choice(["TikTok clip", "YouTube Short", "Instagram Reel", "Stream promo", "X post"]),
                          kind="post", status="posted", owner_id=users["trigger"].id))
    board_cards = [("requested", "Social", 1, "Thumbnails for 3 highlights", "From Social · YouTube", "c9per"),
                   ("requested", "Store", 2, "Drop 002 product photos", "From Store · 5 pieces", "c9per"),
                   ("in_progress", "Production", 0, "Weekend cup highlight edit", "4 marked moments · 90 seconds", "c9per"),
                   ("in_progress", "Partnerships", 7, "Rush+ Energy overlay", "Logo bug and lower third", "kingmille"),
                   ("in_progress", "Social", 7, "Top clipper graphic", "Weekly template", "kingmille"),
                   ("in_review", "Store", 0, "Drop 002 lookbook", "Waiting for Founder", "kingmille"),
                   ("in_review", "Production", 0, "New stream intro, 8 seconds", "Waiting for Producer", "c9per")]
    for i, (status, area, due, title, detail, who) in enumerate(board_cards):
        db.add(CreativeRequest(title=title, detail=detail, from_area=area, status=status, due_on=T + timedelta(days=due),
                               assignee_id=users[who].id, requested_by=users["trigger"].id, position=i))
    for i in range(38):
        who = "c9per" if i < 24 else "kingmille"
        title, area, detail = ("Weekend cup poster", "Social", "Used on 4 platforms") if i == 36 else \
            (("Media kit, November", "Partnerships", "Sent to 6 brands") if i == 37 else (f"Asset {i + 1}", rng.choice(["Social", "Store", "Production"]), None))
        delivered = at(max(M0, T - timedelta(days=(37 - i) % max(1, days_in + 1))), 15)
        db.add(CreativeRequest(title=title, detail=detail, from_area=area, status="delivered", assignee_id=users["kingmille" if i >= 36 else who].id,
                               delivered_at=delivered, due_on=delivered.date(), on_time=(i % 12 != 0), position=100 + i))
    # Gear: Galacta's seat was bought for him (recorded as purchases, so it is in the finances); the studio
    # kit is the company's. Streamers see their gear, never the cost.
    from app.models.money import Expense
    from app.models.tech import EquipmentIssue

    bought = date(2026, 9, 4)
    seat_buy = Expense(on_date=bought, category="equipment", description="Seat 01 setup for Galacta", vendor="Compu Ghana, Accra Mall",
                       amount_minor=cedis(21850), paid_with="bank", reference="CG-INV-20931", streamer_id=galacta.id,
                       entered_by=users["ahmed"].id)
    studio_buy = Expense(on_date=date(2026, 9, 2), category="equipment", description="Studio 01 furniture and internet kit",
                         vendor="Melcom, Achimota", amount_minor=cedis(9600), paid_with="momo", entered_by=users["ahmed"].id)
    db.add_all([seat_buy, studio_buy])
    await db.flush()
    # Sample purchases only: their ledger lines are left out so the sample P&L keeps the figures in the designs.
    # Purchases recorded in Hub OS always post to the ledger.
    for name, tag, to, cond, note, cost, buy, serial in [
            ("4K / 2K display", "SH-EQ-001", "Galacta", "good", None, 4200, seat_buy, "LG27GP850-1183"),
            ("Console", "SH-EQ-002", "Galacta", "good", None, 9800, seat_buy, "PS5-CFI-2016-77"),
            ("Extra controller", "SH-EQ-003", "Galacta", "fault", "Stick drift", 1150, seat_buy, None),
            ("Stream camera", "SH-EQ-004", "Galacta", "good", None, 2300, seat_buy, None),
            ("Headset", "SH-EQ-005", "Galacta", "good", None, 1500, seat_buy, None),
            ("Capture card", "SH-EQ-006", "Galacta", "good", None, 2900, seat_buy, "EG-4KX-0042"),
            ("Gaming table", "SH-EQ-007", "Studio 01", "on_order", None, 2600, studio_buy, None),
            ("Gaming chair", "SH-EQ-008", "Studio 01", "on_order", None, 3200, studio_buy, None),
            ("Satellite internet kit", "SH-EQ-009", "Studio 01", "good", None, 3800, studio_buy, None)]:
        g = Equipment(tag=tag, name=name, assigned_to=to, condition=cond, condition_note=note, purchase_cost_minor=cedis(cost),
                      expense_id=buy.id, purchased_on=buy.on_date, serial_number=serial,
                      streamer_id=galacta.id if buy is seat_buy else None)
        db.add(g)
        await db.flush()
        if tag == "SH-EQ-003":
            db.add(EquipmentIssue(equipment_id=g.id, reported_by=users["galacta"].id, detail="Left stick drifts up when I let go. Hard to aim in Apex."))
    for d, cat, desc, vendor, amount, method in [
            (date(2026, 11, 1), "rent", "Studio rent, November", "East Legon landlord", 3500, "bank"),
            (date(2026, 11, 3), "internet_power", "Fibre internet, November", "Telecel Business", 650, "momo"),
            (date(2026, 11, 6), "software", "Streaming and editing software", "Adobe and StreamElements", 420, "card"),
            (date(2026, 11, 9), "transport", "Courier for merch orders", "Bolt Delivery", 280, "momo")]:
        if d > today():
            continue
        db.add(Expense(on_date=d, category=cat, description=desc, vendor=vendor, amount_minor=cedis(amount), paid_with=method,
                       entered_by=users["ahmed"].id))
    db.add_all([Incident(title="Controller stick drift", started_at=at(T - timedelta(days=3)), cause="Replacement requested", reported_by=users["bernard"].id),
                Incident(title="Power cut, stream dropped for 6 minutes", started_at=at(max(M0, T - timedelta(days=9))), resolved_at=at(max(M0, T - timedelta(days=9)), 23),
                         downtime_minutes=6, cause="Grid outage", fix="Moved to backup · inverter requested", reported_by=users["bernard"].id),
                Incident(title="Late start, capture card not detected", started_at=at(T - timedelta(days=3)), resolved_at=at(T - timedelta(days=3), 21),
                         cause="Loose cable", fix="Cable replaced · added to checklist", reported_by=users["certified"].id)])
    for i, (name, managers) in enumerate([("YouTube channel", "3 managers"), ("Twitch", "2 managers"), ("Kick", "2 managers"),
                                          ("TikTok", "2 managers"), ("Store payments", "Finance only")]):
        db.add(AccessAccount(name=name, managers=managers, position=i))

    # Posts on the website and link pages ------------------------------------------------------------
    from app.models.posts import LinkItem, LinkPage, Post, TicketBooking

    tonight = datetime.combine(T, time(19, 0), ACCRA)
    gaming_night = datetime.combine(T + timedelta(days=(5 - T.weekday()) % 7 + 7), time(16, 0), ACCRA)
    posts = [
        Post(slug="galacta-ranked-apex-tonight", kind="stream", title="Ranked Apex with Galacta, live tonight", topic="Gaming", theme="y",
             summary="Road to Predator, game by game. Come through, drop a question in chat and catch the clutch moments live.",
             streamer_id=galacta.id, image_key="/brand/galacta.jpg", share_image_key="/brand/galacta.jpg", image_layout="thumbnail",
             link_url="https://www.youtube.com/@StreamsHub/live", platform="youtube", starts_at=tonight, ends_at=tonight + timedelta(hours=3),
             status="published", published_at=at(T - timedelta(days=1), 10), pinned=True, created_by=users["trigger"].id,
             views=486, clicks=211, likes=73, shares=38),
        Post(slug="accra-gaming-night-vol-1", kind="event", title="Accra Gaming Night, Vol. 1", topic="Tournament", theme="k",
             summary="EA FC and NBA 2K on the big screens. Play Galacta for a signed jersey, win store vouchers, meet the roster. Food and drinks on site.",
             streamer_id=galacta.id, venue="Streamers Hub Studio, East Legon, Accra", starts_at=gaming_night, ends_at=gaming_night + timedelta(hours=5),
             ticket_price_minor=cedis(50), ticket_capacity=120, tickets_open=True, status="published",
             published_at=at(T - timedelta(days=3), 12), created_by=users["philip"].id, views=1204, clicks=168, likes=142, shares=97),
        Post(slug="season-one-roster-seat-two", kind="news", title="Seat 02 is open: The Competitor", topic="Announcement", theme="t",
             summary="We're signing our second streamer. Ranked grinders and tournament players, this one is yours. Applications are open.",
             link_url="https://streamershubmedia.com/join?as=streamer", status="published", published_at=at(T - timedelta(days=6), 9),
             created_by=users["founder"].id, views=932, clicks=121, likes=88, shares=41),
    ]
    db.add_all(posts)
    await db.flush()
    for name, qty, status in [("Kwame Asante", 2, "interest"), ("Akosua Boateng", 1, "interest"), ("Yaw Darko", 4, "interest")]:
        db.add(TicketBooking(post_id=posts[1].id, code=f"SH-{uuid.uuid4().hex[:6].upper()}", name=name, email=f"{name.split()[0].lower()}@{DOMAIN}",
                             phone_enc=encrypt("0241112223"), phone_last3="223", qty=qty, amount_minor=cedis(50) * qty, status=status))
    org = LinkPage(slug="streamershub", title="Streamers Hub", bio="The content label for the internet's next big streamers.", theme="k")
    gp = LinkPage(slug="galacta", streamer_id=galacta.id, title="Galacta", bio="GTA 6 · NBA 2K · EA FC · Apex Legends. Signing No. 01 at Streamers Hub.", theme="y")
    db.add_all([org, gp])
    await db.flush()
    for page, links in [(org, [("youtube", "YouTube", "https://www.youtube.com/@StreamsHub"), ("tiktok", "TikTok", "https://www.tiktok.com/@streamshub"),
                               ("instagram", "Instagram", "https://www.instagram.com/streamshub"), ("x", "X", "https://x.com/StreamsHub"),
                               ("store", "The store", "https://streamershubmedia.com/store"), ("email", "Work with us", "mailto:contact@streamershubmedia.com")]),
                        (gp, [("youtube", "Watch live on YouTube", "https://www.youtube.com/@StreamsHub/live"), ("twitch", "Twitch", "https://www.twitch.tv/galacta"),
                              ("kick", "Kick", "https://kick.com/galacta"), ("tiktok", "TikTok clips", "https://www.tiktok.com/@galacta"),
                              ("instagram", "Instagram", "https://www.instagram.com/galacta"), ("store", "Galacta merch", "https://streamershubmedia.com/store")])]:
        for n, (kind, label, url) in enumerate(links):
            db.add(LinkItem(page_id=page.id, kind=kind, label=label, url=url, position=n, clicks=[84, 61, 40, 22, 17, 9][n]))

    # Open roles, and messages from the Contact page -----------------------------------------------------
    from app.models.contact import ContactMessage, ContactReply
    from app.services import ContactService

    def role(slug, title, summary, job, flyer, *, views, clicks, likes, shares, published, by, pinned=False):
        return Post(slug=slug, kind="job", title=title, summary=summary, topic="Hiring", theme="k", job=job,
                    image_key=f"/brand/{flyer}.jpg", share_image_key=f"/brand/{flyer}-share.jpg", image_layout="flyer",
                    status="published", published_at=at(T - timedelta(days=published), 9), created_by=users[by].id, pinned=pinned,
                    views=views, clicks=clicks, likes=likes, shares=shares)

    editor_role = role("video-editor-shorts-and-highlights", "Video Editor (Shorts and Highlights)",
                       "Cut our streams into the shorts, highlights and recaps that travel. You'll sit with the producer on stream nights and own the edit queue.",
                       {"department": "creative", "employment": "full_time", "workplace": "on_site", "location": "East Legon, Accra",
                        "closes_on": (T + timedelta(days=21)).isoformat(), "pay": "GHS 3,500 to 5,000 a month", "show_pay": True, "openings": 1,
                        "responsibilities": ["Cut 3 to 5 shorts a day from live streams and marked moments", "Edit a weekly highlight video for YouTube",
                                             "Keep captions, sound and brand rules tight on every clip", "Work with the producer and social team on what to post"],
                        "requirements": ["A showreel of short-form edits (TikTok, Shorts or Reels)", "Premiere Pro or DaVinci Resolve, and CapCut",
                                         "Fast turnaround: a moment cut and posted within the hour", "Evenings on stream nights"],
                        "nice_to_have": ["You know gaming culture", "Motion graphics in After Effects"], "apply_via": "form", "apply_url": None, "closed": False},
                       "hiring-video-editor", views=640, clicks=118, likes=54, shares=63, published=6, by="kingmille", pinned=True)
    intern_role = role("events-and-community-intern", "Events and Community Intern",
                       "Help us run game nights, campus tours and the Discord. Six months, paid, with a real shot at a full-time seat.",
                       {"department": "marketing", "employment": "internship", "workplace": "hybrid", "location": "Accra",
                        "closes_on": (T + timedelta(days=10)).isoformat(), "pay": "Paid internship", "show_pay": True, "openings": 2,
                        "responsibilities": ["Plan and run our monthly Accra Gaming Night", "Look after the community on Discord and WhatsApp",
                                             "Line up schools and partners for campus tours"],
                        "requirements": ["Organised, and good with people", "In Accra, free on weekends for events"],
                        "nice_to_have": ["You've run a tournament or campus event"], "apply_via": "form", "apply_url": None, "closed": False},
                       "hiring-events-intern", views=410, clicks=76, likes=31, shares=39, published=3, by="philip")
    camera_role = role("camera-operator", "Camera Operator",
                       "Run cameras for live streams and studio shoots, two to three evenings a week.",
                       {"department": "production", "employment": "part_time", "workplace": "on_site", "location": "East Legon, Accra",
                        "closes_on": (T - timedelta(days=5)).isoformat(), "pay": None, "show_pay": False, "openings": 1,
                        "responsibilities": ["Set up and run cameras on stream nights", "Shoot BTS for social"],
                        "requirements": ["Your own reel", "Evenings, 2 to 3 a week"], "nice_to_have": [], "apply_via": "form", "apply_url": None,
                        "closed": False},
                       "hiring-camera-operator", views=300, clicks=44, likes=12, shares=22, published=25, by="certified")
    db.add_all([editor_role, intern_role, camera_role])
    await db.flush()

    def message(reason, name, email_key, answers, text, *, hours, status="new", post=None, stage=None, phone="0244556677",
                replies=(), assigned=None, via=None):
        raw, token_hash, token_enc = ContactService.new_token()
        titles = {post.slug: post.title} if post else {}
        sent = sent_from - timedelta(hours=hours)
        if reason == "job" and post:
            answers = {**answers, "role": post.slug, "department": post.job["department"]}
        m = ContactMessage(id=uuid.uuid4(), reference=f"SH-MSG-{ContactService.short_code(5)}", reason=reason, desk=ContactService.REASONS[reason]["desk"],
                           subject=ContactService.subject_for(reason, answers, titles), name=name, email=f"{email_key}@{DOMAIN}",
                           phone_enc=encrypt(phone) if phone else None, phone_last3=phone[-3:] if phone else None,
                           organisation=answers.get("company") or answers.get("organisation") or answers.get("outlet"),
                           payload={**answers, "message": text}, files=[], post_id=post.id if post else None, status=status, stage=stage,
                           token_hash=token_hash, token_enc=token_enc, last_in_at=sent, via=via,
                           assigned_to=users[assigned].id if assigned else None, created_at=sent, updated_at=sent)
        db.add(m)
        thread = [("in", text, None, sent)] + [(d, b, who, sent_from - timedelta(hours=h)) for d, b, who, h in replies]
        for direction, body, who, when in thread:
            label = None
            if who:
                label = {"philip": "Philip, Co-Founder · Partnerships and Growth", "storeops": "Store Operations",
                         "founder": "Founder & President", "kingmille": "King Mille, Creative Lead"}[who]
            db.add(ContactReply(message_id=m.id, direction=direction, body=body, author_user_id=users[who].id if who else None,
                                author_label=label, emailed=True if direction == "out" else None, created_at=when, updated_at=when))
            if direction == "out":
                m.last_out_at = when
                m.first_reply_at = m.first_reply_at or when
            elif direction == "in":
                m.last_in_at = when
        return m

    sent_from = datetime.now(ACCRA)
    message("brand", "Esi Mensah", "esi.afa", {"company": "Afa Foods", "role": "Brand manager", "website": "https://afafoods.example",
            "interests": ["Sponsored stream segment", "Giveaway"], "streamers": ["Galacta"], "goal": "Launch a product",
            "audience": "18 to 30, Accra and Kumasi", "budget": "GHS 5,000 to 15,000", "start": (T + timedelta(days=14)).isoformat(),
            "duration": "1 month"},
            "We're launching a new spicy plantain chip in December and want it in front of gamers. Thinking a weekly segment on Galacta's "
            "stream plus a giveaway. Can you send a rate card and some audience numbers?", hours=3)
    message("booking", "Kojo Asare", "kojo.src", {"organisation": "University of Ghana SRC", "event_name": "Legon Games Week",
            "event_type": "School or campus", "date": (T + timedelta(days=24)).isoformat(), "time": "16:00", "city": "Accra",
            "venue": "The Great Hall", "audience": "500 to 2,000", "streamers": ["Galacta"], "appearance": ["Host or MC", "Play against fans"],
            "budget": "GHS 2,000 to 5,000", "travel": "Not discussed yet"},
            "We'd love Galacta to host the finals night and play the winners. Students have been asking for him all semester.",
            hours=50, status="awaiting", assigned="philip",
            replies=[("out", "Thanks Kojo, we'd love to be part of it. Galacta is free that evening. Could you send the run of show and "
                      "confirm whether the finals will be streamed?", "philip", 30),
                     ("note", "Rate card says GHS 4,000 for a hosted evening. Their budget is close; offer 3,500 with the stream rights.", "philip", 29),
                     ("in", "Run of show attached in the next email. Yes, we'd like it streamed on your channel too.", None, 4)])
    message("press", "Abena Owusu", "abena.joy", {"outlet": "Joy Prime", "role": "Producer", "outlet_url": "https://joy.example",
            "request": "An interview", "who": "The Founder", "deadline": (T + timedelta(days=6)).isoformat(), "format": "In person"},
            "We're doing a segment on Ghana's streaming scene for our Saturday show and would like 15 minutes with your founder at the studio.",
            hours=20, phone=None)
    message("order", "Kwesi Boateng", "kwesi.b", {"order_number": "SH-1043", "issue": "It hasn't arrived"},
            "Ordered the play-head hoodie last Tuesday. Tracking still says packing.", hours=28, status="replied", assigned="storeops",
            replies=[("out", "Sorry about the wait, Kwesi. It went out with the rider this morning, so you should have it by 6pm today.", "storeops", 22)])
    message("tickets", "Ama Serwaa", "ama.s", {"event": posts[1].slug, "issue": "A group or school booking"},
            "Can I book 15 tickets for our school club? We'd pay by bank transfer.", hours=9, post=posts[1])
    message("job", "Yaw Darko", "yaw.edits", {"city": "Madina, Accra", "experience": "1 to 2 years", "availability": "In 2 weeks",
            "portfolio": "https://youtube.com/@yawedits", "pay": "GHS 4,000"},
            "I've cut gaming shorts for two creators for a year, about 30 a week. I'd bring speed and an eye for the funny moment.",
            hours=70, post=editor_role, stage="shortlisted", status="replied", via="whatsapp", assigned="kingmille",
            replies=[("note", "Strong reel. Fast cuts, good captions. Book a test edit.", "kingmille", 60),
                     ("out", "Hi Yaw, thanks for applying. We liked your reel. Could you do a short paid test edit this week? We'll send footage.", "kingmille", 58)])
    message("job", "Efua Mensah", "efua.cuts", {"city": "Tema", "experience": "Under 1 year", "availability": "Right away",
            "portfolio": "https://tiktok.com/@efuacuts"},
            "Self-taught on CapCut and Premiere. I post edits of EA FC moments and one went to 400K views.", hours=12, post=editor_role, stage="applied",
            via="x")
    message("job", "Kofi Adjei", "kofi.a", {"city": "Accra", "experience": "3 to 5 years", "availability": "In a month",
            "portfolio": "https://vimeo.com/kofiadjei", "linkedin": "https://linkedin.com/in/kofiadjei"},
            "Four years editing for a TV station and two YouTube channels. Happy to lead the edit queue.", hours=40, post=editor_role,
            stage="interview", status="replied", via="site", assigned="kingmille",
            replies=[("out", "Thanks Kofi. Could you come in on Thursday at 2pm to meet the team?", "kingmille", 30)])
    message("job", "Nana Ama Boateng", "nana.ama", {"city": "Accra, Osu", "experience": "Starting out", "availability": "Right away"},
            "I ran the gaming club at Ashesi and organised three FIFA tournaments. I'd love to do this full time.", hours=5, post=intern_role,
            stage="applied", via="whatsapp")
    await db.flush()

    # Investors ----------------------------------------------------------------------------------------
    rnd = FundingRound(name="Pilot round", target_minor=cedis(160000), founders_committed_minor=cedis(110000), raised_minor=0, status="open",
                       opened_on=date(2026, 9, 1))
    db.add(rnd)
    await db.flush()
    for d, amount, label, basis in [(date(2026, 9, 1), 333000, "Sep · entry", "Entry valuation for the pilot round, agreed with the first holders."),
                                    (date(2026, 10, 1), 333000, "Oct", "No change: one month of trading, studio built."),
                                    (date(2026, 11, 1), 360000, "Nov", "Store open, first brand deal at contract, audience growing.")]:
        db.add(Valuation(amount_minor=cedis(amount), effective_date=d, basis_note=basis, label=label, set_by=users["founder"].id))
    for i, (name, email_key) in enumerate([("Kwame Mensah", "holder1"), ("Adjoa Boateng", "holder2"), ("Yaw Asante", "holder3")], 1):
        holder_id = AuthService.new_holder_id()
        inv = Investor(internal_number=f"SH-INV-{i:03d}", legal_name=name, email=f"{email_key}@{DOMAIN}", phone_enc=encrypt(f"024000000{i}"),
                       phone_last4=f"000{i}", phone_hash=lookup_hash(f"024000000{i}"), holder_id_hash=AuthService.hash_holder_id(holder_id),
                       holder_id_last4=holder_id[-4:], id_issued_at=at(date(2026, 9, 10)), id_revealed_at=at(date(2026, 9, 10)),
                       kyc_status="verified", status="active", onboarding_step=6, agreement_signed_name=name,
                       agreement_signed_at=at(date(2026, 9, 8)), identity_confirmed_at=at(date(2026, 9, 8)), tier_label="Pilot holder")
        db.add(inv)
        await db.flush()
        db.add(Investment(investor_id=inv.id, round_id=rnd.id, amount_minor=cedis(5000), shares=15000, percent=1.5,
                          signed_at=at(date(2026, 9, 8)), payment_ref=f"BANK-SEP-00{i}", payment_confirmed_at=at(date(2026, 9, 9)),
                          payment_confirmed_by=users["ahmed"].id))
        rnd.raised_minor += cedis(5000)
        db.add_all([InvestorDocument(investor_id=inv.id, kind="agreement", title="Investment agreement", detail="Signed · PDF"),
                    InvestorDocument(investor_id=inv.id, kind="certificate", title="Share certificate", detail="15,000 shares · PDF"),
                    InvestorDocument(investor_id=inv.id, kind="receipt", title="Payment receipt", detail="GHS 5,000 · PDF")])
        if i == 1:
            db.add(InvestorMessage(investor_id=inv.id, from_investor=False, author_user_id=users["aaron"].id,
                                   body="Welcome aboard. Your October report is in the vault. Ask us anything here."))
        out["holders"].append((inv.internal_number, name, inv.email, holder_id))
    # A fourth investor mid-onboarding, payment already matched, to try the welcome flow
    token = "sample-invite-investor-four"
    inv4 = Investor(internal_number="SH-INV-004", legal_name="Esi Owusu", email=f"holder4@{DOMAIN}", status="invited", onboarding_step=1,
                    invite_token_hash=hash_token(token), invite_expires_at=at(T + timedelta(days=7)), phone_last4="0004",
                    phone_enc=encrypt("0240000004"), phone_hash=lookup_hash("0240000004"))
    db.add(inv4)
    await db.flush()
    # Finance hasn't matched this payment yet: the welcome flow waits at step 4 until they do
    db.add(Investment(investor_id=inv4.id, round_id=rnd.id, amount_minor=cedis(5000), shares=15000, percent=1.5))
    db.add_all([InvestorDocument(investor_id=inv4.id, kind="agreement", title="Investment agreement", detail="To sign · PDF"),
                InvestorDocument(investor_id=inv4.id, kind="certificate", title="Share certificate", detail="15,000 shares · PDF"),
                InvestorDocument(investor_id=inv4.id, kind="receipt", title="Payment receipt", detail="GHS 5,000 · PDF")])
    out["invites"].append(("Investor welcome (SH-INV-004)", f"/vault/welcome?token={token}"))
    report_body = ("Highlights\n\nStudio built and equipped. Galacta live six nights a week. YouTube subscribers up 68 this month.\n\n"
                   "Money\n\nIncome rose 41% on October. We are spending within budget, with 7.4 months of runway.\n\n"
                   "Next\n\nSign the first paid brand deal (Rush+ Energy is at contract) and close the pilot round.")
    db.add_all([InvestorReport(title="September 2026 report", period=sep_key, body=report_body, read_minutes=5,
                               published_at=at(oct0 + timedelta(days=3)), published_by=users["ahmed"].id),
                InvestorReport(title="October 2026 report", period=oct_key, body=report_body, read_minutes=6,
                               published_at=at(M0 + timedelta(days=min(4, days_in))), published_by=users["ahmed"].id),
                InvestorReport(title="November 2026 report", period=now_month, body="Draft.", due_on=date(2026, 12, 5))])
    for i, (t, s, d) in enumerate([("Studio built and equipped", "done", "Sep"), ("First streamer signed and live", "done", "Sep"),
                                   ("Store open · Drop 001", "done", "Oct"), ("First paid brand deal", "doing", "At contract"),
                                   ("Close the pilot round", "doing", "78%"), ("Sign roster seat 02", "next", "After pilot")]):
        db.add(Milestone(title=t, status=s, detail=d, position=i))

    # Clippers working the moments: some claimed, some already cut and linked
    await db.flush()
    by_stream = {}
    for m in moment_rows:
        by_stream.setdefault(m.stream_id, []).append(m)
    linked = 0
    for c in (await db.execute(select(Clip).where(Clip.source_stream_id.in_(list(by_stream))).order_by(Clip.posted_at))).scalars():
        if linked >= 6:
            break
        c.moment_id = by_stream[c.source_stream_id][linked % len(by_stream[c.source_stream_id])].id
        linked += 1
    # Two YouTube Shorts waiting for a check. They count in December because November's run is already submitted.
    # Sample counts (with SH_YOUTUBE_API_KEY set they come from YouTube): one close to the claim, one well under it
    for handle, code, title, reported, fetched in [("@nanaframes", "Nf3kQ8yTb2A", "Signed the cap live for a viewer", 6400, 6210),
                                                   ("@accra.afterdark", "Ad7pL0xZq9M", "Galacta sings the anthem", 21000, 11550)]:
        db.add(Clip(clipper_user_id=clippers[handle].id, source_stream_id=live.id, url=f"https://www.youtube.com/shorts/{code}",
                    platform="youtube_shorts", title=title, posted_at=at(T, 8), views_reported=reported, status="submitted",
                    period=next_month(now_month), views_fetched=fetched, views_fetched_at=at(T, 9), platform_channel=handle.lstrip("@").replace(".", " ").title()))
    claimers = [clippers[h] for h in ("@kwesi.cuts", "@nanaframes", "@accra.afterdark")]
    for i, m in enumerate(moment_rows[:6] + [x for x in moment_rows if x.source == "streamer"][:2]):
        for u in (claimers[i % 3:] + claimers[:i % 3])[: (i % 2) + 1]:
            db.add(MomentClaim(moment_id=m.id, clipper_user_id=u.id))

    # Agreements ---------------------------------------------------------------------------------------
    # Templates come with the schema. Everyone signed theirs when they joined; Galacta is part-way through the
    # 6-month trial (counted from the day he signed), and the invited Community lead and SH-INV-004 haven't signed yet.
    await db.flush()
    tpl = {t.key: t for t in (await db.execute(select(AgreementTemplate))).scalars() if t.key}

    def agreement(key, name, *, user=None, investor=None, terms=None, signed=None, offered=None, title=None):
        t = tpl[key]
        a = Agreement(user_id=user.id if user else None, investor_id=investor.id if investor else None, template_id=t.id,
                      template_version=t.version, audience=t.audience, stage=t.stage, title=title or t.name, body=t.body,
                      terms=t.default_terms if terms is None else terms, term_months=t.term_months, renewable=t.renewable,
                      status="offered", offered_at=at(offered or signed or T), offered_by=users["founder"].id)
        if signed:
            a.status, a.signed_name, a.signed_at, a.starts_on = "signed", name, at(signed, 10), signed
            a.ends_on = signed + relativedelta(months=t.term_months) - timedelta(days=1) if t.term_months else None
        db.add(a)
        return a

    vest = "Over 48 months with a 12-month cliff"
    equity = {"philip": 8, "bernard": 8, "aaron": 8, "ahmed": 2, "kingmille": 1.5, "c9per": 1, "trigger": 1, "certified": 1}
    for key, name, role, *_ in staff:
        if role in ("founder", "streamer"):
            continue
        terms = [{"kind": "equity", "label": "Vesting shares", "value": f"{equity[key]:g}%", "detail": vest}] if key in equity else \
            [{"kind": "salary", "label": "Monthly pay", "value": "GHS 1,800", "detail": "Paid on the last working day"}]
        if key == "certified":  # the producer also earns from the streamer he produces
            terms.append({"kind": "revenue_share", "label": "Producer share", "value": "5%",
                          "detail": "Of Galacta's platform earnings (Twitch, YouTube, Kick), paid monthly"})
        agreement("staff-standard", users[key].name, user=users[key], terms=terms, signed=date(2026, 9, 1))
    agreement("staff-standard", "Video editor", user=users["editor"], signed=T - timedelta(days=2),
              terms=[{"kind": "equity", "label": "Vesting shares", "value": "0.5%", "detail": vest}])
    agreement("staff-standard", "", user=users["community"], offered=T - timedelta(days=1),
              terms=[{"kind": "equity", "label": "Vesting shares", "value": "0.5%", "detail": vest},
                     {"kind": "bonus", "label": "Community growth bonus", "value": "GHS 200", "detail": "Each month Discord members grow 10% or more"}])
    trial_start = T - timedelta(days=75)
    trial = agreement("streamer-trial", users["galacta"].name, user=users["galacta"], signed=trial_start,
                      terms=[{"kind": "salary", "label": "Monthly pay", "value": "Salary tier", "detail": "Set on the 1st of each month from the performance score"},
                             {"kind": "revenue_share", "label": "Brand deals", "value": "60% to Galacta", "detail": "Of each deal, paid when the brand pays"}])
    galacta.contract_start, galacta.contract_end = trial.starts_on, trial.ends_on
    for handle, u in designers.items():
        agreement("designer-standard", u.name, user=u, signed=date(2026, 9, 20))
    for handle, u in clippers.items():
        agreement("clipper-standard", u.name, user=u, signed=date(2026, 10, 1))
    for inv in (await db.execute(select(Investor).order_by(Investor.internal_number))).scalars():
        investment = await db.scalar(select(Investment).where(Investment.investor_id == inv.id))
        terms = await AgreementService.investor_terms(investment, inv.tier_label)
        if inv.agreement_signed_at:
            agreement("investor-standard", inv.legal_name, investor=inv, terms=terms, signed=date(2026, 9, 8))
        else:
            agreement("investor-standard", inv.legal_name, investor=inv, terms=terms, offered=T - timedelta(days=1))
    return out


async def main(reset: bool) -> None:
    if settings.is_production:
        raise SystemExit("Refusing to seed: ENVIRONMENT is production. This script writes sample data only.")
    await session_manager.init(setup=False)
    load_models()
    async with session_manager.engine.begin() as conn:
        if reset:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(text("DROP SEQUENCE IF EXISTS order_number_seq"))
            await conn.execute(text("DROP SEQUENCE IF EXISTS design_ref_seq"))
        await setup_schema(conn)
    from app.core.database import async_sessionmaker

    factory = async_sessionmaker(bind=session_manager.engine, expire_on_commit=False, autoflush=False)
    async with factory() as db:
        if await db.scalar(select(func.count()).select_from(User)):
            raise SystemExit("The database already has users. Run with --reset to wipe it and seed again.")
        out = await seed(db, reset)
        await db.commit()
    await session_manager.close()
    print("\nSample data loaded (SAMPLE ONLY, never for production).")
    print(f"Password for every sample account: {out['password']}")
    print("Accounts: founder@, philip@, bernard@, aaron@, ahmed@, certified@, kingmille@, c9per@, trigger@, storeops@, galacta@, "
          "ama_draws@, kofi_inks@, kwesi_cuts@, nanaframes@ ... all @streamershub.test")
    print("One-time codes for these accounts are printed in the API log (they are never emailed).")
    print(f"November payout run: GHS {out['run'][0] // 100:,} for {out['run'][1]} payees, waiting for the Founder.")
    print("\nInvestor Holder IDs (sample holders; contact = the email):")
    for number, name, email, holder_id in out["holders"]:
        print(f"  {number}  {name:<15} {email:<28} {holder_id}")
    print("\nLinks to try onboarding (add to the website URL):")
    for label, path in out["invites"]:
        print(f"  {label}: {path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="drop every table first")
    asyncio.run(main(parser.parse_args().reset))
