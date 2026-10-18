from django.urls import path

from vault import views

app_name = "vault"

urlpatterns = [
    path("", views.home, name="home"),
    path("thread/", views.thread, name="thread"),
    path("send/", views.send, name="send"),
    path("upload/", views.upload, name="upload"),
    path("download/", views.download, name="download"),
    path("lock/", views.lock, name="lock"),
    path("clear/", views.clear, name="clear"),
    path("doc/<uuid:doc_id>/", views.document_detail, name="document"),
    path("doc/<uuid:doc_id>/preview.jpg", views.preview, name="preview"),
    path("doc/<uuid:doc_id>/delete/", views.delete, name="delete"),
    path("doc/<uuid:doc_id>/label/", views.label, name="label"),
    path("doc/<uuid:doc_id>/read-again/", views.reread, name="reread"),
]
