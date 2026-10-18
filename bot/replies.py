"""Everything the bot says, in one place so wording is easy to change.

WhatsApp's policy forbids businesses from asking for, or sending, ID card and
bank numbers in chat. So these texts never ask people to send ID documents,
and ID/bank items are only ever delivered through a secure web link.
"""

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import LINK_MINUTES, PIN_RESET_WAIT_HOURS
from bot.whatsapp import MAX_UPLOAD_MB


def privacy_url() -> str:
    return settings.PUBLIC_BASE_URL + reverse("core:privacy")


def support_line() -> str:
    """WhatsApp requires a way to reach a person when a bot answers."""
    return f"\n\nNeed a person? Email {settings.SUPPORT_EMAIL}" if settings.SUPPORT_EMAIL else ""


FILE_WAITING = "📄 I've received your file. I'll save it as soon as you agree."


def welcome(file_waiting: bool = False) -> str:
    return (
        "👋 Welcome to *Kagaz*: your documents, one message away.\n\n"
        "Send me any document or photo (marksheets, certificates, offer letters, bills…) and I'll keep it safe. "
        "Later just ask: _\"send my 10th marksheet under 100 KB\"_ or _\"what's my 12th roll number?\"_\n\n"
        "🔒 Your files are encrypted with a key unique to you, and an AI reads each one once to name it. "
        "You can delete everything anytime.\n\n"
        + (f"{FILE_WAITING}\n\n" if file_waiting else "")
        + "Tap *I agree* to start."
    )


def consent_reminder(file_waiting: bool = False) -> str:
    return (
        "Please tap *I agree* first. I can't store or read anything until you do."
        + (f"\n\n{FILE_WAITING}" if file_waiting else "")
    )


CONSENT_BUTTONS = [("consent:yes", "I agree"), ("consent:privacy", "Privacy details")]


def privacy_details() -> str:
    return (
        "*How Kagaz handles your data*\n\n"
        "• Each file and its details are encrypted with a key unique to you.\n"
        "• Kagaz staff can't open your documents through our admin tools.\n"
        "• An AI service reads each document once to name it and find its details.\n"
        "• ID cards and bank papers are never sent in this chat. They open on a secure "
        "web page after your PIN.\n"
        "• Type *DELETE MY DATA* anytime to erase everything.\n\n"
        f"Full policy: {privacy_url()}"
    )


CONSENT_GIVEN = (
    "You're all set ✅\n\n"
    "Send your first document now: a marksheet, certificate, offer letter, anything. "
    "You can forward old files from other chats too."
)


def help_text() -> str:
    return (
        "*How to use Kagaz* 📄\n\n"
        "• *Save:* send any document or photo here\n"
        "• *Get it back:* \"send my 12th marksheet\"\n"
        "• *Shrink it:* \"12th marksheet under 100 KB\"\n"
        "• *Change format:* \"degree as PDF\" / \"certificate as JPG\"\n"
        "• *Photo for forms:* \"my photo 200x230 px under 50 KB\"\n"
        "• *Combine:* \"10th and 12th marksheet in one PDF\"\n"
        "• *Ask a detail:* \"what's my 12th roll number?\"\n"
        "• *See everything:* LIST\n"
        "• *Open on the web:* WEB\n"
        "• *Set or change PIN:* PIN · *Forgot it?* FORGOT PIN\n"
        "• *Erase everything:* DELETE MY DATA\n\n"
        "🔒 ID cards and bank papers open on a secure page with your PIN."
        + support_line()
    )


EMPTY_VAULT = "Your Kagaz is empty. Send me a document or photo to save it. Type HELP to see what I can do."
SOMETHING_WENT_WRONG = "Sorry, something went wrong on my side. Please try again in a minute."
AI_UNAVAILABLE = "Sorry, I couldn't understand that right now. Please try again in a minute, or type LIST to see your documents."
AI_BUSY_SEARCHED = "My AI helper is busy right now, so I searched your documents by name."
VOICE_NOT_YET = "I can't listen to voice notes yet. Please type what you need 🙏"
UNSUPPORTED = "I can save documents and photos, and send them back when you ask. Type HELP to see how."
FILE_TOO_LARGE = f"That file is too big. I can save files up to {MAX_UPLOAD_MB} MB."
CANCELLED = "Okay, cancelled 👍"
NOT_FOUND = "I couldn't find that in your Kagaz. Type LIST to see what you've saved."
WIPE_CONFIRM = (
    "⚠️ This permanently erases *all* your documents and details from Kagaz. It can't be undone.\n\n"
    "Are you sure?"
)
WIPE_BUTTONS = [("wipe:yes", "Yes, erase all"), ("cancel", "Cancel")]
WIPED = "All your Kagaz data has been erased 🗑️. Send *hi* anytime to start fresh."
ALREADY_CONSENTED = "You're already set up ✅ Send a document, or type HELP."
PIN_SAVED = "Your PIN is saved 🔐 ID cards and bank papers will only open after this PIN."


def document_limit(limit: int) -> str:
    return (
        f"Your free plan holds {limit} documents and it's full. "
        "Delete one you don't need (e.g. \"delete old electricity bill\") to make room. "
        "Kagaz Plus with unlimited storage is coming soon."
    )


def conversion_limit(limit: int) -> str:
    return (
        f"You've used your {limit} free conversions this month, so here's the original file. "
        "Kagaz Plus with unlimited conversions is coming soon."
    )


def _protected_line(member) -> str:
    return (
        "🔒 Protected: I'll only share it through a secure link that needs your PIN."
        if member.has_pin else
        "🔒 Protected: it will only open through a secure link with a PIN. Type *PIN* to set one now."
    )


def saved(document, member, read=None) -> str:
    details = document.get_details()
    side = f" ({document.side})" if document.side in ("front", "back") else ""
    lines = [f"✅ Saved: *{document.title}*{side}"]
    if details.get("holder_name"):
        lines.append(f"Name: {details['holder_name']}")
    labels = [f["label"] for f in details.get("fields", [])][:6]
    if labels:
        lines.append(f"I can tell you: {', '.join(labels)}")
    if read and read.partner:
        lines.append(f"🔗 Matched with the {read.partner.side} side you saved earlier. "
                     "I'll send both sides together, on one page.")
    elif document.side in ("front", "back") and not document.card_group:
        other = "back" if document.side == "front" else "front"
        lines.append(f"📎 If it has a {other} side, send that too and I'll keep them together.")
    if document.is_sensitive:
        lines.append(_protected_line(member))
    return "\n".join(lines)


def label_question(document, read) -> str:
    """Asked when the AI couldn't tell what a document is (sent with a pick list)."""
    if read.retrying:
        start = (f"✅ Saved *{document.title}*. My AI helper is busy, so I'll read it again in a few minutes. "
                 "You can also tell me now what it is.")
    elif read.problem:
        start = f"✅ Saved *{document.title}*, but I couldn't read it because {read.problem}. What is it?"
    else:
        start = "✅ Saved, but the picture is too unclear for me to tell what it is. What is it?"
    return f"{start}\n\nChoosing a type helps you find it later."


def labelled(document, member, label: str) -> str:
    if document.title == label:
        lines = [f"👍 Got it: saved as *{label}*."]
    else:
        lines = [f"👍 Got it: *{document.title}* is saved as {label}."]
    if document.is_sensitive:
        lines.append(_protected_line(member))
    return "\n".join(lines)


def read_later(title_before: str, document) -> str:
    return f"📖 I've now read *{title_before}*: it's your *{document.title}*. Ask for it anytime."


# --- Forgotten PIN ---

NO_PIN_YET = "You haven't set a PIN yet. Type *PIN* to create one."
FORGOT_PIN_CONFIRM = (
    "🔐 Forgot your PIN?\n\n"
    f"For your safety, a PIN reset takes {PIN_RESET_WAIT_HOURS} hours. Until then your ID cards and bank papers "
    "stay locked, and if someone else started the reset, you can stop it.\n\nStart the reset?"
)
FORGOT_PIN_BUTTONS = [("pinreset:start", "Start reset"), ("pinreset:cancel", "Cancel")]
PIN_RESET_CANCELLED = "✅ PIN reset cancelled. Your PIN stays the same."
PIN_RESET_CANCELLED_BY_USE = (
    "Your PIN was just used correctly, so the PIN reset you started has been cancelled. "
    "If you didn't use it, type *FORGOT PIN* again."
)
PIN_RESET_DONE = "✅ Your new PIN is saved 🔐"


def _when(moment) -> str:
    return f"{timezone.localtime(moment):%d %b, %I:%M %p}"


def pin_reset_started(ready_at) -> str:
    return (
        f"⏳ PIN reset started. It will be ready on *{_when(ready_at)}*.\n\n"
        "After that, send *PIN* here and I'll give you a link to create a new PIN.\n\n"
        "Didn't ask for this? Reply *CANCEL RESET* now, and check WhatsApp → Settings → Linked devices."
    )


def pin_reset_waiting(ready_at) -> str:
    return (
        f"⏳ Your PIN reset will be ready on *{_when(ready_at)}*. Send *PIN* after that.\n\n"
        "If you remember your PIN, you can still use it: that cancels the reset. "
        "Reply *CANCEL RESET* to stop it."
    )


def pin_reset_link(url: str) -> str:
    return (
        f"🔐 Your PIN reset is ready. Create a new PIN here (works once, for {LINK_MINUTES} minutes):\n{url}\n\n"
        "Never share your PIN, and never type it in this chat."
    )


def duplicate(document) -> str:
    return f"You've already saved this one: *{document.title}* 👍"


def _link_footer(has_pin: bool) -> str:
    step = "Enter your PIN" if has_pin else "Create a PIN (you'll use it next time too)"
    return f"{step} on the page. The link works once, for {LINK_MINUTES} minutes."


def pin_link(url: str, has_pin: bool) -> str:
    action = "change your PIN" if has_pin else "set your PIN"
    return (
        f"🔐 Open this secure link to {action} (works once, for {LINK_MINUTES} minutes):\n{url}\n\n"
        "Never share your PIN, and never type it in this chat."
    )


def web_link(url: str, has_pin: bool) -> str:
    return f"🌐 Your Kagaz on the web:\n{url}\n\n{_link_footer(has_pin)}"


def protected_document_link(titles: list[str], url: str, has_pin: bool) -> str:
    names = ", ".join(f"*{t}*" for t in titles[:3])
    return (
        f"🔒 {names} is protected, so I don't send it in chat. Open it securely here:\n{url}\n\n"
        f"{_link_footer(has_pin)}"
    )


def protected_detail_link(label: str, title: str, url: str, has_pin: bool) -> str:
    return (
        f"🔒 Your *{label}* (from {title}) is protected, so I don't send it in chat. "
        f"See it, with the document, here:\n{url}\n\n{_link_footer(has_pin)}"
    )


def confirm_delete_with_pin(titles: list[str], url: str, has_pin: bool) -> str:
    names = ", ".join(f"*{t}*" for t in titles[:3])
    return f"🔒 To delete {names}, confirm on this page:\n{url}\n\n{_link_footer(has_pin)}"


def unlock_to_wipe(url: str) -> str:
    return (
        f"🔒 To erase everything, confirm with your PIN on this link:\n{url}\n\n"
        f"The link works once, for {LINK_MINUTES} minutes."
    )


def choose_document() -> str:
    return "I found a few matches. Which one do you need?"


def confirm_delete(document) -> str:
    return f"Delete *{document.title}* from Kagaz? This can't be undone."


def deleted(title: str) -> str:
    return f"Deleted *{title}* 🗑️"


def field_answer_label(field: dict, document) -> str:
    return f"*{field['label']}* from {document.title}:"


def field_source_caption(document) -> str:
    return f"{document.title}: here's the document, so you can double-check."


def field_not_found(document) -> str:
    return (
        f"I couldn't find that detail in *{document.title}*. "
        "Want the document itself? Just ask for it by name."
    )


def document_list(documents, total: int) -> str:
    lines = [f"*Your documents* ({total})"]
    for number, document in enumerate(documents, 1):
        lines.append(f"{number}. {document.list_label}{' 🔒' if document.is_sensitive else ''}")
    if total > len(documents):
        lines.append(f"…and {total - len(documents)} more.")
    lines.append("\nAsk for any of them by name, or type WEB to see them in your browser.")
    return "\n".join(lines)


def file_caption(name: str, size_kb: int) -> str:
    return f"{name} · {size_kb} KB"
