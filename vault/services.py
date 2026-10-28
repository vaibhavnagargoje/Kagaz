"""Saving, reading, sending and deleting documents."""

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from accounts.models import Member
from core import crypto
from core.llm import LLMError
from vault import conversions, extraction, wordfile
from vault.models import LABEL_CHOICES, PROTECTED_FIELD_WORDS, SENSITIVE_CATEGORIES, Category, Conversion, Document

logger = logging.getLogger(__name__)

# When the AI is busy, read the document again after these waits (minutes).
REREAD_DELAYS_MINUTES = [5, 15, 60, 180, 720]

ACCEPTED_FILES = "I can save PDFs, photos (JPG, PNG, WEBP or iPhone HEIC) and Word .docx files."
OFFICE_FILE = "I can't save Excel or PowerPoint files. Please save it as PDF and send it again."
OTHER_WORD_FILE = (
    "I can only read Word files saved as .docx. Open this one in Word or Google Docs, "
    "save it as .docx or PDF (without a password), and send it again."
)
OFFICE_EXTENSIONS = {"xls", "xlsx", "xlsm", "csv", "ods", "ppt", "pptx", "pps", "ppsx", "odp"}
WORD_EXTENSIONS = {"doc", "dot", "rtf", "odt", "wps"}
OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # Old Office files, and password-protected new ones.

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
    sure: bool = True  # False when the partner was the only candidate, not a proven match.
    choices: list[Document] = field(default_factory=list)  # Possible other sides to ask about.

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


class RefusedFile(Exception):
    """A file Kagaz won't save. The message is safe to show the user."""


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _refusal(data: bytes, claimed: str, filename: str) -> str:
    """Why a file of an unsupported type can't be saved, and what to send instead."""
    extension = _extension(filename)
    if extension in OFFICE_EXTENSIONS or any(word in claimed for word in ("spreadsheet", "excel", "presentation", "powerpoint")):
        return OFFICE_FILE
    if extension in WORD_EXTENSIONS or data[:8] == OLE_HEADER or claimed == "application/msword":
        return OTHER_WORD_FILE
    return f"{ACCEPTED_FILES} Please send it as one of these."


def _checked(data: bytes, mime: str) -> tuple[bytes, str, int | None]:
    """Refuse a file too big or unsafe to handle, and turn iPhone photos into JPEG.

    Returns what to store, its type and its page count. Raises ConversionError.
    """
    if mime == conversions.HEIC:
        with conversions.HEAVY_WORK:
            return conversions.heic_to_jpeg(data), conversions.JPEG, None
    if conversions.is_image(mime):
        conversions.check_image(data)
    elif conversions.is_word(mime):
        wordfile.check(data)
    elif conversions.is_pdf(mime):
        pages = conversions.pdf_page_count(data)
        if pages and pages > conversions.MAX_PDF_PAGES:
            raise conversions.ConversionError(
                f"This PDF has {pages} pages. I can save PDFs of up to {conversions.MAX_PDF_PAGES} pages, "
                "so please split it into smaller parts."
            )
        return data, mime, pages
    return data, mime, None


def save_document(member: Member, data: bytes, mime: str, filename: str = "", caption: str = "") -> SaveResult:
    """Encrypt and store a file, then let the AI read it.

    `mime` is only what the sender claims; the bytes decide. Raises RefusedFile
    for files Kagaz won't keep.
    """
    claimed = mime.split(";")[0].strip().lower()
    mime = conversions.detect_mime(data, fallback="")
    if mime not in conversions.SAVEABLE_TYPES:
        logger.info("Refused a file: claimed type %r, extension %r", claimed, _extension(filename))
        raise RefusedFile(_refusal(data, claimed, filename))

    content_hash = crypto.keyed_hash(member.data_key(), data)
    existing = member.documents.filter(content_hash=content_hash).first()
    if existing:
        return SaveResult(existing, duplicate=True)
    try:
        data, mime, page_count = _checked(data, mime)
    except conversions.ConversionError as exc:
        raise RefusedFile(str(exc)) from exc

    document = Document(
        member=member, mime_type=mime, size_bytes=len(data), page_count=page_count, content_hash=content_hash
    )
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
    except Exception:  # Never leave a document stuck as "processing".
        logger.exception("Reading document %s failed", document.pk)
        return _could_not_read(document, problem="something went wrong while reading it", busy=False)

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
    pairing = pair_card_side(document)
    return ReadResult(
        unclear=not found["recognised"] and not current.get("labelled_by_user"),
        partner=pairing.partner, sure=pairing.sure, choices=pairing.choices,
    )


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


def reread_due_documents(
    limit: int = 20, now: bool = False, include_failed: bool = False
) -> list[tuple[Document, str, ReadResult]]:
    """Try the AI again on documents it skipped while busy.

    `now=True` ignores the waiting schedule. `include_failed=True` also retries
    documents that gave up because the AI stayed unavailable (e.g. no credits).
    Returns (document, title before, result) for each one read now.
    """
    done = []
    due = Document.objects.filter(status=Document.Status.WAITING)
    if not now:
        due = due.filter(next_read_at__lte=timezone.now())
    due = list(due.select_related("member").order_by("next_read_at")[:limit])
    if include_failed:
        gave_up = Document.objects.filter(status=Document.Status.UNREADABLE).select_related("member")
        for document in gave_up:
            if len(due) >= limit:
                break
            if document.get_details().get("read_problem") == "busy":
                document.read_attempts = 0  # A fresh set of retries if the AI is still busy.
                due.append(document)
    for document in due:
        title_before = document.title
        outcome = read_document(document, document.read_file())
        if outcome.retrying:
            break  # Still busy: try the rest later.
        if not outcome.problem:
            done.append((document, title_before, outcome))
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


# At most this many cards are offered in a "which card is this?" list (WhatsApp shows 10 rows).
MAX_PAIR_CHOICES = 9
OPPOSITE_SIDE = {Document.Side.FRONT: Document.Side.BACK, Document.Side.BACK: Document.Side.FRONT}
MASKED = re.compile(r"(x|\*){4,}")


@dataclass
class Pairing:
    partner: Document | None = None
    sure: bool = True
    choices: list[Document] = field(default_factory=list)


def _holder(document: Document) -> str:
    return " ".join((document.get_details().get("holder_name") or "").lower().split())


def _id_numbers(document: Document) -> list[str]:
    """ID numbers printed on the card, letters and digits only ("1234 5678 9012" -> "123456789012")."""
    numbers = []
    for found in document.get_details().get("fields", []):
        key_words = set(found.get("key", "").split("_"))
        if found.get("sensitive") or PROTECTED_FIELD_WORDS & key_words:
            value = re.sub(r"[^0-9a-z*]", "", str(found.get("value", "")).lower())
            if sum(c.isdigit() for c in value) >= 4:
                numbers.append(value)
    return numbers


def _same_number(a: str, b: str) -> bool:
    if MASKED.search(a) or MASKED.search(b):
        # Masked, like XXXX XXXX 9012 on an Aadhaar back: compare the last 4 digits.
        tail_a, tail_b = re.sub(r"\D", "", a)[-4:], re.sub(r"\D", "", b)[-4:]
        return len(tail_a) == 4 and tail_a == tail_b
    return a == b


def _numbers_match(mine: list[str], theirs: list[str]) -> bool:
    return any(_same_number(a, b) for a in mine for b in theirs)


def _pair_candidates(document: Document) -> list[Document]:
    """Unpaired opposite sides of the same kind of card, newest first."""
    return list(
        document.member.documents.filter(
            category=document.category, side=OPPOSITE_SIDE[document.side], card_group__isnull=True
        ).exclude(pk=document.pk).order_by("-created_at")
    )


def _match_candidates(document: Document, candidates: list[Document]) -> tuple[Document | None, list[Document]]:
    """(the side that surely belongs with it, or None; the sides it could still belong with)."""
    numbers = _id_numbers(document)
    if numbers:
        same = [c for c in candidates if _numbers_match(numbers, _id_numbers(c))]
        if len(same) == 1:
            return same[0], []
        # A side showing a different ID number is a different card.
        candidates = [c for c in candidates if not _id_numbers(c) or c in same]
    name = _holder(document)
    if name:
        same_name = [c for c in candidates if _holder(c) == name]
        if len(same_name) == 1:
            return same_name[0], []
        candidates = same_name or [c for c in candidates if not _holder(c)]  # Backs often have no name.
    return None, candidates


def _join(document: Document, partner: Document, sure: bool) -> Pairing:
    group = uuid.uuid4()
    Document.objects.filter(pk__in=[document.pk, partner.pk]).update(card_group=group)
    document.card_group = partner.card_group = group
    return Pairing(partner=partner, sure=sure)


def pair_card_side(document: Document) -> Pairing:
    """Join a card's front and back photos, so they're always sent together.

    Proven by the same ID number or name: joined. The only possible match:
    joined, but not sure (the owner can undo it). Several possible: not
    joined; the owner is asked which card it belongs to.
    """
    if document.side not in OPPOSITE_SIDE or document.card_group:
        return Pairing()
    certain, possible = _match_candidates(document, _pair_candidates(document))
    if certain:
        return _join(document, certain, sure=True)
    if len(possible) == 1:
        return _join(document, possible[0], sure=False)
    return Pairing(choices=possible[:MAX_PAIR_CHOICES])


def pair_choices(document: Document, exclude=()) -> list[Document]:
    """The sides this one could belong with, for asking the owner (never joins by itself)."""
    if document.side not in OPPOSITE_SIDE or document.card_group:
        return []
    excluded = set(exclude)
    candidates = [c for c in _pair_candidates(document) if c.pk not in excluded]
    certain, possible = _match_candidates(document, candidates)
    return ([certain] if certain else possible)[:MAX_PAIR_CHOICES]


def pair_documents(member: Member, doc_id, partner_id) -> Document | None:
    """The owner says these two are one card's sides. Returns the partner, or None if they can't be."""
    found = owned_documents(member, [doc_id, partner_id])
    if len(found) != 2:
        return None
    document, partner = found
    if (document.category != partner.category or OPPOSITE_SIDE.get(document.side) != partner.side
            or document.card_group or partner.card_group):
        return None
    _join(document, partner, sure=True)
    return partner


def unpair(member: Member, doc_id) -> tuple[Document, Document | None] | None:
    """The owner says two joined sides aren't one card. Returns (document, its former partner)."""
    found = owned_documents(member, [doc_id])
    if not found or not found[0].card_group:
        return None
    document = found[0]
    partner = member.documents.filter(card_group=document.card_group).exclude(pk=document.pk).first()
    Document.objects.filter(card_group=document.card_group).update(card_group=None)
    document.card_group = None
    return document, partner


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
    with conversions.HEAVY_WORK:
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
        if member.plan_conversions_left() == 0:
            # This month's plan conversions are used up, so this one uses a pack credit.
            if Member.objects.filter(pk=member.pk, conversion_credits__gt=0).update(
                conversion_credits=F("conversion_credits") - 1
            ):
                member.conversion_credits -= 1
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
