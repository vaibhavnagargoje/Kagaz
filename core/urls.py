from django.urls import path

from core import views

app_name = "core"

urlpatterns = [
    path("", views.home, name="home"),
    path("privacy/", views.privacy, name="privacy"),
    path("terms/", views.terms, name="terms"),
    path("refunds/", views.refunds, name="refunds"),
    path("contact/", views.contact, name="contact"),
    path("healthz/", views.healthz, name="healthz"),
]
