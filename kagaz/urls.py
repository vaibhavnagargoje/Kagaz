from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from accounts.admin_login import admin_login

admin.site.site_header = "Kagaz admin"
admin.site.site_title = "Kagaz admin"

urlpatterns = [
    # Before the admin's own URLs, so this sign-in page (with its wrong-password limit) is used.
    path(f"{settings.ADMIN_URL}/login/", admin_login),
    path(f"{settings.ADMIN_URL}/", admin.site.urls),
    path("webhooks/", include("bot.urls")),
    path("vault/", include("vault.urls")),
    path("billing/", include("billing.urls")),
    path("", include("accounts.urls")),
    path("", include("core.urls")),
]
