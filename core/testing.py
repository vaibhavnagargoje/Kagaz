"""Shared helpers for tests."""

import base64
import io
import shutil
import tempfile
import time
import uuid

from django.core.cache import cache
from django.test import TestCase, override_settings
from huey.contrib.djhuey import HUEY
from PIL import Image, ImageDraw

from accounts.models import Member
from bot.handlers import handle_inbound
from bot.models import InboundMessage
from bot.whatsapp import get_client, reset_clients

TEST_MASTER_KEY = base64.b64encode(b"k" * 32).decode()


def make_jpeg(width=1200, height=1600, text="CBSE MARKSHEET") -> bytes:
    img = Image.effect_noise((width // 4, height // 4), 40).convert("RGB").resize((width, height))
    draw = ImageDraw.Draw(img)
    for y in range(40, height, 80):
        draw.text((40, y), f"{text} ROLL NO 1234567", fill=(0, 0, 0))
    buffer = io.BytesIO()
    img.save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def make_pdf(pages=1) -> bytes:
    from vault.conversions import FileData, merge_to_pdf

    images = [FileData(make_jpeg(text=f"PAGE {n}"), "image/jpeg", f"p{n}.jpg") for n in range(pages)]
    return merge_to_pdf(images)


def extraction_result(**overrides) -> dict:
    """A fake AI answer for vault.extraction."""
    result = {
        "is_document": True,
        "category": "marksheet",
        "title": "CBSE Class 10 Marksheet",
        "holder_name": "Asha Verma",
        "issuer": "CBSE",
        "issue_date": "2016-05-28",
        "expiry_date": "",
        "summary": "Class 10 marksheet.",
        "is_sensitive": False,
        "recognised": True,
        "side": "none",
        "keywords": ["10th", "matric"],
        "fields": [{"key": "roll_number", "label": "Roll number", "value": "1234567", "sensitive": False}],
    }
    result.update(overrides)
    return result


def intent_result(**overrides) -> dict:
    """A fake AI answer for bot.intent."""
    result = {
        "intent": "send_document",
        "refs": [1],
        "send_all": False,
        "merge_into_one_pdf": False,
        "field_key": "",
        "target_kb": 0,
        "output_format": "original",
        "width_px": 0,
        "height_px": 0,
        "side": "both",
        "reply": "",
    }
    result.update(overrides)
    return result


class KagazTestCase(TestCase):
    """Temp storage, a fixed master key, console WhatsApp, inline Huey and an in-memory cache."""

    @classmethod
    def setUpClass(cls):
        cls._media_root = tempfile.mkdtemp()
        cls._overrides = override_settings(
            MEDIA_ROOT=cls._media_root,
            KAGAZ_MASTER_KEY=TEST_MASTER_KEY,
            WHATSAPP_BACKEND="console",
            WHATSAPP_APP_SECRET="test-secret",
            WHATSAPP_VERIFY_TOKEN="verify-me",
            WHATSAPP_PHONE_NUMBER_ID="123",
            PUBLIC_BASE_URL="https://kagaz.test",
            SECURE_SSL_REDIRECT=False,
            CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
            RAZORPAY_KEY_ID="rzp_test_key",
            RAZORPAY_KEY_SECRET="rzp-test-secret",
            RAZORPAY_WEBHOOK_SECRET="rzp-webhook-secret",
        )
        cls._overrides.enable()
        cls._huey_was_immediate = HUEY.immediate
        HUEY.immediate = True
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        HUEY.immediate = cls._huey_was_immediate
        cls._overrides.disable()
        shutil.rmtree(cls._media_root, ignore_errors=True)

    def setUp(self):
        cache.clear()
        reset_clients()
        self.wa = get_client()
        self.wa.stream = io.StringIO()

    def make_member(self, consented=True, wa_id="919999999999") -> Member:
        member = Member.objects.create(wa_id=wa_id, display_name="Asha")
        if consented:
            member.give_consent()
        return member

    def sign_in(self, member: Member, unlocked: bool = False):
        """Sign the test browser (self.client) in to the web vault."""
        from accounts import sessions

        session = self.client.session
        session[sessions.MEMBER_KEY] = str(member.pk)
        session[sessions.LAST_SEEN_KEY] = int(time.time())
        session.save()
        if not member.has_pin:
            member.set_pin("4826")
        if unlocked:
            member.unlock()

    def save_doc(self, member: Member, data: bytes = None, mime: str = "application/pdf", **ai):
        """Save a document directly, with a fake AI reading."""
        from unittest import mock

        from vault import services

        with mock.patch("vault.extraction.structured_call", return_value=extraction_result(**ai)):
            return services.save_document(member, data or make_pdf(), mime, filename="file.pdf").document

    def receive(self, wa_id: str, message: dict) -> InboundMessage:
        """Simulate one incoming WhatsApp message and process it."""
        message = {"from": wa_id, "id": f"wamid.test.{uuid.uuid4().hex}", "timestamp": str(int(time.time())), **message}
        inbound = InboundMessage.objects.create(
            wa_message_id=message["id"], wa_id=wa_id, msg_type=message["type"], payload={"message": message}
        )
        with self.captureOnCommitCallbacks(execute=True):
            handle_inbound(inbound.pk)
        return inbound

    def receive_text(self, wa_id: str, text: str):
        return self.receive(wa_id, {"type": "text", "text": {"body": text}})

    def receive_tap(self, wa_id: str, option_id: str, kind="button_reply"):
        return self.receive(wa_id, {"type": "interactive", "interactive": {"type": kind, kind: {"id": option_id, "title": ""}}})

    def receive_file(self, wa_id: str, path: str, mime: str, filename: str = ""):
        kind = "image" if mime.startswith("image/") else "document"
        media = {"id": f"local:{path}", "mime_type": mime}
        if filename:
            media["filename"] = filename
        return self.receive(wa_id, {"type": kind, kind: media})

    def write_temp_file(self, data: bytes, name: str) -> str:
        path = f"{self._media_root}/{uuid.uuid4().hex}-{name}"
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def sent(self, kind=None) -> list[dict]:
        return [m for m in self.wa.sent if kind is None or m["type"] == kind]

    def last_text(self) -> str:
        texts = self.sent("text")
        return texts[-1]["body"] if texts else ""
