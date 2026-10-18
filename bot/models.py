from django.db import models
from django.utils import timezone


class InboundMessage(models.Model):
    """A message received on the webhook.

    `wa_message_id` is unique so Meta's webhook retries are processed once.
    The raw message is kept only until it has been handled.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        AWAITING_CONSENT = "awaiting_consent", "Waiting for consent"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    wa_message_id = models.CharField(max_length=128, unique=True)
    wa_id = models.CharField("WhatsApp id", max_length=32, db_index=True)
    msg_type = models.CharField(max_length=32)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.msg_type} from …{self.wa_id[-4:]} ({self.status})"

    def finish(self, failed: bool = False):
        # filter().update() so this is safe even if the row was deleted
        # meanwhile (e.g. by "delete my data").
        InboundMessage.objects.filter(pk=self.pk).update(
            status=self.Status.FAILED if failed else self.Status.DONE,
            payload={},
            processed_at=timezone.now(),
        )
