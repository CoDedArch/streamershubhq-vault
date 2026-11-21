"""Every email Streamers Hub sends, in the label's look: ink background, yellow accents."""

from html import escape
from typing import Optional

from app.core.config import settings
from app.services.SesEmailService import send_async
from app.services.money import ghs


def _layout(title: str, body_html: str, cta: Optional[tuple[str, str]] = None) -> str:
    button = ""
    if cta:
        button = (f'<p style="margin:28px 0 8px"><a href="{escape(cta[1])}" style="display:inline-block;background:#FDBB24;'
                  f'color:#141414;font-weight:800;text-transform:uppercase;letter-spacing:.04em;padding:14px 26px;'
                  f'text-decoration:none;font-family:Arial,sans-serif">{escape(cta[0])} &#9654;</a></p>')
    return f"""<!doctype html><html><body style="margin:0;background:#F7F6F1;font-family:Arial,Helvetica,sans-serif;color:#141414">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#F7F6F1;padding:24px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#FFFFFF;border-radius:16px;overflow:hidden">
<tr><td style="background:#141414;padding:22px 28px;color:#F7F6F1;font-weight:900;letter-spacing:.06em;font-size:15px">STREAMERS <span style="color:#FDBB24">HUB</span></td></tr>
<tr><td style="padding:28px">
<h1 style="margin:0 0 14px;font-size:24px;line-height:1.15">{escape(title)}</h1>
<div style="font-size:15px;line-height:1.6;color:#2A2925">{body_html}</div>{button}
</td></tr>
<tr><td style="padding:18px 28px;border-top:1px solid #EEEDE6;color:#5A5851;font-size:12px">Streamers Hub · <a href="mailto:{settings.SH_CONTACT_EMAIL}" style="color:#5A5851">{settings.SH_CONTACT_EMAIL}</a></td></tr>
</table></td></tr></table></body></html>"""


def _p(*lines: str) -> str:
    return "".join(f"<p style=\"margin:0 0 12px\">{line}</p>" for line in lines)


def link(path: str) -> str:
    return settings.SH_FRONTEND_URL.rstrip("/") + path


async def send_code(to: str, name: str, code: str, purpose: str) -> bool:
    what = {"login": "sign in to Hub OS", "portal": "sign in", "vault": "open your vault",
            "step_up": "confirm this action in your vault", "reset": "reset your password"}.get(purpose, "continue")
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"Use this code to {what}. It works once, for 10 minutes.",
              f'<span style="display:inline-block;font-size:30px;letter-spacing:.3em;font-weight:900;background:#141414;color:#FDBB24;padding:12px 18px;border-radius:10px">{code}</span>',
              "If you didn't ask for this, ignore this email and nobody can get in with it.")
    return await send_async(to=[to], subject=f"Your Streamers Hub code: {code}", html=_layout("Your one-time code", body),
                            text=f"Your Streamers Hub code is {code}. It works once, for 10 minutes.")


async def send_invite(to: str, name: str, kind: str, token_path: str) -> bool:
    titles = {"staff": ("You got in.", "Your Hub OS account is ready. Set your password, then work through onboarding."),
              "designer": ("Your Designer Studio is open.", "Your application was approved. Set your password to open your Studio and upload your first design."),
              "clipper": ("Your Clipper Desk is open.", "Your application was approved. Set your password to open your desk, get footage and submit clips."),
              "streamer": ("Welcome to the roster.", "Your Hub OS account is ready. Set your password to open your desk."),
              "investor": ("Welcome to the label.", "Your investment is recorded. Open this link to confirm your details, sign your agreement and get your Holder ID.")}
    title, line = titles.get(kind, titles["staff"])
    body = _p(f"Hi {escape(name.split(' ')[0])},", line, "This link is personal and expires in 7 days.")
    cta = "Open my vault" if kind == "investor" else "Set my password"
    return await send_async(to=[to], subject=f"Streamers Hub: {title}", html=_layout(title, body, (cta, link(token_path))))


async def agreement_offered(to: str, name: str, title: str, path: str) -> bool:
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"<b>{escape(title)}</b> is ready for you to read and sign.",
              "It opens the next time you sign in. Take your time, and ask us anything before you sign.")
    return await send_async(to=[to], subject=f"Streamers Hub: {title} is ready to sign",
                            html=_layout("A new agreement to sign", body, ("Read the agreement", link(path))))


async def send_reset(to: str, name: str, token_path: str) -> bool:
    body = _p(f"Hi {escape(name.split(' ')[0])},", "Use the button to choose a new password. The link works once, for 1 hour.",
              "If you didn't ask for this, you can ignore this email.")
    return await send_async(to=[to], subject="Reset your Streamers Hub password",
                            html=_layout("Reset your password", body, ("Choose a new password", link(token_path))))


async def application_received(to: str, name: str, reference: str, track: str) -> bool:
    what = {"streamer": "streamer", "designer": "merch designer", "clipper": "clipper"}[track]
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"We have your {what} application. Your reference is <b>{reference}</b>.",
              "Every application is read by a person. We will email you with a decision. Not everyone gets in, but everyone gets an answer.")
    return await send_async(to=[to], subject=f"You're in the queue · {reference}", html=_layout("You're in the queue.", body))


async def application_declined(to: str, name: str, reference: str, reason: str) -> bool:
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"Thank you for applying ({reference}). This time we are not taking it further.",
              f"<b>Why:</b> {escape(reason)}", "You are welcome to apply again when things change.")
    return await send_async(to=[to], subject=f"About your application · {reference}", html=_layout("About your application", body))


async def streamer_assessment(to: str, name: str, reference: str) -> bool:
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"Good news on {reference}: we would like you to do a live trial stream in our studio.",
              "Our producer will contact you to agree a date. Bring your setup notes and your best energy.")
    return await send_async(to=[to], subject="You're invited to a studio assessment", html=_layout("Studio assessment", body))


async def order_confirmed(to: str, name: str, number: str, total_minor: int, lines: list[str], track_path: str) -> bool:
    items = "".join(f"<li>{escape(line)}</li>" for line in lines)
    body = _p(f"Thanks, {escape(name.split(' ')[0])}. Your payment went through.", f"<b>Order {number}</b> · {ghs(total_minor)}") + \
        f'<ul style="margin:0 0 12px;padding-left:18px">{items}</ul>' + _p("We will email you again when it ships.")
    return await send_async(to=[to], subject=f"Order {number} confirmed", html=_layout("Order confirmed", body, ("Track my order", link(track_path))))


async def order_status(to: str, name: str, number: str, status_text: str, track_path: str) -> bool:
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"Order <b>{number}</b>: {escape(status_text)}")
    return await send_async(to=[to], subject=f"Order {number}: {status_text}", html=_layout(status_text, body, ("Track my order", link(track_path))))


async def notify(to: list[str], subject: str, title: str, lines: list[str], cta: Optional[tuple[str, str]] = None) -> bool:
    if not to:
        return False
    return await send_async(to=to, subject=subject, html=_layout(title, _p(*[escape(x) for x in lines]),
                                                                  (cta[0], link(cta[1])) if cta else None))


async def interest_received(to: str, name: str, number: str, lines: list[str], total_minor: int) -> bool:
    items = "".join(f"<li>{escape(x)}</li>" for x in lines)
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"We have your list ({escape(number)}). Someone from the team will call or "
              "message you to agree payment and delivery. Nothing is charged until then.",
              f"<ul style=\"margin:0 0 12px;padding-left:18px\">{items}</ul>", f"Estimated total: <b>{ghs(total_minor)}</b> plus delivery.")
    return await send_async(to=[to], subject=f"We have your list · {number}", html=_layout("Thanks. We'll be in touch.", body),
                            text=f"We have your list ({number}). We'll call you to agree payment and delivery.")


async def ticket_confirmed(to: str, name: str, event: str, when: str, code: str, qty: int, status: str, path: str) -> bool:
    if status == "interest":
        title, lead = "Your spot is reserved.", "We'll call or message you to arrange payment. Your reservation code is below."
    else:
        title, lead = "You're going.", f"Your {'ticket' if qty == 1 else f'{qty} tickets'} for {escape(event)}. Show this code at the door."
    body = _p(f"Hi {escape(name.split(' ')[0])},", lead, f"<b>{escape(event)}</b><br>{escape(when)}",
              f'<span style="display:inline-block;font-size:26px;letter-spacing:.2em;font-weight:900;background:#141414;color:#FDBB24;padding:12px 18px;border-radius:10px">{escape(code)}</span>')
    return await send_async(to=[to], subject=f"{event} · {code}", html=_layout(title, body, ("View your ticket", link(path))),
                            text=f"{title} {event}, {when}. Code: {code}. {link(path)}")


def _lines_html(text: str) -> str:
    """A typed message, safe and with its line breaks."""
    paras = [p.strip() for p in (text or "").replace("\r\n", "\n").split("\n\n") if p.strip()]
    return "".join(f'<p style="margin:0 0 12px">{escape(p).replace(chr(10), "<br>")}</p>' for p in paras)


async def contact_received(to: str, name: str, reference: str, reason: str, subject: str, path: str, role: Optional[str] = None) -> bool:
    if role:
        title, lead = "We have your application.", f"Thank you for applying for <b>{escape(role)}</b>. A person on our team reads every application."
    else:
        title, lead = "We have your message.", f"Thanks for getting in touch about <b>{escape(reason.lower())}</b>. The right person on our team will reply."
    body = _p(f"Hi {escape(name.split(' ')[0])},", lead, f"Your reference is <b>{escape(reference)}</b>.",
              "Every reply comes to this email address. You can also read the whole conversation and answer us from the link below.")
    return await send_async(to=[to], subject=f"{'Application received' if role else 'We have your message'} · {reference}",
                            html=_layout(title, body, ("See the conversation", link(path))), reply_to=settings.SH_CONTACT_EMAIL,
                            text=f"{title} Reference {reference}. {link(path)}")


async def contact_reply(to: str, name: str, reference: str, subject: str, text: str, by: str, path: str) -> bool:
    body = _p(f"Hi {escape(name.split(' ')[0])},") + _lines_html(text) + \
        _p(f"<span style=\"color:#5A5851\">{escape(by)} · Streamers Hub</span>",
           "<span style=\"color:#5A5851;font-size:13px\">To answer, use the button so the whole team sees it, or reply to this email.</span>")
    return await send_async(to=[to], subject=f"Re: {subject} [{reference}]", html=_layout(subject, body, ("Reply on our website", link(path))),
                            text=f"{text}\n\n{by} · Streamers Hub\nReply: {link(path)}", reply_to=settings.SH_CONTACT_EMAIL)


async def contact_to_team(reference: str, reason: str, subject: str, who: str, preview: str, os_path: str, *, again: bool = False) -> bool:
    to = [settings.SH_CONTACT_EMAIL] if settings.SH_CONTACT_EMAIL else []
    if not to:
        return False
    title = f"{who} replied" if again else f"New: {reason}"
    body = _p(f"<b>{escape(subject)}</b>", f"From {escape(who)} · {escape(reference)}") + _lines_html(preview[:600])
    return await send_async(to=to, subject=f"{'Reply' if again else 'New message'} · {subject} [{reference}]",
                            html=_layout(title, body, ("Answer in Hub OS", link(os_path))))


async def suggestion_received(to: str, name: str, reference: str, title: str, path: str) -> bool:
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"Thanks for your idea, <b>{escape(title)}</b>. Your reference is <b>{escape(reference)}</b>.",
              "The team it concerns will answer within a week. You can follow it, and read our answers, from the link below.")
    return await send_async(to=[to], subject=f"We have your idea · {reference}", html=_layout("We have your idea.", body, ("Follow my idea", link(path))),
                            text=f"Thanks for your idea, {title}. Reference {reference}. Follow it: {link(path)}")


async def suggestion_update(to: str, name: str, reference: str, title: str, heading: str, text: str, path: str,
                            signed: Optional[str] = None) -> bool:
    """News on an idea: our answer (signed by who wrote it), planned, done, merged, or "not now" and why."""
    body = _p(f"Hi {escape(name.split(' ')[0])},", f"About your idea <b>{escape(title)}</b> ({escape(reference)}):") + _lines_html(text)
    if signed:
        body += _p(f"<span style=\"color:#5A5851\">{escape(signed)} · Streamers Hub</span>")
    return await send_async(to=[to], subject=f"{heading} · {reference}", html=_layout(heading, body, ("See my idea", link(path))),
                            text=f"{heading}. {title} ({reference}): {text}\n{link(path)}")
