"""Everything the bot says, in one place so wording is easy to change.

WhatsApp's policy forbids businesses from asking for, or sending, ID card and
bank numbers in chat. So these texts never ask people to send ID documents,
and ID/bank items are only ever delivered through a secure web link.
"""

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import LINK_MINUTES, PIN_RESET_WAIT_HOURS, PLAN_LIMITS
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
        "Send me any document or photo (marksheets, certificates, offer letters, light bills, rent agreements, "
        "7/12 extracts…) and I'll keep it safe. "
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
    """Sent with the Menu list, so nobody has to remember commands."""
    return (
        "*Kagaz* 📄 Just tell me what you need, in your own words:\n\n"
        "• _\"send my 12th marksheet\"_\n"
        "• _\"10th marksheet under 100 KB\"_\n"
        "• _\"what's my 12th roll number?\"_\n"
        "• _\"my electricity consumer number\"_\n\n"
        "Send any document or photo to save it. Tap *Menu* for everything else.\n\n"
        "🔒 ID cards and bank papers open on a secure page with your PIN."
        + support_line()
    )


def how_to_ask() -> str:
    return (
        "*What you can ask* 💬 (English, Hindi or Hinglish all work)\n\n"
        "• *Save:* send any document here: a PDF, a photo or a Word .docx file\n"
        "• *Get it back:* \"send my 12th marksheet\" / \"my light bill\" / \"rent agreement\"\n"
        "• *Shrink it:* \"12th marksheet under 100 KB\"\n"
        "• *Change format:* \"degree as PDF\" / \"certificate as JPG\"\n"
        "• *Photo for forms:* \"my photo 200x230 px under 50 KB\"\n"
        "• *Combine:* \"10th and 12th marksheet in one PDF\"\n"
        "• *Ask a detail:* \"what's my 12th roll number?\"\n"
        "• *Remove one:* \"delete old electricity bill\"\n\n"
        "Shortcuts, if you like: LIST · WEB · PIN · PLAN · PLUS · REFER"
    )


MENU_PROMPT = "What would you like to do? Tap *Menu* to choose."
MENU_ROWS = [
    ("menu:list", "📄 My documents", "See everything you've saved"),
    ("menu:howto", "💬 How to ask", "Examples of what you can type"),
    ("menu:web", "🌐 Open on the web", "A sign-in link for your browser"),
    ("menu:pin", "🔒 Set or change PIN", "Protects ID cards and bank papers"),
    ("menu:forgot_pin", "❓ Forgot my PIN", "Reset it after a 24-hour wait"),
    ("menu:plan", "📊 My plan", "Your limits and what you've used"),
    ("menu:plus", "⭐ Kagaz Plus", "Unlimited documents and conversions"),
    ("menu:refer", "🎁 Invite friends", "Share Kagaz, get Plus days free"),
    ("menu:privacy", "🛡️ Privacy", "How your documents are kept safe"),
]
MENU_ACTIONS = {row_id.removeprefix("menu:") for row_id, _, _ in MENU_ROWS}


EMPTY_VAULT = "Your Kagaz is empty. Send me a document or photo to save it. Type HELP to see what I can do."
SOMETHING_WENT_WRONG = "Sorry, something went wrong on my side. Please try again in a minute."
AI_UNAVAILABLE = "Sorry, I couldn't understand that right now. Try other words, or tap *Menu*."
AI_BUSY_SEARCHED = "My AI helper is busy right now, so I searched your documents by name."
VOICE_NOT_YET = "I can't listen to voice notes yet. Please type what you need 🙏"
UNSUPPORTED = (
    "I can save documents: PDFs, photos and Word .docx files, and send them back when you ask. "
    "Type HELP to see how."
)
FILE_TOO_LARGE = f"That file is too big. I can save files up to {MAX_UPLOAD_MB} MB."


def upload_limit(limit: int) -> str:
    return f"You've added {limit} files today, the most for one day. Please send the rest tomorrow 🙏"
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
        "Delete one you don't need (e.g. \"delete old electricity bill\") to make room, "
        "or type *PLUS* for unlimited storage."
    )


def conversion_limit(limit: int) -> str:
    return (
        f"You've used your {limit} free conversions this month, so here's the original file. "
        "Type *PLUS* for unlimited conversions, or to buy a few more."
    )


# --- Plans and payments ---

PLANS_UNAVAILABLE = "Plans aren't available right now. Please try again later."
PAYMENTS_DOWN = "Sorry, payments aren't working right now. Please try again in a little while."
REFER_CLOSED = "Inviting friends isn't open right now. Please check back soon."
REFERRAL_NOT_USED = "That code didn't work. It may be mistyped, or a friend's code is already added."


def _date(moment) -> str:
    return f"{timezone.localtime(moment):%d %b %Y}"


def _plus_status(member) -> str:
    if member.current_plan() == member.Plan.FREE:
        return ""
    if member.plan_expires_at:
        return f"You're on Plus until *{_date(member.plan_expires_at)}*. Buying more adds to that.\n\n"
    return "You're on Plus ✅\n\n"


def plus_offer(member, note: str = "") -> str:
    plus = PLAN_LIMITS["plus"]
    return (
        "*Kagaz Plus* ⭐\n"
        "• Unlimited documents\n"
        "• Unlimited conversions (size, PDF/JPG, photos for forms)\n"
        f"• {plus['questions_per_day']} questions a day\n\n"
        + _plus_status(member)
        + (f"{note}\n\n" if note else "")
        + "Tap one to get a payment link (UPI or card):"
    )


def promo_note(promo) -> str:
    return f"🎟️ Code *{promo.code}* applied."


def friend_note(percent: int) -> str:
    return f"🎁 Friend's discount: {percent}% off your first purchase."


def pay_button(payment) -> str:
    return f"Pay ₹{payment.amount}"


def payment_request(payment) -> str:
    """Sent with a "Pay ₹…" button that opens Razorpay's payment page."""
    was = f" ~₹{payment.list_price}~" if payment.discount else ""
    return (
        f"💳 *{payment.product.name}*: ₹{payment.amount}{was}\n\n"
        "Tap the button to pay with any UPI app (Google Pay, PhonePe, Paytm…) or a card.\n\n"
        "It works for 1 hour. I'll message you here as soon as it's paid."
    )


def payment_received(product, member) -> str:
    lines = ["✅ Payment received, thank you!"]
    if product.plus_days:
        until = f" until *{_date(member.plan_expires_at)}*" if member.plan_expires_at else ""
        lines.append(f"You're on *Kagaz Plus*{until}: unlimited documents and conversions.")
    if product.conversions:
        lines.append(f"{product.conversions} conversions added. You have {member.conversion_credits} extra conversions now.")
    return "\n".join(lines)


def referral_reward(days: int, referrer) -> str:
    until = f" You're on Plus until *{_date(referrer.plan_expires_at)}*." if referrer.plan_expires_at else ""
    return f"🎉 A friend you invited just bought Kagaz, so you get {days} days of Plus free.{until}"


def code_applied(promo) -> str:
    if promo.kind == promo.Kind.PERCENT:
        off = f"{promo.value}% off" + (f" (up to ₹{promo.max_discount})" if promo.max_discount is not None else "")
    else:
        off = f"₹{promo.value} off"
    only = ", ".join(p.name for p in promo.products.all())
    return (
        f"🎟️ Code *{promo.code}* applied: {off}{f' on {only}' if only else ''}. "
        "It's kept for your next purchase for 24 hours."
    )


CODE_HOW = "I couldn't find a code in your message. Send it like this: *CODE SAVE20*"


def plan_summary(member) -> str:
    limits = member.limits()
    free = member.current_plan() == member.Plan.FREE
    if free:
        lines = ["*Your plan:* Free"]
    elif member.plan_expires_at:
        lines = [f"*Your plan:* Plus ⭐ until {_date(member.plan_expires_at)}"]
    else:
        lines = ["*Your plan:* Plus ⭐"]
    documents = member.documents.count()
    lines.append(f"• Documents: {documents}" + (f" of {limits['documents']}" if limits["documents"] else " (unlimited)"))
    left = member.plan_conversions_left()
    if left is None:
        lines.append("• Conversions: unlimited")
    else:
        extra = f" + {member.conversion_credits} from packs" if member.conversion_credits else ""
        lines.append(f"• Conversions left this month: {left}{extra}")
    questions = limits["questions_per_day"]
    lines.append(f"• Questions left today: {max(0, questions - member.questions_used_today())} of {questions}")
    lines.append("\nType *PLUS* to " + ("upgrade" if free else "renew") + " · *REFER* to invite friends")
    return "\n".join(lines)


def refer_intro(code: str, program) -> str:
    return (
        "🎁 *Invite friends to Kagaz*\n\n"
        f"Your code: *{code}*\n"
        f"Your friend gets {program.friend_discount_percent}% off their first purchase. "
        f"When they buy, you get {program.referrer_reward_days} days of Plus free.\n\n"
        "Forward the next message to them 👇"
    )


def refer_share(code: str) -> str:
    number = settings.WHATSAPP_DISPLAY_NUMBER
    where = f"https://wa.me/{number}?text=REF%20{code}" if number else "Kagaz on WhatsApp"
    return (
        "I keep my documents on *Kagaz* 📄. It finds them on WhatsApp in seconds and sends them "
        "in the size online forms need.\n\n"
        f"Get a discount with my code: send *REF {code}* to {where}"
    )


def referral_added(percent: int) -> str:
    return f"🎁 Friend's code added: {percent}% off your first purchase. Type *PLUS* to see plans."


def _more_questions(member, on_web: bool) -> str:
    if member.current_plan() != member.Plan.FREE:
        return ""
    where = " on WhatsApp" if on_web else ""
    return f" Plus gives {PLAN_LIMITS['plus']['questions_per_day']} a day: type PLUS{where}."


def question_limit_searched(member, on_web: bool = False) -> str:
    limit = member.limits()["questions_per_day"]
    return f"You've used today's {limit} questions, so I searched your documents by name.{_more_questions(member, on_web)}"


def question_limit(member, on_web: bool = False) -> str:
    limit = member.limits()["questions_per_day"]
    return (
        f"You've used today's {limit} questions and nothing matched by name. "
        f"Try the exact name, or ask for your document list.{_more_questions(member, on_web)}"
    )


def renewal_notice(member) -> str:
    return (
        f"⏳ Your Kagaz Plus ends on *{_date(member.plan_expires_at)}*. Type *PLUS* to renew. "
        "Your documents stay safe either way."
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
        lines.append(pair_line(read))
    elif read and read.choices:
        pass  # A list follows, asking which card it belongs to.
    elif document.side in ("front", "back") and not document.card_group:
        lines.append(f"📎 If it has a {other_side(document.side)} side, send that too and I'll keep them together.")
    if document.is_sensitive:
        lines.append(_protected_line(member))
    return "\n".join(lines)


# --- Front and back of a card ---

PAIR_FAILED = "I couldn't join those two. One of them may already be joined to another side, or deleted."


def other_side(side: str) -> str:
    return "back" if side == "front" else "front"


def pair_line(read) -> str:
    """How a new side was joined to one saved earlier."""
    if read.sure:
        return (f"🔗 Matched with the {read.partner.side} side you saved earlier. "
                "I'll send both sides together, on one page.")
    return (f"🔗 I joined it with the {read.partner.side} side of {_card_name(read.partner)} you saved earlier, "
            "so both sides go together on one page. Wrong card? Tap *Not this card*.")


def _card_name(document) -> str:
    holder = document.get_details().get("holder_name")
    return f"*{document.title}*" + (f" ({holder})" if holder else "")


def which_card(document) -> str:
    return (
        f"Which card is this *{document.side}* side from? "
        f"I have more than one {document.title} {other_side(document.side)} it could go with. Pick it below."
    )


def paired(document, partner) -> str:
    return (
        f"🔗 Done: the {document.side} and {partner.side} of {_card_name(partner)} are joined. "
        "I'll send both sides together, on one page."
    )


def kept_as_new_card(document) -> str:
    return (
        f"👍 Okay, it's kept as a separate card. Send its {other_side(document.side)} side anytime "
        "and I'll join them."
    )


def unpaired(document, choices_follow: bool) -> str:
    if choices_follow:
        return "👍 Okay, I've separated them. Which card does it belong to?"
    return (
        f"👍 Okay, I've separated them. Send the right {other_side(document.side)} side "
        "and I'll join it."
    )


def document_count(total: int, on_web: bool = False) -> str:
    if not total:
        return EMPTY_VAULT
    saved = f"You have {total} document{'s' if total != 1 else ''} saved."
    if on_web:
        return f"{saved} Ask for “my documents” to see them."
    return f"{saved} Tap *Menu* → My documents to see them."


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
