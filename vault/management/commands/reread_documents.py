from django.core.management.base import BaseCommand

from bot.tasks import reread_and_notify


class Command(BaseCommand):
    help = (
        "Read again, right now, the documents the AI skipped while it was busy. "
        "In production the worker (run_huey) does this on a schedule every 10 minutes."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--include-failed",
            action="store_true",
            help="Also retry documents that gave up because the AI stayed unavailable (e.g. no credits).",
        )

    def handle(self, *args, **options):
        count = reread_and_notify(now=True, include_failed=options["include_failed"], limit=200)
        self.stdout.write(f"Read {count} document(s).")
