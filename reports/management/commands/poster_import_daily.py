import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from reports.poster_client import PosterClient, PosterConfigError
from reports.services import import_daily


class Command(BaseCommand):
    help = "Import daily Poster data and store a DailyReport entry."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            required=True,
            help="Date in YYYY-MM-DD format",
        )
        parser.add_argument(
            "--include-products-sales",
            action="store_true",
            help="Fetch dash.getProductsSales (may be slow on large accounts).",
        )

    def handle(self, *args, **options):
        try:
            report_date = dt.date.fromisoformat(options["date"])
        except ValueError as exc:
            raise CommandError("--date must be YYYY-MM-DD") from exc

        client = None
        try:
            client = PosterClient.from_settings()
        except PosterConfigError as exc:
            raise CommandError(str(exc)) from exc

        transactions_count = import_daily(
            client=client,
            report_date=report_date,
            include_products_sales=options["include_products_sales"],
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Saved DailyReport for {report_date} (transactions={transactions_count})"
            )
        )
