import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from accounts.models import PLAN_LIMITS, Member, User
from billing import services
from billing.models import Payment, Product, PromoCode, ReferralProgram
from billing.razorpay import RazorpayError
from bot import replies
from core.testing import KagazTestCase, intent_result
from vault import services as vault_services
from vault.conversions import SendOptions

FRIEND = "918888888888"
MINUTE = timedelta(minutes=1)


def fake_order(**kwargs) -> dict:
    """What Razorpay answers when an order is made."""
    return {"id": f"order_{uuid.uuid4().hex[:14]}", "amount": kwargs["amount"] * 100, "status": "created"}


def checkout_signature(order_id: str, payment_id: str) -> str:
    """What Razorpay's checkout hands the pay page (signed with the test key secret)."""
    return hmac.new(b"rzp-test-secret", f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


class BillingTestCase(KagazTestCase):
    def setUp(self):
        super().setUp()
        self.member = self.make_member()
        # Created by the 0002 data migration.
        self.year = Product.objects.get(code="plus_year")
        self.month = Product.objects.get(code="plus_month")
        self.pack = Product.objects.get(code="conversions_10")

    def buy(self, member, product) -> Payment:
        with mock.patch("billing.razorpay.create_order", side_effect=fake_order), \
                self.captureOnCommitCallbacks(execute=True):
            return services.start_payment(member, product)

    def event(self, payment, paid=None) -> dict:
        paid = payment.amount if paid is None else paid
        return {"event": "order.paid", "payload": {
            "order": {"entity": {
                "id": payment.razorpay_order_id, "receipt": payment.pk.hex, "currency": "INR",
                "amount": payment.amount * 100, "amount_paid": paid * 100, "status": "paid",
            }},
            "payment": {"entity": {"id": "pay_test123", "status": "captured", "order_id": payment.razorpay_order_id}},
        }}

    def report_checkout(self, payment, signature=None, status="captured"):
        """The pay page posting what Razorpay's checkout gave it."""
        razorpay_payment = {"id": "pay_test123", "status": status, "amount": payment.amount * 100,
                            "order_id": payment.razorpay_order_id}
        with mock.patch("billing.razorpay.fetch_payment", return_value=razorpay_payment), \
                self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("billing:verify", args=[payment.pk]), {
                "razorpay_order_id": payment.razorpay_order_id,
                "razorpay_payment_id": "pay_test123",
                "razorpay_signature": signature or checkout_signature(payment.razorpay_order_id, "pay_test123"),
            })

    def post_event(self, event, signature=None):
        body = json.dumps(event).encode()
        signature = signature or hmac.new(b"rzp-webhook-secret", body, hashlib.sha256).hexdigest()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                "/billing/webhook/", body, content_type="application/json",
                headers={"X-Razorpay-Signature": signature},
            )

    def pay(self, member, product) -> Payment:
        payment = self.buy(member, product)
        self.post_event(self.event(payment))
        payment.refresh_from_db()
        return payment

    def promo(self, **fields) -> PromoCode:
        return PromoCode.objects.create(**{"code": "SAVE20", "kind": PromoCode.Kind.PERCENT, "value": 20, **fields})


class PaymentTests(BillingTestCase):
    def test_order_has_the_right_amount(self):
        with mock.patch("billing.razorpay.create_order", side_effect=fake_order) as create:
            payment = services.start_payment(self.member, self.year)
        sent = create.call_args.kwargs
        self.assertEqual(sent["amount"], 399)
        self.assertEqual(sent["receipt"], payment.pk.hex)
        self.assertEqual(payment.status, Payment.Status.CREATED)
        self.assertTrue(payment.razorpay_order_id.startswith("order_"))

    def test_paid_webhook_switches_plus_on(self):
        payment = self.buy(self.member, self.year)
        self.assertEqual(self.post_event(self.event(payment)).status_code, 200)

        self.member.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.PLUS)
        self.assertAlmostEqual(self.member.plan_expires_at, timezone.now() + timedelta(days=365), delta=MINUTE)
        self.assertEqual(payment.status, Payment.Status.PAID)
        self.assertEqual(payment.razorpay_payment_id, "pay_test123")
        self.assertIn("Payment received", self.last_text())

    def test_bad_signature_is_rejected(self):
        payment = self.buy(self.member, self.year)
        self.assertEqual(self.post_event(self.event(payment), signature="deadbeef").status_code, 403)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CREATED)

    def test_repeated_webhook_counts_once(self):
        payment = self.buy(self.member, self.year)
        self.post_event(self.event(payment))
        self.post_event(self.event(payment))
        self.member.refresh_from_db()
        self.assertAlmostEqual(self.member.plan_expires_at, timezone.now() + timedelta(days=365), delta=MINUTE)

    def test_wrong_amount_is_ignored(self):
        payment = self.buy(self.member, self.year)
        with self.assertLogs("billing.services", "ERROR"):
            self.post_event(self.event(payment, paid=1))
        self.member.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.FREE)

    def test_buying_again_adds_to_time_left(self):
        self.member.plan, self.member.plan_expires_at = Member.Plan.PLUS, timezone.now() + timedelta(days=10)
        self.member.save()
        self.pay(self.member, self.month)
        self.member.refresh_from_db()
        self.assertAlmostEqual(self.member.plan_expires_at, timezone.now() + timedelta(days=40), delta=MINUTE)

    def test_pack_adds_conversion_credits(self):
        self.pay(self.member, self.pack)
        self.member.refresh_from_db()
        self.assertEqual(self.member.conversion_credits, 10)
        self.assertEqual(self.member.current_plan(), Member.Plan.FREE)

    def test_new_payment_replaces_the_open_one(self):
        first = self.buy(self.member, self.year)
        self.buy(self.member, self.month)
        first.refresh_from_db()
        self.assertEqual(first.status, Payment.Status.EXPIRED)

    def test_paying_a_replaced_order_still_counts(self):
        """If they pay the old one anyway, they get what they paid for."""
        first = self.buy(self.member, self.year)
        self.buy(self.member, self.month)
        self.post_event(self.event(first))
        first.refresh_from_db()
        self.assertEqual(first.status, Payment.Status.PAID)

    def test_other_events_and_orders_are_ignored(self):
        self.assertEqual(self.post_event({"event": "payment.authorized", "payload": {}}).status_code, 200)
        response = self.post_event({"event": "order.paid", "payload": {"order": {"entity": {
            "id": "order_x", "receipt": "not-ours"}}}})
        self.assertEqual(response.status_code, 200)


class PayPageTests(BillingTestCase):
    def test_pay_page_opens_razorpay_checkout_as_kagaz(self):
        payment = self.buy(self.member, self.year)
        response = self.client.get(reverse("billing:pay", args=[payment.pk]))
        self.assertContains(response, "https://checkout.razorpay.com/v1/checkout.js")
        self.assertContains(response, "Pay ₹399")
        self.assertContains(response, "+91 •••••• 9999")
        self.assertContains(response, 'id="checkout-options"')
        options = response.context["checkout"]
        self.assertEqual(options["order_id"], payment.razorpay_order_id)
        self.assertEqual(options["name"], "Kagaz")
        self.assertEqual(options["prefill"], {"contact": "+919999999999"})
        self.assertEqual(options["amount"], 39900)

    def test_checkout_result_switches_plus_on(self):
        payment = self.buy(self.member, self.year)
        response = self.report_checkout(payment)
        self.assertEqual(response.json(), {"result": "paid"})
        self.member.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.PLUS)
        self.assertIn("Payment received", self.last_text())
        self.assertContains(self.client.get(reverse("billing:pay", args=[payment.pk])), "Paid, thank you!")

    def test_checkout_result_with_a_bad_signature(self):
        payment = self.buy(self.member, self.year)
        with self.assertLogs("billing.services", "WARNING"):
            response = self.report_checkout(payment, signature="deadbeef")
        self.assertEqual(response.status_code, 400)
        self.member.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.FREE)

    def test_not_captured_yet_waits_for_the_webhook(self):
        payment = self.buy(self.member, self.year)
        self.assertEqual(self.report_checkout(payment, status="authorized").json(), {"result": "processing"})
        self.member.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.FREE)
        self.post_event(self.event(payment))
        self.member.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.PLUS)

    def test_pay_page_expires(self):
        payment = self.buy(self.member, self.year)
        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - MINUTE)
        response = self.client.get(reverse("billing:pay", args=[payment.pk]))
        self.assertContains(response, "expired")
        self.assertNotContains(response, "checkout.razorpay.com")


class WhatsAppPlanTests(BillingTestCase):
    def test_plus_shows_buttons_with_prices(self):
        self.receive_text(self.member.wa_id, "PLUS")
        self.assertEqual(self.sent("buttons")[-1]["buttons"], [
            ("buy:plus_year", "Yearly ₹399"), ("buy:plus_month", "Monthly ₹49"), ("buy:conversions_10", "10 conversions ₹19"),
        ])

    def test_plus_members_are_not_offered_packs(self):
        self.member.plan, self.member.plan_expires_at = Member.Plan.PLUS, timezone.now() + timedelta(days=100)
        self.member.save()
        self.receive_text(self.member.wa_id, "renew")
        self.assertEqual([code for code, _ in self.sent("buttons")[-1]["buttons"]], ["buy:plus_year", "buy:plus_month"])

    def test_tap_sends_a_pay_button(self):
        with mock.patch("billing.razorpay.create_order", side_effect=fake_order):
            self.receive_tap(self.member.wa_id, "buy:plus_year")
        payment = Payment.objects.get()
        button = self.sent("link_button")[-1]
        self.assertEqual(button["label"], "Pay ₹399")
        self.assertEqual(button["url"], f"https://kagaz.test/billing/pay/{payment.pk}/")
        self.assertIn("Kagaz Plus · 1 year", button["body"])

    def test_razorpay_down(self):
        with mock.patch("billing.razorpay.create_order", side_effect=RazorpayError("down")), \
                self.assertLogs("bot.handlers", "ERROR"):
            self.receive_tap(self.member.wa_id, "buy:plus_year")
        self.assertEqual(self.last_text(), replies.PAYMENTS_DOWN)
        self.assertEqual(Payment.objects.get().status, Payment.Status.EXPIRED)

    def test_plan_summary(self):
        self.receive_text(self.member.wa_id, "my plan")
        self.assertIn("*Your plan:* Free", self.last_text())
        self.assertIn("Documents: 0 of 25", self.last_text())


class PromoCodeTests(BillingTestCase):
    def test_percent_code_with_a_cap(self):
        self.promo(value=50, max_discount=100)
        self.receive_text(self.member.wa_id, "CODE save20")
        self.assertIn("applied", self.last_text())
        self.member.refresh_from_db()
        self.assertEqual(services.quote(self.member, self.year).amount, 299)  # 50% is 199, capped at 100.
        self.assertEqual(services.quote(self.member, self.month).amount, 25)

    def test_amount_code_with_a_minimum_price(self):
        self.promo(kind=PromoCode.Kind.AMOUNT, value=50, min_amount=100)
        services.apply_code(self.member, "SAVE20")
        self.assertEqual(services.quote(self.member, self.year).amount, 349)
        self.assertEqual(services.quote(self.member, self.month).amount, 49)  # Below the minimum price.

    def test_code_for_some_products_only(self):
        self.promo().products.add(self.year)
        services.apply_code(self.member, "SAVE20")
        self.assertEqual(services.quote(self.member, self.year).discount, 79)
        self.assertEqual(services.quote(self.member, self.month).discount, 0)

    def test_expired_and_unknown_codes(self):
        self.promo(valid_until=timezone.now() - timedelta(days=1))
        self.receive_text(self.member.wa_id, "code SAVE20")
        self.assertIn("isn't active", self.last_text())
        self.receive_text(self.member.wa_id, "code NOPE")
        self.assertIn("couldn't find", self.last_text())

    def test_first_n_uses_count_open_payments(self):
        self.promo(max_uses=1)
        services.apply_code(self.member, "SAVE20")
        payment = self.buy(self.member, self.year)  # Unpaid, but holds the only use.

        friend = self.make_member(wa_id=FRIEND)
        with self.assertRaisesMessage(services.PromoError, "used up"):
            services.apply_code(friend, "SAVE20")

        Payment.objects.filter(pk=payment.pk).update(expires_at=timezone.now() - MINUTE)  # An hour later.
        services.apply_code(friend, "SAVE20")  # The use is free again.

    def test_once_per_member(self):
        self.promo()
        services.apply_code(self.member, "SAVE20")
        payment = self.pay(self.member, self.month)
        self.assertEqual(payment.amount, 40)
        self.member.refresh_from_db()
        self.assertEqual(self.member.pending_promo_code, "")  # Used up by the purchase.
        with self.assertRaisesMessage(services.PromoError, "already used"):
            services.apply_code(self.member, "SAVE20")

    def test_free_purchase_needs_no_razorpay(self):
        self.promo(value=100)
        services.apply_code(self.member, "SAVE20")
        with mock.patch("billing.razorpay.create_order") as create:
            self.receive_tap(self.member.wa_id, "buy:plus_month")
        create.assert_not_called()
        self.member.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.PLUS)
        self.assertIn("Payment received", self.last_text())


class ReferralTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.code = services.referral_code(self.member)

    def friend(self) -> Member:
        friend = self.make_member(wa_id=FRIEND)
        self.assertEqual(services.use_referral_code(friend, f"REF {self.code}"), self.member)
        return friend

    def test_code_in_the_first_message_links_the_friend(self):
        self.receive_text("917777777777", f"Hi Kagaz! ref {self.code.lower()}")
        self.assertEqual(Member.objects.get(wa_id="917777777777").referred_by, self.member)
        self.assertEqual(len(self.sent("buttons")), 1)  # The normal welcome still comes.

    def test_friend_gets_a_discount_and_the_referrer_gets_plus(self):
        friend = self.friend()
        self.assertEqual(services.quote(friend, self.year).amount, 320)  # 20% off 399, rounded down.
        payment = self.pay(friend, self.year)

        self.assertTrue(payment.referrer_rewarded)
        self.member.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.PLUS)
        self.assertAlmostEqual(self.member.plan_expires_at, timezone.now() + timedelta(days=30), delta=MINUTE)

    def test_reward_only_for_the_first_purchase(self):
        friend = self.friend()
        self.pay(friend, self.month)
        self.assertEqual(services.quote(friend, self.year).amount, 399)  # No second friend discount.
        self.pay(friend, self.year)
        self.member.refresh_from_db()
        self.assertAlmostEqual(self.member.plan_expires_at, timezone.now() + timedelta(days=30), delta=MINUTE)

    def test_no_reward_for_a_free_purchase(self):
        friend = self.friend()
        self.promo(value=100)
        services.apply_code(friend, "SAVE20")
        self.buy(friend, self.month)
        self.member.refresh_from_db()
        self.assertEqual(self.member.current_plan(), Member.Plan.FREE)

    def test_max_rewards(self):
        ReferralProgram.objects.filter(pk=1).update(max_rewards_per_member=1)
        self.pay(self.friend(), self.month)
        second = self.make_member(wa_id="917777777777")
        services.use_referral_code(second, f"REF {self.code}")
        self.pay(second, self.month)
        self.member.refresh_from_db()
        self.assertAlmostEqual(self.member.plan_expires_at, timezone.now() + timedelta(days=30), delta=MINUTE)

    def test_own_code_and_late_codes_are_refused(self):
        self.assertIsNone(services.use_referral_code(self.member, f"REF {self.code}"))
        buyer = self.make_member(wa_id="917777777777")
        self.pay(buyer, self.month)
        self.assertIsNone(services.use_referral_code(buyer, f"REF {self.code}"))

    def test_refer_command(self):
        self.receive_text(self.member.wa_id, "refer")
        self.assertIn(f"REF {self.code}", self.last_text())
        self.receive_text(FRIEND, "hi")
        self.receive_tap(FRIEND, "consent:yes")
        self.receive_text(FRIEND, f"code {self.code}")  # A friend's code typed after CODE also works.
        self.assertIn("20% off", self.last_text())


class LimitTests(BillingTestCase):
    def test_question_limit_switches_to_name_search(self):
        self.save_doc(self.member)
        with mock.patch.dict(PLAN_LIMITS["free"], {"questions_per_day": 1}), \
                mock.patch("bot.intent.structured_call", return_value=intent_result()) as ai:
            self.receive_text(self.member.wa_id, "send my 10th marksheet")
            self.receive_text(self.member.wa_id, "send my 10th marksheet")
        self.assertEqual(ai.call_count, 1)
        self.assertEqual(len(self.sent("document")), 2)
        self.assertIn("searched your documents by name", self.sent("text")[-1]["body"])

    def test_pack_credits_are_used_after_the_free_conversions(self):
        document = self.save_doc(self.member)
        self.member.conversion_credits = 1
        self.member.save()
        with mock.patch.dict(PLAN_LIMITS["free"], {"conversions_per_month": 0}):
            self.assertTrue(self.member.can_convert())
            vault_services.prepare_files(self.member, [document], SendOptions(fmt="jpg"))
            self.member.refresh_from_db()
            self.assertEqual(self.member.conversion_credits, 0)
            self.assertFalse(self.member.can_convert())

    def test_free_conversions_come_before_credits(self):
        document = self.save_doc(self.member)
        self.member.conversion_credits = 1
        self.member.save()
        vault_services.prepare_files(self.member, [document], SendOptions(fmt="jpg"))
        self.member.refresh_from_db()
        self.assertEqual(self.member.conversion_credits, 1)

    def test_ended_plus_is_free_again_and_keeps_documents(self):
        self.save_doc(self.member)
        self.member.plan, self.member.plan_expires_at = Member.Plan.PLUS, timezone.now() - timedelta(minutes=1)
        self.member.save()
        self.assertEqual(self.member.current_plan(), Member.Plan.FREE)
        self.assertEqual(self.member.limits()["documents"], 25)
        self.assertEqual(self.member.documents.count(), 1)

    def test_plus_without_an_end_date_never_ends(self):
        self.member.plan = Member.Plan.PLUS
        self.member.save()
        self.assertEqual(self.member.current_plan(), Member.Plan.PLUS)

    def test_renewal_reminder_once_a_day(self):
        self.member.plan, self.member.plan_expires_at = Member.Plan.PLUS, timezone.now() + timedelta(days=3)
        self.member.save()
        self.receive_text(self.member.wa_id, "help")
        self.receive_text(self.member.wa_id, "help")
        reminders = [m for m in self.sent("text") if "Plus ends on" in m["body"]]
        self.assertEqual(len(reminders), 1)


class AdminPageTests(BillingTestCase):
    def test_billing_admin_pages_load(self):
        self.client.force_login(User.objects.create_superuser("boss", password="a-long-admin-password"))
        self.promo()
        for page in ("product", "promocode", "referralprogram", "payment"):
            self.assertEqual(self.client.get(reverse(f"admin:billing_{page}_changelist")).status_code, 200, page)
        self.assertEqual(self.client.get(reverse("admin:billing_promocode_add")).status_code, 200)
