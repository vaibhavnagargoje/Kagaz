from django.core.management.base import BaseCommand

from bot.tasks import reread_and_notify


class Command(BaseCommand):
    help = (
        "Read again, right now, the documents the AI skipped while it was busy. "
        "In production the worker (run_huey) does this on a schedule every 10 minutes."
    )

    def handle(self, *args, **options):
        count = reread_and_notify(now=True)
        self.stdout.write(f"Read {count} document(s).")
