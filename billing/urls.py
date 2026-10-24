from django.urls import path

from billing import views

app_name = "billing"

urlpatterns = [
    path("webhook/", views.razorpay_webhook, name="webhook"),
    path("thanks/", views.thanks, name="thanks"),
]
