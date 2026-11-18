"""Payments behind one interface, so the gateway can be swapped.

PaystackProvider takes mobile money (MTN, Telecel, AirtelTigo) and cards in Ghana. It uses only
SH_PAYSTACK_SECRET_KEY, never a school's Paystack keys. TestProvider is for development without a
key: checkout sends the customer to a test page that can succeed or fail. It refuses to run in
production.
"""

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional, Protocol

import httpx

from app.core.config import settings


@dataclass
class Verified:
    status: str  # success, failed, pending
    amount_minor: int
    channel: Optional[str]  # mobile_money, card
    fees_minor: Optional[int]
    event_key: str


class PaymentProvider(Protocol):
    name: str

    async def start(self, *, reference: str, email: str, amount_minor: int, method: str, callback_url: str) -> str: ...

    async def verify(self, reference: str) -> Verified: ...

    def webhook_ok(self, body: bytes, signature: Optional[str]) -> bool: ...


class PaymentError(Exception):
    pass


class PaystackProvider:
    name = "paystack"
    base = "https://api.paystack.co"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {settings.SH_PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}

    async def start(self, *, reference, email, amount_minor, method, callback_url) -> str:
        channels = ["mobile_money"] if method == "momo" else ["card"]
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(f"{self.base}/transaction/initialize", headers=self._headers(), json={
                "email": email, "amount": amount_minor, "currency": "GHS", "reference": reference,
                "callback_url": callback_url, "channels": channels,
            })
        data = r.json() if r.content else {}
        if r.status_code >= 400 or not data.get("status"):
            raise PaymentError(data.get("message") or "The payment provider didn't answer.")
        return data["data"]["authorization_url"]

    async def verify(self, reference: str) -> Verified:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{self.base}/transaction/verify/{reference}", headers=self._headers())
        data = (r.json() if r.content else {}).get("data") or {}
        status = {"success": "success", "failed": "failed", "abandoned": "failed", "reversed": "failed"}.get(
            data.get("status"), "pending")
        return Verified(status=status, amount_minor=int(data.get("amount") or 0), channel=data.get("channel"),
                        fees_minor=int(data["fees"]) if data.get("fees") is not None else None,
                        event_key=f"paystack:{reference}:{data.get('id')}:{status}")

    def webhook_ok(self, body: bytes, signature: Optional[str]) -> bool:
        if not signature or not settings.SH_PAYSTACK_SECRET_KEY:
            return False
        expected = hmac.new(settings.SH_PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, signature)


TEST_OUTCOMES: dict[str, str] = {}


class TestProvider:
    """No money moves. The customer lands on /checkout/test-pay and picks success or failure."""

    name = "test"

    async def start(self, *, reference, email, amount_minor, method, callback_url) -> str:
        if settings.is_production:
            raise PaymentError("Payments aren't set up yet.")
        return f"{settings.SH_FRONTEND_URL.rstrip('/')}/checkout/test-pay?reference={reference}&method={method}"

    async def verify(self, reference: str) -> Verified:
        outcome = TEST_OUTCOMES.get(reference, "pending")
        return Verified(status=outcome, amount_minor=-1, channel=None, fees_minor=None,
                        event_key=f"test:{reference}:{outcome}")

    def webhook_ok(self, body: bytes, signature: Optional[str]) -> bool:
        return False


def provider() -> PaymentProvider:
    if settings.SH_PAYSTACK_SECRET_KEY:
        return PaystackProvider()
    if settings.payments_test_mode:
        return TestProvider()
    raise PaymentError("Payments aren't set up yet. Contact us to order.")
