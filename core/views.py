from django.http import HttpResponse
from django.shortcuts import render


def home(request):
    return render(request, "core/home.html")


def privacy(request):
    return render(request, "core/privacy.html")


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")
