import json
import mimetypes
import uuid

from django.core.files.base import ContentFile
from django.db import models
from django.utils.text import get_valid_filename

from accounts.models import Member
from core import crypto
from core.models import TimeStampedModel


class Category(models.TextChoices):
    AADHAAR = "aadhaar", "Aadhaar"
    PAN = "pan", "PAN card"
    PASSPORT = "passport", "Passport"
    DRIVING_LICENCE = "driving_licence", "Driving licence"
    VOTER_ID = "voter_id", "Voter ID"
    RATION_CARD = "ration_card", "Ration card"
    ID_CARD = "id_card", "Other ID card"
    MARKSHEET = "marksheet", "Marksheet"
    CERTIFICATE = "certificate", "Certificate"
    DEGREE = "degree", "Degree"
    OFFER_LETTER = "offer_letter", "Offer / appointment letter"
    EXPERIENCE_LETTER = "experience_letter", "Experience / relieving letter"
    SALARY_SLIP = "salary_slip", "Salary slip"
    BANK = "bank", "Bank document"
    TAX = "tax", "Tax paper"
    INSURANCE = "insurance", "Insurance"
    VEHICLE = "vehicle", "Vehicle"
    PROPERTY = "property", "Property / house papers"
    LAND_RECORD = "land_record", "Land record"
    AGREEMENT = "agreement", "Agreement / legal paper"
    BUSINESS = "business", "Business registration"
    MEDICAL = "medical", "Medical"
    UTILITY_BILL = "utility_bill", "Light / water / gas bill"
    BILL = "bill", "Bill / receipt"
    PHOTO = "photo", "Photo"
    SIGNATURE = "signature", "Signature"
    OTHER = "other", "Other"


# ID and bank documents. They are never sent inside WhatsApp (WhatsApp's policy
# forbids sharing ID numbers, and it's safer): the bot sends a secure link and
# the document opens on our website after the PIN.
SENSITIVE_CATEGORIES = {
    Category.AADHAAR,
    Category.PAN,
    Category.PASSPORT,
    Category.DRIVING_LICENCE,
    Category.VOTER_ID,
    Category.RATION_CARD,
    Category.ID_CARD,
    Category.BANK,
    Category.SALARY_SLIP,
    Category.TAX,
}

# What people can pick when the AI can't tell what a document is (WhatsApp
# lists allow 10 rows). Every row that needs protection gets it ("Other ID
# card" covers the rest of the ID cards), so a label can protect. When the AI
# reads it later, the exact type replaces the label.
LABEL_CHOICES = [
    (Category.AADHAAR, "Aadhaar card"),
    (Category.PAN, "PAN card"),
    (Category.ID_CARD, "Other ID card"),
    (Category.BANK, "Bank or tax paper"),
    (Category.CERTIFICATE, "Marksheet or certificate"),
    (Category.PROPERTY, "Property or land paper"),
    (Category.AGREEMENT, "Agreement"),
    (Category.UTILITY_BILL, "Light / water bill"),
    (Category.PHOTO, "Photo or signature"),
    (Category.OTHER, "Something else"),
]
# Shown under a label in WhatsApp's list, where a label alone could be unclear.
LABEL_HINTS = {
    Category.ID_CARD: "Driving licence, passport, voter ID, ration card",
    Category.BANK: "Passbook, statement, salary slip, ITR, Form 16",
    Category.PROPERTY: "Sale deed, 7/12, property tax receipt",
    Category.AGREEMENT: "Rent agreement, affidavit, NOC",
    Category.UTILITY_BILL: "Electricity, water, gas or phone bill",
}

# A detail whose key contains one of these words is an ID or bank number, so
# it is protected even when it's printed on an ordinary document. (A GSTIN
# has the owner's PAN inside it.)
PROTECTED_FIELD_WORDS = {
    "aadhaar", "aadhar", "uid", "uidai", "pan", "passport", "licence", "license", "dl",
    "voter", "epic", "ration", "account", "acct", "card", "upi", "iban", "gst", "gstin",
}


def blob_path(document, filename):
    return f"vault/{document.member_id}/{document.id}.bin"


class Document(TimeStampedModel):
    """One stored file.

    The file itself and everything that identifies a person (title, holder,
    numbers, summary) are encrypted with the member's data key. Only
    non-identifying columns stay in plain text.
    """

    class Status(models.TextChoices):
        PROCESSING = "processing", "Processing"
        READY = "ready", "Ready"
        WAITING = "waiting_for_ai", "Waiting for the AI to read it"
        UNREADABLE = "unreadable", "Stored, AI could not read it"

    class Side(models.TextChoices):
        NONE = "", "Not a card"
        FRONT = "front", "Front"
        BACK = "back", "Back"
        BOTH = "both", "Both sides"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="documents")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PROCESSING)
    category = models.CharField(max_length=32, choices=Category.choices, default=Category.OTHER)
    is_sensitive = models.BooleanField(default=False)
    mime_type = models.CharField(max_length=100)
    size_bytes = models.PositiveIntegerField()
    page_count = models.PositiveSmallIntegerField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    content_hash = models.CharField(max_length=64, db_index=True, editable=False)

    # Which side of a two-sided card this file shows. The front and back of the
    # same card share a card_group, so they are always sent together.
    side = models.CharField(max_length=8, choices=Side.choices, default=Side.NONE, blank=True)
    card_group = models.UUIDField(null=True, blank=True, db_index=True, editable=False)

    # When the AI was busy, reading is retried later (see services.reread_due_documents).
    read_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    next_read_at = models.DateTimeField(null=True, blank=True, editable=False)

    file = models.FileField(upload_to=blob_path, editable=False)
    details_ciphertext = models.BinaryField(null=True, editable=False)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["member", "content_hash"], name="unique_document_per_member"),
        ]

    def __str__(self):
        return f"{self.get_category_display()} ({self.id})"

    # --- Encrypted file ---

    def store_file(self, data: bytes):
        blob = crypto.encrypt(self.member.data_key(), data, aad=self.id.bytes)
        self.file.save(f"{self.id}.bin", ContentFile(blob), save=False)

    def read_file(self) -> bytes:
        with self.file.open("rb") as fh:
            blob = fh.read()
        return crypto.decrypt(self.member.data_key(), blob, aad=self.id.bytes)

    # --- Encrypted details ---
    # Shape: {title, holder_name, issuer, issue_date, summary, keywords: [],
    #         fields: [{key, label, value, sensitive}], original_filename, caption,
    #         is_document, recognised, read_problem, labelled_by_user}

    def get_details(self) -> dict:
        if not hasattr(self, "_details"):
            if self.details_ciphertext:
                raw = crypto.decrypt(self.member.data_key(), self.details_ciphertext, aad=self.id.bytes + b"details")
                self._details = json.loads(raw)
            else:
                self._details = {}
        return self._details

    def set_details(self, details: dict):
        raw = json.dumps(details, ensure_ascii=False).encode()
        self.details_ciphertext = crypto.encrypt(self.member.data_key(), raw, aad=self.id.bytes + b"details")
        self._details = details

    @property
    def title(self) -> str:
        details = self.get_details()
        return details.get("title") or details.get("original_filename") or self.get_category_display()

    @property
    def list_label(self) -> str:
        """Title plus the side, for lists: "Aadhaar Card · front"."""
        return f"{self.title} · {self.side}" if self.side in (self.Side.FRONT, self.Side.BACK) else self.title

    @property
    def needs_label(self) -> bool:
        """The AI couldn't tell what this is, and the owner hasn't told us either."""
        details = self.get_details()
        if details.get("labelled_by_user"):
            return False
        return self.status != self.Status.READY or details.get("recognised") is False

    def get_field(self, key: str) -> dict | None:
        return next((f for f in self.get_details().get("fields", []) if f.get("key") == key), None)

    def is_field_protected(self, field: dict) -> bool:
        """True for anything on an ID/bank document, and for ID or bank numbers anywhere."""
        if self.is_sensitive or field.get("sensitive"):
            return True
        return bool(PROTECTED_FIELD_WORDS & set(field.get("key", "").split("_")))

    def download_name(self) -> str:
        extension = mimetypes.guess_extension(self.mime_type) or ""
        if extension == ".jpe":
            extension = ".jpg"
        stem = get_valid_filename(self.title.replace(" ", "_"))[:60] or "document"
        return f"{stem}{extension}"


class Conversion(models.Model):
    """One file conversion we did for a member (used for plan limits and stats)."""

    class Operation(models.TextChoices):
        COMPRESS = "compress", "Compress to size"
        TO_PDF = "to_pdf", "Convert to PDF"
        TO_JPG = "to_jpg", "Convert to JPG"
        RESIZE = "resize", "Resize image"
        MERGE = "merge", "Merge into one PDF"

    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="conversions")
    operations = models.JSONField(default=list)
    input_bytes = models.PositiveIntegerField()
    output_bytes = models.PositiveIntegerField()
    met_target = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
