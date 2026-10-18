import base64

from django.core.management.base import BaseCommand

from core.crypto import new_data_key


class Command(BaseCommand):
    help = "Print a new random KAGAZ_MASTER_KEY for your .env file."

    def handle(self, *args, **options):
        key = base64.b64encode(new_data_key()).decode()
        self.stdout.write(f"KAGAZ_MASTER_KEY={key}")
        self.stderr.write(
            "Keep this key secret and backed up. If it is lost, stored documents "
            "cannot be decrypted. Never change it once real data exists."
        )
