"""
The web chat: the same conversation as on WhatsApp, in the browser.

The conversation lives in the signed-in session, so it's gone at sign-out.
Entries store only ids and flags. Titles, numbers and lock states are worked
out fresh each time a message is shown, so nothing protected is stored in plain
text, and a number hidden behind the PIN appears as soon as the PIN is entered.
"""

from dataclasses import asdict
from datetime import datetime

from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from bot import commands, handlers, intent, replies, tasks
from core.llm import LLMError
from vault import services
from vault.conversions import SendOptions
from vault.models import LABEL_CHOICES, Document

HISTORY_KEY = "kagaz_chat"
MAX_ENTRIES = 80

EMPTY = "Your Kagaz is empty. Tap the paperclip below to add your first document."
NOT_FOUND = "I couldn't find that. Try other words, or ask for “my documents”."
GONE = "That document isn't in your Kagaz anymore."
AI_BUSY_FOUND = "My AI helper is busy right now, so I searched your documents by name."
AI_BUSY_NOTHING = "My AI helper is busy right now and nothing matched by name. Try other words, or ask for “my documents”."
ON_WHATSAPP_PIN = "To set or change your PIN, type PIN in your Kagaz chat on WhatsApp."
ON_WHATSAPP_FORGOT = "To reset a forgotten PIN, type FORGOT PIN in your Kagaz chat on WhatsApp."
ON_WHATSAPP_WIPE = "To erase everything, type DELETE MY DATA in your Kagaz chat on WhatsApp."
ON_WHATSAPP_PLANS = "Plans, codes and invites are in your Kagaz chat on WhatsApp: type PLUS, PLAN or REFER there."
ALREADY_ON_WEB = "You're already using Kagaz on the web 🙂 Ask for any document, or for “my documents”."
FROM_WHATSAPP = "Here's what you asked for on WhatsApp."

EXAMPLES = [
    "12th marksheet under 100 KB",
    "my 12th roll number",
    "degree certificate as PDF",
    "10th and 12th in one PDF",
    "my photo 200x230 px under 50 KB",
    "aadhaar back side only",
]

KINDS = {"application/pdf": "PDF", "image/jpeg": "JPG", "image/png": "PNG", "image/webp": "WEBP"}


def bot(kind: str, **data) -> dict:
    return {"who": "bot", "type": kind, **data}


def bot_text(text: str) -> dict:
    return bot("text", text=text)


def me(text: str) -> dict:
    return {"who": "me", "type": "text", "text": text}


def me_file(name: str) -> dict:
    return {"who": "me", "type": "file", "name": name}


def _ids(documents) -> list[str]:
    return [str(d.pk) for d in documents]


# --- History ----------------------------------------------------------------


def history(request) -> list[dict]:
    return request.session.get(HISTORY_KEY, [])


def add(request, *entries) -> list[dict]:
    """Append entries to the conversation; returns them with their time."""
    now = timezone.now().isoformat()
    stamped = [{**entry, "at": now} for entry in entries]
    request.session[HISTORY_KEY] = (history(request) + stamped)[-MAX_ENTRIES:]
    return stamped


def clear(request):
    request.session.pop(HISTORY_KEY, None)


# --- Replies ----------------------------------------------------------------


def reply_to_text(member, text: str) -> list[dict]:
    command = handlers.command_for(text)
    if command:
        return _reply_to_command(member, command)

    documents = list(member.documents.exclude(status=Document.Status.PROCESSING)[: intent.MAX_CATALOG])
    if not documents:
        return [bot_text(EMPTY)]
    if not member.use_question():
        return _search_by_name(
            text, documents,
            found=replies.question_limit_searched(member, on_web=True),
            nothing=replies.question_limit(member, on_web=True),
        )
    try:
        parsed = intent.parse(text, documents)
    except LLMError:
        # The AI is down or out of credits.
        member.refund_question()
        return _search_by_name(text, documents, found=AI_BUSY_FOUND, nothing=AI_BUSY_NOTHING)
    return _reply_to_intent(member, parsed)


def _reply_to_command(member, command: commands.Command) -> list[dict]:
    """The web version of a command: some of them live in the WhatsApp chat."""
    action = command.action
    if action == "list":
        return [bot("list")]
    if action == "count":
        return [bot_text(replies.document_count(member.documents.count(), on_web=True))]
    if action == "pin":
        return [bot_text(ON_WHATSAPP_PIN)]
    if action in ("forgot_pin", "cancel_reset"):
        return [bot_text(ON_WHATSAPP_FORGOT)]
    if action == "wipe":
        return [bot_text(ON_WHATSAPP_WIPE)]
    if action == "web":
        return [bot_text(ALREADY_ON_WEB)]
    if action == "privacy":
        return [bot_text(f"How Kagaz keeps your documents safe: {replies.privacy_url()}")]
    if action in ("plus", "plan", "refer", "code", "friend_code"):
        return [bot_text(ON_WHATSAPP_PLANS)]
    return [bot("help")]


def _search_by_name(text: str, documents, found: str, nothing: str) -> list[dict]:
    """Find documents without the AI."""
    matches = [] if intent.mentions_delete(text) else intent.keyword_match(text, documents)
    if not matches:
        return [bot_text(nothing)]
    cards = services.one_per_card(matches)
    return [bot("docs", doc_ids=_ids(cards), note=found, choose=len(cards) > 1)]


def _reply_to_intent(member, parsed: intent.Intent) -> list[dict]:
    if parsed.action in intent.APP_ACTIONS:
        return _reply_to_command(member, commands.Command(intent.APP_ACTIONS[parsed.action]))

    if parsed.action == "send_document":
        cards = services.one_per_card(parsed.documents)  # A card's two sides count as one.
        if not cards:
            return [bot_text(NOT_FOUND)]
        choose = len(cards) > 1 and not (parsed.send_all or parsed.options.merge)
        return [bot("docs", doc_ids=_ids(cards), options=asdict(parsed.options), side=parsed.side, choose=choose)]

    if parsed.action == "ask_field":
        holder = next((d for d in parsed.documents if parsed.field_key and d.get_field(parsed.field_key)), None)
        if holder:
            return [bot("field", doc_id=str(holder.pk), field_key=parsed.field_key)]
        if parsed.documents:
            return [bot("docs", doc_ids=_ids(parsed.documents[:1]), note="I couldn't find that detail, but here's the document.")]
        return [bot_text(NOT_FOUND)]

    if parsed.action == "delete_document":
        return [bot("confirm_delete", doc_ids=_ids(parsed.documents))] if parsed.documents else [bot_text(NOT_FOUND)]
    if not parsed.reply:
        return [bot("help")]
    return [bot_text(parsed.reply)]


def reply_to_action(member, action: str, data) -> list[dict]:
    """Taps on buttons and chips in the chat."""
    found = services.owned_documents(member, [data.get("doc", "")]) if data.get("doc") else []
    document = found[0] if found else None

    if action == "list":
        return [bot("list")]
    if action == "help":
        return [bot("help")]
    if action == "cancel":
        return [bot_text("Okay, cancelled.")]
    if document is None:
        return [bot_text(GONE)]

    if action == "show":
        return [bot("docs", doc_ids=[str(document.pk)])]

    if action == "pair":
        if data.get("value") == "new":
            return [bot_text(replies.kept_as_new_card(document).replace("*", ""))]
        partner = services.pair_documents(member, document.pk, data.get("value", ""))
        return [bot_text(replies.paired(document, partner).replace("*", "") if partner else replies.PAIR_FAILED)]

    if action == "unpair":
        result = services.unpair(member, document.pk)
        if result is None:
            return [bot_text("Okay, they're already separate.")]
        document, former = result
        choices = services.pair_choices(document, exclude=[former.pk] if former else [])
        return [bot("saved", doc_id=str(document.pk), separated=True, pair_choices=_ids(choices))]

    if action == "label":
        labels = dict(LABEL_CHOICES)
        category = data.get("value", "")
        if category not in labels:
            return [bot("help")]
        services.label_document(document, category)
        return [bot("labelled", doc_id=str(document.pk), label=labels[category])]

    if action == "delete":
        if document.is_sensitive and not member.is_unlocked():
            return [bot("needs_pin", text="ID and bank documents need your PIN before they can be deleted. "
                                          "Enter it, then tap Delete again.")]
        title, wa_id = document.title, member.wa_id
        services.delete_document(document)
        # Also tell them on WhatsApp, so a deletion they didn't make doesn't go unnoticed.
        transaction.on_commit(lambda: tasks.send_text(wa_id, replies.deleted(title) + " (on the web)"))
        return [bot_text(f"Deleted {title}.")]

    return [bot("help")]


def reply_to_upload(result) -> list[dict]:
    read = result.read
    return [bot(
        "saved", doc_id=str(result.document.pk), duplicate=result.duplicate, retrying=read.retrying,
        problem=read.problem, unclear=read.unclear, partner_side=read.partner.side if read.partner else "",
        pair_unsure=bool(read.partner) and not read.sure, pair_choices=_ids(read.choices),
    )]


def from_whatsapp_link(action: dict) -> list[dict]:
    """What a WhatsApp link opened, shown as the latest message in the chat."""
    if action.get("op") == "field":
        doc_ids = action.get("doc_ids", [])
        return [bot("field", doc_id=doc_ids[0] if doc_ids else "", field_key=action.get("field_key", ""), note=FROM_WHATSAPP)]
    return [bot("docs", doc_ids=action.get("doc_ids", []), options=action.get("options", {}),
                side=action.get("side", ""), note=FROM_WHATSAPP)]


# --- Showing ----------------------------------------------------------------


def present(member, entries: list[dict], previous_who: str | None = None) -> list[dict]:
    """Turn stored entries into what the templates show, with fresh lock states."""
    unlocked = member.is_unlocked()
    shown, last = [], previous_who
    for entry in entries:
        item = _present(member, entry, unlocked)
        item.update(who=entry["who"], type=item.get("type", entry["type"]), time=_time(entry.get("at")))
        item["first"] = entry["who"] != last
        last = entry["who"]
        shown.append(item)
    return shown


def _present(member, entry: dict, unlocked: bool) -> dict:
    kind = entry["type"]
    if kind in ("text", "needs_pin"):
        return {"text": entry.get("text", "")}
    if kind == "file":
        return {"name": entry.get("name", "")}
    if kind == "help":
        return {"examples": EXAMPLES}
    if kind == "list":
        documents = list(member.documents.all())
        return {"documents": documents, "limit": member.limits()["documents"]}

    documents = services.owned_documents(member, entry.get("doc_ids") or [entry.get("doc_id", "")])
    if not documents:
        return {"type": "text", "text": GONE}

    if kind == "docs":
        documents = services.with_card_sides(documents, entry.get("side", ""))
        options = options_from(entry.get("options") or {})
        groups = services.card_groups(documents)
        if options.merge and len(groups) > 1:
            cards = [combined_card(documents, options, unlocked)]
        else:
            cards = [card(group, options, unlocked) for group in groups]
        return {"intro": entry.get("note") or _intro(cards, entry.get("choose")), "cards": cards}

    document = documents[0]
    if kind == "field":
        found = document.get_field(entry.get("field_key", ""))
        return {
            "intro": entry.get("note", ""),
            "field": found,
            "locked": bool(found) and document.is_field_protected(found) and not unlocked,
            "card": card([document], SendOptions(), unlocked),
        }
    if kind == "saved":
        pair_options = []
        if not document.card_group and entry.get("pair_choices"):
            pair_options = [
                {"id": str(d.pk), "label": _pair_label(d)}
                for d in services.owned_documents(member, entry["pair_choices"]) if not d.card_group
            ]
        return {
            "card": card([document], SendOptions(), unlocked),
            "lines": _saved_lines(document, entry, asking_pair=bool(pair_options)),
            "duplicate": entry.get("duplicate", False),
            "separated": entry.get("separated", False),
            "ask_label": document.needs_label and not entry.get("duplicate") and not entry.get("separated"),
            "document": document,
            "label_choices": LABEL_CHOICES,
            "pair_options": pair_options,
            "can_unpair": bool(document.card_group) and entry.get("pair_unsure", False),
        }
    if kind == "labelled":
        text = f"Got it: saved as {entry.get('label', '')}."
        if document.is_sensitive:
            text += " It's protected, so it opens only after your PIN."
        return {"type": "text", "text": text}
    if kind == "confirm_delete":
        return {"documents": documents}
    return {"type": "text", "text": ""}


def _time(value: str | None):
    try:
        return timezone.localtime(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return timezone.localtime()


def _intro(cards: list[dict], choose: bool) -> str:
    if choose:
        return f"I found {len(cards)} matches. Which one do you need?"
    if len(cards) == 1:
        only = cards[0]
        if only.get("combined"):
            return "Here they are, combined into one PDF."
        if only["pair"]:
            return f"Here's your {only['title']}, both sides on one page."
        return f"Here's your {only['title']}."
    return "Here they are."


def _pair_label(document: Document) -> str:
    """A chip naming a card side: "Aadhaar Card · front · Asha Verma"."""
    holder = document.get_details().get("holder_name")
    return f"{document.list_label} · {holder or f'saved {timezone.localtime(document.created_at):%d %b, %H:%M}'}"


def _saved_lines(document: Document, entry: dict, asking_pair: bool = False) -> list[str]:
    other = replies.other_side(document.side)
    if entry.get("separated"):
        if asking_pair:
            return ["Okay, I've separated them. Which card is it from?"]
        return [f"Okay, I've separated them. Add the right {other} side and I'll join it."]
    lines = []
    if entry.get("duplicate"):
        lines.append("You've already saved this one.")
    elif entry.get("retrying"):
        lines.append("My AI helper is busy, so I'll read it again in a few minutes.")
    elif entry.get("problem"):
        lines.append(f"I couldn't read it because {entry['problem']}.")
    elif entry.get("unclear"):
        lines.append("The picture is too unclear for me to tell what it is.")
    else:
        labels = [f["label"] for f in document.get_details().get("fields", [])][:6]
        if labels:
            lines.append("I can tell you: " + ", ".join(labels) + ".")
    if entry.get("partner_side") and document.card_group:
        if entry.get("pair_unsure"):
            lines.append(f"Joined with the {entry['partner_side']} side you saved earlier, so both sides stay "
                         "together. Wrong card? Tap “Not this card”.")
        else:
            lines.append(f"Matched with the {entry['partner_side']} side you saved earlier. Both sides stay together.")
    elif asking_pair:
        lines.append(f"I have more than one {document.title} {other} it could go with. Which card is it from?")
    elif document.side in (Document.Side.FRONT, Document.Side.BACK) and not document.card_group:
        lines.append(f"If it has a {other} side, add that too and I'll keep them together.")
    if document.is_sensitive:
        lines.append("Protected: it opens only after your PIN.")
    return lines


# --- Document cards ---------------------------------------------------------


def _size(size_bytes: int) -> str:
    if size_bytes < 1024 * 1024:
        return f"{max(1, round(size_bytes / 1024))} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def card(group: list[Document], options: SendOptions, unlocked: bool) -> dict:
    """One attachment bubble: a single document, or both sides of a card."""
    first = group[0]
    pair = len(group) == 2
    sensitive = any(d.is_sensitive for d in group)
    if pair:
        kind, tone, meta = "2 SIDES", "pair", "Front + back · one page"
    else:
        kind = KINDS.get(first.mime_type, "FILE")
        tone = {"PDF": "pdf", "FILE": "file"}.get(kind, "img")
        parts = [kind]
        if first.page_count and first.page_count > 1:
            parts.append(f"{first.page_count} pages")
        parts.append(_size(first.size_bytes))
        if first.side in (Document.Side.FRONT, Document.Side.BACK):
            parts.append(first.get_side_display())
        meta = " · ".join(parts)
    return {
        "ids": _ids(group),
        "pair": pair,
        "front_id": str(group[0].pk) if pair else "",
        "back_id": str(group[1].pk) if pair else "",
        "title": first.title,
        "kind": kind,
        "tone": tone,
        "meta": meta,
        "sensitive": sensitive,
        "locked": sensitive and not unlocked,
        "options": options,
        "options_label": describe_options(options),
        "open_url": reverse("vault:document", args=[first.pk]),
    }


def combined_card(documents: list[Document], options: SendOptions, unlocked: bool) -> dict:
    sensitive = any(d.is_sensitive for d in documents)
    titles = list(dict.fromkeys(d.title for d in documents))
    return {
        "combined": True, "ids": _ids(documents), "pair": False, "title": f"{len(titles)} documents in one PDF",
        "kind": "PDF", "tone": "pdf", "meta": ", ".join(titles), "sensitive": sensitive,
        "locked": sensitive and not unlocked, "options": options, "options_label": describe_options(options),
        "open_url": "",
    }


def options_from(data) -> SendOptions:
    """Size/format options from a form or a stored request, kept within sane limits."""
    def number(name, low, high):
        try:
            value = int(data.get(name) or 0)
        except (TypeError, ValueError):
            return 0
        return value if low <= value <= high else 0

    width, height = number("width", 16, 5000), number("height", 16, 5000)
    return SendOptions(
        target_kb=number("target_kb", 5, 100_000),
        fmt=data.get("fmt") if data.get("fmt") in ("pdf", "jpg") else "original",
        width=width if height else 0,
        height=height if width else 0,
        merge=data.get("merge") in (True, "1", "true", "on"),
    )


def describe_options(options: SendOptions) -> str:
    parts = []
    if options.merge:
        parts.append("one PDF")
    if options.fmt != "original":
        parts.append(options.fmt.upper())
    if options.width and options.height:
        parts.append(f"{options.width}×{options.height} px")
    if options.target_kb:
        parts.append(f"under {options.target_kb} KB")
    return ", ".join(parts)

