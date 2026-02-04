import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from reports.poster_client import PosterAPIError, PosterClient, PosterConfigError


class Command(BaseCommand):
    help = "Call Poster API endpoint with optional JSON params."

    def add_arguments(self, parser):
        parser.add_argument("path", help="Endpoint path, e.g. 'api/v3/some/endpoint'")
        parser.add_argument(
            "--params",
            default="{}",
            help="JSON object of query params. Example: '{\"dateFrom\": \"2026-02-01\"}'",
        )
        parser.add_argument(
            "--method",
            default="GET",
            help="HTTP method (GET or POST). Default: GET",
        )
        parser.add_argument(
            "--body",
            default=None,
            help="JSON object to send as JSON body (POST).",
        )
        parser.add_argument(
            "--form",
            default=None,
            help="JSON object to send as form-data (POST).",
        )

    def handle(self, *args, **options):
        try:
            params = json.loads(options["params"])
            if not isinstance(params, dict):
                raise ValueError
        except ValueError as exc:
            raise CommandError("--params must be a JSON object") from exc

        if options["body"] and options["form"]:
            raise CommandError("Use only one of --body or --form")

        json_body = None
        form_body = None
        if options["body"] is not None:
            try:
                json_body = json.loads(options["body"])
                if not isinstance(json_body, dict):
                    raise ValueError
            except ValueError as exc:
                raise CommandError("--body must be a JSON object") from exc

        if options["form"] is not None:
            try:
                form_body = json.loads(options["form"])
                if not isinstance(form_body, dict):
                    raise ValueError
            except ValueError as exc:
                raise CommandError("--form must be a JSON object") from exc

        try:
            client = PosterClient.from_settings()
            data: Any = client.request(
                options["method"],
                options["path"],
                params=params,
                json_body=json_body,
                form_body=form_body,
            )
        except (PosterConfigError, PosterAPIError) as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(json.dumps(data, ensure_ascii=False, indent=2))
