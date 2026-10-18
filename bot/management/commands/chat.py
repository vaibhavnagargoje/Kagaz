import logging
import mimetypes
import time
import uuid
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from huey.contrib.djhuey import HUEY

from accounts.models import Member
from bot.handlers import handle_inbound
from bot.models import InboundMessage
from bot.whatsapp import get_client
from vault import services

BANNER = """
Kagaz chat simulator. You are +{phone}.
  Type a message like you would on WhatsApp, or:
  /file <path>   send a document or photo
  /tap <n>       tap option n from the last buttons or list
  /reset         erase this test user and everything waiting, start fresh
  /quit          leave
Files the bot sends you are saved in {outbox}

Use dummy documents only while AI_PROVIDER=gemini is on Google's free tier.
"""


class Command(BaseCommand):
    help = "Chat with the bot in your terminal as if on WhatsApp (no Meta account needed)."

    def add_arguments(self, parser):
        parser.add_argument("--phone", default="919999999999", help="Pretend WhatsApp number.")
        parser.add_argument("--name", default="Test User", help="Pretend WhatsApp profile name.")

    def handle(self, *args, phone, name, **options):
        if settings.WHATSAPP_BACKEND != "console":
            raise CommandError("Set WHATSAPP_BACKEND=console in .env to use the chat simulator.")
        logging.getLogger().setLevel(logging.WARNING)
        HUEY.immediate = True  # Run follow-up jobs inline instead of needing a worker.
        client = get_client()
        self.stdout.write(BANNER.format(phone=phone, outbox=Path(settings.MEDIA_ROOT) / "outbox"))

        while True:
            try:
                line = input("\n👤 You: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/reset":
                self._reset(phone)
                continue
            try:
                message = self._build_message(line, client)
            except CommandError as exc:
                self.stderr.write(str(exc))
                continue
            self._deliver(phone, name, message)

    def _build_message(self, line: str, client) -> dict:
        if line.startswith("/file "):
            path = Path(line.removeprefix("/file ").strip().strip('"')).expanduser().resolve()
            if not path.is_file():
                raise CommandError(f"No such file: {path}")
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            media = {"id": f"local:{path}", "mime_type": mime}
            if mime.startswith("image/"):
                return {"type": "image", "image": media}
            return {"type": "document", "document": {**media, "filename": path.name}}

        if line.startswith("/tap "):
            try:
                option_id, title = client.options[int(line.removeprefix("/tap ").strip()) - 1]
            except (ValueError, IndexError):
                raise CommandError("No such option.")
            reply_type = "button_reply" if client.options_kind == "button" else "list_reply"
            return {"type": "interactive", "interactive": {"type": reply_type, reply_type: {"id": option_id, "title": title}}}

        return {"type": "text", "text": {"body": line}}

    def _deliver(self, phone: str, name: str, message: dict):
        message = {"from": phone, "id": f"wamid.console.{uuid.uuid4().hex}", "timestamp": str(int(time.time())), **message}
        inbound = InboundMessage.objects.create(
            wa_message_id=message["id"],
            wa_id=phone,
            msg_type=message["type"],
            payload={"message": message, "profile_name": name},
        )
        handle_inbound(inbound.pk)

    def _reset(self, phone: str):
        """Delete this test user, their documents and any messages still waiting."""
        member = Member.objects.filter(wa_id=phone).first()
        if member:
            services.delete_member_data(member)
        InboundMessage.objects.filter(wa_id=phone).delete()
        self.stdout.write("Erased. You're a brand-new user again.")
