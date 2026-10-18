from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Kagaz admin"
admin.site.site_title = "Kagaz admin"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("webhooks/", include("bot.urls")),
    path("vault/", include("vault.urls")),
    path("", include("accounts.urls")),
    path("", include("core.urls")),
]
