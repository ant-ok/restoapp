import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from reports.services import scan_anomalies


class Command(BaseCommand):
    help = "Scan transactions for anomalies and save DataIssue records."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True, help="YYYY-MM-DD")

    def handle(self, *args, **options):
        try:
            report_date = dt.date.fromisoformat(options["date"])
        except ValueError as exc:
            raise CommandError("--date must be YYYY-MM-DD") from exc

        count = scan_anomalies(report_date)
        self.stdout.write(self.style.SUCCESS(f"Saved {count} issue(s) for {report_date}"))
