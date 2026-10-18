import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core import crypto
from core.models import TimeStampedModel

# What each plan allows. None means unlimited.
PLAN_LIMITS = {
    "free": {"documents": 25, "conversions_per_month": 5},
    "plus": {"documents": None, "conversions_per_month": None},
    "family": {"documents": None, "conversions_per_month": None},
}

PIN_MAX_ATTEMPTS = 5
PIN_LOCK_MINUTES = 30
# After a correct PIN, sensitive documents stay unlocked this long.
UNLOCK_MINUTES = 5
# PIN links expire after this long and work only once.
LINK_MINUTES = 10
# A forgotten PIN can be replaced only after this wait, so someone who briefly
# has the owner's phone can't take over: the owner sees it and can cancel.
PIN_RESET_WAIT_HOURS = 24


class User(AbstractUser):
    """Staff account for the Django admin. WhatsApp users are `Member`s."""


class Member(TimeStampedModel):
    """A person using Kagaz through WhatsApp, identified by their WhatsApp id."""

    class Plan(models.TextChoices):
        FREE = "free", "Free"
        PLUS = "plus", "Plus"
        FAMILY = "family", "Family"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    wa_id = models.CharField("WhatsApp id", max_length=32, unique=True)
    display_name = models.CharField(max_length=120, blank=True)
    plan = models.CharField(max_length=16, choices=Plan.choices, default=Plan.FREE)
    is_blocked = models.BooleanField(default=False)
    consent_given_at = models.DateTimeField(null=True, blank=True)
    last_active_at = models.DateTimeField(null=True, blank=True)

    # This member's data key, encrypted with the master key. Deleting it
    # makes every stored blob of theirs unreadable.
    wrapped_key = models.BinaryField(editable=False)

    pin_hash = models.CharField(max_length=128, blank=True, editable=False)
    pin_failed_attempts = models.PositiveSmallIntegerField(default=0, editable=False)
    pin_locked_until = models.DateTimeField(null=True, blank=True, editable=False)
    unlocked_until = models.DateTimeField(null=True, blank=True, editable=False)
    # Set while a forgotten-PIN reset is waiting: the time a new PIN may be set.
    pin_reset_ready_at = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self):
        return f"…{self.wa_id[-4:]}"

    def save(self, *args, **kwargs):
        if not self.wrapped_key:
            self.wrapped_key = crypto.wrap_key(crypto.new_data_key(), aad=self.id.bytes)
        super().save(*args, **kwargs)

    def data_key(self) -> bytes:
        if not hasattr(self, "_data_key"):
            self._data_key = crypto.unwrap_key(self.wrapped_key, aad=self.id.bytes)
        return self._data_key

    # --- Consent ---

    @property
    def has_consented(self) -> bool:
        return self.consent_given_at is not None

    def give_consent(self):
        self.consent_given_at = timezone.now()
        self.save(update_fields=["consent_given_at", "updated_at"])

    # --- Plan limits ---

    def limits(self) -> dict:
        return PLAN_LIMITS[self.plan]

    def can_add_document(self) -> bool:
        limit = self.limits()["documents"]
        return limit is None or self.documents.count() < limit

    def can_convert(self) -> bool:
        limit = self.limits()["conversions_per_month"]
        if limit is None:
            return True
        month_start = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return self.conversions.filter(created_at__gte=month_start).count() < limit

    # --- PIN ---

    @property
    def has_pin(self) -> bool:
        return bool(self.pin_hash)

    def set_pin(self, raw_pin: str):
        self.pin_hash = make_password(raw_pin)
        self.pin_failed_attempts = 0
        self.pin_locked_until = None
        self.pin_reset_ready_at = None
        self.save(update_fields=["pin_hash", "pin_failed_attempts", "pin_locked_until", "pin_reset_ready_at", "updated_at"])

    def is_pin_locked(self) -> bool:
        return bool(self.pin_locked_until and self.pin_locked_until > timezone.now())

    def check_pin(self, raw_pin: str) -> bool:
        """Check a PIN, locking the member out after too many wrong tries."""
        if not self.has_pin or self.is_pin_locked():
            return False
        if check_password(raw_pin, self.pin_hash):
            self.pin_failed_attempts = 0
            self.save(update_fields=["pin_failed_attempts", "updated_at"])
            return True
        self.pin_failed_attempts += 1
        if self.pin_failed_attempts >= PIN_MAX_ATTEMPTS:
            self.pin_failed_attempts = 0
            self.pin_locked_until = timezone.now() + timedelta(minutes=PIN_LOCK_MINUTES)
        self.save(update_fields=["pin_failed_attempts", "pin_locked_until", "updated_at"])
        return False

    def unlock(self):
        self.unlocked_until = timezone.now() + timedelta(minutes=UNLOCK_MINUTES)
        self.save(update_fields=["unlocked_until", "updated_at"])

    def is_unlocked(self) -> bool:
        return bool(self.unlocked_until and self.unlocked_until > timezone.now())

    # --- Forgotten PIN ---

    @property
    def pin_reset_pending(self) -> bool:
        return self.pin_reset_ready_at is not None

    def pin_reset_ready(self) -> bool:
        return bool(self.pin_reset_ready_at and self.pin_reset_ready_at <= timezone.now())

    def start_pin_reset(self):
        self.pin_reset_ready_at = timezone.now() + timedelta(hours=PIN_RESET_WAIT_HOURS)
        self.save(update_fields=["pin_reset_ready_at", "updated_at"])

    def cancel_pin_reset(self) -> bool:
        """Stop a waiting reset. Returns True if there was one."""
        if not self.pin_reset_pending:
            return False
        self.pin_reset_ready_at = None
        self.save(update_fields=["pin_reset_ready_at", "updated_at"])
        return True


class AccessLinkQuerySet(models.QuerySet):
    def usable(self):
        return self.filter(used_at__isnull=True, expires_at__gt=timezone.now())

    def find_usable(self, raw_token: str):
        return self.usable().select_related("member").filter(token_hash=AccessLink.hash_token(raw_token)).first()


class AccessLink(models.Model):
    """Single-use, short-lived web link for entering a PIN.

    PINs are never typed into the WhatsApp chat, because anyone holding the
    phone could scroll back and read them.
    """

    class Purpose(models.TextChoices):
        SET_PIN = "set_pin", "Set PIN"
        UNLOCK = "unlock", "Unlock"  # Open a protected document or detail, or confirm a deletion.
        LOGIN = "login", "Open the web vault"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="access_links")
    purpose = models.CharField(max_length=16, choices=Purpose.choices)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    # What to do once the PIN is accepted, e.g. {"op": "send", "doc_ids": [...]}.
    action = models.JSONField(null=True, blank=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AccessLinkQuerySet.as_manager()

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode()).hexdigest()

    @classmethod
    def issue(cls, member: Member, purpose: str, action: dict | None = None) -> str:
        """Create a link and return its full URL. Only the token's hash is stored."""
        raw_token = secrets.token_urlsafe(32)
        cls.objects.create(
            member=member,
            purpose=purpose,
            token_hash=cls.hash_token(raw_token),
            action=action,
            expires_at=timezone.now() + timedelta(minutes=LINK_MINUTES),
        )
        return settings.PUBLIC_BASE_URL + reverse("accounts:access_link", args=[raw_token])

    def mark_used(self):
        self.used_at = timezone.now()
        self.save(update_fields=["used_at"])
