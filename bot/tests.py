import hashlib
import hmac
import json
from datetime import timedelta
from unittest import mock

from django.urls import reverse
from django.utils import timezone

from accounts.models import PLAN_LIMITS, Member
from bot import replies
from bot.models import InboundMessage
from core.llm import LLMError
from core.testing import KagazTestCase, extraction_result, intent_result, make_docx, make_jpeg, make_pdf
from vault.models import Conversion, Document

WA_ID = "919999999999"


def signed(body: bytes) -> str:
    return "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()


class WebhookTests(KagazTestCase):
    url = "/webhooks/whatsapp/"

    def payload(self, message_id="wamid.1"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": "123"},
                "contacts": [{"wa_id": WA_ID, "profile": {"name": "Asha"}}],
                "messages": [{"from": WA_ID, "id": message_id, "type": "text", "text": {"body": "hi"}}],
            }}]}],
        }

    def post(self, payload, signature=None):
        body = json.dumps(payload).encode()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                self.url, body, content_type="application/json",
                headers={"X-Hub-Signature-256": signature or signed(body)},
            )

    def test_subscription_check(self):
        ok = self.client.get(self.url, {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "42"})
        self.assertEqual(ok.content, b"42")
        bad = self.client.get(self.url, {"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "42"})
        self.assertEqual(bad.status_code, 403)

    def test_bad_signature_is_rejected(self):
        response = self.post(self.payload(), signature="sha256=deadbeef")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(InboundMessage.objects.exists())

    def test_meta_retries_are_processed_once(self):
        self.post(self.payload())
        self.post(self.payload())
        self.assertEqual(InboundMessage.objects.count(), 1)
        self.assertEqual(len(self.sent("buttons")), 1)  # One welcome / consent prompt.
        self.assertEqual(Member.objects.get(wa_id=WA_ID).display_name, "Asha")

    def test_payload_is_cleared_after_processing(self):
        self.post(self.payload())
        self.assertEqual(InboundMessage.objects.get().payload, {})


class ConversationTests(KagazTestCase):
    def save_file(self, member, data=None, mime="application/pdf", filename="file.pdf", **ai):
        path = self.write_temp_file(data or make_pdf(), filename)
        with mock.patch("vault.extraction.structured_call", return_value=extraction_result(**ai)):
            self.receive_file(member.wa_id, path, mime, filename)
        return member.documents.order_by("-created_at").first()

    def ask(self, member, text, **intent):
        with mock.patch("bot.intent.structured_call", return_value=intent_result(**intent)) as ai:
            self.receive_text(member.wa_id, text)
        return ai

    def test_documents_wait_for_consent_then_get_saved(self):
        member = self.make_member(consented=False)
        path = self.write_temp_file(make_pdf(), "marks.pdf")
        self.receive_file(member.wa_id, path, "application/pdf", "marks.pdf")
        self.assertIn("I agree", str(self.sent("buttons")[-1]["buttons"]))
        self.assertFalse(Document.objects.exists())

        with mock.patch("vault.extraction.structured_call", return_value=extraction_result()):
            self.receive_tap(member.wa_id, "consent:yes")
        self.assertEqual(Document.objects.count(), 1)
        self.assertIn("Saved: *CBSE Class 10 Marksheet*", self.last_text())

    def test_text_before_consent_always_gets_a_reply(self):
        member = self.make_member(consented=False)
        path = self.write_temp_file(make_pdf(), "cert.pdf")
        self.receive_file(member.wa_id, path, "application/pdf", "cert.pdf")
        self.assertIn("received your file", self.sent("buttons")[-1]["body"])

        self.receive_text(member.wa_id, "what is my roll no")
        self.assertEqual(len(self.sent("buttons")), 2)
        reminder = self.sent("buttons")[-1]["body"]
        self.assertIn("tap *I agree* first", reminder)
        self.assertIn("received your file", reminder)

    def test_many_files_before_consent_ask_only_once(self):
        member = self.make_member(consented=False)
        for n in range(3):
            path = self.write_temp_file(make_pdf(pages=n + 1), f"f{n}.pdf")
            self.receive_file(member.wa_id, path, "application/pdf", f"f{n}.pdf")
        self.assertEqual(len(self.sent("buttons")), 1)

    def test_saving_replies_with_what_was_found(self):
        member = self.make_member()
        self.save_file(member)
        reply = self.last_text()
        self.assertIn("CBSE Class 10 Marksheet", reply)
        self.assertIn("Roll number", reply)
        self.assertNotIn("1234567", reply)  # Values are never echoed when saving.

    def test_send_document_compressed_to_size(self):
        member = self.make_member()
        document = self.save_file(member, data=make_pdf(pages=2))
        self.ask(member, "10th marksheet under 150kb", target_kb=150)
        sent = self.sent("document")[-1]
        self.assertLessEqual(len(sent["data"]), 150 * 1024)
        self.assertTrue(sent["filename"].startswith("CBSE_Class_10_Marksheet"))
        self.assertEqual(Conversion.objects.get().operations, ["compress"])
        self.assertEqual(document.member, member)

    def test_photo_resized_for_exam_form(self):
        member = self.make_member()
        self.save_file(member, data=make_jpeg(), mime="image/jpeg", filename="me.jpg", category="photo", title="My photo")
        self.ask(member, "photo 200x230 under 50kb", width_px=200, height_px=230, target_kb=50)
        sent = self.sent("document")[-1]
        self.assertEqual(sent["mime"], "image/jpeg")
        self.assertLessEqual(len(sent["data"]), 50 * 1024)

    def test_size_too_big_to_make_is_explained(self):
        member = self.make_member()
        self.save_file(member, data=make_jpeg(), mime="image/jpeg", filename="me.jpg", category="photo", title="My photo")
        self.ask(member, "my photo 10000x20000 px", width_px=10000, height_px=20000)
        sent = self.sent("document")[-1]
        self.assertEqual(sent["data"], member.documents.get().read_file())  # The original, not resized.
        self.assertIn("biggest I can make is 5000 × 5000 px", self.last_text())
        self.assertFalse(Conversion.objects.exists())

    def test_file_type_comes_from_the_bytes(self):
        member = self.make_member()
        document = self.save_file(member, data=make_jpeg(), mime="application/pdf", filename="scan.pdf")
        self.assertEqual(document.mime_type, "image/jpeg")

    def test_excel_file_is_refused_politely(self):
        member = self.make_member()
        path = self.write_temp_file(b"PK\x03\x04 not really a workbook", "marks.xlsx")
        self.receive_file(member.wa_id, path, "application/vnd.ms-excel", "marks.xlsx")
        self.assertIn("Excel or PowerPoint", self.last_text())
        self.assertFalse(Document.objects.exists())

    def test_word_file_asked_as_pdf_is_sent_as_it_is(self):
        member = self.make_member()
        original = make_docx(paragraphs=["Asha Verma", "Curriculum Vitae"])
        self.save_file(member, data=original, mime="application/octet-stream", filename="Resume.docx",
                       category="other", title="Asha Verma Resume")
        self.ask(member, "send my resume as pdf", output_format="pdf")
        sent = self.sent("document")[-1]
        self.assertEqual(sent["data"], original)
        self.assertTrue(sent["filename"].endswith(".docx"))
        self.assertIn("Save as", self.last_text())

    def test_daily_upload_limit_on_whatsapp(self):
        member = self.make_member()
        for _ in range(PLAN_LIMITS["free"]["uploads_per_day"]):
            member.use_upload()
        self.save_file(member)
        self.assertIn("files today", self.last_text())
        self.assertFalse(Document.objects.exists())

    def test_sensitive_document_needs_pin_link(self):
        member = self.make_member()
        self.save_file(member, category="pan", title="PAN Card")
        self.ask(member, "send pan card")
        self.assertEqual(self.sent("document"), [])
        self.assertIn("https://kagaz.test/k/", self.last_text())
        self.assertIn("PAN Card", self.last_text())

    def test_protected_document_never_sent_in_chat_even_right_after_pin(self):
        member = self.make_member()
        self.save_file(member, category="aadhaar", title="Aadhaar Card")
        member.set_pin("4826")
        member.unlock()
        self.ask(member, "send my aadhaar")
        self.assertEqual(self.sent("document"), [])
        self.assertIn("/k/", self.last_text())

    def test_ask_for_a_number_sends_it_with_the_document(self):
        member = self.make_member()
        self.save_file(member)
        self.ask(member, "10th roll no?", intent="ask_field", field_key="roll_number")
        texts = [m["body"] for m in self.sent("text")]
        self.assertEqual(texts[-1], "1234567")
        self.assertIn("Roll number", texts[-2])
        source = self.sent("document")[-1]
        self.assertTrue(source["filename"].startswith("CBSE_Class_10_Marksheet"))
        self.assertIn("double-check", source["caption"])

    def test_id_number_on_an_ordinary_document_is_protected(self):
        """An Aadhaar number printed on an income certificate still never goes in chat."""
        member = self.make_member()
        self.save_file(
            member, category="certificate", title="Income Certificate",
            fields=[{"key": "aadhaar_number", "label": "Aadhaar number", "value": "1234 5678 9012", "sensitive": False}],
        )
        self.ask(member, "aadhaar number?", intent="ask_field", field_key="aadhaar_number")
        self.assertNotIn("1234 5678 9012", json.dumps([m.get("body") for m in self.sent("text")]))
        self.assertEqual(self.sent("document"), [])
        self.assertIn("/k/", self.last_text())
        self.assertIn("Aadhaar number", self.last_text())

    def test_web_command_sends_a_sign_in_link(self):
        member = self.make_member()
        self.receive_text(member.wa_id, "web")
        self.assertIn("https://kagaz.test/k/", self.last_text())
        self.assertTrue(member.access_links.filter(purpose="login").exists())

    def test_ai_down_still_finds_documents_by_name(self):
        member = self.make_member()
        self.save_file(member, title="CBSE Class 12 Marksheet", keywords=["12th"])
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("quota")):
            self.receive_text(member.wa_id, "mera 12th marksheet bhejo")
        self.assertTrue(self.sent("document")[-1]["filename"].startswith("CBSE_Class_12_Marksheet"))

    def test_ai_down_never_guesses_a_deletion(self):
        member = self.make_member()
        self.save_file(member, title="CBSE Class 12 Marksheet")
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("quota")):
            self.receive_text(member.wa_id, "delete 12th marksheet")
        self.assertEqual(Document.objects.count(), 1)
        self.assertEqual(self.sent("document"), [])

    def test_bot_never_asks_for_id_documents(self):
        """WhatsApp's policy: businesses must not ask people to share ID numbers."""
        for text in (replies.welcome(), replies.help_text(), replies.CONSENT_GIVEN):
            self.assertNotRegex(text.lower(), r"\bpan\b|aadhaar|passport")

    def test_several_matches_ask_which_one(self):
        member = self.make_member()
        self.save_file(member, data=make_pdf(), title="Class 10 Marksheet")
        self.save_file(member, data=make_pdf(pages=2), title="Class 12 Marksheet")
        self.ask(member, "marksheet under 200kb", refs=[1, 2], target_kb=200)
        rows = self.sent("list")[-1]["rows"]
        self.assertEqual(len(rows), 2)

        self.receive_tap(member.wa_id, rows[1][0], kind="list_reply")
        sent = self.sent("document")[-1]
        self.assertLessEqual(len(sent["data"]), 200 * 1024)

    def test_merge_into_one_pdf(self):
        member = self.make_member()
        self.save_file(member, data=make_pdf(), title="Class 10 Marksheet")
        self.save_file(member, data=make_pdf(), title="Class 12 Marksheet")
        self.ask(member, "10th and 12th in one pdf", refs=[1, 2], merge_into_one_pdf=True)
        self.assertEqual(self.sent("document")[-1]["filename"], "Kagaz_combined.pdf")

    def test_ai_cannot_point_at_unknown_documents(self):
        member = self.make_member()
        self.save_file(member)
        other = self.make_member(wa_id="918888888888")
        self.save_file(other, data=make_pdf(pages=2))
        self.ask(member, "send it", refs=[7, 99])
        self.assertEqual(self.sent("document"), [])
        self.assertIn("couldn't find", self.last_text())

    def test_commands_do_not_call_the_ai(self):
        member = self.make_member()
        self.save_file(member)
        with mock.patch("bot.intent.structured_call") as ai:
            self.receive_text(member.wa_id, "help")
            self.receive_text(member.wa_id, "LIST")
        ai.assert_not_called()
        self.assertIn("CBSE Class 10 Marksheet", self.last_text())

    def test_everyday_wording_and_typos_work_without_the_ai(self):
        member = self.make_member()
        self.save_file(member)
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("402")) as ai:
            self.receive_text(member.wa_id, "show my document list")
            self.assertIn("*Your documents* (1)", self.last_text())
            self.receive_text(member.wa_id, "how many document do you have ?")
            self.assertIn("You have 1 document saved", self.last_text())
            self.receive_text(member.wa_id, "lsit")
            self.assertIn("*Your documents* (1)", self.last_text())
            self.receive_text(member.wa_id, "kitne documents hai")
            self.assertIn("You have 1 document saved", self.last_text())
            self.receive_text(member.wa_id, "upgrde")
            self.assertIn("Kagaz Plus", self.sent("buttons")[-1]["body"])
        ai.assert_not_called()
        self.assertEqual(self.sent("document"), [])  # Nothing random sent.

    def test_erasing_everything_needs_the_exact_words(self):
        member = self.make_member()
        self.save_file(member)
        with mock.patch("bot.intent.structured_call", return_value=intent_result(intent="other", refs=[], reply="?")):
            self.receive_text(member.wa_id, "delete my dta")
        self.assertNotIn("Yes, erase all", str(self.sent("buttons")))

    def test_hi_sends_help_with_the_menu_and_a_tap_runs_it(self):
        member = self.make_member()
        self.save_file(member)
        self.receive_text(member.wa_id, "hi")
        menu = self.sent("list")[-1]
        self.assertIn("in your own words", menu["body"])
        self.assertIn(("menu:list", "📄 My documents", "See everything you've saved"), menu["rows"])

        self.receive_tap(member.wa_id, "menu:list", kind="list_reply")
        self.assertIn("*Your documents* (1)", self.last_text())
        self.receive_tap(member.wa_id, "menu:howto", kind="list_reply")
        self.assertIn("What you can ask", self.last_text())

    def test_menu_ids_cannot_reach_other_commands(self):
        member = self.make_member()
        self.save_file(member)
        self.receive_tap(member.wa_id, "menu:wipe", kind="list_reply")
        self.assertNotIn("Yes, erase all", str(self.sent("buttons")))
        self.assertTrue(Member.objects.exists())

    def test_ai_understands_app_requests_too(self):
        member = self.make_member()
        self.save_file(member)
        self.ask(member, "plus kitne ka hai bhai, mujhe lena hai", intent="upgrade", refs=[])
        self.assertIn("Kagaz Plus", self.sent("buttons")[-1]["body"])
        self.ask(member, "how much stuff have I stored with you so far", intent="count_documents", refs=[])
        self.assertIn("You have 1 document saved", self.last_text())

    def test_a_failed_ai_call_does_not_use_up_a_question(self):
        member = self.make_member()
        self.save_file(member)
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("402")):
            self.receive_text(member.wa_id, "mera 10th wala marksheet bhejo please")
        self.assertEqual(member.questions_used_today(), 0)

    def test_ai_down_asks_instead_of_guessing_on_a_close_call(self):
        member = self.make_member()
        self.save_file(member, data=make_pdf(), title="Class 10 Marksheet", keywords=[])
        self.save_file(member, data=make_pdf(pages=2), title="Class 12 Marksheet", keywords=[])
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("402")):
            self.receive_text(member.wa_id, "marksheet bhejo")
        self.assertEqual(self.sent("document"), [])
        self.assertEqual(len(self.sent("list")[-1]["rows"]), 2)

    def test_ai_down_finds_a_typo(self):
        member = self.make_member()
        self.save_file(member, title="Passport Size Photo", category="photo", keywords=[])
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("402")):
            self.receive_text(member.wa_id, "pasport photo")
        self.assertTrue(self.sent("document")[-1]["filename"].startswith("Passport_Size_Photo"))

    def test_ai_down_with_no_match_offers_the_menu(self):
        member = self.make_member()
        self.save_file(member)
        with mock.patch("bot.intent.structured_call", side_effect=LLMError("402")):
            self.receive_text(member.wa_id, "electricity bill from last year")
        self.assertEqual(self.sent("document"), [])
        self.assertIn("tap *Menu*", self.sent("list")[-1]["body"])

    def test_a_promo_code_typed_alone_is_applied(self):
        from billing.models import PromoCode

        member = self.make_member()
        PromoCode.objects.create(code="SAVE20", kind=PromoCode.Kind.PERCENT, value=20)
        self.receive_text(member.wa_id, "save20")
        member.refresh_from_db()
        self.assertEqual(member.pending_promo_code, "SAVE20")
        self.assertIn("Code *SAVE20* applied", self.sent("buttons")[-1]["body"])

    def test_off_topic_message_gets_polite_reply(self):
        member = self.make_member()
        self.save_file(member)
        self.ask(member, "write my essay", intent="other", refs=[], reply="I can only help with your documents.")
        self.assertEqual(self.last_text(), "I can only help with your documents.")

    def test_delete_one_document(self):
        member = self.make_member()
        document = self.save_file(member)
        self.ask(member, "delete marksheet", intent="delete_document")
        self.receive_tap(member.wa_id, f"del:{document.pk}")
        self.assertFalse(Document.objects.exists())

    def test_wipe_without_pin_erases_everything(self):
        member = self.make_member()
        self.save_file(member)
        self.receive_text(member.wa_id, "DELETE MY DATA")
        self.receive_tap(member.wa_id, "wipe:yes")
        self.assertFalse(Member.objects.exists())
        self.assertFalse(Document.objects.exists())
        self.assertFalse(InboundMessage.objects.exists())
        self.assertIn("erased", self.last_text())

    def test_wipe_with_pin_asks_for_pin(self):
        member = self.make_member()
        member.set_pin("4826")
        self.receive_tap(member.wa_id, "wipe:yes")
        self.assertTrue(Member.objects.exists())
        self.assertIn("/k/", self.last_text())

    def test_free_plan_document_limit(self):
        member = self.make_member()
        with mock.patch.dict(PLAN_LIMITS["free"], {"documents": 1}):
            self.save_file(member)
            self.save_file(member, data=make_pdf(pages=2))
        self.assertEqual(member.documents.count(), 1)
        self.assertIn("free plan", self.last_text())


class CardAndLabelConversationTests(KagazTestCase):
    def setUp(self):
        super().setUp()
        self.member = self.make_member()

    def save_card(self, side, category="vehicle", title="Vehicle RC", holder="Asha Verma"):
        path = self.write_temp_file(make_jpeg(800, 500, text=side), f"{side}.jpg")
        result = extraction_result(category=category, title=title, side=side, holder_name=holder)
        with mock.patch("vault.extraction.structured_call", return_value=result):
            self.receive_file(self.member.wa_id, path, "image/jpeg")
        return self.member.documents.order_by("-created_at").first()

    def ask(self, text, **intent):
        with mock.patch("bot.intent.structured_call", return_value=intent_result(**intent)):
            self.receive_text(self.member.wa_id, text)

    def test_second_side_is_matched_and_announced(self):
        self.save_card("front")
        self.assertIn("If it has a back side", self.last_text())
        self.save_card("back", holder="Asha Verma")
        self.assertIn("Matched with the front side", self.last_text())

    def test_only_possible_side_is_joined_with_an_undo_button(self):
        front = self.save_card("front")
        back = self.save_card("back", holder="")  # No name or number: it's only the one candidate.
        message = self.sent("buttons")[-1]
        self.assertIn("joined it with the front side of *Vehicle RC* (Asha Verma)", message["body"])
        self.assertEqual(message["buttons"], [(f"unpair:{back.pk}", "Not this card")])

        self.receive_tap(self.member.wa_id, f"unpair:{back.pk}")
        front.refresh_from_db()
        back.refresh_from_db()
        self.assertIsNone(front.card_group)
        self.assertIsNone(back.card_group)
        self.assertIn("separated them", self.last_text())

    def test_back_with_two_possible_fronts_asks_which_card(self):
        mine = self.save_card("front", category="pan", title="PAN Card", holder="Vaibhav Nagargoje")
        self.save_card("front", category="pan", title="PAN Card", holder="Sundardas Nagargoje")
        back = self.save_card("back", category="pan", title="PAN Card", holder="")
        question = self.sent("list")[-1]
        self.assertIn("Which card is this *back* side from?", question["body"])
        self.assertEqual(len(question["rows"]), 3)  # Two fronts + "None of these".
        self.assertIn("Vaibhav Nagargoje", str(question["rows"]))

        self.receive_tap(self.member.wa_id, f"pair:{back.pk}:{mine.pk}", kind="list_reply")
        mine.refresh_from_db()
        back.refresh_from_db()
        self.assertEqual(mine.card_group, back.card_group)
        self.assertIsNotNone(back.card_group)
        self.assertIn("are joined", self.last_text())

    def test_back_first_then_front_still_pairs(self):
        back = self.save_card("back", category="aadhaar", title="Aadhaar Card", holder="")
        self.assertIn("If it has a front side", self.last_text())
        front = self.save_card("front", category="aadhaar", title="Aadhaar Card")
        front.refresh_from_db()
        back.refresh_from_db()
        self.assertEqual(front.card_group, back.card_group)
        self.assertIsNotNone(front.card_group)

    def test_none_of_these_keeps_it_separate(self):
        self.save_card("front", category="pan", title="PAN Card", holder="Vaibhav Nagargoje")
        self.save_card("front", category="pan", title="PAN Card", holder="Sundardas Nagargoje")
        back = self.save_card("back", category="pan", title="PAN Card", holder="")
        self.receive_tap(self.member.wa_id, f"pair:{back.pk}:new", kind="list_reply")
        back.refresh_from_db()
        self.assertIsNone(back.card_group)
        self.assertIn("kept as a separate card", self.last_text())

    def test_cannot_pair_with_someone_elses_document(self):
        other = self.make_member(wa_id="918888888888")
        theirs = self.save_doc(other, data=make_jpeg(800, 500, text="theirs"), mime="image/jpeg",
                               category="pan", title="PAN Card", side="front")
        back = self.save_card("back", category="pan", title="PAN Card", holder="")
        self.receive_tap(self.member.wa_id, f"pair:{back.pk}:{theirs.pk}", kind="list_reply")
        back.refresh_from_db()
        self.assertIsNone(back.card_group)

    def test_card_comes_as_one_page_with_both_sides(self):
        self.save_card("front")
        self.save_card("back", holder="")
        self.ask("send my rc", refs=[1, 2])  # The AI may pick both sides: still one card.
        self.assertEqual(self.sent("list"), [])
        sent = self.sent("document")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]["filename"].endswith("_both_sides.pdf"))

    def test_just_one_side_when_asked(self):
        self.save_card("front")
        back = self.save_card("back", holder="")
        self.ask("rc back side", refs=[2], side="back")
        sent = self.sent("document")[-1]
        self.assertEqual(sent["data"], back.read_file())

    def test_protected_card_link_covers_both_sides(self):
        self.save_card("front", category="aadhaar", title="Aadhaar Card")
        self.save_card("back", category="aadhaar", title="Aadhaar Card", holder="")
        self.ask("send my aadhaar", refs=[1])
        self.assertEqual(self.sent("document"), [])
        self.assertIn("*Aadhaar Card* is protected", self.last_text())  # Named once, not twice.
        action = self.member.access_links.get().action
        self.assertEqual(len(action["doc_ids"]), 2)

    def test_unclear_document_asks_what_it_is(self):
        path = self.write_temp_file(make_jpeg(), "blurry.jpg")
        result = extraction_result(recognised=False, category="other", title="")
        with mock.patch("vault.extraction.structured_call", return_value=result):
            self.receive_file(self.member.wa_id, path, "image/jpeg")
        question = self.sent("list")[-1]
        self.assertIn("too unclear", question["body"])
        self.assertEqual(len(question["rows"]), 10)

        document = self.member.documents.get()
        self.receive_tap(self.member.wa_id, f"label:{document.pk}:pan", kind="list_reply")
        document.refresh_from_db()
        self.assertEqual(document.category, "pan")
        self.assertTrue(document.is_sensitive)
        self.assertIn("saved as *PAN card*", self.last_text())

    def test_busy_ai_also_offers_the_type_list(self):
        path = self.write_temp_file(make_pdf(), "scan.pdf")
        with mock.patch("vault.extraction.structured_call", side_effect=LLMError("busy")):
            self.receive_file(self.member.wa_id, path, "application/pdf", "scan.pdf")
        self.assertIn("read it again in a few minutes", self.sent("list")[-1]["body"])


class ForgotPinTests(KagazTestCase):
    def setUp(self):
        super().setUp()
        self.member = self.make_member()
        self.member.set_pin("4826")

    def start_reset(self):
        self.receive_text(self.member.wa_id, "forgot pin")
        self.assertIn("Start reset", str(self.sent("buttons")[-1]["buttons"]))
        self.receive_tap(self.member.wa_id, "pinreset:start")
        self.member.refresh_from_db()

    def test_reset_waits_24_hours(self):
        self.start_reset()
        self.assertIn("PIN reset started", self.last_text())
        hours = (self.member.pin_reset_ready_at - timezone.now()).total_seconds() / 3600
        self.assertAlmostEqual(hours, 24, delta=0.1)

        self.receive_text(self.member.wa_id, "pin")
        self.assertIn("will be ready on", self.last_text())
        self.assertFalse(self.member.access_links.exists())

    def test_after_the_wait_a_new_pin_can_be_set_without_the_old_one(self):
        self.start_reset()
        Member.objects.filter(pk=self.member.pk).update(pin_reset_ready_at=timezone.now() - timedelta(seconds=1))
        self.receive_text(self.member.wa_id, "pin")
        url = self.last_text().split("\n")[1]
        page = self.client.get(url.removeprefix("https://kagaz.test"))
        self.assertContains(page, "Create a new PIN")
        self.assertNotContains(page, "Current PIN")

        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(url.removeprefix("https://kagaz.test"), {"pin": "7391", "pin_confirm": "7391"})
        self.member.refresh_from_db()
        self.assertTrue(self.member.check_pin("7391"))
        self.assertFalse(self.member.pin_reset_pending)
        self.assertIn("new PIN is saved", self.last_text())

    def test_reset_works_even_when_locked_out(self):
        for _ in range(5):
            self.member.check_pin("0000")
        self.start_reset()
        Member.objects.filter(pk=self.member.pk).update(pin_reset_ready_at=timezone.now() - timedelta(seconds=1))
        self.receive_text(self.member.wa_id, "pin")
        url = self.last_text().split("\n")[1]
        self.assertEqual(self.client.get(url.removeprefix("https://kagaz.test")).status_code, 200)

    def test_cancel_reset(self):
        self.start_reset()
        self.receive_text(self.member.wa_id, "cancel reset")
        self.member.refresh_from_db()
        self.assertFalse(self.member.pin_reset_pending)
        self.assertIn("reset cancelled", self.last_text())

    def test_link_dies_if_the_reset_is_cancelled(self):
        self.start_reset()
        Member.objects.filter(pk=self.member.pk).update(pin_reset_ready_at=timezone.now() - timedelta(seconds=1))
        self.receive_text(self.member.wa_id, "pin")
        url = self.last_text().split("\n")[1]
        self.receive_text(self.member.wa_id, "cancel reset")
        response = self.client.get(url.removeprefix("https://kagaz.test"))
        self.assertContains(response, "isn't available", status_code=410)

    def test_using_the_pin_cancels_a_reset(self):
        self.start_reset()
        self.sign_in(self.member)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("accounts:unlock"), {"pin": "4826", "next": "/vault/"})
        self.member.refresh_from_db()
        self.assertFalse(self.member.pin_reset_pending)
        self.assertIn("reset you started has been cancelled", self.last_text())

    def test_forgot_without_a_pin(self):
        other = self.make_member(wa_id="918888888888")
        self.receive_text(other.wa_id, "forgot pin")
        self.assertIn("haven't set a PIN", self.last_text())
