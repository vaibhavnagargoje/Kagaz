from django.contrib import messages
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from accounts import sessions
from accounts.forms import EnterPinForm, LinkChangePinForm, LinkEnterPinForm, LinkSetPinForm
from accounts.models import PIN_LOCK_MINUTES, AccessLink, Member
from bot import replies, tasks
from vault import chat, services

LINK_FORMS = {"enter": LinkEnterPinForm, "set": LinkSetPinForm, "change": LinkChangePinForm}


@never_cache
@require_http_methods(["GET", "POST"])
def access_link(request, token):
    """The page behind a one-time link sent on WhatsApp: PIN, then the web vault."""
    link = AccessLink.objects.find_usable(token)
    if link is None:
        return render(request, "accounts/link_invalid.html", status=410)
    member = link.member
    # A forgotten-PIN reset: allowed only once the 24-hour wait is over and it
    # wasn't cancelled. It also works while locked out (that's often why).
    resetting = (link.action or {}).get("op") == "reset_pin"
    if resetting and not member.pin_reset_ready():
        return render(request, "accounts/link_invalid.html", {"reset_cancelled": True}, status=410)
    if member.is_pin_locked() and not resetting:
        return _locked(request)

    # This browser is already signed in and the PIN was entered a moment ago:
    # don't ask again.
    signed_in = sessions.current_member(request)
    if signed_in == member and member.is_unlocked() and link.purpose != AccessLink.Purpose.SET_PIN:
        link.mark_used()
        return _finish(request, member, link)

    if resetting or not member.has_pin:
        mode = "set"
    elif link.purpose == AccessLink.Purpose.SET_PIN:
        mode = "change"
    else:
        mode = "enter"
    form = LINK_FORMS[mode](request.POST or None)

    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # Lock the rows so parallel guesses can't skip the attempt counter.
            member = Member.objects.select_for_update().get(pk=member.pk)
            link = AccessLink.objects.select_for_update().get(pk=link.pk)
            if link.used_at or (resetting and not member.pin_reset_ready()):
                return render(request, "accounts/link_invalid.html", {"reset_cancelled": resetting}, status=410)

            if mode == "set":
                accepted = True
            else:
                accepted = member.check_pin(form.cleaned_data["pin" if mode == "enter" else "current_pin"])

            if accepted:
                wa_id = member.wa_id
                # Using the right PIN proves the owner remembers it: stop any reset.
                if mode != "set" and member.cancel_pin_reset():
                    transaction.on_commit(lambda: tasks.send_text(wa_id, replies.PIN_RESET_CANCELLED_BY_USE))
                if mode in ("set", "change"):
                    member.set_pin(form.cleaned_data["pin"])
                    saved_text = replies.PIN_RESET_DONE if resetting else replies.PIN_SAVED
                    transaction.on_commit(lambda: tasks.send_text(wa_id, saved_text))
                member.unlock()
                link.mark_used()

        if accepted:
            sessions.sign_in(request, member, shared_device=form.cleaned_data.get("shared_device", False))
            if mode in ("set", "change"):
                messages.success(request, "Your PIN is saved.")
            return _finish(request, member, link)
        if member.is_pin_locked():
            return _locked(request)
        form.add_error("pin" if mode == "enter" else "current_pin", "Wrong PIN. Please try again.")

    return render(request, "accounts/access_link.html", {
        "form": form, "mode": mode, "purpose": link.purpose, "resetting": resetting,
    })


def _finish(request, member: Member, link: AccessLink):
    """Do what the link was for, once the PIN has been accepted."""
    action = link.action or {}
    op = action.get("op")

    if op == "wipe":
        wa_id = member.wa_id
        sessions.sign_out(request)
        services.delete_member_data(member)
        transaction.on_commit(lambda: tasks.send_text(wa_id, replies.WIPED))
        return render(request, "accounts/erased.html")

    if op == "delete":
        for document in services.owned_documents(member, action.get("doc_ids", [])):
            title = document.title
            services.delete_document(document)
            chat.add(request, chat.bot_text(f"Deleted {title}."))
            transaction.on_commit(lambda title=title: tasks.send_text(member.wa_id, replies.deleted(title)))
        return redirect("vault:home")

    if op in ("send", "field"):
        # Shown as the newest message in the web chat.
        chat.add(request, *chat.from_whatsapp_link(action))
    return redirect("vault:home")


@never_cache
@require_http_methods(["GET", "POST"])
@sessions.member_required
def unlock(request):
    """Enter the PIN again while signed in, to open ID and bank documents.

    The chat's PIN sheet posts here with JavaScript and gets JSON back.
    """
    member = request.member
    fetch = request.headers.get("X-Requested-With") == "fetch"
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        next_url = reverse("vault:home")
    if member.is_unlocked():
        return JsonResponse({"ok": True}) if fetch else redirect(next_url)
    if member.is_pin_locked():
        return _locked_json() if fetch else _locked(request)

    form = EnterPinForm(request.POST or None)
    error = "Use your 4 to 6 digit PIN."
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            member = Member.objects.select_for_update().get(pk=member.pk)
            accepted = member.check_pin(form.cleaned_data["pin"])
            if accepted:
                member.unlock()
                if member.cancel_pin_reset():
                    wa_id = member.wa_id
                    transaction.on_commit(lambda: tasks.send_text(wa_id, replies.PIN_RESET_CANCELLED_BY_USE))
        if accepted:
            return JsonResponse({"ok": True}) if fetch else redirect(next_url)
        if member.is_pin_locked():
            sessions.sign_out(request)
            return _locked_json() if fetch else _locked(request)
        error = "Wrong PIN. Please try again."
        form.add_error("pin", error)
    if fetch:
        return JsonResponse({"ok": False, "error": error}, status=400)
    return render(request, "accounts/unlock.html", {"form": form, "next": next_url, "member": member})


@require_POST
def sign_out(request):
    shared = request.session.get(sessions.SHARED_KEY, False)
    sessions.sign_out(request)
    return render(request, "accounts/signed_out.html", {"just_signed_out": True, "shared_device": shared})


def _locked(request):
    return render(request, "accounts/link_locked.html", {"minutes": PIN_LOCK_MINUTES}, status=429)


def _locked_json():
    return JsonResponse({
        "ok": False, "locked": True,
        "error": f"Too many wrong tries. ID documents are locked for {PIN_LOCK_MINUTES} minutes.",
    }, status=429)
