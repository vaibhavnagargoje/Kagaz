"""
Razorpay Payment Links: we create a link, the person pays by UPI or card on
Razorpay's page, and Razorpay tells our webhook (billing/views.py).

Docs: https://razorpay.com/docs/api/payments/payment-links/
"""

import hashlib
import hmac

import requests
from django.conf import settings

API_URL = "https://api.razorpay.com/v1"


class RazorpayError(Exception):
    pass


def _post(path: str, body: dict | None = None) -> dict:
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RazorpayError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set in .env.")
    try:
        response = requests.post(
            f"{API_URL}/{path}",
            json=body or {},
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            timeout=20,
        )
    except requests.RequestException as exc:
        raise RazorpayError(f"POST {path} failed: {exc}") from exc
    if response.status_code >= 400:
        raise RazorpayError(f"POST {path} -> {response.status_code}: {response.text[:300]}")
    return response.json()


def create_payment_link(*, amount: int, reference_id: str, description: str, contact: str,
                        expire_by: int, callback_url: str, notes: dict) -> dict:
    """Create a link for `amount` rupees. Returns Razorpay's answer ("id", "short_url", …)."""
    return _post("payment_links", {
        "amount": amount * 100,  # Razorpay counts in paise.
        "currency": "INR",
        "accept_partial": False,
        "reference_id": reference_id,
        "description": description,
        "customer": {"contact": contact},
        # We send the link on WhatsApp ourselves.
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "expire_by": expire_by,
        "callback_url": callback_url,
        "callback_method": "get",
        "notes": notes,
    })


def cancel_payment_link(link_id: str) -> dict:
    return _post(f"payment_links/{link_id}/cancel")


def verify_signature(body: bytes, signature: str) -> bool:
    """Check the X-Razorpay-Signature header: HMAC-SHA256 of the raw body with the webhook secret."""
    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
