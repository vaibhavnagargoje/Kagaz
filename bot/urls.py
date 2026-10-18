from django.urls import path

from bot import views

app_name = "bot"

urlpatterns = [
    path("whatsapp/", views.whatsapp_webhook, name="whatsapp_webhook"),
]
