from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("k/<str:token>/", views.access_link, name="access_link"),
    path("unlock/", views.unlock, name="unlock"),
    path("sign-out/", views.sign_out, name="sign_out"),
]
