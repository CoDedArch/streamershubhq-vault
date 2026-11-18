"""Posts on the website: a streamer's next live stream, an event with tickets, or news. Each post has a
share page (with a preview image for WhatsApp and other apps) and its own numbers: views, clicks, likes
and shares. Also link pages: one for the label and one per streamer, each a list of their socials."""

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, Row, fk

POST_KINDS = {"stream": "Live stream", "event": "Event", "news": "News", "job": "Open role"}
POST_TOPICS = ["Gaming", "Music", "Sport", "Comedy", "Lifestyle", "Tech", "Giveaway", "Collab", "Tournament",
               "Meet and greet", "Merch drop", "Announcement"]
POST_THEMES = {"y": "Yellow", "k": "Ink", "t": "Teal", "light": "Paper"}
EMPLOYMENT = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract", "internship": "Internship",
              "freelance": "Freelance"}
WORKPLACE = {"on_site": "On site", "hybrid": "Hybrid", "remote": "Remote"}
PLATFORMS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick", "tiktok": "TikTok", "instagram": "Instagram",
             "facebook": "Facebook", "x": "X", "other": "Online"}


class Post(Row, Base):
    __tablename__ = "posts"
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(10), default="stream")  # POST_KINDS
    title: Mapped[str] = mapped_column(String(160))
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    topic: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    theme: Mapped[str] = mapped_column(String(6), default="y")
    streamer_id: Mapped[Optional[uuid.UUID]] = fk("streamers")
    image_key: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    share_image_key: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)  # small JPEG for link previews
    image_layout: Mapped[str] = mapped_column(String(10), default="thumbnail")  # thumbnail (wide) or flyer (tall)
    link_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # the stream, or more about the event
    platform: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    starts_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    venue: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    ticket_price_minor: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # None: free entry
    ticket_capacity: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ticket_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)  # tickets sold elsewhere
    tickets_open: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(12), default="draft", index=True)  # draft, published, archived
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[uuid.UUID]] = fk("users")
    views: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    # Open roles only: department, employment, workplace, location, closes_on, pay, show_pay, openings,
    # responsibilities, requirements, nice_to_have, apply_via (form or link), apply_url, closed
    job: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class PostEvent(Row, Base):
    """One view, click or share. `visitor` is a one-way hash; no names, emails or IP addresses are kept."""

    __tablename__ = "post_events"
    post_id: Mapped[uuid.UUID] = fk("posts", nullable=False, ondelete="CASCADE")
    kind: Mapped[str] = mapped_column(String(8))  # view, click, share
    visitor: Mapped[str] = mapped_column(String(32), index=True)
    via: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # whatsapp, x, facebook, copy, site, direct
    target: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)  # watch, tickets, more, apply


class PostLike(Row, Base):
    """An anonymous like: one per visitor per post."""

    __tablename__ = "post_likes"
    post_id: Mapped[uuid.UUID] = fk("posts", nullable=False, ondelete="CASCADE")
    visitor: Mapped[str] = mapped_column(String(32))
    __table_args__ = (UniqueConstraint("post_id", "visitor"),)


class TicketBooking(Row, Base):
    """Tickets for an event. While the store takes interest only, a booking is a reservation we follow up;
    once payments are on, it is paid for at once."""

    __tablename__ = "ticket_bookings"
    post_id: Mapped[uuid.UUID] = fk("posts", nullable=False, ondelete="RESTRICT")
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # shown on the ticket
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255))
    phone_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    phone_last3: Mapped[Optional[str]] = mapped_column(String(3), nullable=True)
    qty: Mapped[int] = mapped_column(Integer)
    amount_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    # interest (reserved, to follow up), pending (sent to pay), paid, free (no charge), cancelled
    status: Mapped[str] = mapped_column(String(12), index=True)
    payment_ref: Mapped[Optional[str]] = mapped_column(String(60), unique=True, nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    handled_by: Mapped[Optional[uuid.UUID]] = fk("users")


LINK_KINDS = {"youtube": "YouTube", "twitch": "Twitch", "kick": "Kick", "tiktok": "TikTok", "instagram": "Instagram",
              "x": "X", "facebook": "Facebook", "snapchat": "Snapchat", "whatsapp": "WhatsApp", "discord": "Discord",
              "website": "Website", "email": "Email", "store": "Store", "other": "Link"}


class LinkPage(Row, Base):
    """A link page: the label's (no streamer) or one streamer's."""

    __tablename__ = "link_pages"
    slug: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    streamer_id: Mapped[Optional[uuid.UUID]] = fk("streamers", ondelete="CASCADE")
    title: Mapped[str] = mapped_column(String(80))
    bio: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    avatar_key: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    theme: Mapped[str] = mapped_column(String(6), default="k")
    views: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("streamer_id"),)


class LinkPageShare(Row, Base):
    """One share of a link page (anonymous, like a post's)."""

    __tablename__ = "link_page_shares"
    page_id: Mapped[uuid.UUID] = fk("link_pages", nullable=False, ondelete="CASCADE")
    visitor: Mapped[str] = mapped_column(String(32), index=True)
    via: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)


class LinkItem(Row, Base):
    __tablename__ = "link_items"
    page_id: Mapped[uuid.UUID] = fk("link_pages", nullable=False, ondelete="CASCADE")
    kind: Mapped[str] = mapped_column(String(12), default="other")
    label: Mapped[str] = mapped_column(String(80))
    url: Mapped[str] = mapped_column(String(500))
    position: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
