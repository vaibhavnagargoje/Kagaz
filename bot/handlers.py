"""
What the bot does with each incoming WhatsApp message.

    webhook -> InboundMessage -> tasks.process_inbound -> handle_inbound

Delivery rules (see `perform`):
- Ordinary documents (marksheets, certificates…) and their details are sent
  right here in the chat. A detail always comes with its document, so the
  person can double-check it.
- ID and bank documents, and ID/bank numbers found on any document, are never
  sent in chat. The bot replies with a one-time link; after the PIN they open
  in the web vault.
"""

import logging
import re
from dataclasses import asdict, fields

from django.utils import timezone

from accounts.models import AccessLink, Member
from billing import services as billing
from billing.models import Payment, Product, ReferralProgram
from billing.razorpay import RazorpayError
from bot import intent, replies, tasks
from bot.models import InboundMessage
from bot.whatsapp import FileTooLarge, WhatsAppError, get_client
from core.llm import LLMError
from vault import services
from vault.conversions import ConversionError, SendOptions
from vault.models import LABEL_CHOICES, Document

logger = logging.getLogger(__name__)

HELP_WORDS = {"hi", "hii", "hello", "hey", "help", "start", "menu", "namaste", "?"}
LIST_WORDS = {"list", "my documents", "documents", "show all", "all documents"}
PIN_WORDS = {"pin", "set pin", "change pin", "new pin"}
FORGOT_WORDS = {
    "forgot pin", "forgot my pin", "forgot", "reset pin", "reset my pin", "pin reset",
    "pin bhool gaya", "pin bhul gaya", "pin bhool gayi", "pin bhul gayi",
}
CANCEL_RESET_WORDS = {"cancel reset", "cancel pin reset", "stop reset", "stop pin reset"}
WEB_WORDS = {"web", "website", "login", "log in", "open web", "browser", "laptop"}
WIPE_WORDS = {"delete my data", "delete all my data", "delete everything", "erase my data"}
PRIVACY_WORDS = {"privacy", "privacy policy"}
PLUS_WORDS = {"plus", "kagaz plus", "upgrade", "pricing", "price", "prices", "plans", "buy", "renew", "subscribe"}
PLAN_WORDS = {"plan", "my plan", "usage", "my usage"}
REFER_WORDS = {"refer", "invite", "referral", "refer a friend", "invite friends", "my code"}
CODE_COMMAND = re.compile(r"^(?:code|promo|coupon)\s*[:#-]?\s*([a-z0-9_-]{2,32})$")
REFERRAL_COMMAND = re.compile(r"^ref(?:erral)?(?:\s*code)?\s*[:#-]?\s*[a-z0-9]{6}$")

FORMAT_CODES = {"original": "o", "pdf": "p", "jpg": "j"}


# --- Entry point ------------------------------------------------------------


def handle_inbound(message_id: int):
    inbound = InboundMessage.objects.filter(pk=message_id, status=InboundMessage.Status.PENDING).first()
    if inbound is None:
        return
    message = inbound.payload.get("message", {})
    profile_name = inbound.payload.get("profile_name", "")[:120]
    member, _ = Member.objects.get_or_create(wa_id=inbound.wa_id, defaults={"display_name": profile_name})
    client = get_client()
    client.mark_read(inbound.wa_message_id)

    try:
        if member.is_blocked:
            inbound.finish()
            return
        Member.objects.filter(pk=member.pk).update(last_active_at=timezone.now())
        if not member.has_consented:
            # A friend's invite code in the first message ("Hi! REF K7M2QX").
            billing.use_referral_code(member, message.get("text", {}).get("body", ""))
            _before_consent(member, inbound, message)
            return
        _dispatch(member, message)
        inbound.finish()
        fresh = Member.objects.filter(pk=member.pk).first()  # Gone after DELETE MY DATA.
        if fresh and billing.renewal_notice_due(fresh):
            client.send_text(fresh.wa_id, replies.renewal_notice(fresh))
    except Exception:
        logger.exception("Failed to handle inbound message %s", inbound.pk)
        inbound.finish(failed=True)
        try:
            client.send_text(member.wa_id, replies.SOMETHING_WENT_WRONG)
        except WhatsAppError:
            logger.exception("Could not send the error reply for message %s", inbound.pk)


# --- Consent ----------------------------------------------------------------


def _before_consent(member: Member, inbound: InboundMessage, message: dict):
    client = get_client()
    choice = _reply_id(message)

    if choice == "consent:yes":
        member.give_consent()
        inbound.finish()
        client.send_text(member.wa_id, replies.CONSENT_GIVEN)
        # Files sent before agreeing were held back; process them now.
        waiting = InboundMessage.objects.filter(wa_id=member.wa_id, status=InboundMessage.Status.AWAITING_CONSENT)
        waiting_ids = list(waiting.values_list("pk", flat=True))
        waiting.update(status=InboundMessage.Status.PENDING)
        for pk in waiting_ids:
            tasks.process_inbound(pk)
        return

    if choice == "consent:privacy":
        inbound.finish()
        client.send_text(member.wa_id, replies.privacy_details())
        client.send_buttons(member.wa_id, "Tap *I agree* to start.", replies.CONSENT_BUTTONS)
        return

    earlier = InboundMessage.objects.filter(wa_id=member.wa_id).exclude(pk=inbound.pk)
    files_waiting = earlier.filter(status=InboundMessage.Status.AWAITING_CONSENT).exists()
    is_file = message.get("type") in ("document", "image")

    if is_file:
        InboundMessage.objects.filter(pk=inbound.pk).update(status=InboundMessage.Status.AWAITING_CONSENT)
        if files_waiting:
            return  # Several files forwarded at once: we already asked once.
    else:
        inbound.finish()

    if not earlier.exists():
        text = replies.welcome(file_waiting=is_file)
    else:
        text = replies.consent_reminder(file_waiting=is_file or files_waiting)
    client.send_buttons(member.wa_id, text, replies.CONSENT_BUTTONS)


# --- Routing ----------------------------------------------------------------


def _reply_id(message: dict) -> str:
    interactive = message.get("interactive", {})
    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
    return reply.get("id", "")


def _dispatch(member: Member, message: dict):
    kind = message.get("type")
    if kind in ("document", "image"):
        _save(member, message[kind])
    elif kind == "text":
        _handle_text(member, message.get("text", {}).get("body", ""))
    elif kind == "interactive":
        _handle_choice(member, _reply_id(message))
    elif kind == "button":
        _handle_text(member, message.get("button", {}).get("text", ""))
    elif kind == "audio":
        get_client().send_text(member.wa_id, replies.VOICE_NOT_YET)
    else:
        get_client().send_text(member.wa_id, replies.UNSUPPORTED)


def _save(member: Member, media: dict):
    client = get_client()
    if not member.can_add_document():
        client.send_text(member.wa_id, replies.document_limit(member.limits()["documents"]))
        return
    try:
        data, downloaded_mime = client.download_media(media["id"])
    except FileTooLarge:
        client.send_text(member.wa_id, replies.FILE_TOO_LARGE)
        return

    result = services.save_document(
        member,
        data,
        media.get("mime_type") or downloaded_mime,
        filename=media.get("filename", ""),
        caption=media.get("caption", ""),
    )
    # We have our own encrypted copy now, so ask Meta to delete theirs.
    client.delete_media(media["id"])

    if result.duplicate:
        client.send_text(member.wa_id, replies.duplicate(result.document))
    elif result.read.needs_label:
        _ask_label(member, result.document, result.read)
    else:
        client.send_text(member.wa_id, replies.saved(result.document, member, result.read))


def _ask_label(member: Member, document: Document, read):
    """The AI couldn't tell what this is: let the owner pick."""
    rows = [(f"label:{document.pk}:{category}", label, "") for category, label in LABEL_CHOICES]
    get_client().send_list(member.wa_id, replies.label_question(document, read), "Choose type", rows)


def _handle_text(member: Member, text: str):
    client = get_client()
    command = " ".join(text.lower().split()).strip(" .!")
    if command in HELP_WORDS:
        client.send_text(member.wa_id, replies.help_text())
    elif command in LIST_WORDS:
        _send_list(member)
    elif command in PIN_WORDS:
        _send_pin_link(member)
    elif command in FORGOT_WORDS:
        _forgot_pin(member)
    elif command in CANCEL_RESET_WORDS:
        _cancel_pin_reset(member)
    elif command in WEB_WORDS:
        _send_web_link(member)
    elif command in WIPE_WORDS:
        client.send_buttons(member.wa_id, replies.WIPE_CONFIRM, replies.WIPE_BUTTONS)
    elif command in PRIVACY_WORDS:
        client.send_text(member.wa_id, replies.privacy_details())
    elif command in PLUS_WORDS:
        _offer_plus(member)
    elif command in PLAN_WORDS:
        client.send_text(member.wa_id, replies.plan_summary(member))
    elif command in REFER_WORDS:
        _send_invite(member)
    elif code := CODE_COMMAND.match(command):
        _apply_code(member, code.group(1))
    elif REFERRAL_COMMAND.match(command):
        _add_friend_code(member, command)
    else:
        _understand(member, text)


def _understand(member: Member, text: str):
    client = get_client()
    documents = list(member.documents.exclude(status=Document.Status.PROCESSING)[: intent.MAX_CATALOG])
    if not documents:
        client.send_text(member.wa_id, replies.EMPTY_VAULT)
        return
    if not member.use_question():
        _fallback_search(member, text, documents, over_limit=True)
        return
    try:
        parsed = intent.parse(text, documents)
    except LLMError as exc:
        logger.warning("Could not understand a message from member %s: %s", member.pk, exc)
        _fallback_search(member, text, documents)
        return
    _run_intent(member, parsed)


def _fallback_search(member: Member, text: str, documents: list[Document], over_limit: bool = False):
    """The AI is down, or today's questions are used up: still find documents by name."""
    client = get_client()
    matches = [] if intent.mentions_delete(text) else intent.keyword_match(text, documents)
    if not matches:
        client.send_text(member.wa_id, replies.question_limit(member) if over_limit else replies.AI_UNAVAILABLE)
        return
    client.send_text(member.wa_id, replies.question_limit_searched(member) if over_limit else replies.AI_BUSY_SEARCHED)
    if len(matches) == 1:
        perform(member, {"op": "send", "doc_ids": [str(matches[0].pk)], "options": {}})
    else:
        _ask_which(member, matches, SendOptions())


def _run_intent(member: Member, parsed: intent.Intent):
    client = get_client()
    documents = parsed.documents

    if parsed.action == "send_document":
        wants_several = parsed.send_all or parsed.options.merge
        cards = services.one_per_card(documents)  # A card's two sides count as one.
        if not cards:
            client.send_text(member.wa_id, replies.NOT_FOUND)
        elif len(cards) > 1 and not wants_several:
            _ask_which(member, cards, parsed.options, parsed.side)
        else:
            chosen = cards if wants_several else cards[:1]
            perform(member, {
                "op": "send", "doc_ids": [str(d.pk) for d in chosen],
                "options": asdict(parsed.options), "side": parsed.side,
            })

    elif parsed.action == "ask_field":
        holder = next((d for d in documents if parsed.field_key and d.get_field(parsed.field_key)), None)
        if not documents:
            client.send_text(member.wa_id, replies.NOT_FOUND)
        elif holder is None:
            client.send_text(member.wa_id, replies.field_not_found(documents[0]))
        else:
            perform(member, {"op": "field", "doc_ids": [str(holder.pk)], "field_key": parsed.field_key})

    elif parsed.action == "list_documents":
        _send_list(member)

    elif parsed.action == "delete_document":
        if not documents:
            client.send_text(member.wa_id, replies.NOT_FOUND)
        elif len(documents) == 1:
            _confirm_delete(member, documents[0])
        else:
            rows = [(f"delask:{d.pk}", d.title, _describe(d)) for d in documents]
            client.send_list(member.wa_id, "Which one should I delete?", "Choose", rows)

    elif parsed.action == "help":
        client.send_text(member.wa_id, replies.help_text())

    else:
        client.send_text(member.wa_id, parsed.reply or replies.help_text())


def _handle_choice(member: Member, choice: str):
    client = get_client()
    kind, _, rest = choice.partition(":")
    if kind == "consent":
        client.send_text(member.wa_id, replies.ALREADY_CONSENTED)
    elif kind == "s":
        try:
            doc_id, options, side = _decode_choice(choice)
        except ValueError:
            client.send_text(member.wa_id, replies.NOT_FOUND)
            return
        perform(member, {"op": "send", "doc_ids": [doc_id], "options": asdict(options), "side": side})
    elif kind == "label":
        doc_id, _, category = rest.partition(":")
        documents = services.owned_documents(member, [doc_id])
        labels = dict(LABEL_CHOICES)
        if not documents or category not in labels:
            client.send_text(member.wa_id, replies.NOT_FOUND)
            return
        services.label_document(documents[0], category)
        client.send_text(member.wa_id, replies.labelled(documents[0], member, labels[category]))
    elif choice == "pinreset:start":
        _start_pin_reset(member)
    elif choice == "pinreset:cancel":
        _cancel_pin_reset(member)
    elif kind == "delask":
        documents = services.owned_documents(member, [rest])
        if documents:
            _confirm_delete(member, documents[0])
        else:
            client.send_text(member.wa_id, replies.NOT_FOUND)
    elif kind == "del":
        perform(member, {"op": "delete", "doc_ids": [rest]})
    elif choice == "wipe:yes":
        perform(member, {"op": "wipe"})
    elif kind == "buy":
        _buy(member, rest)
    elif kind == "cancel":
        client.send_text(member.wa_id, replies.CANCELLED)
    else:
        client.send_text(member.wa_id, replies.help_text())


# --- Delivery rules ---------------------------------------------------------


def perform(member: Member, action: dict):
    """Carry out a request, or send a secure link when it touches ID/bank items."""
    client = get_client()
    op = action["op"]

    if op == "wipe":
        if member.has_pin and not member.is_unlocked():
            url = AccessLink.issue(member, AccessLink.Purpose.UNLOCK, action)
            client.send_text(member.wa_id, replies.unlock_to_wipe(url))
        else:
            _execute(member, action, [])
        return

    documents = services.owned_documents(member, action.get("doc_ids", []))
    if not documents:
        client.send_text(member.wa_id, replies.NOT_FOUND)
        return
    if op == "send":
        # Both sides of a card go together (one page), unless they asked for one side.
        documents = services.with_card_sides(documents, action.get("side", ""))
        action = {**action, "doc_ids": [str(d.pk) for d in documents]}
    protected = services.needs_pin(documents, action.get("field_key", ""))

    if protected and op in ("send", "field"):
        # Never inside WhatsApp, even right after a PIN: it opens on the web.
        url = AccessLink.issue(member, AccessLink.Purpose.UNLOCK, action)
        if op == "field":
            label = (documents[0].get_field(action.get("field_key", "")) or {}).get("label", "detail")
            text = replies.protected_detail_link(label, documents[0].title, url, member.has_pin)
        else:
            text = replies.protected_document_link(_titles(documents), url, member.has_pin)
        client.send_text(member.wa_id, text)
        return

    if protected and op == "delete" and not member.is_unlocked():
        url = AccessLink.issue(member, AccessLink.Purpose.UNLOCK, action)
        client.send_text(member.wa_id, replies.confirm_delete_with_pin(_titles(documents), url, member.has_pin))
        return

    _execute(member, action, documents)


def _execute(member: Member, action: dict, documents: list[Document]):
    """Do it in the chat. Only reached for items that are allowed in WhatsApp."""
    client = get_client()
    op = action.get("op")

    if op == "wipe":
        wa_id = member.wa_id
        services.delete_member_data(member)
        client.send_text(wa_id, replies.WIPED)

    elif op == "field":
        document = documents[0]
        found = document.get_field(action.get("field_key", ""))
        if not found:
            client.send_text(member.wa_id, replies.field_not_found(document))
            return
        client.send_text(member.wa_id, replies.field_answer_label(found, document))
        client.send_text(member.wa_id, found["value"])  # On its own so it's easy to copy.
        original = services.prepare_files(member, [document], SendOptions()).files[0]
        client.send_document(
            member.wa_id, original.data, original.mime, original.name, caption=replies.field_source_caption(document)
        )

    elif op == "delete":
        for document in documents:
            title = document.title
            services.delete_document(document)
            client.send_text(member.wa_id, replies.deleted(title))

    elif op == "send":
        allowed = {f.name for f in fields(SendOptions)}
        options = SendOptions(**{k: v for k, v in action.get("options", {}).items() if k in allowed})
        if options.converts and not member.can_convert():
            client.send_text(member.wa_id, replies.conversion_limit(member.limits()["conversions_per_month"]))
            options = SendOptions()
        try:
            prepared = services.prepare_files(member, documents, options)
        except ConversionError as exc:
            client.send_text(member.wa_id, str(exc))
            return
        for item in prepared.files:
            client.send_document(
                member.wa_id, item.data, item.mime, item.name, caption=replies.file_caption(item.name, item.size_kb)
            )
        for note in prepared.notes:
            client.send_text(member.wa_id, note)


# --- Helpers ----------------------------------------------------------------


def _describe(document: Document) -> str:
    details = document.get_details()
    parts = [document.get_category_display()]
    if document.card_group:
        parts.append("front + back")
    if details.get("holder_name"):
        parts.append(details["holder_name"])
    parts.append(f"saved {timezone.localtime(document.created_at):%d %b %Y}")
    return " · ".join(parts)


def _titles(documents: list[Document]) -> list[str]:
    return list(dict.fromkeys(d.title for d in documents))  # A card's two sides share a title.


def _encode_choice(doc_id, options: SendOptions, side: str = "") -> str:
    parts = [
        "s", str(doc_id), str(options.target_kb), FORMAT_CODES[options.fmt],
        str(options.width), str(options.height), side or "-",
    ]
    return ":".join(parts)


def _decode_choice(value: str) -> tuple[str, SendOptions, str]:
    _, doc_id, target_kb, fmt, width, height, side = value.split(":")
    formats = {code: name for name, code in FORMAT_CODES.items()}
    options = SendOptions(target_kb=int(target_kb), fmt=formats.get(fmt, "original"), width=int(width), height=int(height))
    return doc_id, options, side if side in ("front", "back") else ""


def _ask_which(member: Member, documents: list[Document], options: SendOptions, side: str = ""):
    rows = [(_encode_choice(d.pk, options, side), d.title, _describe(d)) for d in documents[:10]]
    get_client().send_list(member.wa_id, replies.choose_document(), "Choose", rows)


def _confirm_delete(member: Member, document: Document):
    get_client().send_buttons(
        member.wa_id, replies.confirm_delete(document), [(f"del:{document.pk}", "Yes, delete"), ("cancel", "Cancel")]
    )


def _send_list(member: Member):
    documents = list(member.documents.all()[:30])
    if not documents:
        get_client().send_text(member.wa_id, replies.EMPTY_VAULT)
        return
    get_client().send_text(member.wa_id, replies.document_list(documents, member.documents.count()))


def _send_pin_link(member: Member):
    if member.pin_reset_pending:
        _continue_pin_reset(member)
        return
    url = AccessLink.issue(member, AccessLink.Purpose.SET_PIN)
    get_client().send_text(member.wa_id, replies.pin_link(url, member.has_pin))


# --- Forgotten PIN: reset after a 24-hour wait --------------------------------


def _forgot_pin(member: Member):
    client = get_client()
    if not member.has_pin:
        client.send_text(member.wa_id, replies.NO_PIN_YET)
    elif member.pin_reset_pending:
        _continue_pin_reset(member)
    else:
        client.send_buttons(member.wa_id, replies.FORGOT_PIN_CONFIRM, replies.FORGOT_PIN_BUTTONS)


def _start_pin_reset(member: Member):
    if not member.has_pin:
        get_client().send_text(member.wa_id, replies.NO_PIN_YET)
    elif member.pin_reset_pending:
        _continue_pin_reset(member)
    else:
        member.start_pin_reset()
        get_client().send_text(member.wa_id, replies.pin_reset_started(member.pin_reset_ready_at))


def _continue_pin_reset(member: Member):
    """A reset is waiting: send the link once it's ready, else say when it will be."""
    if member.pin_reset_ready():
        url = AccessLink.issue(member, AccessLink.Purpose.SET_PIN, {"op": "reset_pin"})
        get_client().send_text(member.wa_id, replies.pin_reset_link(url))
    else:
        get_client().send_text(member.wa_id, replies.pin_reset_waiting(member.pin_reset_ready_at))


def _cancel_pin_reset(member: Member):
    text = replies.PIN_RESET_CANCELLED if member.cancel_pin_reset() else replies.CANCELLED
    get_client().send_text(member.wa_id, text)


def _send_web_link(member: Member):
    url = AccessLink.issue(member, AccessLink.Purpose.LOGIN)
    get_client().send_text(member.wa_id, replies.web_link(url, member.has_pin))


# --- Plans and payments -----------------------------------------------------


def _offer_plus(member: Member):
    client = get_client()
    products = billing.products_for(member)
    if not products:
        client.send_text(member.wa_id, replies.PLANS_UNAVAILABLE)
        return
    offers = [billing.quote(member, product) for product in products]
    promo = next((o.promo for o in offers if o.promo), None)
    if promo:
        note = replies.promo_note(promo)
    elif any(o.referral for o in offers):
        note = replies.friend_note(billing.friend_discount_percent(member))
    else:
        note = ""
    buttons = [(f"buy:{o.product.code}", f"{o.product.button_label} ₹{o.amount}") for o in offers]
    client.send_buttons(member.wa_id, replies.plus_offer(member, note), buttons)


def _buy(member: Member, code: str):
    client = get_client()
    product = Product.objects.filter(code=code, is_active=True).first()
    if product is None:
        client.send_text(member.wa_id, replies.PLANS_UNAVAILABLE)
        return
    try:
        payment = billing.start_payment(member, product)
    except RazorpayError:
        logger.exception("Could not create a payment link for member %s", member.pk)
        client.send_text(member.wa_id, replies.PAYMENTS_DOWN)
        return
    # A free (100% off) purchase is already switched on, and mark_paid sent the confirmation.
    if payment.status != Payment.Status.PAID:
        client.send_link_button(
            member.wa_id, replies.payment_request(payment), replies.pay_button(payment), billing.pay_url(payment)
        )


def _apply_code(member: Member, code: str):
    if Member.objects.filter(referral_code=code.upper()).exists():
        _add_friend_code(member, f"ref {code}")  # They typed "CODE K7M2QX" with a friend's code.
        return
    try:
        promo = billing.apply_code(member, code)
    except billing.PromoError as exc:
        get_client().send_text(member.wa_id, str(exc))
        return
    get_client().send_text(member.wa_id, replies.code_applied(promo))


def _send_invite(member: Member):
    client = get_client()
    program = ReferralProgram.get()
    if not program.is_active:
        client.send_text(member.wa_id, replies.REFER_CLOSED)
        return
    code = billing.referral_code(member)
    client.send_text(member.wa_id, replies.refer_intro(code, program))
    client.send_text(member.wa_id, replies.refer_share(code))  # On its own, so it's easy to forward.


def _add_friend_code(member: Member, text: str):
    client = get_client()
    if not ReferralProgram.get().is_active:
        client.send_text(member.wa_id, replies.REFER_CLOSED)
    elif billing.use_referral_code(member, text):
        client.send_text(member.wa_id, replies.referral_added(billing.friend_discount_percent(member)))
    else:
        client.send_text(member.wa_id, replies.REFERRAL_NOT_USED)
