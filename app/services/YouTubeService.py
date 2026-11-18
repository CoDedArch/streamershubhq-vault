"""Public view counts for YouTube Shorts clips, from the YouTube Data API v3 (videos.list, part=statistics).

One request reads up to 50 videos and costs 1 unit of the free 10,000-a-day allowance. Without SH_YOUTUBE_API_KEY
nothing is fetched and clips are checked by hand, as before. A failed fetch never blocks a submission.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)
API = "https://www.googleapis.com/youtube/v3/videos"
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
transport: Optional[httpx.AsyncBaseTransport] = None  # tests put a stand-in for YouTube here


@dataclass
class Stats:
    views: int
    channel: Optional[str]
    title: Optional[str]


class FetchFailed(Exception):
    pass


def enabled() -> bool:
    return bool(settings.SH_YOUTUBE_API_KEY)


def video_id(url: str) -> Optional[str]:
    """youtube.com/shorts/ID, youtube.com/watch?v=ID, youtu.be/ID, m.youtube.com and /live/ID links."""
    m = re.search(r"(?:youtube\.com/(?:shorts|live|embed)/|youtu\.be/|[?&]v=)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m and _ID.match(m.group(1)) else None


async def fetch(ids: list[str]) -> dict[str, Stats]:
    """Stats for each id YouTube knows. A private or deleted video is simply missing from the answer."""
    out: dict[str, Stats] = {}
    if not enabled() or not ids:
        return out
    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
        for i in range(0, len(ids), 50):
            try:
                r = await client.get(API, params={"part": "statistics,snippet", "id": ",".join(ids[i:i + 50]),
                                                  "key": settings.SH_YOUTUBE_API_KEY})
            except httpx.HTTPError as exc:
                raise FetchFailed("YouTube didn't answer.") from exc
            if r.status_code != 200:
                reason = ((r.json() if r.content else {}).get("error") or {}).get("message", "")[:120]
                logger.warning("YouTube stats request failed: %s %s", r.status_code, reason)
                raise FetchFailed("YouTube refused the request. Check the API key and its daily quota.")
            for item in r.json().get("items", []):
                stats, snippet = item.get("statistics") or {}, item.get("snippet") or {}
                if "viewCount" in stats:
                    out[item["id"]] = Stats(views=int(stats["viewCount"]), channel=snippet.get("channelTitle"),
                                            title=snippet.get("title"))
    return out
