"""
What people can buy (Plus, conversion packs), promo codes, the referral
program and payments. Everything here is edited in the admin.

Amounts are whole rupees. Razorpay works in paise, so billing/razorpay.py
multiplies by 100.
"""

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from accounts.models import Member


class Product(models.Model):
    code = models.SlugField(max_length=32, unique=True, help_text="Used inside buttons. Don't change it once live.")
    name = models.CharField(max_length=60, help_text='Shown on the payment page, e.g. "Kagaz Plus · 1 year".')
    button_label = models.CharField(max_length=14, help_text='WhatsApp button, shown with the price: "Yearly" → "Yearly ₹399".')
    price = models.PositiveIntegerField("price (₹)")
    plus_days = models.PositiveIntegerField(default=0, help_text="Days of Plus it adds (0 for a pack).")
    conversions = models.PositiveIntegerField(default=0, help_text="Conversion credits it adds (0 for Plus).")
    is_active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0, help_text="Button order. WhatsApp shows at most 3.")

    class Meta:
        ordering = ["order", "price"]

    def __str__(self):
        return self.name


class PromoCode(models.Model):
    class Kind(models.TextChoices):
        PERCENT = "percent", "% off"
        AMOUNT = "amount", "₹ off"

    code = models.CharField(max_length=32, unique=True, help_text="What people type after CODE, e.g. SAVE20.")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.PERCENT)
    value = models.PositiveIntegerField(help_text="The percent (e.g. 20) or the rupees off (e.g. 50).")
    max_discount = models.PositiveIntegerField(
        "max discount (₹)", null=True, blank=True, help_text="For % codes: the most it takes off. Empty means no cap."
    )
    min_amount = models.PositiveIntegerField(
        "minimum price (₹)", null=True, blank=True, help_text="Works only on products costing at least this much."
    )
    products = models.ManyToManyField(Product, blank=True, help_text="Empty means every product.")
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True, help_text="Empty means no end date.")
    max_uses = models.PositiveIntegerField(
        null=True, blank=True, help_text="E.g. 10 for the first 10 people. Empty means unlimited."
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.code

    def clean(self):
        if self.kind == self.Kind.PERCENT and self.value > 100:
            raise ValidationError({"value": "A percent can't be more than 100."})

    def save(self, *args, **kwargs):
        self.code = "".join(self.code.split()).upper()
        super().save(*args, **kwargs)

    def uses(self) -> int:
        """Paid uses plus payment links still open, so "first 10" can't be oversold."""
        return self.payments.filter(
            Q(status=Payment.Status.PAID) | Q(status=Payment.Status.CREATED, expires_at__gt=timezone.now())
        ).count()

    def discount_on(self, price: int) -> int:
        if self.kind == self.Kind.PERCENT:
            off = price * self.value // 100
            if self.max_discount is not None:
                off = min(off, self.max_discount)
        else:
            off = self.value
        return min(off, price)


class ReferralProgram(models.Model):
    """Settings for inviting friends. There is one row; edit it in the admin."""

    is_active = models.BooleanField(default=True)
    friend_discount_percent = models.PositiveSmallIntegerField(
        default=20, help_text="Taken off the invited friend's first purchase."
    )
    referrer_reward_days = models.PositiveSmallIntegerField(
        default=30, help_text="Days of Plus for the person who invited, after the friend's first paid purchase."
    )
    max_rewards_per_member = models.PositiveSmallIntegerField(
        null=True, blank=True, default=12, help_text="How many friends' purchases one person can be rewarded for. Empty means unlimited."
    )

    class Meta:
        verbose_name = "referral program"
        verbose_name_plural = "referral program"

    def __str__(self):
        return "Referral program"

    @classmethod
    def get(cls) -> "ReferralProgram":
        program, _ = cls.objects.get_or_create(pk=1)
        return program


class Payment(models.Model):
    class Status(models.TextChoices):
        CREATED = "created", "Waiting for payment"
        PAID = "paid", "Paid"
        EXPIRED = "expired", "Expired or cancelled"
        REFUNDED = "refunded", "Refunded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Kept when a member erases their data: it's an accounting record with no documents in it.
    member = models.ForeignKey(Member, null=True, on_delete=models.SET_NULL, related_name="payments")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="payments")
    promo = models.ForeignKey(PromoCode, null=True, blank=True, on_delete=models.SET_NULL, related_name="payments")
    referral_discount = models.BooleanField(default=False, help_text="The invited friend's discount was used.")
    list_price = models.PositiveIntegerField("list price (₹)")
    discount = models.PositiveIntegerField("discount (₹)", default=0)
    amount = models.PositiveIntegerField("amount (₹)")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATED)
    referrer_rewarded = models.BooleanField(default=False)
    razorpay_order_id = models.CharField(max_length=40, blank=True, db_index=True)
    razorpay_payment_id = models.CharField(max_length=40, blank=True)
    # The pay page (/billing/pay/<id>/) stops offering to pay after this.
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.product} · ₹{self.amount} ({self.get_status_display()})"
