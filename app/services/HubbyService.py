"""Hubby, the helper in the corner of Hub OS and the portals: answers "where do I…" questions for any role.

Two modes, and Hubby always says which one it is in:
- AI mode: Claude answers from the guide entries this person can use (services/hubby_guide.py). It only
  guides: it never sees anyone's data and can't change anything. "Take me there" links come from the guide,
  never from the model, so they always open a real page this person can open.
- Offline guide: the website searches the same guide by the words typed. Used when there's no API key, the
  Founder has switched AI off, the monthly cap is reached, the person has asked a lot today, or the AI fails.

AI answers stream to the website as they're written. When the AI fails (an error, or no words within
SH_HUBBY_TIMEOUT_SECONDS), that question is answered from the guide instead, and the same if it breaks off
mid-answer. After 3 failures in a row Hubby stays on the guide for 5 minutes, then tries AI again.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AREAS, DEPARTMENTS, Access
from app.core.config import settings
from app.core.security import now
from app.models.hubby import HubbyQuestion
from app.models.people import Department, StaffProfile, User
from app.services.CompanyService import day_start, today
from app.services.hubby_guide import AREA_DEPARTMENT, GUIDE, Entry
from app.services.SettingsService import get_setting

logger = logging.getLogger(__name__)

MODELS = {"claude-opus-5-5": "Claude Opus 5.5", "claude-haiku-5-5": "Claude Haiku 5.5"}
# List prices, US dollars per million tokens: input, output, cache write (5 minutes), cache read
PRICES = {"claude-opus-5-5": (4.00, 20.00, 5.00, 0.20), "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01)}
# On a refusal the API re-runs the question on a model chosen for it (Haiku has no server-side fallback)
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5"}
FAILS_TO_PAUSE = 3
PAUSE = timedelta(minutes=5)
KEEP_QUESTIONS = timedelta(days=30)
KEEP_NUMBERS = timedelta(days=100)
MAX_A_DAY = 200  # questions of any kind from one person in a day, so the log can't be flooded
STALL_SECONDS = 15.0  # once the answer is coming, the longest wait for the next words
LONGEST_SECONDS = 60.0  # a whole answer
PORTAL_NAMES = {"designer": "the Designer Studio", "clipper": "the Clipper Desk", "investor": "the Investor Vault"}


@dataclass
class Asker:
    role: str
    name: str
    title: Optional[str] = None
    department: Optional[str] = None
    user_id: Any = None
    investor_id: Any = None
    access: Optional[Access] = None
    portal: Optional[str] = None  # designer, clipper or investor; None in Hub OS

    @property
    def first_name(self) -> str:
        return (self.name or "").split(" ")[0]


# The AI's health, for this API process -----------------------------------------------------------------------
_fails = 0
_paused_until: Optional[datetime] = None
_last_error: Optional[dict] = None


def reset_health() -> None:
    global _fails, _paused_until, _last_error
    _fails, _paused_until, _last_error = 0, None, None


def _failed(why: str) -> None:
    global _fails, _paused_until, _last_error
    _fails += 1
    _last_error = {"at": now().isoformat(), "message": why[:300]}
    if _fails >= FAILS_TO_PAUSE:
        _paused_until = now() + PAUSE
        _fails = 0
    logger.warning("Hubby AI failed, answering from the guide: %s", why)


def _succeeded() -> bool:
    """Returns True when the AI had been failing, so Hubby can say it's back."""
    global _fails, _paused_until
    was_down = _fails > 0 or _paused_until is not None
    _fails, _paused_until = 0, None
    return was_down


def sdk_ready() -> bool:
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


_client: Any = None
_client_key = ""


def make_client() -> Any:
    """One client for the process. Tests replace this function."""
    global _client, _client_key
    key = settings.anthropic_key
    if _client is None or _client_key != key:
        import anthropic

        # The first words must come within SH_HUBBY_TIMEOUT_SECONDS (checked in answer_stream); this only stops a
        # stalled connection
        _client = anthropic.AsyncAnthropic(api_key=key, max_retries=0,
                                           timeout=anthropic.Timeout(60.0, connect=5.0, read=STALL_SECONDS))
        _client_key = key
    return _client


client_factory: Callable[[], Any] = make_client


# Who can see what ----------------------------------------------------------------------------------------------
def can_see(entry: Entry, asker: Asker) -> bool:
    if entry.portal:
        return asker.portal == entry.portal
    a = asker.access
    if asker.portal or a is None or not a.can(entry.area or ""):
        return False
    role = a.user.role
    return {
        None: True,
        "founder": a.is_founder,
        "lead": role in ("founder", "cofounder", "dept_head"),
        "finance_work": a.does_finance,
        "streamer": role == "streamer",
        "not_streamer": role != "streamer",
        "not_founder": role != "founder",
    }.get(entry.only, False)


async def _leads(db: AsyncSession) -> dict[str, str]:
    """department slug -> "Name (title)" of its lead."""
    rows = (await db.execute(select(Department.slug, User.name, StaffProfile.title)
                             .join(User, User.id == Department.lead_user_id)
                             .outerjoin(StaffProfile, StaffProfile.user_id == User.id)
                             .where(User.status == "active"))).all()
    return {slug: (f"{name} ({title})" if title and title.lower() not in name.lower() else name) for slug, name, title in rows}


def _who_has(entry: Entry, asker: Asker, leads: dict[str, str]) -> Optional[str]:
    """For an entry this person can't use: whose job it is, in a sentence."""
    if entry.portal or entry.only == "streamer" or asker.access is None:
        return None
    area = entry.area or ""
    desk = AREAS.get(area, (area,))[0]
    founder = leads.get("leadership", "the Founder")
    my_head = leads.get(asker.department or "", "")
    head = f"your department head, {my_head}" if my_head and asker.access.user.role not in ("dept_head", "cofounder") else "the Founder"
    if entry.only == "founder" or area in ("command", "agreements", "settings"):
        return f"Only the Founder does this. Ask {founder}."
    if entry.only == "finance_work":
        return f"Finance does this. Ask {leads.get('finance', 'the Finance team')}."
    if entry.only == "lead" or area == "people":
        return f"Department heads and the Founder do this, in People and teams. Ask {head}."
    if area == "review":
        return f"Reviews are done by the Founder and the Creative, Social and Production desks. Ask {head}."
    if area == "inbox":
        return f"Messages from the website go to the desk they're for. Ask {head}."
    who = leads.get(AREA_DEPARTMENT.get(area) or "", "")
    return f"That's on the {desk} desk. Ask {who}." if who else f"That's on the {desk} desk."


async def guide_for(db: AsyncSession, asker: Asker) -> dict:
    mine = [e for e in GUIDE if can_see(e, asker)]
    leads = await _leads(db) if asker.access is not None else {}
    others = []
    for e in GUIDE:
        if e in mine:
            continue
        who = _who_has(e, asker, leads)
        if who:
            others.append({"id": e.id, "title": e.title, "words": e.words, "ask": who})
    return {"entries": [entry_out(e) for e in mine], "elsewhere": others}


def entry_out(e: Entry) -> dict:
    return {"id": e.id, "title": e.title, "where": e.where, "path": e.path, "steps": list(e.steps), "words": e.words}


PORTAL_PAGES = [("/studio", "Designer Studio"), ("/desk", "Clipper Desk"), ("/vault", "Investor Vault")]


def page_name(path: str) -> str:
    path = (path or "").split("?")[0].split("#")[0].rstrip("/") or "/"
    for prefix, name in PORTAL_PAGES:
        if path == prefix or path.startswith(prefix + "/"):
            return name
    matches = [(route, label) for label, route, _ in AREAS.values() if path == route or path.startswith(route + "/")]
    return max(matches, key=lambda m: len(m[0]))[1] if matches else "Hub OS"


# Mode -------------------------------------------------------------------------------------------------------------
def _month_start() -> datetime:
    return day_start(today().replace(day=1))


async def month_spend_micro(db: AsyncSession) -> int:
    return int(await db.scalar(select(func.coalesce(func.sum(HubbyQuestion.cost_micro_usd), 0))
                               .where(HubbyQuestion.created_at >= _month_start())) or 0)


def _mine(asker: Asker):
    return HubbyQuestion.investor_id == asker.investor_id if asker.investor_id else HubbyQuestion.user_id == asker.user_id


async def _asked_today(db: AsyncSession, asker: Asker, mode: Optional[str] = None) -> int:
    q = select(func.count()).select_from(HubbyQuestion).where(_mine(asker), HubbyQuestion.created_at >= day_start(today()))
    if mode:
        q = q.where(HubbyQuestion.mode == mode)
    return int(await db.scalar(q) or 0)


async def status(db: AsyncSession, asker: Optional[Asker], cfg: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """("ai", None), or ("guide", why)."""
    cfg = cfg or await get_setting(db, "hubby")
    if not settings.anthropic_key:
        return "guide", "no_key"
    if not sdk_ready():
        return "guide", "no_sdk"
    if not cfg.get("ai_on", True):
        return "guide", "off"
    if await month_spend_micro(db) >= float(cfg["monthly_cap_usd"]) * 1_000_000:
        return "guide", "cap"
    if _paused_until and now() < _paused_until:
        return "guide", "cooldown"
    if asker and await _asked_today(db, asker, "ai") >= int(cfg["daily_per_person"]):
        return "guide", "daily"
    return "ai", None


# Asking -------------------------------------------------------------------------------------------------------------
INSTRUCTIONS = """You are Hubby, the friendly helper inside Streamers Hub's apps. Streamers Hub is a streaming and \
media label in Accra, Ghana. Its team works in Hub OS; designers use the Designer Studio, clippers the Clipper Desk, \
and investors the Investor Vault. People ask you how to get around: where a page is, which button to press, how \
something is done.

Answer only from the guide below. It has the entries this person can use, and a list of things other desks do.
- When an entry answers the question, write one short sentence, then one to four numbered steps (1. 2. 3.) that \
name pages and buttons exactly as the guide does, in **bold**.
- When the question is about something another desk does, say so in a sentence or two, using that line of the guide.
- When the guide doesn't cover it, say so plainly and point them to the place named in the context. Never make up \
a page, button or rule that isn't in the guide.
- You can't see or change anyone's data, such as pay, tasks or figures. If they ask for their own figures, tell \
them where in the app they can see them.
- If the question isn't about using these apps, say in one sentence that you only help with getting around.
- "Here" or "this page" means the page they're on, given in the context.
- Write for someone who may be new to software and may read English as a second language: short sentences, \
everyday words, no jargon, no headings.
- Finish with a last line on its own: ENTRY: followed by the id of the guide entry you used (for example \
ENTRY: give-task), or ENTRY: none. The app turns it into a "Take me there" button and hides the line."""

TAG = "ENTRY:"


def guide_text(guide: dict) -> str:
    parts = ["<guide>"]
    for e in guide["entries"]:
        steps = "\n".join(f"{i}. {s}" for i, s in enumerate(e["steps"], 1))
        parts.append(f'<entry id="{e["id"]}">\nTitle: {e["title"]}\nWhere: {e["where"]}\nSteps:\n{steps}\n'
                     f'Other words for it: {e["words"]}\n</entry>')
    if guide["elsewhere"]:
        parts.append("<other_desks>")
        parts += [f"- {o['title']}: {o['ask']}" for o in guide["elsewhere"]]
        parts.append("</other_desks>")
    parts.append("</guide>")
    return "\n".join(parts)


def _context(asker: Asker, page: str) -> str:
    if asker.portal == "investor":
        who, nowhere = "an investor (a holder) in the Investor Vault", "Messages in the vault, to ask the founders"
    elif asker.portal:
        who, nowhere = f"a {asker.role} using {PORTAL_NAMES[asker.portal]}", "Ideas on their page (Suggest an idea), to ask the team"
    else:
        title = f", {asker.title}" if asker.title else ""
        dept = f", in {dict(DEPARTMENTS).get(asker.department, asker.department)}" if asker.department else ""
        who, nowhere = f"{asker.role}{title}{dept}, in Hub OS", "Suggestions (Send an idea), or their department head"
    return (f"<context>\nAsking: {asker.first_name}, {who}.\nThey are on: {page_name(page)} ({page or '/'}).\n"
            f"If the guide doesn't cover it, point them to: {nowhere}.\n</context>")


def cost_micro(model: str, usage: Any) -> tuple[int, dict]:
    tokens = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
    }
    p_in, p_out, p_write, p_read = PRICES.get(model, PRICES["claude-opus-5-5"])
    dollars = (tokens["input_tokens"] * p_in + tokens["output_tokens"] * p_out + tokens["cache_write_tokens"] * p_write
               + tokens["cache_read_tokens"] * p_read) / 1_000_000
    return round(dollars * 1_000_000), tokens


def _is_tag(line: str) -> bool:
    return line.strip().strip("*`").upper().startswith(TAG)


class Visible:
    """The answer as people should see it, while it streams: everything except the ENTRY: line. The line being
    written is held back only while it could still turn out to be that line, so what's shown only ever grows."""

    def __init__(self) -> None:
        self.full = ""
        self.shown = ""

    def text(self, final: bool = False) -> str:
        *done, last = self.full.split("\n")
        keep = [line for line in done if not _is_tag(line)]
        tail = last.strip().strip("*`").upper()
        hold = _is_tag(last) or (not final and tail != "" and TAG.startswith(tail))
        return "\n".join(keep + ([] if hold else [last]))

    def push(self, delta: str) -> str:
        """Add what just arrived; return the new part to show."""
        self.full += delta
        now_shown = self.text()
        new = now_shown[len(self.shown):] if now_shown.startswith(self.shown) else ""
        self.shown = now_shown
        return new

    def entry_id(self) -> Optional[str]:
        for line in reversed(self.full.split("\n")):
            if _is_tag(line):
                value = line.strip().strip("*`").split(":", 1)[1].strip().strip("*`").strip().lower()
                return None if value in ("", "none") else value
        return None


def split_answer(text: str) -> tuple[str, list[str]]:
    """(the sentence or two, the numbered steps): for the history sent with a follow-up question."""
    import re

    intro, steps = [], []
    for line in text.split("\n"):
        m = re.match(r"\s*(?:\d+[.)]|[-*•])\s+(.*)", line)
        if m:
            steps.append(m.group(1).strip())
        elif line.strip():
            intro.append(line.strip())
    return " ".join(intro), steps


async def begin(db: AsyncSession, asker: Asker, question: str, page: str) -> dict:
    """Before the answer: the mode, and the question noted. The caller commits."""
    cfg = await get_setting(db, "hubby")
    mode, reason = await status(db, asker, cfg)
    row = HubbyQuestion(user_id=asker.user_id, investor_id=asker.investor_id, role=asker.role, page=(page or "")[:200],
                        question=question[:300], mode="guide", reason=reason)
    db.add(row)
    await db.flush()
    out = {"id": row.id, "mode": mode, "reason": reason}
    if mode == "ai":
        out["model"] = cfg["model"] if cfg.get("model") in MODELS else "claude-opus-5-5"
        out["guide"] = await guide_for(db, asker)
        row.model = out["model"]
    return out


class _Late(Exception):
    pass


async def answer_stream(start: dict, asker: Asker, question: str, page: str, history: list[dict]):
    """The AI's answer as it's written: ("delta", text) pieces, then one ("done" | "guide" | "error", details).
    "guide" means the AI didn't start in time or failed before any words (answer from the guide); "error" means it
    broke off mid-answer (the website swaps in the guide's answer). Every outcome also returns the row's numbers."""
    model, guide = start["model"], start["guide"]
    messages: list[dict] = []
    for turn in history[-3:]:
        q, a = (turn.get("q") or "").strip()[:300], (turn.get("a") or "").strip()[:600]
        if q and a:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    messages.append({"role": "user", "content": f"{_context(asker, page)}\n\n<question>{question}</question>"})
    kwargs: dict[str, Any] = dict(
        model=model, max_tokens=2048,
        # The guide is the same for every question this person asks, so it is cached
        system=[{"type": "text", "text": f"{INSTRUCTIONS}\n\n{guide_text(guide)}", "cache_control": {"type": "ephemeral"}}],
        messages=messages,
        output_config={"effort": "low"},
    )
    if model in FALLBACK_MODELS:
        kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
    visible = Visible()
    started = time.monotonic()
    first = settings.SH_HUBBY_TIMEOUT_SECONDS
    manager = None
    stream = None

    def left() -> float:
        spent = time.monotonic() - started
        wait = (first - spent) if not visible.shown else min(STALL_SECONDS, LONGEST_SECONDS - spent)
        if wait <= 0:
            raise _Late("No answer in time" if not visible.shown else "The answer stopped coming")
        return wait

    try:
        manager = client_factory().beta.messages.stream(**kwargs)
        stream = await asyncio.wait_for(manager.__aenter__(), timeout=left())
        events = stream.__aiter__()
        while True:
            try:
                event = await asyncio.wait_for(events.__anext__(), timeout=left())
            except StopAsyncIteration:
                break
            if getattr(event, "type", "") == "text":
                piece = visible.push(event.text)
                if piece:
                    yield "delta", piece
        final = await asyncio.wait_for(stream.get_final_message(), timeout=STALL_SECONDS)
        if not final.stop_reason:  # the connection ended without the answer's end: it broke off
            raise _Late("The answer stopped part way")
    except Exception as e:  # any failure: the guide answers instead
        why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        _failed(why)
        paused = bool(_paused_until and now() < _paused_until)
        reason = "cooldown" if paused else "failed"
        yield ("error" if visible.shown else "guide"), {"reason": reason, "row": {"reason": "failed", "ms": _ms(started)}}
        return
    finally:
        if manager is not None and stream is not None:
            try:
                await manager.__aexit__(None, None, None)
            except Exception:  # closing a broken stream: nothing more to do
                pass
    cost, tokens = cost_micro(model, final.usage)
    row = {"model": model, "ms": _ms(started), "cost_micro_usd": cost, **tokens}
    if final.stop_reason == "refusal":
        # Not the AI being down: this one question is answered from the guide
        yield ("error" if visible.shown else "guide"), {"reason": "refused", "row": {**row, "reason": "refused"}}
        return
    back = _succeeded()
    text = visible.text(final=True).strip()
    entries = {e["id"]: e for e in guide["entries"]}
    entry = entries.get(visible.entry_id() or "")
    intro, steps = split_answer(text)
    answer = {"text": text, "entry_id": entry["id"] if entry else None, "path": entry["path"] if entry else None,
              "source": entry["title"] if entry else None, "found": bool(entry or steps)}
    yield "done", {"answer": answer, "back": back, "row": {**row, "mode": "ai", "reason": None, "entry_id": answer["entry_id"]},
                   "summary": " ".join([intro, *steps])[:600]}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def record(db: AsyncSession, question_id, values: dict) -> None:
    row = await db.get(HubbyQuestion, question_id)
    if row:
        for k, v in values.items():
            setattr(row, k, v)
        await db.flush()


async def too_many_today(db: AsyncSession, asker: Asker) -> bool:
    return await _asked_today(db, asker) >= MAX_A_DAY


async def feedback(db: AsyncSession, asker: Asker, question_id, helpful: Optional[bool]) -> bool:
    row = await db.get(HubbyQuestion, question_id)
    if not row or (row.investor_id != asker.investor_id if asker.investor_id else row.user_id != asker.user_id):
        return False
    row.helpful = helpful
    await db.flush()
    return True


# The Founder's view ----------------------------------------------------------------------------------------------
async def overview(db: AsyncSession) -> dict:
    cfg = await get_setting(db, "hubby")
    mode, reason = await status(db, None, cfg)
    start = _month_start()
    month = (await db.execute(select(
        func.count().filter(HubbyQuestion.mode == "ai"), func.count().filter(HubbyQuestion.mode == "guide"),
        func.count().filter(HubbyQuestion.reason == "failed"),
        func.count().filter(HubbyQuestion.helpful.is_(True)), func.count().filter(HubbyQuestion.helpful.is_(False)),
    ).where(HubbyQuestion.created_at >= start))).one()
    words = func.lower(func.trim(HubbyQuestion.question))
    top = (await db.execute(select(
        func.min(HubbyQuestion.question), func.count(), func.sum(case((HubbyQuestion.helpful.is_(True), 1), else_=0)),
        func.sum(case((HubbyQuestion.helpful.is_(False), 1), else_=0)),
    ).where(HubbyQuestion.question.is_not(None), HubbyQuestion.created_at >= now() - KEEP_QUESTIONS)
        .group_by(words).order_by(func.count().desc(), func.min(HubbyQuestion.created_at).desc()).limit(10))).all()
    spent = await month_spend_micro(db)
    return {
        "settings": cfg, "models": [{"id": k, "label": v} for k, v in MODELS.items()],
        "key_set": bool(settings.anthropic_key), "sdk_ready": sdk_ready(), "mode": mode, "reason": reason,
        "paused_until": _paused_until.isoformat() if _paused_until and now() < _paused_until else None,
        "last_error": _last_error,
        "month": {"spent_usd": round(spent / 1_000_000, 4), "cap_usd": float(cfg["monthly_cap_usd"]), "ai": month[0],
                  "guide": month[1], "failed": month[2], "helpful": month[3], "unhelpful": month[4]},
        "top": [{"question": q, "times": n, "helpful": int(up or 0), "unhelpful": int(down or 0)} for q, n, up, down in top],
    }


async def tidy(db: AsyncSession, at: Optional[datetime] = None) -> None:
    """Daily: clear question text older than 30 days, and drop rows older than 100 days."""
    at = at or now()
    await db.execute(update(HubbyQuestion).where(HubbyQuestion.created_at < at - KEEP_QUESTIONS, HubbyQuestion.question.is_not(None))
                     .values(question=None))
    await db.execute(delete(HubbyQuestion).where(HubbyQuestion.created_at < at - KEEP_NUMBERS))
