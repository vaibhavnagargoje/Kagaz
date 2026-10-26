from types import SimpleNamespace
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from google.genai import errors as gemini_errors

from core import crypto, llm
from core.testing import TEST_MASTER_KEY, KagazTestCase

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


@override_settings(AI_PROVIDER="gemini", AI_MODEL="gemini-test", GEMINI_API_KEY="test-key")
class GeminiTests(SimpleTestCase):
    def fake_client(self, **behaviour):
        client = mock.Mock()
        client.models.generate_content = mock.Mock(**behaviour)
        return mock.patch("core.llm._gemini_client", return_value=client), client

    def test_sends_file_text_and_schema_and_parses_json(self):
        patcher, client = self.fake_client(return_value=SimpleNamespace(text='{"ok": true}'))
        with patcher:
            result = llm.structured_call(
                system="Be brief.", parts=[llm.File(b"%PDF-1.4", "application/pdf"), "Read this."], schema=SCHEMA
            )
        self.assertEqual(result, {"ok": True})
        call = client.models.generate_content.call_args.kwargs
        self.assertEqual(call["model"], "gemini-test")
        self.assertEqual(call["contents"][0].inline_data.mime_type, "application/pdf")
        self.assertEqual(call["contents"][1].text, "Read this.")
        self.assertEqual(call["config"].response_json_schema, SCHEMA)
        self.assertEqual(call["config"].system_instruction, "Be brief.")

    def test_daily_limit_gives_clear_error(self):
        error = gemini_errors.ClientError(429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}})
        patcher, _ = self.fake_client(side_effect=error)
        with patcher, self.assertRaisesMessage(llm.LLMError, "usage limit"):
            llm.structured_call(system="", parts=["hi"], schema=SCHEMA)

    def test_used_up_credits_say_so(self):
        error = gemini_errors.ClientError(402, {"error": {"code": 402, "message": "Your prepayment credits are depleted."}})
        patcher, _ = self.fake_client(side_effect=error)
        with patcher, self.assertLogs("core.llm", "ERROR") as logs, self.assertRaisesMessage(llm.LLMError, "credits"):
            llm.structured_call(system="", parts=["hi"], schema=SCHEMA)
        self.assertIn("aistudio.google.com/billing", logs.output[0])

    def test_blocked_answer_is_an_error(self):
        patcher, _ = self.fake_client(return_value=SimpleNamespace(text=None))
        with patcher, self.assertRaises(llm.LLMError):
            llm.structured_call(system="", parts=["hi"], schema=SCHEMA)

    @override_settings(GEMINI_API_KEY="")
    def test_missing_key_is_reported(self):
        with self.assertRaisesMessage(llm.LLMError, "GEMINI_API_KEY"):
            llm.structured_call(system="", parts=["hi"], schema=SCHEMA)


@override_settings(AI_PROVIDER="claude", AI_MODEL="claude-haiku-5-5")
class ClaudeTests(SimpleTestCase):
    def test_builds_blocks_and_parses_json(self):
        client = mock.Mock()
        client.messages.create.return_value = SimpleNamespace(
            stop_reason="end_turn", content=[SimpleNamespace(type="text", text='{"ok": false}')]
        )
        with mock.patch("core.llm._claude_client", return_value=client):
            result = llm.structured_call(system="s", parts=[llm.File(b"jpg", "image/jpeg"), "hi"], schema=SCHEMA)
        self.assertEqual(result, {"ok": False})
        blocks = client.messages.create.call_args.kwargs["messages"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["image", "text"])

    @override_settings(AI_PROVIDER="openai")
    def test_unknown_provider(self):
        with self.assertRaises(ImproperlyConfigured):
            llm.structured_call(system="", parts=["hi"], schema=SCHEMA)


@override_settings(KAGAZ_MASTER_KEY=TEST_MASTER_KEY)
class CryptoTests(SimpleTestCase):
    def test_round_trip(self):
        key = crypto.new_data_key()
        blob = crypto.encrypt(key, b"PAN ABCDE1234F", aad=b"doc-1")
        self.assertNotIn(b"ABCDE1234F", blob)
        self.assertEqual(crypto.decrypt(key, blob, aad=b"doc-1"), b"PAN ABCDE1234F")

    def test_tampering_is_detected(self):
        key = crypto.new_data_key()
        blob = bytearray(crypto.encrypt(key, b"secret"))
        blob[-1] ^= 1
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt(key, bytes(blob))

    def test_blob_is_bound_to_its_document(self):
        key = crypto.new_data_key()
        blob = crypto.encrypt(key, b"secret", aad=b"doc-1")
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt(key, blob, aad=b"doc-2")

    def test_wrong_key_fails(self):
        blob = crypto.encrypt(crypto.new_data_key(), b"secret")
        with self.assertRaises(crypto.DecryptionError):
            crypto.decrypt(crypto.new_data_key(), blob)

    @override_settings(KAGAZ_MASTER_KEY="")
    def test_missing_master_key_is_a_clear_error(self):
        with self.assertRaises(ImproperlyConfigured):
            crypto.master_key()

    def test_keyed_hash_depends_on_key(self):
        data = b"same file"
        self.assertNotEqual(
            crypto.keyed_hash(crypto.new_data_key(), data), crypto.keyed_hash(crypto.new_data_key(), data)
        )


class MemberKeyTests(KagazTestCase):
    def test_each_member_gets_their_own_wrapped_key(self):
        first = self.make_member(wa_id="911111111111")
        second = self.make_member(wa_id="912222222222")
        self.assertEqual(len(first.data_key()), 32)
        self.assertNotEqual(first.data_key(), second.data_key())
        self.assertNotIn(first.data_key(), bytes(first.wrapped_key))

    def test_key_survives_reload(self):
        member = self.make_member()
        key = member.data_key()
        member.refresh_from_db()
        del member._data_key
        self.assertEqual(member.data_key(), key)


@override_settings(
    BUSINESS_NAME="Darkbasalt Labs", BUSINESS_ADDRESS="Karmala, Maharashtra", SUPPORT_EMAIL="hello@example.com",
    WHATSAPP_DISPLAY_NUMBER="",
)
class SitePageTests(KagazTestCase):
    def test_home_shows_prices_from_the_admin(self):
        from billing.models import Product

        Product.objects.filter(code="plus_year").update(price=349)
        response = self.client.get("/")
        self.assertContains(response, "₹349 <span>a year</span>", html=False)
        self.assertContains(response, "₹49 <span>a month</span>", html=False)
        self.assertContains(response, "10 conversions")
        self.assertNotContains(response, "Coming soon")

    def test_policy_pages(self):
        for url, text in (
            ("/terms/", "Terms &amp; Conditions"),
            ("/refunds/", "Refunds within 7 days"),
            ("/privacy/", "Privacy policy"),
            ("/contact/", "Karmala, Maharashtra"),
        ):
            response = self.client.get(url)
            self.assertContains(response, text, msg_prefix=url)
            self.assertContains(response, "Darkbasalt Labs", msg_prefix=url)  # Footer, on every page.