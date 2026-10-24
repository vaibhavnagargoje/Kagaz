"""
Buying Plus and conversion packs.

    PLUS → buttons → start_payment() → Razorpay link on WhatsApp → person pays
    → Razorpay webhook → handle_event() → mark_paid() → Plus or credits + message

Prices come from the admin (billing.models). A member's price is the better of
their promo code (typed with CODE) and their invited-friend discount; the two
don't add up.
"""

import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import Member
from billing import razorpay
from billing.models import Payment, Product, PromoCode, ReferralProgram

logger = logging.getLogger(__name__)

# A payment link works this long.
LINK_HOURS = 1
# A code typed with CODE is kept this long for the next purchase.
PROMO_KEEP_HOURS = 24
# In the last days of a paid plan, the first chat of the day gets a renewal reminder.
RENEWAL_NOTICE_DAYS = 7

# Invite codes skip 0/O and 1/I, which are easy to mix up.
REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_LENGTH = 6
# "REF K7M2QX", "ref: k7m2qx", "Hi! referral code K7M2QX"
REFERRAL_IN_TEXT = re.compile(r"\bref(?:erral)?(?:\s*code)?\s*[:#-]?\s*([a-z0-9]{6})\b", re.IGNORECASE)


class PromoError(Exception):
    """Why a promo code can't be used, safe to show the user."""


@dataclass
class Quote:
    product: Product
    discount: int = 0
    promo: PromoCode | None = None
    referral: bool = False

    @property
    def amount(self) -> int:
        return self.product.price - self.discount


def has_paid(member) -> bool:
    return member.payments.filter(status=Payment.Status.PAID, amount__gt=0).exists()


def products_for(member) -> list[Product]:
    """What to offer (WhatsApp shows at most 3 buttons). Plus members aren't offered packs."""
    products = Product.objects.filter(is_active=True)
    if member.current_plan() != Member.Plan.FREE:
        products = products.filter(plus_days__gt=0)
    return list(products[:3])


# --- Promo codes ------------------------------------------------------------


def _check_promo(promo: PromoCode, member):
    now = timezone.now()
    if not promo.is_active or promo.valid_from > now or (promo.valid_until and promo.valid_until <= now):
        raise PromoError(f"The code {promo.code} isn't active right now.")
    if Payment.objects.filter(promo=promo, member=member, status=Payment.Status.PAID).exists():
        raise PromoError(f"You've already used the code {promo.code}.")
    if promo.max_uses is not None and promo.uses() >= promo.max_uses:
        raise PromoError(f"Sorry, the code {promo.code} has been used up.")


def _promo_fits(promo: PromoCode, product: Product) -> bool:
    if promo.min_amount is not None and product.price < promo.min_amount:
        return False
    return not promo.products.exists() or promo.products.filter(pk=product.pk).exists()


def apply_code(member, code: str) -> PromoCode:
    """Keep a promo code for the member's next purchase. Raises PromoError."""
    code = "".join(code.split()).upper()
    promo = PromoCode.objects.filter(code=code).first()
    if promo is None:
        raise PromoError(f"I couldn't find the code {code}. Please check the spelling.")
    _check_promo(promo, member)
    now = timezone.now()
    Member.objects.filter(pk=member.pk).update(pending_promo_code=promo.code, pending_promo_set_at=now)
    member.pending_promo_code, member.pending_promo_set_at = promo.code, now
    return promo


def pending_promo(member) -> PromoCode | None:
    if not member.pending_promo_code or not member.pending_promo_set_at:
        return None
    if member.pending_promo_set_at < timezone.now() - timedelta(hours=PROMO_KEEP_HOURS):
        return None
    return PromoCode.objects.filter(code=member.pending_promo_code).first()


def friend_discount_percent(member) -> int:
    """The invited friend's discount: their first purchase only."""
    program = ReferralProgram.get()
    if not program.is_active or not member.referred_by_id or has_paid(member):
        return 0
    return program.friend_discount_percent


def quote(member, product: Product) -> Quote:
    best = Quote(product)
    promo = pending_promo(member)
    if promo and _promo_fits(promo, product):
        try:
            _check_promo(promo, member)
        except PromoError:
            pass
        else:
            best = Quote(product, promo.discount_on(product.price), promo=promo)
    percent = friend_discount_percent(member)
    off = min(product.price, product.price * percent // 100)
    if off > best.discount:
        best = Quote(product, off, referral=True)
    return best


# --- Referrals --------------------------------------------------------------


def referral_code(member) -> str:
    """The member's invite code, made the first time they ask."""
    if not member.referral_code:
        for _ in range(10):
            code = "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))
            if not Member.objects.filter(referral_code=code).exists():
                Member.objects.filter(pk=member.pk, referral_code__isnull=True).update(referral_code=code)
                break
        member.refresh_from_db(fields=["referral_code"])
    return member.referral_code


def use_referral_code(member, text: str):
    """Link a member to the friend whose code is in the text. Returns that friend, or None.

    Only before the member's first purchase, only once, and never their own code.
    """
    match = REFERRAL_IN_TEXT.search(text or "")
    if not match or member.referred_by_id or has_paid(member):
        return None
    referrer = Member.objects.filter(referral_code=match.group(1).upper()).exclude(pk=member.pk).first()
    if referrer is None:
        return None
    Member.objects.filter(pk=member.pk, referred_by__isnull=True).update(referred_by=referrer)
    member.referred_by = referrer
    return referrer


# --- Paying -----------------------------------------------------------------


def _cancel_open_links(member):
    """A new link replaces the member's older unpaid ones, so they don't hold promo uses."""
    open_payments = member.payments.filter(status=Payment.Status.CREATED)
    for payment in open_payments:
        if payment.razorpay_link_id:
            try:
                razorpay.cancel_payment_link(payment.razorpay_link_id)
            except razorpay.RazorpayError:
                logger.warning("Could not cancel payment link %s", payment.razorpay_link_id)
    open_payments.update(status=Payment.Status.EXPIRED)


def start_payment(member, product: Product) -> Payment:
    """Make a payment and its Razorpay link. A free (100% off) purchase is switched on at once.

    Raises razorpay.RazorpayError if Razorpay can't be reached.
    """
    _cancel_open_links(member)
    with transaction.atomic():
        promo = pending_promo(member)
        if promo:
            # Lock the code, so two people can't take its last use at the same moment.
            PromoCode.objects.select_for_update().filter(pk=promo.pk).first()
        offer = quote(member, product)
        payment = Payment.objects.create(
            member=member, product=product, promo=offer.promo, referral_discount=offer.referral,
            list_price=product.price, discount=offer.discount, amount=offer.amount,
            expires_at=timezone.now() + timedelta(hours=LINK_HOURS),
        )
    if payment.amount == 0:
        mark_paid(payment)
        payment.refresh_from_db()
        return payment
    try:
        link = razorpay.create_payment_link(
            amount=payment.amount,
            reference_id=payment.pk.hex,
            description=product.name,
            contact=f"+{member.wa_id}",
            expire_by=int(payment.expires_at.timestamp()),
            callback_url=settings.PUBLIC_BASE_URL + reverse("billing:thanks"),
            notes={"payment": str(payment.pk), "product": product.code},
        )
    except razorpay.RazorpayError:
        Payment.objects.filter(pk=payment.pk).update(status=Payment.Status.EXPIRED)
        raise
    payment.razorpay_link_id, payment.link_url = link["id"], link["short_url"]
    payment.save(update_fields=["razorpay_link_id", "link_url"])
    return payment


def _give(member, plus_days: int = 0, conversions: int = 0):
    """Add Plus days (on top of any time left) and conversion credits. The member row must be locked."""
    fields = []
    if plus_days and not (member.plan != Member.Plan.FREE and member.plan_expires_at is None):
        # (Plus granted by hand with no end date stays as it is.)
        still_paid = member.current_plan() != Member.Plan.FREE
        start = member.plan_expires_at if still_paid else timezone.now()
        member.plan = Member.Plan.PLUS
        member.plan_expires_at = start + timedelta(days=plus_days)
        fields += ["plan", "plan_expires_at"]
    if conversions:
        member.conversion_credits += conversions
        fields.append("conversion_credits")
    if fields:
        member.save(update_fields=[*fields, "updated_at"])


def _reward_referrer(member, payment: Payment):
    """Plus days for whoever invited this member, after the member's first paid purchase."""
    program = ReferralProgram.get()
    if not (program.is_active and program.referrer_reward_days and member.referred_by_id and payment.amount > 0):
        return None
    if member.payments.filter(status=Payment.Status.PAID, amount__gt=0).exclude(pk=payment.pk).exists():
        return None
    referrer = Member.objects.select_for_update().filter(pk=member.referred_by_id).first()
    if referrer is None:
        return None
    rewarded = Payment.objects.filter(member__referred_by=referrer, referrer_rewarded=True).count()
    if program.max_rewards_per_member is not None and rewarded >= program.max_rewards_per_member:
        return None
    _give(referrer, plus_days=program.referrer_reward_days)
    return referrer


def _chatted_recently(member) -> bool:
    from bot.tasks import FREE_REPLY_WINDOW

    return bool(member.last_active_at and member.last_active_at > timezone.now() - FREE_REPLY_WINDOW)


def mark_paid(payment: Payment, razorpay_payment_id: str = "") -> bool:
    """Switch on what was bought. Safe to call twice: only the first call does anything."""
    from bot import replies, tasks

    with transaction.atomic():
        payment = Payment.objects.select_for_update(of=("self",)).select_related("product", "promo").get(pk=payment.pk)
        if payment.status in (Payment.Status.PAID, Payment.Status.REFUNDED):
            return False
        payment.status = Payment.Status.PAID
        payment.paid_at = timezone.now()
        payment.razorpay_payment_id = razorpay_payment_id or payment.razorpay_payment_id
        fields = ["status", "paid_at", "razorpay_payment_id", "referrer_rewarded"]

        member = Member.objects.select_for_update().filter(pk=payment.member_id).first()
        if member is None:  # They erased their data before the payment came through.
            payment.save(update_fields=fields)
            return True
        product = payment.product
        _give(member, plus_days=product.plus_days, conversions=product.conversions)
        if payment.promo and member.pending_promo_code == payment.promo.code:
            Member.objects.filter(pk=member.pk).update(pending_promo_code="", pending_promo_set_at=None)
        referrer = _reward_referrer(member, payment)
        payment.referrer_rewarded = referrer is not None
        payment.save(update_fields=fields)

        wa_id, text = member.wa_id, replies.payment_received(product, member)
        transaction.on_commit(lambda: tasks.send_text(wa_id, text))
        if referrer is not None and _chatted_recently(referrer):
            days = ReferralProgram.get().referrer_reward_days
            ref_wa_id, ref_text = referrer.wa_id, replies.referral_reward(days, referrer)
            transaction.on_commit(lambda: tasks.send_text(ref_wa_id, ref_text))
    logger.info("Payment %s paid: %s for member %s", payment.pk, product.code, member.pk)
    return True


def handle_event(event: dict):
    """A Razorpay webhook event (already signature-checked)."""
    kind = event.get("event", "")
    link = event.get("payload", {}).get("payment_link", {}).get("entity", {})
    try:
        payment_id = uuid.UUID(str(link.get("reference_id", "")))
    except ValueError:
        logger.info("Ignored Razorpay event %s with no Kagaz reference", kind)
        return
    payment = Payment.objects.filter(pk=payment_id, razorpay_link_id=link.get("id") or "-").first()
    if payment is None:
        logger.warning("Razorpay event %s for an unknown payment link %s", kind, link.get("id"))
        return

    if kind == "payment_link.paid":
        if link.get("amount_paid") != payment.amount * 100 or link.get("currency", "INR") != "INR":
            logger.error("Payment %s: Razorpay says %s paise were paid, expected %s", payment.pk,
                         link.get("amount_paid"), payment.amount * 100)
            return
        paid = event["payload"].get("payment", {}).get("entity", {})
        mark_paid(payment, paid.get("id", ""))
    elif kind in ("payment_link.expired", "payment_link.cancelled"):
        Payment.objects.filter(pk=payment.pk, status=Payment.Status.CREATED).update(status=Payment.Status.EXPIRED)


# --- Reminders --------------------------------------------------------------


def renewal_notice_due(member) -> bool:
    """True once a day in the last week of a paid plan that has an end date."""
    if member.current_plan() == Member.Plan.FREE or not member.plan_expires_at:
        return False
    if member.plan_expires_at - timezone.now() > timedelta(days=RENEWAL_NOTICE_DAYS):
        return False
    return cache.add(f"renewal-notice:{member.pk}:{timezone.localdate():%Y%m%d}", 1, 60 * 60 * 25)
