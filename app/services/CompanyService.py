"""Company-level numbers shared by the Founder's command centre, department desks and the investor vault.
Every figure is worked out here so dashboards do no business maths."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.applications import Application
from app.models.clips import ClipperProfile
from app.models.money import Budget, CashBalance, LedgerEntry, PayrollRun
from app.models.partnerships import Brand, Deal
from app.models.people import Goal, KpiSnapshot, User
from app.models.store import Order
from app.models.talent import CreativeRequest, RosterSeat, SocialPost, Stream, Streamer
from app.models.tech import Incident
from app.services.money import month_key, prev_month

ACCRA = ZoneInfo("Africa/Accra")
PAID_STATUSES = ("paid", "packing", "to_ship", "shipped", "delivered")
OPEN_ORDER_STATUSES = ("paid", "packing", "to_ship")


def today() -> date:
    return datetime.now(ACCRA).date()


def month_bounds(key: str) -> tuple[date, date]:
    y, m = map(int, key.split("-"))
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    return start, end


def day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, ACCRA).astimezone(timezone.utc)


async def latest_kpi(db: AsyncSession, key: str) -> Optional[float]:
    row = (await db.execute(select(KpiSnapshot.value).where(KpiSnapshot.metric_key == key)
                            .order_by(KpiSnapshot.captured_at.desc()).limit(1))).scalar_one_or_none()
    return float(row) if row is not None else None


async def kpi_change(db: AsyncSession, key: str, since: datetime) -> Optional[float]:
    latest = await latest_kpi(db, key)
    before = (await db.execute(select(KpiSnapshot.value).where(KpiSnapshot.metric_key == key,
                                                               KpiSnapshot.captured_at < since)
                               .order_by(KpiSnapshot.captured_at.desc()).limit(1))).scalar_one_or_none()
    if latest is None or before is None:
        return None
    return latest - float(before)


async def ledger_totals(db: AsyncSession, key: str) -> dict:
    start, end = month_bounds(key)
    rows = (await db.execute(select(LedgerEntry.type, LedgerEntry.category, func.sum(LedgerEntry.amount_minor))
                             .where(LedgerEntry.on_date >= start, LedgerEntry.on_date < end)
                             .group_by(LedgerEntry.type, LedgerEntry.category))).all()
    out = {"revenue": {}, "expense": {}}
    for t, cat, total in rows:
        out[t][cat] = int(total or 0)
    out["income_minor"] = sum(out["revenue"].values())
    out["spend_minor"] = sum(out["expense"].values())
    out["net_minor"] = out["income_minor"] - out["spend_minor"]
    return out


async def money_summary(db: AsyncSession) -> dict:
    key = month_key(today())
    now_m = await ledger_totals(db, key)
    cash = (await db.execute(select(CashBalance).order_by(CashBalance.on_date.desc()).limit(1))).scalar_one_or_none()
    spends = [now_m["spend_minor"]]
    k = key
    for _ in range(2):
        k = prev_month(k)
        spends.append((await ledger_totals(db, k))["spend_minor"])
    spends = [s for s in spends if s]
    burn = sum(spends) / len(spends) if spends else 0
    runway = round(cash.amount_minor / burn, 1) if cash and burn else None
    return {"month": key, "income_minor": now_m["income_minor"], "spend_minor": now_m["spend_minor"],
            "net_minor": now_m["net_minor"], "cash_minor": cash.amount_minor if cash else None,
            "cash_date": cash.on_date.isoformat() if cash else None, "runway_months": runway,
            "burn_minor": int(burn)}


async def pilot_targets(db: AsyncSession) -> list[dict]:
    goals = list((await db.execute(select(Goal).where(Goal.is_pilot_target.is_(True)).order_by(Goal.sort))).scalars())
    out = []
    for g in goals:
        current = await latest_kpi(db, g.metric_key) if g.metric_key else None
        current = float(g.current) if current is None else current
        target = float(g.target)
        pct = min(100, round(current * 100 / target)) if target else 0
        item = {"key": g.metric_key, "label": g.title, "current": current, "target": target, "unit": g.unit,
                "percent": pct, "value": f"{current:,.0f} / {target:,.0f}", "note": "6-month target"}
        if g.metric_key == "posting_streak_days":
            item.update(value=f"{current:,.0f}-day streak", percent=100 if current >= 1 else 0, note="On target")
        if g.metric_key == "paid_brand_deals":
            signed = await db.scalar(select(func.count()).select_from(Deal).where(Deal.status.in_(("signed", "done"))))
            at_contract = (await db.execute(select(Deal).where(Deal.status == "at_contract"))).scalars().all()
            item["current"] = float(signed or 0)
            item["value"] = f"{signed or 0} signed · {len(at_contract)} at contract"
            item["percent"] = 100 if signed else (70 if at_contract else 20)
            if at_contract:
                item["note"] = f"{len(at_contract)} at contract · GHS {at_contract[0].value_minor // 100:,}"
        out.append(item)
    return out


async def pipeline(db: AsyncSession) -> dict:
    deals = (await db.execute(select(Deal, Brand).join(Brand, Brand.id == Deal.brand_id)
                              .where(Brand.status.in_(("in_talks", "at_contract")),
                                     Deal.status.in_(("draft", "at_contract"))))).all()
    return {"value_minor": sum(d.value_minor for d, _ in deals), "deals": len(deals)}


async def store_month(db: AsyncSession, key: str) -> dict:
    start, end = month_bounds(key)
    orders = (await db.execute(select(Order).where(Order.status.in_(PAID_STATUSES), Order.paid_at >= day_start(start),
                                                   Order.paid_at < day_start(end)))).scalars().all()
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    ledger = await ledger_totals(db, key)  # includes cash sales at the studio, recorded by Finance
    return {"sales_minor": ledger["revenue"].get("store", 0), "orders": len(orders),
            "orders_week": sum(1 for o in orders if o.paid_at and o.paid_at >= week_ago)}


async def company_kpis(db: AsyncSession) -> list[dict]:
    key = month_key(today())
    month_start = day_start(month_bounds(key)[0])
    cur, last = await ledger_totals(db, key), await ledger_totals(db, prev_month(key))
    money = await money_summary(db)
    viewers, subs = await latest_kpi(db, "avg_live_viewers"), await latest_kpi(db, "yt_subscribers")
    dv, ds = await kpi_change(db, "avg_live_viewers", month_start), await kpi_change(db, "yt_subscribers", month_start)
    pipe = await pipeline(db)
    store = await store_month(db, key)
    growth = round((cur["income_minor"] - last["income_minor"]) * 100 / last["income_minor"]) if last["income_minor"] else None

    def change(v, suffix):
        if v is None:
            return None, "sub"
        return (f"▲ {v:,.0f} {suffix}" if v >= 0 else f"▼ {abs(v):,.0f} {suffix}"), ("up" if v >= 0 else "dn")

    vd, vc = change(dv, "this month")
    sd, sc = change(ds, "this month")
    return [
        {"key": "revenue", "label": "Revenue · month to date", "money": cur["income_minor"], "tone": "y",
         "note": (f"▲ {growth}% vs last month" if growth >= 0 else f"▼ {abs(growth)}% vs last month") if growth is not None else None,
         "note_tone": "up" if (growth or 0) >= 0 else "dn"},
        {"key": "runway", "label": "Cash runway", "value": f"{money['runway_months']} months" if money["runway_months"] else "—",
         "note": "at current burn", "note_tone": "sub"},
        {"key": "viewers", "label": "Avg live viewers", "value": f"{viewers:,.0f}" if viewers is not None else "—", "note": vd, "note_tone": vc},
        {"key": "subs", "label": "YouTube subscribers", "value": f"{subs:,.0f}" if subs is not None else "—", "note": sd, "note_tone": sc},
        {"key": "pipeline", "label": "Brand pipeline", "money": pipe["value_minor"], "compact": True,
         "note": f"{pipe['deals']} deal{'s' if pipe['deals'] != 1 else ''} in talks", "note_tone": "sub"},
        {"key": "store", "label": "Store sales · MTD", "money": store["sales_minor"],
         "note": f"▲ {store['orders_week']} orders this week", "note_tone": "up"},
    ]


async def revenue_by_month(db: AsyncSession, months_back: int = 2, months_ahead: int = 4) -> list[dict]:
    key = month_key(today())
    keys = [key]
    for _ in range(months_back - 1):
        keys.insert(0, prev_month(keys[0]))
    k = key
    from app.services.money import next_month
    for _ in range(months_ahead):
        k = next_month(k)
        keys.append(k)
    rows = []
    for k in keys:
        if k <= key:
            t = await ledger_totals(db, k)
            brand = t["revenue"].get("brand", 0)
            other = t["revenue"].get("platform", 0) + t["revenue"].get("store", 0)
            forecast = False
        else:
            budget = (await db.execute(select(Budget).where(Budget.month == k, Budget.type == "revenue"))).scalars().all()
            brand = sum(b.planned_minor for b in budget if b.category == "brand")
            other = sum(b.planned_minor for b in budget if b.category != "brand")
            forecast = True
        rows.append({"month": k, "brand_minor": brand, "other_minor": other, "total_minor": brand + other, "forecast": forecast})
    top = max((r["total_minor"] for r in rows), default=0) or 1
    for r in rows:
        r["brand_height"] = round(r["brand_minor"] * 90 / top)
        r["other_height"] = round(r["other_minor"] * 90 / top)
    return rows


async def network(db: AsyncSession) -> dict:
    pending = lambda track: select(func.count()).select_from(Application).where(  # noqa: E731
        Application.track == track, Application.status.in_(("new", "reviewing")))
    return {
        "streamers": await db.scalar(select(func.count()).select_from(Streamer).where(Streamer.status == "signed")) or 0,
        "seats": await db.scalar(select(func.count()).select_from(RosterSeat)) or 0,
        "open_seats": await db.scalar(select(func.count()).select_from(RosterSeat).where(RosterSeat.streamer_id.is_(None))) or 0,
        "designers": await db.scalar(select(func.count()).select_from(User).where(User.role == "designer", User.status == "active")) or 0,
        "designers_pending": await db.scalar(pending("designer")) or 0,
        "clippers": await db.scalar(select(func.count()).select_from(ClipperProfile).join(User, User.id == ClipperProfile.user_id).where(
            ClipperProfile.status == "active", User.is_sample.is_(False))) or 0,
        "clippers_pending": await db.scalar(pending("clipper")) or 0,
        "brands_signed": await db.scalar(select(func.count()).select_from(Brand).where(Brand.status == "signed")) or 0,
        "brands_in_talks": await db.scalar(select(func.count()).select_from(Brand).where(Brand.status.in_(("in_talks", "at_contract")))) or 0,
    }


async def live_now(db: AsyncSession) -> Optional[dict]:
    row = (await db.execute(select(Stream, Streamer).join(Streamer, Streamer.id == Stream.streamer_id)
                            .where(Stream.status == "live").order_by(Stream.actual_start.desc()).limit(1))).first()
    if not row:
        return None
    stream, streamer = row
    return {"streamer": streamer.stage_name, "title": stream.title, "watching": stream.watching_now}


async def department_cards(db: AsyncSession) -> list[dict]:
    key = month_key(today())
    start, end = month_bounds(key)
    s0, s1 = day_start(start), day_start(end)

    streams = (await db.execute(select(Stream).where(Stream.scheduled_start >= s0, Stream.scheduled_start < s1,
                                                     Stream.status.in_(("done", "live"))))).scalars().all()
    on_time = sum(1 for s in streams if s.actual_start and (s.actual_start - s.scheduled_start) <= timedelta(minutes=5))
    kept = round(on_time * 100 / len(streams)) if streams else 0
    talks = await db.scalar(select(func.count()).select_from(Brand).where(Brand.status.in_(("in_talks", "at_contract")))) or 0
    signed = await db.scalar(select(func.count()).select_from(Deal).where(Deal.status.in_(("signed", "done")))) or 0
    posts = await db.scalar(select(func.count()).select_from(SocialPost).where(SocialPost.on_date >= start, SocialPost.on_date < end,
                                                                             SocialPost.status == "posted")) or 0
    streak = await latest_kpi(db, "posting_streak_days") or 0
    delivered = await db.scalar(select(func.count()).select_from(CreativeRequest).where(
        CreativeRequest.status == "delivered", CreativeRequest.delivered_at >= s0)) or 0
    in_review = await db.scalar(select(func.count()).select_from(CreativeRequest).where(CreativeRequest.status == "in_review")) or 0
    applicants = await db.scalar(select(func.count()).select_from(Application).where(
        Application.track == "streamer", Application.status.in_(("new", "reviewing")))) or 0
    seats = await db.scalar(select(func.count()).select_from(RosterSeat).where(RosterSeat.streamer_id.is_(None))) or 0
    uptimes = [float(s.uptime_percent) for s in streams if s.uptime_percent is not None]
    uptime = round(sum(uptimes) / len(uptimes), 1) if uptimes else None
    open_inc = await db.scalar(select(func.count()).select_from(Incident).where(Incident.resolved_at.is_(None))) or 0
    run = (await db.execute(select(PayrollRun).where(PayrollRun.period == key))).scalar_one_or_none()
    open_orders = (await db.execute(select(Order).where(Order.status.in_(OPEN_ORDER_STATUSES)))).scalars().all()
    ship_today = sum(1 for o in open_orders if o.status == "to_ship")

    def card(name, slug, lead, status, number, line, pct, href):
        tone = {"On track": "ok", "Watch": "warn", "Action": "no"}[status]
        return {"name": name, "slug": slug, "lead": lead, "status": status, "tone": tone, "number": number,
                "line": line, "percent": max(0, min(100, pct)), "href": href}

    run_line = "no payout run yet this month"
    run_status, run_pct = "On track", 30
    if run:
        run_line = {"draft": "payout run being prepared", "submitted": "payout run awaiting approval",
                    "approved": "payout run approved · paying out", "paid": "payout run paid"}[run.status]
        run_status = "Action" if run.status == "submitted" else "On track"
        run_pct = {"draft": 40, "submitted": 75, "approved": 90, "paid": 100}[run.status]
    return [
        card("Content and Production", "production", "Producer", "On track" if kept >= 85 else "Watch",
             f"{len(streams)} streams", f"this month · {kept}% schedule kept", kept, "/os/production"),
        card("Marketing and Partnerships", "partnerships", "Head of Marketing", "On track" if signed else "Watch",
             f"{talks} deal{'s' if talks != 1 else ''}", f"in talks · {signed} signed{' yet' if not signed else ''}",
             40 if not signed else 80, "/os/partnerships"),
        card("Social Media", "social", "Social Manager", "On track" if streak >= 7 else "Watch",
             f"{posts} posts", f"this month · {streak:,.0f}-day streak", min(100, round(streak * 100 / 30)), "/os/social"),
        card("Creative", "creative", "Creative Lead", "On track", f"{delivered} assets", f"delivered · {in_review} in review",
             min(100, 50 + delivered * 2), "/os/creative"),
        card("Talent and Recruitment", "talent", "Founder", "On track", f"{applicants} applicants",
             f"for {seats} open seat{'s' if seats != 1 else ''}", min(100, applicants * 3), "/os/review"),
        card("Tech and Dev", "tech", "Tech Lead", "On track" if (uptime or 100) >= 98 and open_inc <= 2 else "Watch",
             f"{uptime}% uptime" if uptime is not None else "No streams yet",
             f"stream uptime · {open_inc} open issue{'s' if open_inc != 1 else ''}", round(uptime or 0), "/os/tech"),
        card("Finance", "finance", "CFO", run_status, f"GHS {(run.total_minor if run else 0) // 100:,}", run_line, run_pct, "/os/finance"),
        card("Operations and Store", "store", "Founder", "On track" if ship_today <= 5 else "Watch",
             f"{len(open_orders)} orders", f"open · {ship_today} to ship today", 65 if open_orders else 100, "/os/store"),
    ]


async def company_summary(db: AsyncSession) -> dict:
    return {"pilot_targets": await pilot_targets(db), "money": await money_summary(db)}
