"""
Sign-in for the Django admin, with a limit on wrong passwords.

The admin lives at a secret address (ADMIN_URL in .env). On top of that, too
many wrong passwords from one address lock it out for a while, even if the
next password is right.
"""

import logging

from django.contrib import admin
from django.core.cache import cache
from django.http import HttpResponse

logger = logging.getLogger(__name__)

ADMIN_LOGIN_MAX_FAILURES = 5
ADMIN_LOGIN_LOCK_MINUTES = 15


def _client_ip(request) -> str:
    # Requests reach Django only through Nginx (Gunicorn listens on a unix
    # socket), and Nginx sets X-Real-IP to the visitor's real address.
    return request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR", "")


def admin_login(request, extra_context=None):
    ip = _client_ip(request)
    key = f"admin-login-failures:{ip}"
    if (cache.get(key) or 0) >= ADMIN_LOGIN_MAX_FAILURES:
        return HttpResponse(
            f"Too many failed sign-in attempts. Try again in {ADMIN_LOGIN_LOCK_MINUTES} minutes.",
            status=429,
            content_type="text/plain",
        )

    response = admin.site.login(request, extra_context)
    if request.method == "POST":
        if request.user.is_staff:
            cache.delete(key)
        else:
            # The lock window starts at the first wrong password.
            cache.add(key, 0, ADMIN_LOGIN_LOCK_MINUTES * 60)
            failures = cache.incr(key)
            logger.warning("Failed admin sign-in from %s (%s of %s)", ip, failures, ADMIN_LOGIN_MAX_FAILURES)
    return response
