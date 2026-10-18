from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from vault.models import Document


@receiver(post_delete, sender=Document)
def delete_stored_blob(sender, instance, **kwargs):
    """Remove the encrypted file too (also runs when a member is deleted)."""
    if instance.file:
        storage, name = instance.file.storage, instance.file.name
        transaction.on_commit(lambda: storage.delete(name))
