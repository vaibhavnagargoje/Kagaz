"""
WhatsApp Cloud API client.

`get_client()` returns the real client, or a console client that prints
messages in the terminal when WHATSAPP_BACKEND=console (used by
`python manage.py chat` and the tests).
"""

import hashlib
import hmac
import logging
import mimetypes
import sys
from functools import lru_cache
from pathlib import Path

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

GRAPH_URL = "https://graph.facebook.com"
GRAPH_API_VERSION = "v23.0"
MAX_UPLOAD_MB = 25


class WhatsAppError(Exception):
    pass


class FileTooLarge(WhatsAppError):
    pass


def verify_signature(body: bytes, signature_header: str) -> bool:
    """Check Meta's X-Hub-Signature-256 header against our app secret."""
    secret = settings.WHATSAPP_APP_SECRET
    if not secret or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _max_upload_bytes() -> int:
    return MAX_UPLOAD_MB * 1024 * 1024


class CloudClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"

    def _request(self, method: str, path: str, timeout: int = 30, **kwargs) -> dict:
        url = f"{GRAPH_URL}/{GRAPH_API_VERSION}/{path}"
        try:
            response = self.session.request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise WhatsAppError(f"{method} {path} failed: {exc}") from exc
        if response.status_code >= 400:
            raise WhatsAppError(f"{method} {path} -> {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    def _send(self, to: str, message: dict) -> dict:
        body = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, **message}
        return self._request("POST", f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages", json=body)

    def send_text(self, to: str, body: str):
        self._send(to, {"type": "text", "text": {"preview_url": False, "body": clip(body, 4096)}})

    def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]):
        """Up to 3 reply buttons as (id, title)."""
        self._send(to, {
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": clip(body, 1024)},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": button_id, "title": clip(title, 20)}}
                    for button_id, title in buttons[:3]
                ]},
            },
        })

    def send_list(self, to: str, body: str, button: str, rows: list[tuple[str, str, str]], section: str = "Documents"):
        """A pick-one list of up to 10 rows as (id, title, description)."""
        self._send(to, {
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": clip(body, 1024)},
                "action": {
                    "button": clip(button, 20),
                    "sections": [{
                        "title": clip(section, 24),
                        "rows": [
                            {"id": row_id, "title": clip(title, 24), **({"description": clip(description, 72)} if description else {})}
                            for row_id, title, description in rows[:10]
                        ],
                    }],
                },
            },
        })

    def send_link_button(self, to: str, body: str, label: str, url: str):
        """A message with one button that opens a web page (e.g. "Pay ₹49")."""
        self._send(to, {
            "type": "interactive",
            "interactive": {
                "type": "cta_url",
                "body": {"text": clip(body, 1024)},
                "action": {"name": "cta_url", "parameters": {"display_text": clip(label, 20), "url": url}},
            },
        })

    def send_document(self, to: str, data: bytes, mime: str, filename: str, caption: str = ""):
        # Always sent as a document: WhatsApp recompresses anything sent as a photo.
        media_id = self.upload_media(data, mime, filename)
        document = {"id": media_id, "filename": filename}
        if caption:
            document["caption"] = clip(caption, 1024)
        self._send(to, {"type": "document", "document": document})

    def mark_read(self, message_id: str):
        """Blue ticks plus a typing indicator while we work on the reply."""
        try:
            self._request("POST", f"{settings.WHATSAPP_PHONE_NUMBER_ID}/messages", json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            })
        except WhatsAppError:
            logger.warning("Could not mark message %s as read", message_id)

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        info = self._request("GET", media_id)
        if int(info.get("file_size") or 0) > _max_upload_bytes():
            raise FileTooLarge()
        try:
            # The download URL expires within minutes and needs the same bearer token.
            response = self.session.get(info["url"], timeout=120)
            response.raise_for_status()
        except (requests.RequestException, KeyError) as exc:
            raise WhatsAppError(f"Media download failed for {media_id}") from exc
        if len(response.content) > _max_upload_bytes():
            raise FileTooLarge()
        return response.content, info.get("mime_type", "application/octet-stream")

    def upload_media(self, data: bytes, mime: str, filename: str) -> str:
        result = self._request(
            "POST",
            f"{settings.WHATSAPP_PHONE_NUMBER_ID}/media",
            timeout=120,
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, data, mime)},
        )
        return result["id"]

    def delete_media(self, media_id: str):
        try:
            self._request("DELETE", media_id)
        except WhatsAppError:
            logger.info("Meta did not delete media %s", media_id)


class ConsoleClient:
    """Prints messages instead of sending them; sent files go to storage/outbox/.

    Development only: files in the outbox are NOT encrypted.
    """

    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.sent: list[dict] = []
        # Last buttons or list rows shown, so `/tap 2` in the chat simulator works.
        self.options: list[tuple[str, str]] = []
        self.options_kind = ""

    def _show(self, text: str):
        self.stream.write(f"\n🤖 Kagaz: {text}\n")
        self.stream.flush()

    def send_text(self, to, body):
        self.sent.append({"type": "text", "to": to, "body": body})
        self._show(body)

    def send_buttons(self, to, body, buttons):
        self.sent.append({"type": "buttons", "to": to, "body": body, "buttons": buttons})
        self.options, self.options_kind = list(buttons), "button"
        choices = "\n".join(f"  [{n}] {title}" for n, (_, title) in enumerate(buttons, 1))
        self._show(f"{body}\n{choices}\n  (type /tap <number>)")

    def send_list(self, to, body, button, rows, section="Documents"):
        self.sent.append({"type": "list", "to": to, "body": body, "rows": rows})
        self.options, self.options_kind = [(row_id, title) for row_id, title, _ in rows], "list"
        choices = "\n".join(
            f"  [{n}] {title}{f': {desc}' if desc else ''}" for n, (_, title, desc) in enumerate(rows, 1)
        )
        self._show(f"{body}\n{choices}\n  (type /tap <number>)")

    def send_link_button(self, to, body, label, url):
        self.sent.append({"type": "link_button", "to": to, "body": body, "label": label, "url": url})
        self._show(f"{body}\n  [{label}] → {url}")

    def send_document(self, to, data, mime, filename, caption=""):
        outbox = Path(settings.MEDIA_ROOT) / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / filename
        path.write_bytes(data)
        self.sent.append({"type": "document", "to": to, "filename": filename, "mime": mime, "data": data, "caption": caption})
        self._show(f"📎 {filename} ({max(1, round(len(data) / 1024))} KB) -> {path}\n{caption}".rstrip())

    def mark_read(self, message_id):
        pass

    def download_media(self, media_id):
        # The chat simulator uses "local:<path>" as the media id.
        path = Path(media_id.removeprefix("local:"))
        if path.stat().st_size > _max_upload_bytes():
            raise FileTooLarge()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path.read_bytes(), mime

    def delete_media(self, media_id):
        pass


@lru_cache(maxsize=None)
def _client_for(backend: str):
    return ConsoleClient() if backend == "console" else CloudClient()


def get_client():
    return _client_for(settings.WHATSAPP_BACKEND)


def reset_clients():
    """Forget cached clients (used by tests)."""
    _client_for.cache_clear()
