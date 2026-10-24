from django.conf import settings
from django.http import HttpResponse
from django.shortcuts import render

from accounts.models import PLAN_LIMITS
from billing.models import Product


def _page(request, template: str, **context):
    business = {
        "name": settings.BUSINESS_NAME,
        "address": settings.BUSINESS_ADDRESS,
        "email": settings.SUPPORT_EMAIL,
        "phone": settings.SUPPORT_PHONE,
        "whatsapp": settings.WHATSAPP_DISPLAY_NUMBER,
    }
    return render(request, template, {"business": business, "free": PLAN_LIMITS["free"], **context})


def _period(days: int) -> str:
    return {365: "a year", 366: "a year", 30: "a month", 31: "a month"}.get(days, f"for {days} days")


def home(request):
    products = list(Product.objects.filter(is_active=True))
    return _page(
        request, "core/home.html",
        plus_prices=[{"price": p.price, "period": _period(p.plus_days)} for p in products if p.plus_days],
        packs=[p for p in products if p.conversions and not p.plus_days],
        plus=PLAN_LIMITS["plus"],
    )


def privacy(request):
    return _page(request, "core/privacy.html")


def terms(request):
    return _page(request, "core/terms.html")


def refunds(request):
    return _page(request, "core/refunds.html")


def contact(request):
    return _page(request, "core/contact.html")


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
