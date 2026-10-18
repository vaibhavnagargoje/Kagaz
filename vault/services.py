"""Saving, reading, sending and deleting documents."""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.models import Member
from core import crypto
from core.llm import LLMError
from vault import conversions, extraction
from vault.models import LABEL_CHOICES, SENSITIVE_CATEGORIES, Category, Conversion, Document

logger = logging.getLogger(__name__)

# When the AI is busy, read the document again after these waits (minutes).
REREAD_DELAYS_MINUTES = [5, 15, 60, 180, 720]

# Titles that say nothing about the document (camera and scanner file names).
GENERIC_TITLE = re.compile(
    r"^(saved on|img|image|dsc|pxl|scan|scanned|document|doc|file|photo|screenshot|whatsapp|wa\d)\b|^[\d\s]+$",
    re.IGNORECASE,
)


@dataclass
class ReadResult:
    problem: str = ""  # Why the AI couldn't read it, safe to show the user.
    retrying: bool = False  # The AI was busy; it will be read again later.
    unclear: bool = False  # Read, but the AI couldn't tell what kind of document it is.
    partner: Document | None = None  # The other side of the same card, if just matched.

    @property
    def needs_label(self) -> bool:
        return bool(self.problem) or self.unclear


@dataclass
class SaveResult:
    document: Document
    duplicate: bool = False
    read: ReadResult = field(default_factory=ReadResult)

    @property
    def problem(self) -> str:
        return self.read.problem


def _title_from_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = " ".join(stem.replace("_", " ").replace("-", " ").split())
    return stem[:120] or f"Saved on {timezone.localdate():%d %b %Y}"


def save_document(member: Member, data: bytes, mime: str, filename: str = "", caption: str = "") -> SaveResult:
    """Encrypt and store a file, then let the AI read it."""
    mime = mime.split(";")[0].strip().lower()
    content_hash = crypto.keyed_hash(member.data_key(), data)
    existing = member.documents.filter(content_hash=content_hash).first()
    if existing:
        return SaveResult(existing, duplicate=True)

    document = Document(member=member, mime_type=mime, size_bytes=len(data), content_hash=content_hash)
    if conversions.is_pdf(mime):
        document.page_count = conversions.pdf_page_count(data)
    document.store_file(data)
    document.set_details({
        "title": _title_from_filename(filename),
        "original_filename": filename,
        "caption": caption,
    })
    try:
        with transaction.atomic():
            document.save()
    except IntegrityError:
        # The same file arrived twice at once; keep the first copy.
        document.file.delete(save=False)
        return SaveResult(member.documents.get(content_hash=content_hash), duplicate=True)

    return SaveResult(document, read=read_document(document, data))


# --- Reading with the AI ----------------------------------------------------


def read_document(document: Document, data: bytes) -> ReadResult:
    """Fill in the document's details using the AI."""
    current = document.get_details()
    try:
        found = extraction.extract(
            data, document.mime_type, filename=current.get("original_filename", ""), caption=current.get("caption", "")
        )
    except extraction.ExtractionError as exc:
        return _could_not_read(document, problem=str(exc), busy=False)
    except LLMError as exc:
        logger.warning("AI could not read document %s: %s", document.pk, exc)
        return _could_not_read(document, problem="the AI service was busy", busy=True)

    category = found["category"]
    if current.get("labelled_by_user") and category == Category.OTHER:
        category = document.category  # Keep what the owner told us.
    document.category = category
    document.is_sensitive = found["is_sensitive"] or category in SENSITIVE_CATEGORIES
    document.side = found["side"]
    document.expiry_date = found["expiry_date"]
    document.status = Document.Status.READY
    document.next_read_at = None
    document.set_details({
        **current,
        "title": found["title"] or current.get("title", ""),
        "holder_name": found["holder_name"],
        "issuer": found["issuer"],
        "issue_date": found["issue_date"],
        "summary": found["summary"],
        "keywords": found["keywords"],
        "fields": found["fields"],
        "is_document": found["is_document"],
        "recognised": found["recognised"],
        "read_problem": "",
    })
    document.save()
    partner = pair_card_side(document)
    return ReadResult(unclear=not found["recognised"] and not current.get("labelled_by_user"), partner=partner)


def _could_not_read(document: Document, problem: str, busy: bool) -> ReadResult:
    retrying = False
    if busy:
        document.read_attempts += 1
        if document.read_attempts <= len(REREAD_DELAYS_MINUTES):
            retrying = True
            wait = REREAD_DELAYS_MINUTES[document.read_attempts - 1]
            document.next_read_at = timezone.now() + timedelta(minutes=wait)
    if retrying:
        document.status = Document.Status.WAITING
    else:
        document.status = Document.Status.UNREADABLE
        document.next_read_at = None
    document.set_details({**document.get_details(), "read_problem": "busy" if busy else "unreadable"})
    document.save()
    return ReadResult(problem=problem, retrying=retrying)


def reread_due_documents(limit: int = 20, now: bool = False) -> list[tuple[Document, str]]:
    """Try the AI again on documents it skipped while busy.

    `now=True` ignores the waiting schedule. Returns (document, title before)
    for each one read now.
    """
    done = []
    due = Document.objects.filter(status=Document.Status.WAITING)
    if not now:
        due = due.filter(next_read_at__lte=timezone.now())
    due = due.select_related("member").order_by("next_read_at")[:limit]
    for document in due:
        title_before = document.title
        outcome = read_document(document, document.read_file())
        if outcome.retrying:
            break  # Still busy: try the rest later.
        if not outcome.problem:
            done.append((document, title_before))
    return done


def label_document(document: Document, category: str):
    """The owner says what a document is (when the AI couldn't tell).

    A label can add protection but never remove it, so nobody holding the
    phone can unprotect an ID card by relabelling it.
    """
    details = document.get_details()
    if category != Category.OTHER and GENERIC_TITLE.match(details.get("title", "") or "Saved on"):
        details["title"] = dict(LABEL_CHOICES)[category]
    details["labelled_by_user"] = True
    document.set_details(details)
    document.category = category
    document.is_sensitive = document.is_sensitive or category in SENSITIVE_CATEGORIES
    document.save()


# --- Two-sided cards --------------------------------------------------------


def _holder(document: Document) -> str:
    return " ".join((document.get_details().get("holder_name") or "").lower().split())


def pair_card_side(document: Document) -> Document | None:
    """Join a card's front and back photos, so they're always sent together."""
    opposite = {Document.Side.FRONT: Document.Side.BACK, Document.Side.BACK: Document.Side.FRONT}.get(document.side)
    if not opposite or document.card_group:
        return None
    candidates = list(
        document.member.documents.filter(category=document.category, side=opposite, card_group__isnull=True)
        .exclude(pk=document.pk)
        .order_by("-created_at")
    )
    name = _holder(document)
    if name:
        same_name = [c for c in candidates if _holder(c) == name]
        no_name = [c for c in candidates if not _holder(c)]  # Backs often have no name.
        partner = same_name[0] if same_name else (no_name[0] if len(no_name) == 1 else None)
    else:
        partner = candidates[0] if len(candidates) == 1 else None
    if partner is None:
        return None  # Not sure which card it belongs to: don't guess.

    group = uuid.uuid4()
    Document.objects.filter(pk__in=[document.pk, partner.pk]).update(card_group=group)
    document.card_group = partner.card_group = group
    return partner


def _side_order(document: Document) -> int:
    return {Document.Side.FRONT: 0, Document.Side.BACK: 1}.get(document.side, 2)


def with_card_sides(documents: list[Document], side: str = "") -> list[Document]:
    """Add the other side of every card, or keep just one side if asked."""
    groups = {d.card_group for d in documents if d.card_group}
    sides_by_group = {}
    if groups:
        for d in documents[0].member.documents.filter(card_group__in=groups):
            sides_by_group.setdefault(d.card_group, []).append(d)
    result = []
    for document in documents:
        sides = sorted(sides_by_group.get(document.card_group, [document]), key=_side_order)
        if side in (Document.Side.FRONT, Document.Side.BACK):
            sides = [s for s in sides if s.side == side] or [document]
        result.extend(sides)
    return list(dict.fromkeys(result))


def one_per_card(documents: list[Document]) -> list[Document]:
    """Treat a card's two sides as one item (for "which one?" lists)."""
    seen, result = set(), []
    for document in documents:
        key = document.card_group or document.pk
        if key not in seen:
            seen.add(key)
            result.append(document)
    return result


def card_groups(documents: list[Document]) -> list[list[Document]]:
    """Group documents so both sides of a card sit together, front first."""
    groups, index = [], {}
    for document in documents:
        if document.card_group and document.card_group in index:
            groups[index[document.card_group]].append(document)
            continue
        if document.card_group:
            index[document.card_group] = len(groups)
        groups.append([document])
    return [sorted(group, key=_side_order) for group in groups]


# --- Access -----------------------------------------------------------------


def owned_documents(member: Member, doc_ids) -> list[Document]:
    """The given documents, only if they belong to this member, in the given order."""
    ids = []
    for value in doc_ids:
        try:
            ids.append(str(uuid.UUID(str(value))))
        except ValueError:
            continue
    found = {str(d.pk): d for d in member.documents.filter(pk__in=ids)}
    return [found[i] for i in dict.fromkeys(ids) if i in found]


def needs_pin(documents: list[Document], field_key: str = "") -> bool:
    """True if any document, or the detail asked for, is an ID/bank item.

    Such items are never sent inside WhatsApp, and on the website they need a
    recent PIN.
    """
    if any(d.is_sensitive for d in documents):
        return True
    if field_key and documents:
        found = documents[0].get_field(field_key)
        return bool(found and documents[0].is_field_protected(found))
    return False


# --- Sending ----------------------------------------------------------------


def _file_data(document: Document) -> conversions.FileData:
    return conversions.FileData(document.read_file(), document.mime_type, document.download_name())


def prepare_files(member: Member, documents: list[Document], options: conversions.SendOptions) -> conversions.Prepared:
    """Decrypt documents and apply any conversion the member asked for.

    Both sides of a card become one page (front above back) before anything else.
    """
    files = []
    for group in card_groups(documents):
        if len(group) == 2:
            stem = group[0].download_name().rsplit(".", 1)[0]
            sheet = conversions.card_sheet(_file_data(group[0]), _file_data(group[1]))
            files.append(conversions.FileData(sheet, conversions.PDF, f"{stem}_both_sides.pdf"))
        else:
            files.extend(_file_data(d) for d in group)
    prepared = conversions.prepare(files, options)
    if prepared.operations:
        Conversion.objects.create(
            member=member,
            operations=prepared.operations,
            input_bytes=sum(d.size_bytes for d in documents),
            output_bytes=sum(len(f.data) for f in prepared.files),
            met_target=prepared.met_target,
        )
    return prepared


# --- Deleting ---------------------------------------------------------------


def delete_document(document: Document):
    if document.card_group:
        # The other side becomes a single card again, free to pair with a new photo.
        Document.objects.filter(card_group=document.card_group).exclude(pk=document.pk).update(card_group=None)
    document.delete()


def delete_member_data(member: Member):
    """Erase a member completely: documents, stored files, keys and message log."""
    from bot.models import InboundMessage

    with transaction.atomic():
        InboundMessage.objects.filter(wa_id=member.wa_id).delete()
        member.delete()
