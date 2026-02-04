import datetime as dt

from django.core.management.base import BaseCommand, CommandError

from reports.models import DailyReport, ReportTemplate


class Command(BaseCommand):
    help = "Generate a simple report from stored DailyReport data."

    def add_arguments(self, parser):
        parser.add_argument("--template", required=True, help="Template name")
        parser.add_argument("--date-from", required=True, help="YYYY-MM-DD")
        parser.add_argument("--date-to", required=True, help="YYYY-MM-DD")

    def handle(self, *args, **options):
        try:
            date_from = dt.date.fromisoformat(options["date_from"])
            date_to = dt.date.fromisoformat(options["date_to"])
        except ValueError as exc:
            raise CommandError("--date-from/--date-to must be YYYY-MM-DD") from exc

        try:
            template = ReportTemplate.objects.get(name=options["template"])
        except ReportTemplate.DoesNotExist as exc:
            raise CommandError("Template not found") from exc

        metrics = template.config.get("metrics", ["transactions_count", "revenue", "avg_check"])

        rows = []
        for report in DailyReport.objects.filter(date__range=[date_from, date_to]).order_by("date"):
            row = {"date": report.date.isoformat()}
            for metric in metrics:
                row[metric] = getattr(report, metric, None)
            rows.append(row)

        self.stdout.write(str({"template": template.name, "rows": rows}))
