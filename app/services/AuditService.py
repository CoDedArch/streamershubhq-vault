"""The audit log: who did what, before and after, when and from where."""

from typing import Any, Optional

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import client_ip
from app.models.people import AuditLog, User


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


async def audit(db: AsyncSession, actor: Optional[User], action: str, entity: str, entity_id: Any = None, *,
                summary: Optional[str] = None, before: Optional[dict] = None, after: Optional[dict] = None,
                request: Optional[Request] = None, actor_label: Optional[str] = None) -> None:
    from app.core.access import AREAS, acting_area, acting_note

    area = acting_area()
    label = actor_label or (f"{actor.name} ({actor.role})" if actor else "system")
    if area:
        label = f"{label} · stepping in on {AREAS[area][0]}"
        acting_note(summary or action.replace(".", " ").replace("_", " ").capitalize())
    db.add(AuditLog(
        actor_user_id=actor.id if actor else None,
        actor_label=label[:160],
        action=action, entity=entity, entity_id=str(entity_id)[:160] if entity_id is not None else None,
        summary=summary, before=_plain(before), after=_plain(after), ip=client_ip(request) if request else None,
    ))
