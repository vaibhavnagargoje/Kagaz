"""
Signing members in to the website.

Members have no password. They open a one-time link that the bot sends on
WhatsApp and enter their PIN; that signs in *this browser only*.

- The session ends after 30 idle minutes (10 on a shared/cyber café computer,
  where it also ends when the browser closes).
- Separately, ID and bank documents need a recent PIN (Member.is_unlocked).
"""

import time
from functools import wraps

from django.shortcuts import render
from django.views.decorators.cache import never_cache

from accounts.models import Member

MEMBER_KEY = "kagaz_member"
LAST_SEEN_KEY = "kagaz_last_seen"
SHARED_KEY = "kagaz_shared_device"

IDLE_MINUTES = 30
SHARED_IDLE_MINUTES = 10


def sign_in(request, member: Member, shared_device: bool = False):
    request.session.cycle_key()  # New session id, so an old cookie can't be reused.
    request.session[MEMBER_KEY] = str(member.pk)
    request.session[SHARED_KEY] = shared_device
    _touch(request)


def sign_out(request):
    request.session.flush()


def current_member(request) -> Member | None:
    member_id = request.session.get(MEMBER_KEY)
    if not member_id:
        return None
    idle_minutes = SHARED_IDLE_MINUTES if request.session.get(SHARED_KEY) else IDLE_MINUTES
    if time.time() - request.session.get(LAST_SEEN_KEY, 0) > idle_minutes * 60:
        sign_out(request)
        return None
    member = Member.objects.filter(pk=member_id, is_blocked=False).first()
    if member is None:
        sign_out(request)
        return None
    _touch(request)
    return member


def _touch(request):
    request.session[LAST_SEEN_KEY] = int(time.time())
    if request.session.get(SHARED_KEY):
        request.session.set_expiry(0)  # Cookie ends when the browser closes.
    else:
        request.session.set_expiry(IDLE_MINUTES * 60)


def member_required(view):
    """Only for signed-in members; sets request.member. Pages are never cached."""

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        member = current_member(request)
        if member is None:
            return render(request, "accounts/signed_out.html", status=401)
        request.member = member
        return view(request, *args, **kwargs)

    return never_cache(wrapper)
