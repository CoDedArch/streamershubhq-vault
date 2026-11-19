"""When S3 won't take a file, the website gets a message it can show (with the CORS header the browser needs),
not a bare 500 that the browser blocks and the site reads as "We can't reach Streamers Hub"."""

import io
import logging

import httpx
from botocore.exceptions import ClientError

from app.services import S3Service
from tests.conftest import sign_in

ORIGIN = "http://site.test"  # SH_FRONTEND_URL in the tests


def jpeg() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (1600, 900), (253, 187, 36)).save(out, "JPEG")
    return out.getvalue()


class RefusingS3:
    def put_object(self, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "Access Denied"}}, "PutObject")


async def test_a_refused_upload_comes_back_as_a_message_the_site_can_show(app, monkeypatch, caplog):
    staff = await sign_in(app, "trigger")
    # As the browser sends it: the site's Origin, and a server error comes back as a response, not an exception
    browser = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://api.test",
                                cookies=staff.cookies, headers={"Origin": ORIGIN})
    opts = (await browser.get("/api/v1/posts")).json()["options"]
    galacta = next(s["id"] for s in opts["streamers"] if s["name"] == "Galacta")
    post = (await browser.post("/api/v1/posts", json={"kind": "stream", "title": "Galacta, 13 hours in", "topic": "Gaming",
                                                       "theme": "y", "streamer_id": galacta, "platform": "twitch",
                                                       "link_url": "twitch.tv/theonlygalacta", "starts_at": "2026-11-19T05:00:00+00:00"})).json()

    monkeypatch.setattr(S3Service, "use_s3", lambda: True)
    monkeypatch.setattr(S3Service, "_client", lambda: RefusingS3())
    with caplog.at_level(logging.ERROR, logger="app.services.S3Service"):
        r = await browser.post(f"/api/v1/posts/{post['id']}/image", data={"layout": "thumbnail"},
                               files={"file": ("banner.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 503
    assert r.headers.get("access-control-allow-origin") == ORIGIN
    assert r.json()["detail"] == "We couldn't save the file just now (storage error: AccessDenied). Try again in a minute."
    assert any("AccessDenied" in rec.getMessage() and rec.exc_info for rec in caplog.records)  # the full error is in the logs

    # The post is as it was
    monkeypatch.undo()
    row = (await browser.get(f"/api/v1/posts/{post['id']}/stats")).json()["post"]
    assert row["image_url"] is None
