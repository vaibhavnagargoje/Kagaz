"""
The web vault: a chat with Kagaz in the browser, after the PIN.

Requests from the page's JavaScript (header "X-Requested-With: fetch") get back
just the new message bubbles, or JSON. Without JavaScript every form still
works and the page simply reloads.
"""

import zipfile
from io import BytesIO
from urllib.parse import quote

from django.contrib import messages
from django.db import transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import content_disposition_header, urlencode
from django.views.decorators.http import require_POST

from accounts.models import UNLOCK_MINUTES, Member
from accounts.sessions import member_required
from bot import intent, replies, tasks
from bot.whatsapp import MAX_UPLOAD_MB
from vault import chat, conversions, services
from vault.conversions import ConversionError
from vault.models import LABEL_CHOICES, Document

PREVIEW_TYPES = {conversions.PDF} | conversions.IMAGE_TYPES
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


def _is_fetch(request) -> bool:
    return request.headers.get("X-Requested-With") == "fetch"


def _entries_response(request, member, entries, previous_who):
    return render(request, "vault/_entries.html", {
        "entries": chat.present(member, entries, previous_who=previous_who),
    })


# --- The chat ---------------------------------------------------------------


@member_required
def home(request):
    member = request.member
    return render(request, "vault/chat.html", {
        "member": member,
        "entries": chat.present(member, chat.history(request)),
        "unlocked": member.is_unlocked(),
        "unlock_minutes": UNLOCK_MINUTES,
        "first_name": (member.display_name or "").split(" ")[0],
        "suggestions": _suggestions(member),
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    })


@member_required
def thread(request):
    """The whole conversation, re-shown (e.g. after the PIN, so hidden numbers appear)."""
    return render(request, "vault/_entries.html", {"entries": chat.present(request.member, chat.history(request))})


@member_required
@require_POST
def send(request):
    member = request.member
    history = chat.history(request)
    previous_who = history[-1]["who"] if history else None
    action = request.POST.get("action", "")
    text = request.POST.get("text", "").strip()[: intent.MAX_MESSAGE_CHARS]

    if action:
        label = request.POST.get("label", "").strip()[:120]
        mine = [chat.me(label)] if label else []
        answer = chat.reply_to_action(member, action, request.POST)
    elif text:
        mine, answer = [chat.me(text)], chat.reply_to_text(member, text)
    else:
        return HttpResponseBadRequest() if _is_fetch(request) else redirect("vault:home")

    chat.add(request, *mine)
    added = chat.add(request, *answer)
    if _is_fetch(request):
        return _entries_response(request, member, added, "me" if mine else previous_who)
    return redirect("vault:home")


@member_required
@require_POST
def upload(request):
    member = request.member
    upload = request.FILES.get("file")
    mine = [chat.me_file(upload.name)] if upload else []
    if upload is None or upload.size > MAX_UPLOAD_BYTES:
        answer = [chat.bot_text(f"Please choose a PDF or photo up to {MAX_UPLOAD_MB} MB.")]
    elif not member.can_add_document():
        answer = [chat.bot_text(
            f"Your free plan holds {member.limits()['documents']} documents and it's full. "
            "Delete one you don't need to make room, or type PLUS on WhatsApp for unlimited storage."
        )]
    else:
        data = upload.read()
        mime = conversions.detect_mime(data, fallback=upload.content_type or "application/octet-stream")
        answer = chat.reply_to_upload(services.save_document(member, data, mime, filename=upload.name))

    chat.add(request, *mine)
    added = chat.add(request, *answer)
    if _is_fetch(request):
        return _entries_response(request, member, added, "me" if mine else None)
    return redirect("vault:home")


@member_required
@require_POST
def clear(request):
    chat.clear(request)
    return redirect("vault:home")


@member_required
@require_POST
def lock(request):
    """Close ID documents again right away (e.g. before handing the phone to someone)."""
    Member.objects.filter(pk=request.member.pk).update(unlocked_until=None)
    if _is_fetch(request):
        return JsonResponse({"ok": True})
    return redirect(_safe_back(request.POST.get("back", "")))


def _suggestions(member) -> list[dict]:
    """Quick-reply chips above the message box."""
    recent = list(member.documents.exclude(status=Document.Status.PROCESSING)[:12])
    if not recent:
        return [{"attach": True, "label": "Add a document"}, {"action": "help", "label": "What can you do?"}]
    chips = [{"action": "list", "label": "My documents"}]
    seen = set()
    for document in services.one_per_card(recent):
        if document.needs_label:
            continue
        if document.title not in seen and len(chips) < 4:
            seen.add(document.title)
            chips.append({"action": "show", "doc": str(document.pk), "label": document.title})
    chips.append({"action": "help", "label": "What can you do?"})
    return chips


# --- Files ------------------------------------------------------------------


@member_required
@require_POST
def download(request):
    member = request.member
    documents = services.owned_documents(member, request.POST.getlist("doc"))
    if not documents:
        raise Http404
    fetch = _is_fetch(request)
    back = _safe_back(request.POST.get("back", ""))

    if services.needs_pin(documents) and not member.is_unlocked():
        if fetch:
            return JsonResponse({"error": "pin"}, status=403)
        messages.info(request, "Enter your PIN to download ID and bank documents.")
        return redirect(_unlock_url(back))

    options = chat.options_from(request.POST)
    if options.converts and not member.can_convert():
        limit = member.limits()["conversions_per_month"]
        problem = (
            f"You've used your {limit} free conversions this month. You can still download the original, "
            "or type PLUS on WhatsApp for more."
        )
        return _problem(request, problem, back, fetch)
    try:
        prepared = services.prepare_files(member, documents, options)
    except ConversionError as exc:
        return _problem(request, str(exc), back, fetch)

    if len(prepared.files) == 1:
        item = prepared.files[0]
        response = _attachment(item.data, item.mime, item.name)
    else:
        buffer, used_names = BytesIO(), set()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in prepared.files:
                archive.writestr(_unique_name(item.name, used_names), item.data)
        response = _attachment(buffer.getvalue(), "application/zip", "Kagaz_documents.zip")

    if prepared.notes:
        if fetch:
            response["X-Kagaz-Note"] = quote(" ".join(prepared.notes))
        else:
            for note in prepared.notes:
                messages.warning(request, note)
    return response


def _problem(request, text: str, back: str, fetch: bool):
    if fetch:
        return JsonResponse({"error": text}, status=400)
    messages.error(request, text)
    return redirect(back)


# --- Document viewer --------------------------------------------------------


@member_required
def document_detail(request, doc_id):
    member = request.member
    document = _owned_or_404(member, doc_id)
    unlocked = member.is_unlocked()
    details = document.get_details()
    rows = [
        {"label": f["label"], "value": f["value"], "locked": document.is_field_protected(f) and not unlocked}
        for f in details.get("fields", [])
    ]
    locked = document.is_sensitive and not unlocked
    sides = services.with_card_sides([document])  # Both sides, if this is half of a card.
    return render(request, "vault/document.html", {
        "member": member,
        "document": document,
        "details": details,
        "fields": rows,
        "locked": locked,
        "unlocked": unlocked,
        "unlock_minutes": UNLOCK_MINUTES,
        "can_preview": document.mime_type in PREVIEW_TYPES,
        "size_kb": max(1, round(document.size_bytes / 1024)),
        "unlock_url": _unlock_url(request.path),
        "sides": sides if len(sides) == 2 else None,
        "other_side": next((d for d in sides if d != document), None),
        "label_choices": LABEL_CHOICES,
        "can_retry_reading": document.status == Document.Status.WAITING or details.get("read_problem") == "busy",
    })


@member_required
def preview(request, doc_id):
    member = request.member
    document = _owned_or_404(member, doc_id)
    if document.is_sensitive and not member.is_unlocked():
        return HttpResponseForbidden()
    try:
        jpeg = conversions.preview_jpeg(document.read_file(), document.mime_type)
    except ConversionError:
        raise Http404
    return HttpResponse(jpeg, content_type="image/jpeg")


@member_required
@require_POST
def label(request, doc_id):
    """The owner says what a document is, when the AI couldn't tell."""
    member = request.member
    document = _owned_or_404(member, doc_id)
    labels = dict(LABEL_CHOICES)
    category = request.POST.get("category", "")
    if category in labels:
        services.label_document(document, category)
        messages.success(request, f"Saved as {labels[category]}.")
    return redirect("vault:document", doc_id=document.pk)


@member_required
@require_POST
def reread(request, doc_id):
    """Ask the AI to read a document again now (it was busy before)."""
    member = request.member
    document = _owned_or_404(member, doc_id)
    outcome = services.read_document(document, document.read_file())
    if outcome.retrying:
        messages.warning(request, "The AI helper is still busy. I'll keep trying in the background.")
    elif outcome.problem:
        messages.error(request, f"I couldn't read it because {outcome.problem}.")
    else:
        messages.success(request, f"Read it: {document.title}.")
    return redirect("vault:document", doc_id=document.pk)


@member_required
@require_POST
def delete(request, doc_id):
    member = request.member
    document = _owned_or_404(member, doc_id)
    if document.is_sensitive and not member.is_unlocked():
        return redirect(_unlock_url(reverse("vault:document", args=[document.pk])))
    title, wa_id = document.title, member.wa_id
    services.delete_document(document)
    # Also tell them on WhatsApp, so a deletion they didn't make doesn't go unnoticed.
    transaction.on_commit(lambda: tasks.send_text(wa_id, replies.deleted(title) + " (on the web)"))
    chat.add(request, chat.bot_text(f"Deleted {title}."))
    return redirect("vault:home")


# --- Helpers ----------------------------------------------------------------


def _owned_or_404(member, doc_id) -> Document:
    documents = services.owned_documents(member, [doc_id])
    if not documents:
        raise Http404
    return documents[0]


def _unlock_url(next_path: str) -> str:
    return f"{reverse('accounts:unlock')}?{urlencode({'next': next_path})}"


def _safe_back(value: str) -> str:
    """Only send people back to pages inside the vault."""
    return value if value.startswith("/vault/") and not value.startswith("//") else reverse("vault:home")


def _unique_name(name: str, used: set) -> str:
    candidate, number = name, 2
    while candidate in used:
        stem, dot, extension = name.rpartition(".")
        candidate = f"{stem} ({number}).{extension}" if dot else f"{name} ({number})"
        number += 1
    used.add(candidate)
    return candidate


def _attachment(data: bytes, mime: str, filename: str) -> HttpResponse:
    response = HttpResponse(data, content_type=mime)
    response["Content-Disposition"] = content_disposition_header(as_attachment=True, filename=filename)
    return response
