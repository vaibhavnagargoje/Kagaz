"""Background jobs. Locally they run immediately; in production a worker runs them (python manage.py run_huey)."""

import logging
from datetime import timedelta

from django.utils import timezone
from huey import crontab
from huey.contrib.djhuey import db_periodic_task, db_task

from accounts.models import AccessLink
from bot.models import InboundMessage

logger = logging.getLogger(__name__)

# WhatsApp lets a business message someone freely only within 24 hours of their last message.
FREE_REPLY_WINDOW = timedelta(hours=23)


@db_task()
def process_inbound(message_id: int):
    from bot.handlers import handle_inbound

    handle_inbound(message_id)


@db_task(retries=2, retry_delay=10)
def send_text(wa_id: str, text: str):
    """A WhatsApp message triggered from the website (e.g. "PIN saved", "Deleted …")."""
    from bot.whatsapp import get_client

    get_client().send_text(wa_id, text)


def reread_and_notify(now: bool = False, include_failed: bool = False, limit: int = 20) -> int:
    """Read documents the AI skipped while busy; tell people who chatted recently.

    If a card side could belong to several cards, they're asked which one.
    """
    from bot import replies
    from bot.handlers import ask_which_card
    from bot.whatsapp import WhatsAppError, get_client
    from vault import services

    done = services.reread_due_documents(limit=limit, now=now, include_failed=include_failed)
    for document, title_before, read in done:
        member = document.member
        if member.last_active_at and member.last_active_at > timezone.now() - FREE_REPLY_WINDOW:
            try:
                text = replies.read_later(title_before, document)
                if read.partner and not read.sure:
                    text += "\n\n" + replies.pair_line(read)
                    get_client().send_buttons(member.wa_id, text, [(f"unpair:{document.pk}", "Not this card")])
                else:
                    get_client().send_text(member.wa_id, text)
                if read.choices:
                    ask_which_card(member, document, read.choices)
            except WhatsAppError:
                logger.warning("Could not tell member %s that document %s was read", member.pk, document.pk)
    return len(done)


@db_periodic_task(crontab(minute="*/10"))
def reread_documents():
    reread_and_notify()


@db_periodic_task(crontab(hour="3", minute="0"))
def clean_up():
    """Drop message logs older than 30 days, expired PIN links, and expired web sessions (with their chats)."""
    from django.contrib.sessions.models import Session

    InboundMessage.objects.filter(created_at__lt=timezone.now() - timedelta(days=30)).delete()
    AccessLink.objects.filter(expires_at__lt=timezone.now() - timedelta(days=1)).delete()
    Session.objects.filter(expire_date__lt=timezone.now()).delete()
