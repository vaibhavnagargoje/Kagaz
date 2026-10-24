from django.contrib import admin

from billing.models import Payment, Product, PromoCode, ReferralProgram


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "price", "plus_days", "conversions", "is_active", "order")
    list_editable = ("price", "is_active", "order")


@admin.register(PromoCode)
class PromoCodeAdmin(admin.ModelAdmin):
    list_display = ("code", "discount", "valid_until", "uses_label", "is_active")
    list_filter = ("is_active", "kind")
    search_fields = ("code",)
    filter_horizontal = ("products",)

    @admin.display(description="Discount")
    def discount(self, promo):
        if promo.kind == PromoCode.Kind.PERCENT:
            cap = f" (max ₹{promo.max_discount})" if promo.max_discount is not None else ""
            return f"{promo.value}%{cap}"
        return f"₹{promo.value}"

    @admin.display(description="Used")
    def uses_label(self, promo):
        return f"{promo.uses()} / {promo.max_uses if promo.max_uses is not None else '∞'}"


@admin.register(ReferralProgram)
class ReferralProgramAdmin(admin.ModelAdmin):
    list_display = ("__str__", "is_active", "friend_discount_percent", "referrer_reward_days", "max_rewards_per_member")

    def has_add_permission(self, request):
        return not ReferralProgram.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    """Read-only. Refunds happen in the Razorpay dashboard; then change the member's plan end date."""

    list_display = ("created_at", "member", "product", "amount", "discount", "promo", "status", "paid_at")
    list_filter = ("status", "product", "promo")
    search_fields = ("razorpay_order_id", "razorpay_payment_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
