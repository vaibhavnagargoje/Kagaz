import zipfile
from dataclasses import asdict
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
from core.llm import File, LLMError
from core.testing import (
    WORD_NS, KagazTestCase, blank_pdf, extraction_result, intent_result, make_docx, make_heic, make_jpeg, make_pdf,
    png_header,
)
from vault import conversions, services, wordfile
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

    def upload_card(self, side, holder, name):
        result = extraction_result(category="pan", title="PAN Card", side=side, holder_name=holder)
        upload = SimpleUploadedFile(name, make_jpeg(800, 500, text=name), content_type="image/jpeg")
        with mock.patch("vault.extraction.structured_call", return_value=result):
            return self.client.post(reverse("vault:upload"), {"file": upload}, headers=FETCH)

    def test_web_upload_asks_which_card_and_a_chip_joins_them(self):
        self.upload_card("front", "Vaibhav Nagargoje", "mine.jpg")
        self.upload_card("front", "Sundardas Nagargoje", "father.jpg")
        answer = self.upload_card("back", "", "back.jpg")
        self.assertContains(answer, "Which card is it from?")
        self.assertContains(answer, "Vaibhav Nagargoje")
        self.assertContains(answer, 'data-value="new"')

        back = Document.objects.get(side="back")
        mine = next(d for d in Document.objects.filter(side="front") if "Vaibhav" in d.get_details()["holder_name"])
        self.assertContains(self.tap("pair", back, value=str(mine.pk)), "are joined")
        back.refresh_from_db()
        mine.refresh_from_db()
        self.assertEqual(back.card_group, mine.card_group)
        self.assertNotContains(self.client.get(reverse("vault:home")), 'data-value="new"')  # Question answered.

    def test_web_not_this_card(self):
        self.upload_card("front", "Vaibhav Nagargoje", "mine.jpg")
        answer = self.upload_card("back", "", "back.jpg")
        self.assertContains(answer, "Not this card")
        back = Document.objects.get(side="back")
        self.assertContains(self.tap("unpair", back, label="Not this card"), "separated them")
        back.refresh_from_db()
        self.assertIsNone(back.card_group)

    def test_web_understands_the_same_commands(self):
        self.save_doc(self.member, title="CBSE Class 12 Marksheet")
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("402")) as ai:
            self.assertContains(self.client.post(reverse("vault:send"), {"text": "show my document list"}, headers=FETCH),
                                "Your documents (1")
            self.assertContains(self.client.post(reverse("vault:send"), {"text": "kitne documents hai"}, headers=FETCH),
                                "You have 1 document saved")
            self.assertContains(self.client.post(reverse("vault:send"), {"text": "upgrde"}, headers=FETCH),
                                "on WhatsApp")
        ai.assert_not_called()

    def test_empty_upload_is_explained(self):
        self.assertContains(self.client.post(reverse("vault:upload"), {}, headers=FETCH), "choose a PDF, photo or Word file")

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


class FileSafetyTests(SimpleTestCase):
    """Huge or hostile files ("file bombs") are refused or drawn smaller, never decoded in full."""

    def test_huge_photos_are_refused_before_decoding(self):
        for width, height in ((20000, 20000), (9000, 9000)):
            with self.assertRaisesMessage(ConversionError, "too big"):
                conversions.open_image(png_header(width, height))

    def test_only_known_photo_formats_are_opened(self):
        buffer = BytesIO()
        Image.new("RGB", (50, 50)).save(buffer, "GIF")
        with self.assertRaisesMessage(ConversionError, "couldn't open"):
            conversions.open_image(buffer.getvalue())

    def test_giant_pdf_page_is_drawn_at_a_safe_size(self):
        giant = blank_pdf(size_pt=14400)  # 5 metres square: 900 megapixels at 150 dpi.
        pages, total = conversions.pdf_to_jpegs(giant)
        width, height = Image.open(BytesIO(pages[0])).size
        self.assertLessEqual(width * height, conversions.MAX_RENDER_PIXELS)
        self.assertEqual(total, 1)
        self.assertTrue(conversions.preview_jpeg(giant, conversions.PDF))

    def test_long_pdf_to_jpg_says_pages_were_left_out(self):
        prepared = conversions.prepare([FileData(blank_pdf(pages=7), conversions.PDF, "long.pdf")], SendOptions(fmt="jpg"))
        self.assertEqual(len(prepared.files), conversions.MAX_JPG_PAGES)
        self.assertIn("long.pdf has 7 pages; I sent the first 5", prepared.notes[0])

    def test_merge_has_limits(self):
        jpeg = FileData(make_jpeg(200, 200), conversions.JPEG, "a.jpg")
        with self.assertRaisesMessage(ConversionError, "up to 20 files"):
            conversions.merge_to_pdf([jpeg] * 21)
        long = FileData(blank_pdf(pages=150), conversions.PDF, "long.pdf")
        with self.assertRaisesMessage(ConversionError, "more than 200 pages"):
            conversions.merge_to_pdf([long, long])

    def test_sizes_that_cant_be_made_are_skipped_with_a_note(self):
        huge = SendOptions(width=10000, height=20000)
        self.assertEqual((huge.width, huge.height), (0, 0))
        self.assertFalse(huge.converts)
        self.assertIn("biggest I can make is 5000 × 5000 px", huge.notes[0])
        self.assertIn("both the width and the height", SendOptions(width=300).notes[0])
        self.assertIn("smallest size I can aim for is 5 KB", SendOptions(target_kb=2).notes[0])
        self.assertEqual(SendOptions(width=200, height=230, target_kb=50).notes, ())
        again = SendOptions(**asdict(huge))  # As stored in a link or a chat entry.
        self.assertEqual(again.notes, huge.notes)

    def test_prepare_passes_the_notes_on(self):
        photo = FileData(make_jpeg(400, 300), conversions.JPEG, "me.jpg")
        prepared = conversions.prepare([photo], SendOptions(width=10000, height=20000))
        self.assertEqual(prepared.files, [photo])
        self.assertEqual(prepared.operations, [])
        self.assertIn("biggest I can make", prepared.notes[0])

    def test_ai_reads_the_first_pages_and_the_last(self):
        picked = conversions.pdf_pick_pages(blank_pdf(pages=100), first=5, last=1)
        self.assertEqual(conversions.pdf_page_count(picked), 6)
        short = blank_pdf(pages=4)
        self.assertEqual(conversions.pdf_pick_pages(short, first=5, last=1), short)

    def test_real_type_comes_from_the_bytes(self):
        self.assertEqual(conversions.detect_mime(make_jpeg(100, 100), fallback="x"), conversions.JPEG)
        self.assertEqual(conversions.detect_mime(make_heic(), fallback="x"), conversions.HEIC)
        self.assertEqual(conversions.detect_mime(make_docx(), fallback="x"), conversions.DOCX)
        plain_zip = BytesIO()
        with zipfile.ZipFile(plain_zip, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
        self.assertEqual(conversions.detect_mime(plain_zip.getvalue(), fallback="x"), "x")


class WordFileTests(SimpleTestCase):
    def test_text_keeps_paragraphs_and_table_rows(self):
        data = make_docx(paragraphs=["Rent Agreement", "Between Asha and Ravi"], table=[["Rent", "12000"], ["Deposit", "50000"]])
        self.assertEqual(wordfile.text(data), "Rent Agreement\nBetween Asha and Ravi\nRent | 12000\nDeposit | 50000")

    def test_biggest_pictures_first_and_logos_skipped(self):
        logo = BytesIO()
        Image.new("RGB", (40, 40), "navy").save(logo, "JPEG")
        small_scan, big_scan = make_jpeg(400, 300), make_jpeg(1200, 900)
        found = wordfile.pictures(make_docx(pictures=[logo.getvalue(), small_scan, big_scan]))
        self.assertEqual(found, [big_scan, small_scan])

    def test_zip_bomb_is_refused(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            with archive.open("word/document.xml", "w") as part:
                for _ in range(101):
                    part.write(bytes(1024 * 1024))  # 101 MB of zeros packs into about 100 KB.
        self.assertLess(len(buffer.getvalue()), 200 * 1024)
        with self.assertRaisesMessage(ConversionError, "looks damaged"):
            wordfile.check(buffer.getvalue())

    def test_too_many_parts_is_refused(self):
        parts = {f"word/part{n}.xml": "<x/>" for n in range(wordfile.MAX_ENTRIES)}
        with self.assertRaisesMessage(ConversionError, "looks damaged"):
            wordfile.check(make_docx(extra_parts=parts))

    def test_macros_are_refused(self):
        with self.assertRaisesMessage(ConversionError, "macros"):
            wordfile.check(make_docx(extra_parts={"word/vbaProject.bin": b"macro"}))

    def test_xml_tricks_are_refused(self):
        trick = (
            '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
            f'<w:document {WORD_NS}><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p></w:body></w:document>'
        )
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", trick)
        with self.assertRaisesMessage(ConversionError, "looks damaged"):
            wordfile.text(buffer.getvalue())

    def test_word_files_are_sent_as_they_are(self):
        word = FileData(make_docx(), conversions.DOCX, "Resume.docx")
        pdf = FileData(make_pdf(), conversions.PDF, "Marks.pdf")
        prepared = conversions.prepare([word], SendOptions(fmt="pdf", target_kb=50))
        self.assertEqual(prepared.files, [word])
        self.assertEqual(prepared.operations, [])
        self.assertIn("Resume.docx is a Word file, so I sent it as it is", prepared.notes[0])
        self.assertIn("Save as", prepared.notes[0])

        merged = conversions.prepare([word, pdf], SendOptions(merge=True))
        self.assertEqual([f.name for f in merged.files], ["Marks.pdf", "Resume.docx"])  # Nothing to merge it with.


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


class FileTypeTests(KagazTestCase):
    def setUp(self):
        super().setUp()
        self.member = self.make_member()

    def save(self, data, mime="", filename="file", **ai):
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result(**ai)) as call:
            result = services.save_document(self.member, data, mime, filename=filename)
        return result.document, call

    def test_the_bytes_decide_the_type_not_the_claim(self):
        document, _ = self.save(make_jpeg(300, 200), "application/pdf")
        self.assertEqual(document.mime_type, conversions.JPEG)
        with self.assertRaises(services.RefusedFile):
            self.save(b"just some text", "image/jpeg", filename="photo.jpg")

    def test_unsupported_files_are_refused_with_a_helpful_message(self):
        sheet = BytesIO()
        with zipfile.ZipFile(sheet, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
        cases = [
            (sheet.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "marks.xlsx", "Excel"),
            (services.OLE_HEADER + bytes(100), "application/msword", "old.doc", "save it as .docx or PDF"),
            (b"hello", "text/plain", "notes.txt", "Word .docx"),
        ]
        for data, mime, filename, expected in cases:
            with self.subTest(filename), self.assertRaisesMessage(services.RefusedFile, expected):
                self.save(data, mime, filename=filename)
        self.assertFalse(Document.objects.exists())

    def test_too_big_files_are_refused_before_saving(self):
        with self.assertRaisesMessage(services.RefusedFile, "too big"):
            self.save(png_header(20000, 20000), "image/png")
        with self.assertRaisesMessage(services.RefusedFile, "201 pages"):
            self.save(blank_pdf(pages=201), "application/pdf")
        self.assertFalse(Document.objects.exists())

    def test_a_100_page_pdf_is_saved_and_the_ai_sees_6_pages(self):
        document, call = self.save(blank_pdf(pages=100), "application/pdf", filename="agreement.pdf")
        self.assertEqual(document.page_count, 100)
        sent_pdf, hints = call.call_args.kwargs["parts"]
        self.assertEqual(conversions.pdf_page_count(sent_pdf.data), 6)
        self.assertIn("The PDF has 100 pages", hints)

    def test_iphone_photo_is_saved_as_jpeg(self):
        document, _ = self.save(make_heic(), "image/heic", filename="IMG_0042.HEIC")
        self.assertEqual(document.mime_type, conversions.JPEG)
        self.assertEqual(conversions.detect_mime(document.read_file()), conversions.JPEG)
        self.assertTrue(document.download_name().endswith(".jpg"))

    def test_word_file_is_kept_as_it_is_and_read_as_text_and_pictures(self):
        original = make_docx(paragraphs=["Leave and Licence Agreement"], table=[["Rent", "12000"]],
                             pictures=[make_jpeg(900, 700)])
        document, call = self.save(original, conversions.DOCX, filename="rent.docx", category="agreement")
        self.assertEqual(document.read_file(), original)
        self.assertEqual(document.mime_type, conversions.DOCX)
        self.assertEqual(document.category, "agreement")
        self.assertTrue(document.download_name().endswith(".docx"))

        text, picture, hints = call.call_args.kwargs["parts"]
        self.assertIn("Rent | 12000", text)
        self.assertIsInstance(picture, File)
        self.assertIn("This is a Word file", hints)

        prepared = services.prepare_files(self.member, [document], SendOptions(fmt="pdf"))
        self.assertEqual(prepared.files[0].data, original)
        self.assertIn("Save as", prepared.notes[0])

    def test_word_file_with_macros_is_refused(self):
        with self.assertRaisesMessage(services.RefusedFile, "macros"):
            self.save(make_docx(extra_parts={"word/vbaProject.bin": b"x"}), conversions.DOCX)

    def test_a_reading_crash_never_leaves_it_processing(self):
        with mock.patch("vault.extraction.structured_call", side_effect=RuntimeError("bug")):
            document = services.save_document(self.member, make_pdf(), "application/pdf").document
        self.assertEqual(document.status, Document.Status.UNREADABLE)

    def test_new_kinds_of_documents(self):
        tax, _ = self.save(blank_pdf(pages=1), "application/pdf", category="tax", title="Form 16")
        id_card, _ = self.save(blank_pdf(pages=2), "application/pdf", category="id_card", title="Ayushman Card")
        bill, _ = self.save(blank_pdf(pages=3), "application/pdf", category="utility_bill", title="Light Bill", fields=[
            {"key": "consumer_number", "label": "Consumer number", "value": "170012345678", "sensitive": False},
        ])
        business, _ = self.save(blank_pdf(pages=4), "application/pdf", category="business", title="GST Certificate")
        self.assertTrue(tax.is_sensitive)
        self.assertTrue(id_card.is_sensitive)
        self.assertFalse(bill.is_sensitive)
        self.assertFalse(bill.is_field_protected(bill.get_field("consumer_number")))
        self.assertTrue(business.is_field_protected({"key": "gstin", "value": "27ABCDE1234F1Z5"}))

    def test_daily_upload_limit(self):
        limit = self.member.limits()["uploads_per_day"]
        self.assertTrue(all(self.member.use_upload() for _ in range(limit)))
        self.assertFalse(self.member.use_upload())

    def test_web_upload_refuses_excel_and_keeps_word(self):
        self.sign_in(self.member)
        sheet = BytesIO()
        with zipfile.ZipFile(sheet, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
        upload = SimpleUploadedFile("marks.xlsx", sheet.getvalue(), content_type="application/vnd.ms-excel")
        answer = self.client.post(reverse("vault:upload"), {"file": upload}, headers=FETCH)
        self.assertContains(answer, "Excel or PowerPoint")

        upload = SimpleUploadedFile("Resume.docx", make_docx(), content_type="application/octet-stream")
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result(category="other", title="Resume")):
            answer = self.client.post(reverse("vault:upload"), {"file": upload}, headers=FETCH)
        self.assertContains(answer, "DOCX")
        document = self.member.documents.get()
        self.assertContains(self.client.get(reverse("vault:document", args=[document.pk])), "This is a Word file")


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

    def test_same_id_number_pairs_even_with_several_fronts(self):
        number = lambda value: [{"key": "aadhaar_number", "label": "Aadhaar number", "value": value, "sensitive": True}]
        mine = self.save_doc(self.member, data=make_jpeg(800, 500, text="f1"), mime="image/jpeg", category="aadhaar",
                             title="Aadhaar Card", side="front", fields=number("1234 5678 9012"))
        self.save_doc(self.member, data=make_jpeg(800, 500, text="f2"), mime="image/jpeg", category="aadhaar",
                      title="Aadhaar Card", side="front", holder_name="Ravi Verma", fields=number("5555 6666 7777"))
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result(
                category="aadhaar", title="Aadhaar Card", side="back", holder_name="", fields=number("XXXX XXXX 9012"))):
            result = services.save_document(self.member, make_jpeg(800, 500, text="b"), "image/jpeg")
        self.assertEqual(result.read.partner, mine)
        self.assertTrue(result.read.sure)

    def test_a_different_id_number_is_never_joined(self):
        number = lambda value: [{"key": "aadhaar_number", "label": "Aadhaar number", "value": value, "sensitive": True}]
        front = self.save_doc(self.member, data=make_jpeg(800, 500, text="f"), mime="image/jpeg", category="aadhaar",
                              title="Aadhaar Card", side="front", fields=number("1234 5678 9012"))
        self.save_doc(self.member, data=make_jpeg(800, 500, text="b"), mime="image/jpeg", category="aadhaar",
                      title="Aadhaar Card", side="back", holder_name="", fields=number("XXXX XXXX 4444"))
        front.refresh_from_db()
        self.assertIsNone(front.card_group)

    def test_only_candidate_is_joined_but_not_sure(self):
        self.card("front")
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result(
                category="aadhaar", title="Aadhaar Card", side="back", holder_name="")):
            result = services.save_document(self.member, make_jpeg(800, 500, text="b"), "image/jpeg")
        self.assertIsNotNone(result.read.partner)
        self.assertFalse(result.read.sure)

    def test_two_possible_fronts_become_choices(self):
        self.card("front", holder="Asha Verma")
        self.card("front", holder="Ravi Verma")
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result(
                category="aadhaar", title="Aadhaar Card", side="back", holder_name="")):
            result = services.save_document(self.member, make_jpeg(800, 500, text="b"), "image/jpeg")
        self.assertIsNone(result.read.partner)
        self.assertEqual(len(result.read.choices), 2)

    def test_pairing_rules_for_the_owners_choice(self):
        front = self.card("front", category="pan", title="PAN Card")
        back = self.card("back", category="aadhaar", title="Aadhaar Card", holder="")
        self.assertIsNone(services.pair_documents(self.member, back.pk, front.pk))  # Different kinds of card.
        aadhaar_front = self.card("front")  # Joined with the only possible back, but not sure.
        self.assertIsNone(services.pair_documents(self.member, back.pk, aadhaar_front.pk))  # Already joined.
        _, former = services.unpair(self.member, back.pk)
        self.assertEqual(former, aadhaar_front)
        self.assertEqual(services.pair_documents(self.member, back.pk, aadhaar_front.pk), aadhaar_front)

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

    def test_documents_that_gave_up_can_be_read_again(self):
        Document.objects.filter(pk=self.document.pk).update(read_attempts=len(services.REREAD_DELAYS_MINUTES))
        self.make_due()
        with mock.patch("vault.extraction.structured_call", side_effect=LLMError("402")):
            services.reread_due_documents()
        self.assertEqual(Document.objects.get(pk=self.document.pk).status, Document.Status.UNREADABLE)

        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()):
            self.assertEqual(services.reread_due_documents(now=True), [])  # Only with the flag.
            done = services.reread_due_documents(now=True, include_failed=True)
        self.assertEqual(len(done), 1)
        self.assertEqual(Document.objects.get(pk=self.document.pk).status, Document.Status.READY)

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
