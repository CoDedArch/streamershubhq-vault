"""Hubby: the guide matches each role, it works with no API key, the AI's answer streams with a link taken from the
guide, a failing AI falls back to the guide (before or during the answer, and pausing after 3 failures), and the
Founder's limits hold."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.security import now
from app.models.hubby import HubbyQuestion
from app.models.people import AppSetting
from app.services import HubbyService
from tests.conftest import new_client, sign_in, vault_sign_in

ANSWER = ["You give tasks from ", "the person's page.\n1. Open **People and teams** and click the person.\n",
          "2. Under **Tasks**, type it and click **Add**.\nEN", "TRY: give-task"]


class FakeClaude:
    """Stands in for AsyncAnthropic's streaming: sends `pieces` as text events. `error` is raised when the stream opens,
    or after `break_after` pieces; `delay` holds back the first words."""

    def __init__(self):
        self.pieces, self.error, self.break_after, self.delay, self.stop_reason, self.calls = list(ANSWER), None, None, 0, "end_turn", []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self)


class FakeStream:
    def __init__(self, fake):
        self.fake = fake

    async def __aenter__(self):
        if self.fake.error and self.fake.break_after is None:
            raise self.fake.error
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self.events()

    async def events(self):
        await asyncio.sleep(self.fake.delay)
        for i, piece in enumerate(self.fake.pieces):
            if self.fake.break_after == i:
                raise self.fake.error
            yield SimpleNamespace(type="text", text=piece)

    async def get_final_message(self):
        usage = SimpleNamespace(input_tokens=120, output_tokens=80, cache_creation_input_tokens=0, cache_read_input_tokens=3000)
        return SimpleNamespace(stop_reason=self.fake.stop_reason, usage=usage)


@pytest.fixture
def ai(monkeypatch):
    """AI mode with a fake Claude."""
    monkeypatch.setattr(settings, "SH_ANTHROPIC_API_KEY", "test-key")
    HubbyService.reset_health()
    fake = FakeClaude()
    monkeypatch.setattr(HubbyService, "client_factory", lambda: fake)
    yield fake
    HubbyService.reset_health()


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.setattr(settings, "SH_ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "")
    HubbyService.reset_health()


async def set_hubby(db, **value):
    row = (await db.execute(select(AppSetting).where(AppSetting.key == "hubby"))).scalar_one_or_none()
    if row:
        row.value = {**row.value, **value}
    else:
        db.add(AppSetting(key="hubby", value=value))
    await db.commit()


async def ask(client, question, page="/os", history=None, **params):
    """POST /hubby/ask and read its events: (the text streamed, {event name: data} for the other events)."""
    r = await client.post("/api/v1/hubby/ask", params=params, json={"question": question, "page": page, "history": history or []})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/event-stream")
    text, events = "", {}
    for block in r.text.strip().split("\n\n"):
        name = block.split("\n")[0].removeprefix("event: ")
        data = json.loads(block.split("\n")[1].removeprefix("data: "))
        if name == "delta":
            text += data["t"]
        else:
            events[name] = data
    return text, events


def test_the_entry_line_never_shows_while_streaming():
    v = HubbyService.Visible()
    shown = "".join(v.push(p) for p in ["Open **Settings**.\n", "E", "NTRY", ": hubby-settings"])
    assert shown == "Open **Settings**.\n" and v.entry_id() == "hubby-settings"
    v = HubbyService.Visible()
    shown = "".join(v.push(p) for p in ["Ex", "port it.\n**ENTRY:** export-ledger\n"])
    assert "ENTRY" not in shown and shown.startswith("Export it.") and v.entry_id() == "export-ledger"
    v = HubbyService.Visible()
    v.push("Ask Ahmed.\nENTRY: none")
    assert v.text(final=True) == "Ask Ahmed." and v.entry_id() is None
    assert HubbyService.split_answer("Here's how.\n1. Open **People**.\n2. Click **Add**.") == ("Here's how.", ["Open **People**.", "Click **Add**."])


async def test_without_a_key_hubby_answers_from_the_guide(app, db, no_key):
    c9 = await sign_in(app, "c9per")
    assert (await c9.get("/api/v1/hubby/status")).json() == {"mode": "guide", "reason": "no_key"}
    guide = (await c9.get("/api/v1/hubby/guide", params={"page": "/os/creative"})).json()
    ids = {e["id"] for e in guide["entries"]}
    assert {"my-tasks", "send-idea", "creative-request", "check-design"} <= ids
    assert not ids & {"approve-run", "give-task", "record-money", "d-upload", "i-stake"}
    assert guide["starters"][0] == "How do I add a creative request?" and guide["fallback"] == "Suggestions"
    # What other desks do, and who to ask: never the steps
    elsewhere = {o["id"]: o for o in guide["elsewhere"]}
    assert "King Mille" in elsewhere["give-task"]["ask"] and "steps" not in elsewhere["give-task"]
    assert "Ahmed" in elsewhere["record-money"]["ask"]
    text, events = await ask(c9, "where do I see my tasks", "/os/creative")
    assert text == "" and events["start"]["mode"] == "guide" and events["guide"]["reason"] == "no_key"
    row = await db.get(HubbyQuestion, events["guide"]["id"])
    assert row.question == "where do I see my tasks" and row.cost_micro_usd == 0 and row.role == "staff"


async def test_each_role_gets_its_own_guide(app, db, seeded, no_key):
    async def ids(client, **params):
        return {e["id"] for e in (await client.get("/api/v1/hubby/guide", params=params)).json()["entries"]}

    founder = await ids(await sign_in(app, "founder"))
    assert {"approve-run", "hubby-settings", "step-in", "invite", "decide", "record-money"} <= founder and "running-late" not in founder
    ahmed = await ids(await sign_in(app, "ahmed"))
    assert {"record-money", "payout-run", "purchase", "confirm-investment"} <= ahmed and "approve-run" not in ahmed
    galacta = await ids(await sign_in(app, "galacta"))
    assert {"running-late", "streamer-pay", "my-links"} <= galacta and "my-pay" not in galacta
    designer = await ids(await sign_in(app, "ama_draws"))
    assert designer and all(i.startswith("d-") for i in designer)
    clipper = await ids(await sign_in(app, "kwesi_cuts"))
    assert clipper and all(i.startswith("c-") for i in clipper)
    _, _, email, holder_id = seeded["holders"][0]
    vault = await vault_sign_in(app, holder_id, email)
    guide = (await vault.get("/api/v1/hubby/guide", params={"in": "vault", "page": "/vault/home"})).json()
    assert {e["id"] for e in guide["entries"]} == {e.id for e in HubbyService.GUIDE if e.portal == "investor"}
    assert guide["elsewhere"] == [] and guide["fallback"] == "Messages"
    _, events = await ask(vault, "how do I download my documents", "/vault/home", **{"in": "vault"})
    assert events["guide"]["reason"] == "no_key"
    assert (await new_client(app).get("/api/v1/hubby/guide")).status_code == 401


async def test_the_ai_answer_streams_with_a_link_from_the_guide(app, db, ai):
    lead = await sign_in(app, "kingmille")
    assert (await lead.get("/api/v1/hubby/status")).json() == {"mode": "ai", "reason": None}
    text, events = await ask(lead, "How do I give C9PER a task?", "/os/people", [{"q": "hi", "a": "Hello! Ask me anything."}])
    assert text.startswith("You give tasks from the person's page.\n1. Open **People and teams**") and "ENTRY" not in text
    done = events["done"]
    assert events["start"]["mode"] == "ai" and done["back"] is False
    assert done["answer"]["path"] == "/os/people" and done["answer"]["source"] == "Give someone a task"
    assert done["answer"]["text"] == text.strip()
    call = ai.calls[-1]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default" and call["betas"] == [HubbyService.FALLBACK_BETA]
    assert call["output_config"] == {"effort": "low"} and call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert 'id="give-task"' in call["system"][0]["text"] and 'id="approve-run"' not in call["system"][0]["text"]
    assert [m["role"] for m in call["messages"]] == ["user", "assistant", "user"]
    assert "People and teams" in call["messages"][-1]["content"] and "King" in call["messages"][-1]["content"]
    row = await db.get(HubbyQuestion, done["id"])
    await db.refresh(row)
    assert row.mode == "ai" and row.entry_id == "give-task" and row.cache_read_tokens == 3000 and row.ms is not None
    assert row.cost_micro_usd == round((120 * 4 + 80 * 20 + 3000 * 0.2))  # $ per million tokens -> micro-dollars

    # A link to an entry this person can't use is dropped, whatever the model says
    ai.pieces = ["Click **Approve run**.\nENTRY: approve-run"]
    _, events = await ask(lead, "approve payroll")
    assert events["done"]["answer"]["path"] is None and events["done"]["answer"]["entry_id"] is None

    # Haiku: no server-side fallback, so none is asked for
    await set_hubby(db, model="claude-haiku-5-5")
    await ask(lead, "where are my tasks", "/os/me")
    assert ai.calls[-1]["model"] == "claude-haiku-5-5" and "fallbacks" not in ai.calls[-1]
    await set_hubby(db, model="claude-opus-5-5")


async def test_a_failing_ai_falls_back_then_pauses_then_comes_back(app, db, ai, monkeypatch):
    trigger = await sign_in(app, "trigger")
    ai.error = ConnectionError("down")
    reasons = [(await ask(trigger, "how do I verify a clip", "/os/social"))[1]["guide"]["reason"] for _ in range(3)]
    assert reasons == ["failed", "failed", "cooldown"]
    assert (await trigger.get("/api/v1/hubby/status")).json() == {"mode": "guide", "reason": "cooldown"}
    calls = len(ai.calls)
    assert (await ask(trigger, "how do I verify a clip"))[1]["guide"]["reason"] == "cooldown" and len(ai.calls) == calls
    # Five minutes later it tries again, and says it's back
    HubbyService._paused_until = now() - timedelta(seconds=1)
    ai.error = None
    assert (await ask(trigger, "how do I verify a clip"))[1]["done"]["back"] is True
    assert (await ask(trigger, "how do I verify a clip"))[1]["done"]["back"] is False

    # No words within the time limit: the guide answers
    monkeypatch.setattr(settings, "SH_HUBBY_TIMEOUT_SECONDS", 0.2)
    ai.delay = 0.6
    text, events = await ask(trigger, "how do I schedule a post", "/os/social")
    assert text == "" and events["guide"]["reason"] == "failed"
    ai.delay = 0

    # It breaks off mid-answer: what came is followed by an error, and the website swaps in the guide
    ai.error, ai.break_after = ConnectionError("dropped"), 2
    text, events = await ask(trigger, "how do I schedule a post", "/os/social")
    assert text.startswith("You give tasks") and events["error"]["reason"] == "failed" and "done" not in events
    row = await db.get(HubbyQuestion, events["error"]["id"])
    await db.refresh(row)
    assert row.mode == "guide" and row.reason == "failed"
    ai.error, ai.break_after = None, None

    # A refusal answers from the guide without counting as the AI being down
    HubbyService.reset_health()
    ai.stop_reason = "refusal"
    _, events = await ask(trigger, "how do I schedule a post")
    assert events["error"]["reason"] == "refused" and HubbyService._fails == 0
    ai.stop_reason = "end_turn"


async def test_the_founders_limits_switch_to_the_guide(app, db, ai):
    certified = await sign_in(app, "certified")
    status = lambda: certified.get("/api/v1/hubby/status")  # noqa: E731
    await set_hubby(db, ai_on=False)
    assert (await status()).json()["reason"] == "off"
    await set_hubby(db, ai_on=True, daily_per_person=1)
    await ask(certified, "how do I plan streams", "/os/production")
    assert (await status()).json()["reason"] == "daily"
    await set_hubby(db, daily_per_person=30, monthly_cap_usd=1)
    db.add(HubbyQuestion(role="founder", mode="ai", question="spend", cost_micro_usd=1_000_000))
    await db.commit()
    assert (await status()).json()["reason"] == "cap"
    await set_hubby(db, monthly_cap_usd=10)
    assert (await status()).json() == {"mode": "ai", "reason": None}


async def test_feedback_and_the_founders_view(app, db, ai):
    c9, philip, founder = await sign_in(app, "c9per"), await sign_in(app, "philip"), await sign_in(app, "founder")
    first = (await ask(c9, "Where are my tasks?", "/os/me"))[1]["done"]
    await ask(c9, "where are my tasks? ", "/os/creative")
    assert (await c9.post(f"/api/v1/hubby/questions/{first['id']}/feedback", json={"helpful": True})).status_code == 200
    assert (await philip.post(f"/api/v1/hubby/questions/{first['id']}/feedback", json={"helpful": False})).status_code == 404

    assert (await c9.get("/api/v1/hubby/overview")).status_code == 403
    view = (await founder.get("/api/v1/hubby/overview")).json()
    assert view["key_set"] and view["sdk_ready"] and view["mode"] == "ai"
    assert view["month"]["ai"] >= 2 and view["month"]["spent_usd"] > 0 and view["month"]["helpful"] >= 1
    top = {t["question"].strip().lower(): t for t in view["top"]}
    assert top["where are my tasks?"]["times"] == 2 and top["where are my tasks?"]["helpful"] == 1
    assert [m["id"] for m in view["models"]] == ["claude-opus-5-5", "claude-haiku-5-5"]

    assert (await founder.put("/api/v1/settings/hubby", json={"model": "gpt"})).status_code == 400
    assert (await founder.put("/api/v1/settings/hubby", json={"monthly_cap_usd": 0})).status_code == 400
    r = await founder.put("/api/v1/settings/hubby", json={"ai_on": False, "monthly_cap_usd": 25})
    assert r.status_code == 200 and r.json()["ai_on"] is False and r.json()["monthly_cap_usd"] == 25
    assert (await founder.put("/api/v1/settings/hubby", json={"ai_on": True, "monthly_cap_usd": 10})).status_code == 200


async def test_question_text_is_cleared_after_30_days(app, db):
    old = HubbyQuestion(role="staff", mode="guide", question="an old question", created_at=now() - timedelta(days=31))
    ancient = HubbyQuestion(role="staff", mode="ai", question=None, cost_micro_usd=5, created_at=now() - timedelta(days=120))
    db.add_all([old, ancient])
    await db.commit()
    await HubbyService.tidy(db)
    await db.commit()
    await db.refresh(old)
    assert old.question is None and await db.get(HubbyQuestion, ancient.id) is None
