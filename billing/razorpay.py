"""
Razorpay Standard Checkout: we create an order, our own page
(/billing/pay/<id>/) opens Razorpay's checkout for it with Kagaz's name and
logo, and Razorpay confirms the payment to that page and to our webhook.

Docs: https://razorpay.com/docs/payments/payment-gateway/web-integration/standard/
"""

import hashlib
import hmac

import requests
from django.conf import settings

API_URL = "https://api.razorpay.com/v1"


class RazorpayError(Exception):
    pass


def _request(method: str, path: str, body: dict | None = None) -> dict:
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RazorpayError("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET are not set in .env.")
    try:
        response = requests.request(
            method,
            f"{API_URL}/{path}",
            json=body,
            auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET),
            timeout=20,
        )
    except requests.RequestException as exc:
        raise RazorpayError(f"{method} {path} failed: {exc}") from exc
    if response.status_code >= 400:
        raise RazorpayError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
    return response.json()


def create_order(*, amount: int, receipt: str, notes: dict) -> dict:
    """An order for `amount` rupees. Returns Razorpay's answer ("id", …)."""
    return _request("POST", "orders", {
        "amount": amount * 100,  # Razorpay counts in paise.
        "currency": "INR",
        "receipt": receipt,
        "notes": notes,
    })


def fetch_payment(payment_id: str) -> dict:
    """A payment's details ("status", "amount", "order_id", …)."""
    return _request("GET", f"payments/{payment_id}")


def _hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_checkout(order_id: str, payment_id: str, signature: str) -> bool:
    """Check what the checkout hands our page after paying: HMAC of "order|payment" with the key secret."""
    if not settings.RAZORPAY_KEY_SECRET or not signature:
        return False
    expected = _hmac(settings.RAZORPAY_KEY_SECRET, f"{order_id}|{payment_id}".encode())
    return hmac.compare_digest(expected, signature)


def verify_signature(body: bytes, signature: str) -> bool:
    """Check the webhook's X-Razorpay-Signature header: HMAC of the raw body with the webhook secret."""
    if not settings.RAZORPAY_WEBHOOK_SECRET or not signature:
        return False
    return hmac.compare_digest(_hmac(settings.RAZORPAY_WEBHOOK_SECRET, body), signature)
