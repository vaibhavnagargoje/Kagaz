import re
import time
from datetime import timedelta

from django.test import Client
from django.urls import reverse
from django.utils import timezone

from accounts import sessions
from accounts.models import AccessLink, Member
from core.testing import KagazTestCase
from vault.models import Document


def token_from(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def path_of(url: str) -> str:
    return url.removeprefix("https://kagaz.test")


class PinTests(KagazTestCase):
    def test_lockout_after_too_many_wrong_pins(self):
        member = self.make_member()
        member.set_pin("4826")
        for _ in range(5):
            self.assertFalse(member.check_pin("0000"))
        self.assertTrue(member.is_pin_locked())
        self.assertFalse(member.check_pin("4826"))  # Even the right PIN is refused while locked.

    def test_correct_pin_resets_failures(self):
        member = self.make_member()
        member.set_pin("4826")
        member.check_pin("0000")
        self.assertTrue(member.check_pin("4826"))
        self.assertEqual(member.pin_failed_attempts, 0)


class AccessLinkPageTests(KagazTestCase):
    def post(self, url, data):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(path_of(url), data)

    def signed_in_member_id(self):
        return self.client.session.get(sessions.MEMBER_KEY)

    def test_set_pin_flow_signs_in(self):
        member = self.make_member()
        url = AccessLink.issue(member, AccessLink.Purpose.SET_PIN)

        self.assertContains(self.client.get(path_of(url)), "Create your PIN")
        self.assertContains(self.post(url, {"pin": "1234", "pin_confirm": "1234"}), "too easy to guess")
        self.assertContains(self.post(url, {"pin": "4826", "pin_confirm": "4862"}), "are different")
        response = self.post(url, {"pin": "4826", "pin_confirm": "4826"})
        self.assertRedirects(response, reverse("vault:home"))

        member.refresh_from_db()
        self.assertTrue(member.check_pin("4826"))
        self.assertEqual(self.signed_in_member_id(), str(member.pk))
        self.assertIn("PIN is saved", self.last_text())  # Also confirmed on WhatsApp.
        self.assertEqual(self.client.get(path_of(url)).status_code, 410)  # Single use.

    def test_real_browser_form_post_passes_csrf(self):
        """Browsers only send a real Origin header if the page's referrer policy allows it."""
        member = self.make_member()
        path = path_of(AccessLink.issue(member, AccessLink.Purpose.SET_PIN))
        browser = Client(enforce_csrf_checks=True)

        page = browser.get(path)
        self.assertEqual(page.headers["Referrer-Policy"], "same-origin")
        token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.content.decode()).group(1)
        with self.captureOnCommitCallbacks(execute=True):
            response = browser.post(
                path, {"csrfmiddlewaretoken": token, "pin": "4826", "pin_confirm": "4826"},
                headers={"Origin": "http://testserver"},
            )
        self.assertEqual(response.status_code, 302)

    def test_changing_pin_needs_current_pin(self):
        member = self.make_member()
        member.set_pin("4826")
        url = AccessLink.issue(member, AccessLink.Purpose.SET_PIN)
        response = self.post(url, {"current_pin": "9999", "pin": "7391", "pin_confirm": "7391"})
        self.assertContains(response, "Wrong PIN")
        member.refresh_from_db()
        self.assertTrue(member.check_pin("4826"))
        self.assertIsNone(self.signed_in_member_id())

        self.post(url, {"current_pin": "4826", "pin": "7391", "pin_confirm": "7391"})
        member.refresh_from_db()
        self.assertTrue(member.check_pin("7391"))

    def test_protected_document_opens_in_the_vault_not_whatsapp(self):
        member = self.make_member()
        member.set_pin("4826")
        document = self.save_doc(member, category="aadhaar", title="Aadhaar Card")
        url = AccessLink.issue(
            member, AccessLink.Purpose.UNLOCK,
            {"op": "send", "doc_ids": [str(document.pk)], "options": {"target_kb": 200}},
        )
        response = self.post(url, {"pin": "4826"})
        self.assertRedirects(response, reverse("vault:home"))
        self.assertEqual(self.sent("document"), [])  # Nothing sent inside WhatsApp.

        vault = self.client.get(reverse("vault:home"))
        self.assertContains(vault, "what you asked for on WhatsApp")  # The newest chat message.
        self.assertContains(vault, "Download · under 200 KB")
        member.refresh_from_db()
        self.assertTrue(member.is_unlocked())

    def test_same_browser_signed_in_and_unlocked_skips_the_pin(self):
        member = self.make_member()
        self.sign_in(member, unlocked=True)
        document = self.save_doc(member, category="pan", title="PAN Card")
        url = AccessLink.issue(member, AccessLink.Purpose.UNLOCK, {"op": "send", "doc_ids": [str(document.pk)]})
        self.assertRedirects(self.client.get(path_of(url)), reverse("vault:home"))

    def test_another_browser_still_needs_the_pin(self):
        member = self.make_member()
        member.set_pin("4826")
        member.unlock()
        url = AccessLink.issue(member, AccessLink.Purpose.LOGIN)
        self.assertContains(self.client.get(path_of(url)), "Enter your PIN")

    def test_shared_computer_session_ends_with_the_browser(self):
        member = self.make_member()
        member.set_pin("4826")
        url = AccessLink.issue(member, AccessLink.Purpose.LOGIN)
        self.post(url, {"pin": "4826", "shared_device": "on"})
        self.assertTrue(self.client.session.get_expire_at_browser_close())

    def test_repeated_wrong_pins_lock_the_page(self):
        member = self.make_member()
        member.set_pin("4826")
        url = AccessLink.issue(member, AccessLink.Purpose.UNLOCK, {"op": "wipe"})
        for _ in range(4):
            self.assertContains(self.post(url, {"pin": "1111"}), "Wrong PIN")
        response = self.post(url, {"pin": "1111"})
        self.assertEqual(response.status_code, 429)
        self.assertTrue(Member.objects.filter(pk=member.pk).exists())

    def test_wipe_after_pin(self):
        member = self.make_member()
        member.set_pin("4826")
        self.save_doc(member)
        url = AccessLink.issue(member, AccessLink.Purpose.UNLOCK, {"op": "wipe"})
        self.assertContains(self.post(url, {"pin": "4826"}), "Everything is erased")
        self.assertFalse(Member.objects.exists())
        self.assertFalse(Document.objects.exists())
        self.assertIn("erased", self.last_text())

    def test_delete_protected_document_after_pin(self):
        member = self.make_member()
        member.set_pin("4826")
        document = self.save_doc(member, category="pan", title="PAN Card")
        url = AccessLink.issue(member, AccessLink.Purpose.UNLOCK, {"op": "delete", "doc_ids": [str(document.pk)]})
        self.post(url, {"pin": "4826"})
        self.assertFalse(Document.objects.exists())
        self.assertIn("Deleted *PAN Card*", self.last_text())

    def test_expired_link_is_refused(self):
        member = self.make_member()
        url = AccessLink.issue(member, AccessLink.Purpose.SET_PIN)
        AccessLink.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.client.get(path_of(url)).status_code, 410)

    def test_only_token_hash_is_stored(self):
        member = self.make_member()
        url = AccessLink.issue(member, AccessLink.Purpose.SET_PIN)
        self.assertFalse(AccessLink.objects.filter(token_hash=token_from(url)).exists())
        self.assertIsNotNone(AccessLink.objects.find_usable(token_from(url)))


class WebSessionTests(KagazTestCase):
    def test_vault_needs_sign_in(self):
        response = self.client.get(reverse("vault:home"))
        self.assertEqual(response.status_code, 401)
        self.assertContains(response, "type <strong>WEB</strong>", status_code=401)

    def test_idle_session_signs_out(self):
        member = self.make_member()
        self.sign_in(member)
        session = self.client.session
        session[sessions.LAST_SEEN_KEY] = int(time.time()) - (sessions.IDLE_MINUTES * 60 + 5)
        session.save()
        self.assertEqual(self.client.get(reverse("vault:home")).status_code, 401)

    def test_sign_out(self):
        member = self.make_member()
        self.sign_in(member)
        self.assertContains(self.client.post(reverse("accounts:sign_out")), "signed out")
        self.assertEqual(self.client.get(reverse("vault:home")).status_code, 401)

    def test_unlock_page(self):
        member = self.make_member()
        self.sign_in(member)
        unlock = reverse("accounts:unlock") + "?next=/vault/"
        self.assertContains(self.client.post(unlock, {"pin": "0000", "next": "/vault/"}), "Wrong PIN")
        self.assertRedirects(self.client.post(unlock, {"pin": "4826", "next": "/vault/"}), "/vault/")
        member.refresh_from_db()
        self.assertTrue(member.is_unlocked())

    def test_unlock_never_redirects_to_other_sites(self):
        member = self.make_member()
        self.sign_in(member)
        response = self.client.post(reverse("accounts:unlock"), {"pin": "4826", "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("vault:home"))
