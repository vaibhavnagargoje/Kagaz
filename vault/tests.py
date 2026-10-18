import zipfile
from datetime import timedelta
from io import BytesIO
from unittest import mock

from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image
from pypdf import PdfWriter

from accounts.models import Member
from core.llm import LLMError
from core.testing import KagazTestCase, extraction_result, intent_result, make_jpeg, make_pdf
from vault import conversions, services
from vault.conversions import ConversionError, FileData, SendOptions
from vault.models import Document

AADHAAR_FIELD = {"key": "aadhaar_number", "label": "Aadhaar number", "value": "1234 5678 9012", "sensitive": False}
ROLL_FIELD = {"key": "roll_number", "label": "Roll number", "value": "1234567", "sensitive": False}


FETCH = {"X-Requested-With": "fetch"}


class WebChatTests(KagazTestCase):
    def setUp(self):
        super().setUp()
        self.member = self.make_member()
        self.sign_in(self.member)

    def say(self, text, **intent):
        with mock.patch("bot.intent.structured_call", return_value=intent_result(**intent)):
            return self.client.post(reverse("vault:send"), {"text": text}, headers=FETCH)

    def tap(self, action, document=None, value="", label="tap"):
        data = {"action": action, "doc": str(document.pk) if document else "", "value": value, "label": label}
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("vault:send"), data, headers=FETCH)

    def download(self, documents, headers=None, **options):
        return self.client.post(
            reverse("vault:download"), {"doc": [str(d.pk) for d in documents], **options}, headers=headers or {}
        )

    def test_chat_page_greets_and_suggests(self):
        self.save_doc(self.member, title="CBSE Class 12 Marksheet")
        page = self.client.get(reverse("vault:home"))
        self.assertContains(page, "I'm Kagaz")
        self.assertContains(page, 'data-label="CBSE Class 12 Marksheet"')  # Quick-reply chip.
        self.assertContains(page, "Unlock ID documents")
        self.assertIn("no-store", page["Cache-Control"])

    def test_a_question_gets_bubbles_back_and_stays_in_the_chat(self):
        self.save_doc(self.member, title="CBSE Class 12 Marksheet", fields=[ROLL_FIELD])
        answer = self.say("12th roll number", intent="ask_field", field_key="roll_number")
        self.assertContains(answer, "1234567")
        self.assertContains(answer, "double-check")
        self.assertNotContains(answer, "<html")  # Just the new bubbles.

        page = self.client.get(reverse("vault:home"))
        self.assertContains(page, "12th roll number")  # The question, still there after a reload.
        self.assertContains(page, "1234567")

    def test_protected_detail_appears_once_the_pin_is_entered(self):
        self.save_doc(self.member, category="certificate", title="Income Certificate", fields=[AADHAAR_FIELD])
        answer = self.say("aadhaar number", intent="ask_field", field_key="aadhaar_number")
        self.assertNotContains(answer, "1234 5678 9012")
        self.assertContains(answer, "Enter PIN to see it")

        unlock = self.client.post(reverse("accounts:unlock"), {"pin": "4826"}, headers=FETCH)
        self.assertEqual(unlock.json(), {"ok": True})
        self.assertContains(self.client.get(reverse("vault:thread"), headers=FETCH), "1234 5678 9012")

    def test_pin_sheet_wrong_pin(self):
        response = self.client.post(reverse("accounts:unlock"), {"pin": "0000"}, headers=FETCH)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Wrong PIN", response.json()["error"])

    def test_documents_answer_with_a_card(self):
        self.save_doc(self.member, data=make_pdf(pages=2), title="CBSE Class 12 Marksheet")
        answer = self.say("12th under 150kb", target_kb=150)
        self.assertContains(answer, "Here&#x27;s your CBSE Class 12 Marksheet.")
        self.assertContains(answer, "Download · under 150 KB")
        self.assertContains(answer, 'name="target_kb" value="150"')

    def test_ai_down_still_finds_by_name(self):
        self.save_doc(self.member, title="CBSE Class 12 Marksheet", keywords=["12th"])
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("quota")):
            answer = self.client.post(reverse("vault:send"), {"text": "12th marksheet"}, headers=FETCH)
        self.assertContains(answer, "searched your documents by name")
        self.assertContains(answer, "CBSE Class 12 Marksheet")

    def test_list_and_show(self):
        document = self.save_doc(self.member, title="CBSE Class 12 Marksheet")
        self.assertContains(self.tap("list", label="My documents"), "Your documents (1")
        self.assertContains(self.tap("show", document, label=document.title), "your CBSE Class 12 Marksheet.")

    def test_tapping_a_type_labels_the_document(self):
        document = self.save_doc(self.member, recognised=False, category="other", title="")
        answer = self.tap("label", document, value="pan", label="PAN card")
        self.assertContains(answer, "saved as PAN card")
        self.assertTrue(Document.objects.get(pk=document.pk).is_sensitive)

    def test_delete_asks_first_and_protected_needs_pin(self):
        document = self.save_doc(self.member, title="Old Bill")
        self.assertContains(self.say("delete old bill", intent="delete_document"), "be undone")
        self.assertContains(self.tap("delete", document, label="Delete Old Bill"), "Deleted Old Bill.")
        self.assertIn("on the web", self.last_text())

        card = self.save_doc(self.member, data=make_pdf(pages=2), category="pan", title="PAN Card")
        self.assertContains(self.tap("delete", card, label="Delete PAN Card"), "need your PIN")
        self.assertTrue(Document.objects.filter(pk=card.pk).exists())

    def test_upload_from_the_chat(self):
        upload = SimpleUploadedFile("marks.pdf", make_pdf(), content_type="text/plain")
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()):
            answer = self.client.post(reverse("vault:upload"), {"file": upload}, headers=FETCH)
        self.assertContains(answer, "Saved")
        self.assertContains(answer, "CBSE Class 10 Marksheet")
        self.assertEqual(Document.objects.get().mime_type, "application/pdf")  # Real type, not the browser's claim.
        self.assertContains(self.client.get(reverse("vault:home")), "marks.pdf")

    def test_upload_without_javascript_returns_to_the_chat(self):
        upload = SimpleUploadedFile("marks.pdf", make_pdf(), content_type="application/pdf")
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()):
            response = self.client.post(reverse("vault:upload"), {"file": upload})
        self.assertRedirects(response, reverse("vault:home"))

    def test_empty_upload_is_explained(self):
        self.assertContains(self.client.post(reverse("vault:upload"), {}, headers=FETCH), "choose a PDF or photo")

    def test_clear_chat_and_lock_now(self):
        self.say("hello there", intent="help")
        self.client.post(reverse("vault:clear"))
        self.assertNotContains(self.client.get(reverse("vault:home")), "hello there")

        self.member.unlock()
        self.client.post(reverse("vault:lock"))
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_unlocked())

    def test_download_with_size_limit(self):
        document = self.save_doc(self.member, data=make_pdf(pages=2))
        response = self.download([document], target_kb=150)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertLessEqual(len(response.content), 150 * 1024)
        self.assertIn("no-store", response["Cache-Control"])

    def test_protected_download_needs_a_recent_pin(self):
        document = self.save_doc(self.member, category="pan", title="PAN Card")
        response = self.download([document])
        self.assertRedirects(response, reverse("accounts:unlock") + "?next=%2Fvault%2F", fetch_redirect_response=False)
        self.assertEqual(self.download([document], headers=FETCH).json(), {"error": "pin"})
        self.member.unlock()
        self.assertEqual(self.download([document], headers=FETCH).status_code, 200)

    def test_impossible_size_is_reported(self):
        document = self.save_doc(self.member, data=make_pdf(pages=3))
        response = self.download([document], headers=FETCH, target_kb=5)
        self.assertIn("smallest", response["X-Kagaz-Note"])

    def test_several_files_come_as_a_zip(self):
        document = self.save_doc(self.member, data=make_pdf(pages=2))
        response = self.download([document], fmt="jpg")
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(len(zipfile.ZipFile(BytesIO(response.content)).namelist()), 2)

    def test_combined_pdf(self):
        first = self.save_doc(self.member, data=make_pdf(pages=1))
        second = self.save_doc(self.member, data=make_pdf(pages=2))
        response = self.download([first, second], merge="1")
        self.assertEqual(conversions.pdf_page_count(response.content), 3)

    def test_preview_is_a_safe_jpeg_and_respects_the_pin(self):
        ordinary = self.save_doc(self.member)
        protected = self.save_doc(self.member, data=make_pdf(pages=2), category="aadhaar", title="Aadhaar Card")
        response = self.client.get(reverse("vault:preview", args=[ordinary.pk]))
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(self.client.get(reverse("vault:preview", args=[protected.pk])).status_code, 403)
        self.member.unlock()
        self.assertEqual(self.client.get(reverse("vault:preview", args=[protected.pk])).status_code, 200)

    def test_viewer_hides_id_numbers_until_pin(self):
        document = self.save_doc(self.member, category="certificate", title="Income Certificate",
                                 fields=[ROLL_FIELD, AADHAAR_FIELD])
        page = self.client.get(reverse("vault:document", args=[document.pk]))
        self.assertContains(page, "1234567")
        self.assertNotContains(page, "1234 5678 9012")
        self.member.unlock()
        self.assertContains(self.client.get(reverse("vault:document", args=[document.pk])), "1234 5678 9012")

    def test_delete_from_the_viewer_tells_whatsapp_and_the_chat(self):
        document = self.save_doc(self.member, title="Old Bill")
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("vault:delete", args=[document.pk]))
        self.assertFalse(Document.objects.exists())
        self.assertIn("on the web", self.last_text())
        self.assertContains(self.client.get(reverse("vault:home")), "Deleted Old Bill.")

    def test_other_members_documents_are_invisible(self):
        other = self.make_member(wa_id="918888888888")
        theirs = self.save_doc(other)
        self.assertEqual(self.client.get(reverse("vault:document", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("vault:preview", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.download([theirs]).status_code, 404)
        self.assertEqual(self.client.post(reverse("vault:delete", args=[theirs.pk])).status_code, 404)
        self.assertContains(self.tap("show", theirs), "isn&#x27;t in your Kagaz")

    def test_signed_out_chat_requests_get_401(self):
        self.client.post(reverse("accounts:sign_out"))
        self.assertEqual(self.client.post(reverse("vault:send"), {"text": "hi"}, headers=FETCH).status_code, 401)


class ConversionTests(SimpleTestCase):
    def test_compress_image_under_target(self):
        data, fits = conversions.compress_image(make_jpeg(), 60 * 1024)
        self.assertTrue(fits)
        self.assertLessEqual(len(data), 60 * 1024)

    def test_compress_pdf_under_target_keeps_pages(self):
        pdf = make_pdf(pages=2)
        data, fits = conversions.compress_pdf(pdf, 150 * 1024)
        self.assertTrue(fits)
        self.assertLessEqual(len(data), 150 * 1024)
        self.assertEqual(conversions.pdf_page_count(data), 2)

    def test_small_file_is_left_alone(self):
        pdf = make_pdf()
        data, fits = conversions.compress_pdf(pdf, len(pdf) + 1)
        self.assertTrue(fits)
        self.assertEqual(data, pdf)

    def test_resize_gives_exact_pixels(self):
        data = conversions.resize_image(make_jpeg(), 200, 230)
        self.assertEqual(Image.open(BytesIO(data)).size, (200, 230))

    def test_merge_combines_pdf_and_image(self):
        merged = conversions.merge_to_pdf([
            FileData(make_pdf(pages=2), conversions.PDF, "a.pdf"),
            FileData(make_jpeg(), conversions.JPEG, "b.jpg"),
        ])
        self.assertEqual(conversions.pdf_page_count(merged), 3)

    def test_transparent_png_converts_to_pdf(self):
        buffer = BytesIO()
        Image.new("RGBA", (300, 200), (255, 0, 0, 100)).save(buffer, "PNG")
        self.assertEqual(conversions.pdf_page_count(conversions.image_to_pdf(buffer.getvalue())), 1)

    def test_password_protected_pdf_gives_friendly_error(self):
        writer = PdfWriter(clone_from=BytesIO(make_pdf()))
        writer.encrypt("secret")
        buffer = BytesIO()
        writer.write(buffer)
        with self.assertRaisesMessage(ConversionError, "password-protected"):
            conversions.compress_pdf(buffer.getvalue(), 1024)

    def test_prepare_reports_impossible_target(self):
        prepared = conversions.prepare([FileData(make_pdf(pages=3), conversions.PDF, "big.pdf")], SendOptions(target_kb=5))
        self.assertFalse(prepared.met_target)
        self.assertIn("smallest I could make", prepared.notes[0])

    def test_prepare_pdf_to_jpg_per_page(self):
        prepared = conversions.prepare([FileData(make_pdf(pages=2), conversions.PDF, "m.pdf")], SendOptions(fmt="jpg"))
        self.assertEqual([f.name for f in prepared.files], ["m_page1.jpg", "m_page2.jpg"])
        self.assertEqual(prepared.operations, ["to_jpg"])


class DocumentStorageTests(KagazTestCase):
    def save(self, member, data=None, mime="application/pdf", **ai):
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result(**ai)):
            return services.save_document(member, data or make_pdf(), mime, filename="marks.pdf")

    def test_file_and_details_are_encrypted_at_rest(self):
        member = self.make_member()
        original = make_pdf()
        document = self.save(member, original).document

        with default_storage.open(document.file.name, "rb") as fh:
            stored = fh.read()
        self.assertNotEqual(stored, original)
        self.assertNotIn(b"Asha Verma", bytes(document.details_ciphertext))
        self.assertNotIn(b"1234567", bytes(document.details_ciphertext))

        reloaded = Document.objects.get(pk=document.pk)
        self.assertEqual(reloaded.read_file(), original)
        self.assertEqual(reloaded.title, "CBSE Class 10 Marksheet")
        self.assertEqual(reloaded.get_field("roll_number")["value"], "1234567")
        self.assertEqual(reloaded.status, Document.Status.READY)

    def test_same_file_twice_is_a_duplicate(self):
        member = self.make_member()
        data = make_pdf()
        first = self.save(member, data)
        second = self.save(member, data)
        self.assertTrue(second.duplicate)
        self.assertEqual(second.document, first.document)
        self.assertEqual(member.documents.count(), 1)

    def test_id_documents_are_always_sensitive(self):
        member = self.make_member()
        document = self.save(member, category="pan", is_sensitive=False).document
        self.assertTrue(document.is_sensitive)

    def test_document_kept_when_ai_is_down(self):
        member = self.make_member()
        with mock.patch("vault.extraction.structured_call", side_effect=LLMError("down")):
            result = services.save_document(member, make_pdf(), "application/pdf", filename="my_degree.pdf")
        self.assertEqual(result.document.status, Document.Status.WAITING)  # Will be read again later.
        self.assertTrue(result.read.retrying)
        self.assertEqual(result.document.title, "my degree")
        self.assertIn("busy", result.problem)

    def test_deleting_document_removes_blob(self):
        member = self.make_member()
        document = self.save(member).document
        name = document.file.name
        with self.captureOnCommitCallbacks(execute=True):
            services.delete_document(document)
        self.assertFalse(default_storage.exists(name))


class CardSideTests(KagazTestCase):
    def setUp(self):
        super().setUp()
        self.member = self.make_member()

    def card(self, side, holder="Asha Verma", category="aadhaar", title="Aadhaar Card"):
        return self.save_doc(self.member, data=make_jpeg(800, 500, text=side), mime="image/jpeg",
                             category=category, title=title, side=side, holder_name=holder)

    def test_front_and_back_pair_up(self):
        front = self.card("front")
        back = self.card("back", holder="")  # Backs often have no name.
        front.refresh_from_db()
        back.refresh_from_db()
        self.assertIsNotNone(front.card_group)
        self.assertEqual(front.card_group, back.card_group)

    def test_different_people_are_not_paired(self):
        front = self.card("front", holder="Asha Verma")
        self.card("back", holder="Ravi Verma")
        front.refresh_from_db()
        self.assertIsNone(front.card_group)

    def test_nameless_back_with_two_possible_fronts_is_not_guessed(self):
        self.card("front", holder="Asha Verma")
        self.card("front", holder="Ravi Verma")
        back = self.card("back", holder="")
        back.refresh_from_db()
        self.assertIsNone(back.card_group)

    def test_both_sides_go_on_one_page(self):
        front, back = self.card("front"), self.card("back", holder="")
        documents = services.with_card_sides([Document.objects.get(pk=front.pk)])
        self.assertEqual([d.side for d in documents], ["front", "back"])
        prepared = services.prepare_files(self.member, documents, SendOptions())
        self.assertEqual(len(prepared.files), 1)
        self.assertTrue(prepared.files[0].name.endswith("_both_sides.pdf"))
        self.assertEqual(conversions.pdf_page_count(prepared.files[0].data), 1)
        self.assertEqual(prepared.operations, [])  # Not counted against free conversions.

    def test_asking_for_one_side(self):
        front, back = self.card("front"), self.card("back", holder="")
        only_back = services.with_card_sides([Document.objects.get(pk=front.pk)], side="back")
        self.assertEqual(only_back, [back])

    def test_deleting_one_side_frees_the_other(self):
        front, back = self.card("front"), self.card("back", holder="")
        services.delete_document(Document.objects.get(pk=back.pk))
        front.refresh_from_db()
        self.assertIsNone(front.card_group)


class RereadTests(KagazTestCase):
    def setUp(self):
        super().setUp()
        self.member = self.make_member()
        with mock.patch("vault.extraction.structured_call", side_effect=LLMError("busy")):
            self.document = services.save_document(self.member, make_pdf(), "application/pdf", filename="scan_001.pdf").document

    def make_due(self):
        Document.objects.filter(pk=self.document.pk).update(next_read_at=timezone.now() - timedelta(seconds=1))

    def test_busy_ai_schedules_a_reread(self):
        self.assertEqual(self.document.status, Document.Status.WAITING)
        self.assertEqual(self.document.read_attempts, 1)
        self.assertGreater(self.document.next_read_at, timezone.now())

    def test_reread_reads_waiting_documents(self):
        self.make_due()
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()):
            done = services.reread_due_documents()
        self.assertEqual(len(done), 1)
        document = Document.objects.get(pk=self.document.pk)
        self.assertEqual(document.status, Document.Status.READY)
        self.assertEqual(document.title, "CBSE Class 10 Marksheet")

    def test_not_due_yet_is_skipped(self):
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()) as ai:
            self.assertEqual(services.reread_due_documents(), [])
        ai.assert_not_called()

    def test_gives_up_after_several_tries(self):
        Document.objects.filter(pk=self.document.pk).update(read_attempts=len(services.REREAD_DELAYS_MINUTES))
        self.make_due()
        with mock.patch("vault.extraction.structured_call", side_effect=LLMError("busy")):
            services.reread_due_documents()
        self.assertEqual(Document.objects.get(pk=self.document.pk).status, Document.Status.UNREADABLE)

    def test_people_chatting_recently_are_told(self):
        from bot.tasks import reread_and_notify

        self.make_due()
        Member.objects.filter(pk=self.member.pk).update(last_active_at=timezone.now())
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()):
            self.assertEqual(reread_and_notify(), 1)
        self.assertIn("I've now read *scan 001*", self.last_text())

    def test_people_who_left_long_ago_are_not_messaged(self):
        from bot.tasks import reread_and_notify

        self.make_due()
        Member.objects.filter(pk=self.member.pk).update(last_active_at=timezone.now() - timedelta(days=2))
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()):
            reread_and_notify()
        self.assertEqual(self.sent("text"), [])  # Outside WhatsApp's 24-hour window.


class LabelTests(KagazTestCase):
    def setUp(self):
        super().setUp()
        self.member = self.make_member()

    def test_label_names_and_protects_the_document(self):
        document = self.save_doc(self.member, recognised=False, category="other", title="")
        services.label_document(document, "pan")
        document = Document.objects.get(pk=document.pk)
        self.assertEqual(document.category, "pan")
        self.assertTrue(document.is_sensitive)
        self.assertEqual(document.title, "PAN card")
        self.assertFalse(document.needs_label)

    def test_a_label_can_never_remove_protection(self):
        document = self.save_doc(self.member, category="aadhaar", title="Aadhaar Card")
        services.label_document(document, "other")
        self.assertTrue(Document.objects.get(pk=document.pk).is_sensitive)

    def test_web_label_and_read_again(self):
        self.sign_in(self.member)
        with mock.patch("vault.extraction.structured_call", side_effect=LLMError("busy")):
            document = services.save_document(self.member, make_pdf(), "application/pdf").document
        page = self.client.get(reverse("vault:document", args=[document.pk]))
        self.assertContains(page, "Try reading again")
        self.assertContains(page, "Marksheet or certificate")

        self.client.post(reverse("vault:label", args=[document.pk]), {"category": "certificate"})
        self.assertEqual(Document.objects.get(pk=document.pk).category, "certificate")

        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()):
            self.client.post(reverse("vault:reread", args=[document.pk]))
        self.assertEqual(Document.objects.get(pk=document.pk).status, Document.Status.READY)


class WebCardTests(KagazTestCase):
    def test_vault_offers_both_sides_on_one_page(self):
        member = self.make_member()
        self.sign_in(member, unlocked=True)
        front = self.save_doc(member, data=make_jpeg(800, 500, text="front"), mime="image/jpeg",
                              category="aadhaar", title="Aadhaar Card", side="front")
        back = self.save_doc(member, data=make_jpeg(800, 500, text="back"), mime="image/jpeg",
                             category="aadhaar", title="Aadhaar Card", side="back", holder_name="")
        with mock.patch("bot.intent.structured_call", return_value=intent_result(refs=[1])):
            self.client.post(reverse("vault:send"), {"text": "my aadhaar"})
        page = self.client.get(reverse("vault:home"))
        self.assertContains(page, "both sides on one page")
        self.assertContains(page, "Front + back · one page")
        self.assertContains(page, f'data-front="{front.pk}" data-back="{back.pk}"')

        response = self.client.post(reverse("vault:download"), {"doc": [str(front.pk), str(back.pk)]})
        self.assertTrue(response["Content-Disposition"].endswith('_both_sides.pdf"'))
        self.assertEqual(conversions.pdf_page_count(response.content), 1)

    def test_document_page_links_the_other_side(self):
        member = self.make_member()
        self.sign_in(member, unlocked=True)
        front = self.save_doc(member, data=make_jpeg(800, 500, text="front"), mime="image/jpeg",
                              category="vehicle", title="Vehicle RC", side="front")
        self.save_doc(member, data=make_jpeg(800, 500, text="back"), mime="image/jpeg",
                      category="vehicle", title="Vehicle RC", side="back", holder_name="")
        page = self.client.get(reverse("vault:document", args=[front.pk]))
        self.assertContains(page, "Kept together with the")
        self.assertContains(page, "Both sides on one page")
