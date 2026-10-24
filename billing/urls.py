from django.urls import path

from billing import views

app_name = "billing"

urlpatterns = [
    path("pay/<uuid:payment_id>/", views.pay, name="pay"),
    path("pay/<uuid:payment_id>/verify/", views.verify, name="verify"),
    path("webhook/", views.razorpay_webhook, name="webhook"),
]
