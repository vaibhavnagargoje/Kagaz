import hmac
import json
import logging

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from bot import tasks
from bot.models import InboundMessage
from bot.whatsapp import verify_signature

logger = logging.getLogger(__name__)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def whatsapp_webhook(request):
    if request.method == "GET":
        return _verify_subscription(request)

    if not verify_signature(request.body, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("Rejected webhook call with a bad signature")
        return HttpResponseForbidden()
    try:
        payload = json.loads(request.body)
    except ValueError:
        return HttpResponseBadRequest()

    for value in _change_values(payload):
        if value.get("metadata", {}).get("phone_number_id") not in (None, settings.WHATSAPP_PHONE_NUMBER_ID):
            continue
        names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
        for message in value.get("messages", []):
            _queue(message, names.get(message.get("from"), ""))
        for status in value.get("statuses", []):
            if status.get("status") == "failed":
                logger.warning("WhatsApp could not deliver message %s: %s", status.get("id"), status.get("errors"))

    # Reply fast; Meta retries webhooks that take too long.
    return HttpResponse("OK")


def _verify_subscription(request):
    """Meta calls this once when you register the webhook URL."""
    token = request.GET.get("hub.verify_token", "")
    expected = settings.WHATSAPP_VERIFY_TOKEN
    if request.GET.get("hub.mode") == "subscribe" and expected and hmac.compare_digest(token, expected):
        return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
    return HttpResponseForbidden()


def _change_values(payload: dict):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == "messages":
                yield change.get("value", {})


def _queue(message: dict, profile_name: str):
    if not message.get("id") or not message.get("from"):
        return
    inbound, created = InboundMessage.objects.get_or_create(
        wa_message_id=message["id"],
        defaults={
            "wa_id": message["from"],
            "msg_type": message.get("type", ""),
            "payload": {"message": message, "profile_name": profile_name},
        },
    )
    if created:
        transaction.on_commit(lambda: tasks.process_inbound(inbound.pk))
