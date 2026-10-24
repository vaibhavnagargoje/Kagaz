import json
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.templatetags.static import static
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from billing import razorpay, services
from billing.models import Payment

logger = logging.getLogger(__name__)


def _state(payment: Payment) -> str:
    if payment.status == Payment.Status.PAID:
        return "paid"
    if payment.status == Payment.Status.CREATED and payment.razorpay_order_id and payment.expires_at > timezone.now():
        return "open"
    return "expired"


@never_cache
def pay(request, payment_id):
    """Kagaz's own pay page (the "Pay ₹…" button on WhatsApp opens it). It opens Razorpay's checkout."""
    payment = get_object_or_404(Payment.objects.select_related("product", "promo", "member"), pk=payment_id)
    context = {"payment": payment, "state": _state(payment)}
    member = payment.member
    if member:
        context["account"] = f"+{member.wa_id[:2]} •••••• {member.wa_id[-4:]}"
    if context["state"] == "open":
        context["verify_url"] = reverse("billing:verify", args=[payment.pk])
        context["checkout"] = {
            "key": settings.RAZORPAY_KEY_ID,
            "order_id": payment.razorpay_order_id,
            "amount": payment.amount * 100,
            "currency": "INR",
            "name": "Kagaz",
            "description": payment.product.name,
            "image": request.build_absolute_uri(static("kagaz/brand/kagaz-app-icon-1024.png")),
            "prefill": {"contact": f"+{member.wa_id}"} if member else {},
            "theme": {"color": "#1d2b53"},
        }
    return render(request, "billing/pay.html", context)


@require_POST
def verify(request, payment_id):
    """The pay page reports what Razorpay's checkout handed it after paying."""
    payment = get_object_or_404(Payment, pk=payment_id)
    result = services.confirm_checkout(
        payment,
        request.POST.get("razorpay_order_id", ""),
        request.POST.get("razorpay_payment_id", ""),
        request.POST.get("razorpay_signature", ""),
    )
    return JsonResponse({"result": result}, status=400 if result == "invalid" else 200)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """Razorpay tells us an order was paid (a backup for the pay page's own confirmation)."""
    if not razorpay.verify_signature(request.body, request.headers.get("X-Razorpay-Signature", "")):
        logger.warning("Rejected Razorpay webhook with a bad signature")
        return HttpResponseForbidden()
    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest()
    services.handle_event(event)
    return HttpResponse("OK")
