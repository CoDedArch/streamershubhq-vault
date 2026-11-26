"""Emails about tasks and goals: one bundled email once the lead stops adding, at most one every 30 minutes, nothing
for your own tasks or ones already done, and one morning email for what's due today or went overdue yesterday."""

from datetime import timedelta

from sqlalchemy import select

from app.core.security import now
from app.models.people import Task, User
from app.services import TaskNoticeService
from app.services.CompanyService import today
from tests.conftest import sign_in


async def user_id(db, key: str) -> str:
    return str((await db.execute(select(User.id).where(User.email == f"{key}@streamershub.test"))).scalar_one())


def to(mail: list[dict], key: str) -> list[dict]:
    return [m for m in mail if m["to"] == [f"{key}@streamershub.test"]]


async def test_new_work_is_bundled_into_one_email_and_spaced_out(app, db):
    lead = await sign_in(app, "kingmille")
    c9 = await user_id(db, "c9per")
    start = now()
    for title, due in [("Poster for Friday", today() + timedelta(days=1)), ("Sort the photo archive", None)]:
        r = await lead.post("/api/v1/tasks", json={"title": title, "owner_user_id": c9, "due_on": due.isoformat() if due else None})
        assert r.status_code == 200
    r = await lead.post("/api/v1/goals", json={"title": "Clips posted", "owner_user_id": c9, "target": 20, "unit": "clips",
                                               "period_start": today().isoformat(), "period_end": (today() + timedelta(days=5)).isoformat()})
    assert r.status_code == 200
    # One done before the email goes: left out
    done = (await lead.post("/api/v1/tasks", json={"title": "Already sorted", "owner_user_id": c9})).json()
    await lead.patch(f"/api/v1/tasks/{done['id']}", json={"status": "done"})

    # Still adding: nothing yet
    assert to(await TaskNoticeService.assignment_mail(db, start + timedelta(minutes=3)), "c9per") == []
    # Ten quiet minutes later: one email with all three
    mail = to(await TaskNoticeService.assignment_mail(db, start + timedelta(minutes=11)), "c9per")
    await db.commit()
    assert len(mail) == 1
    m = mail[0]
    assert m["subject"] == "2 tasks and 1 goal for you in Hub OS" and m["title"] == "New in Hub OS: 2 tasks and 1 goal"
    assert m["lines"][0] == "King Mille added these for you:"
    assert any(x.startswith("Task: Poster for Friday · due ") for x in m["lines"])
    assert "Task: Sort the photo archive · no due date" in m["lines"]
    assert any(x.startswith("Goal: Clips posted · 20 clips by ") for x in m["lines"])
    assert not any("Already sorted" in x for x in m["lines"]) and m["cta"] == ("Open my tasks and goals", "/os/me")
    # Sent once only
    assert to(await TaskNoticeService.assignment_mail(db, start + timedelta(minutes=12)), "c9per") == []

    # Another task straight after: quiet for long enough, but the last email was under 30 minutes ago, so it waits
    await lead.post("/api/v1/tasks", json={"title": "Thumbnail for Thursday", "owner_user_id": c9})
    assert to(await TaskNoticeService.assignment_mail(db, start + timedelta(minutes=25)), "c9per") == []
    mail = to(await TaskNoticeService.assignment_mail(db, start + timedelta(minutes=42)), "c9per")
    await db.commit()
    assert len(mail) == 1 and mail[0]["subject"] == "New task: Thumbnail for Thursday"


async def test_no_email_for_your_own_tasks_or_older_ones(app, db):
    founder = await sign_in(app, "founder")
    me = await user_id(db, "founder")
    await founder.post("/api/v1/tasks", json={"title": "Send the investor update", "owner_user_id": me})
    mail = await TaskNoticeService.assignment_mail(db, now() + timedelta(hours=1))
    assert to(mail, "founder") == []
    # The sample data's tasks have nobody recorded as giving them, so nobody is emailed about them
    assert all(not any(x.startswith("Task: Weekly task") for x in m["lines"]) for m in mail)


async def test_one_morning_email_for_tasks_due_today_and_overdue_since_yesterday(app, db):
    trigger = await user_id(db, "trigger")
    d = today()
    for title, due, status in [("Post the stream schedule", d, "open"), ("Reply to brand DMs", d - timedelta(days=1), "open"),
                               ("Next week's plan", d + timedelta(days=1), "open"), ("Old overdue", d - timedelta(days=3), "open"),
                               ("Already posted", d, "done")]:
        db.add(Task(owner_user_id=trigger, title=title, due_on=due, status=status))
    await db.commit()
    mail = to(await TaskNoticeService.due_reminder_mail(db, d), "trigger")
    assert len(mail) == 1
    lines = mail[0]["lines"]
    assert "• Post the stream schedule" in lines and "• Reply to brand DMs" in lines
    assert not any(x in " ".join(lines) for x in ("Next week's plan", "Old overdue", "Already posted"))
    assert lines.index("Due today:") < lines.index("• Post the stream schedule") and lines[2].startswith("Overdue since ")
