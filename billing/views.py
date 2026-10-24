import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from billing import razorpay, services

logger = logging.getLogger(__name__)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """Razorpay tells us a payment link was paid, expired or cancelled."""
    if not razorpay.verify_signature(request.body, request.headers.get("X-Razorpay-Signature", "")):
        logger.warning("Rejected Razorpay webhook with a bad signature")
        return HttpResponseForbidden()
    try:
        event = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest()
    services.handle_event(event)
    return HttpResponse("OK")


def thanks(request):
    """Where Razorpay sends people after paying. Nothing is switched on here: only the webhook does that."""
    return render(request, "billing/thanks.html")
