from __future__ import annotations

import datetime as dt
import json
import hashlib
import os
from typing import Any, Dict, Iterable, Optional, List
from urllib.parse import urlencode, urlparse

import requests
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_GET, require_http_methods
from django.utils import timezone

from reports.models import (
    DataIssue,
    DailyReport,
    PosterAccount,
    ReportTemplate,
    UserPosterAccount,
    Insight,
    Transaction,
    TelegramSettings,
    Spot,
    PendingTelegramChat,
)
from reports.poster_client import PosterClient, PosterConfigError
from reports.services import (
    import_daily,
    scan_anomalies,
    generate_insights,
    send_telegram_message,
    build_report_text,
    send_telegram_message_to,
    daily_summary_by_spot,
    get_spot_name,
)

from openpyxl import Workbook


def home(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("/reports/")
    return render(request, "home.html")


def about(request: HttpRequest) -> HttpResponse:
    return render(request, "about.html")


def _normalize_base_url(value: str) -> str:
    value = value.strip()
    if not value:
        return value
    parsed = urlparse(value)
    if not parsed.scheme:
        value = "https://" + value
        parsed = urlparse(value)
    if not parsed.path or parsed.path == "/":
        value = value.rstrip("/") + "/api"
    return value


@login_required
def onboarding(request: HttpRequest) -> HttpResponse:
    message = None
    error = None
    accounts = list(UserPosterAccount.objects.filter(user=request.user).order_by("-updated_at"))
    account = next((a for a in accounts if a.is_active), None)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            base_url = (request.POST.get("account_base_url") or "").strip()
            api_token = (request.POST.get("api_token") or "").strip()
            auth_style = (request.POST.get("auth_style") or "query_token").strip()
            base_url = _normalize_base_url(base_url)
            if not base_url or not api_token:
                error = "Заполните URL аккаунта и API ключ."
            else:
                UserPosterAccount.objects.filter(user=request.user).update(is_active=False)
                UserPosterAccount.objects.update_or_create(
                    user=request.user,
                    account_base_url=base_url,
                    defaults={
                        "api_token": api_token,
                        "auth_style": auth_style,
                        "is_active": True,
                    },
                )
                message = "Аккаунт сохранен. Можно переходить к отчетам."
        elif action == "select":
            account_id = request.POST.get("account_id")
            if account_id:
                UserPosterAccount.objects.filter(user=request.user).update(is_active=False)
                UserPosterAccount.objects.filter(user=request.user, id=account_id).update(is_active=True)
                message = "Аккаунт переключен."
        accounts = list(UserPosterAccount.objects.filter(user=request.user).order_by("-updated_at"))
        account = next((a for a in accounts if a.is_active), None)
    return render(
        request,
        "onboarding.html",
        {"message": message, "error": error, "account": account, "accounts": accounts},
    )


@login_required
def telegram_settings(request: HttpRequest) -> HttpResponse:
    message = None
    settings = TelegramSettings.objects.filter(user=request.user).first()
    if request.method == "POST":
        chat_ids = (request.POST.get("chat_ids") or "").strip()
        auto_daily = request.POST.get("auto_daily") == "on"
        daily_time = (request.POST.get("daily_time") or "23:59").strip()
        timezone_name = (request.POST.get("timezone") or "UTC").strip()
        template_name = (request.POST.get("template_name") or "").strip()
        include_spots = request.POST.get("include_spots") == "on"
        include_issues = request.POST.get("include_issues") == "on"
        include_returns = request.POST.get("include_returns") == "on"
        metrics = request.POST.getlist("metrics")
        spot_ids = request.POST.getlist("spot_ids")
        notify_issue_types = request.POST.getlist("notify_issue_types")
        auto_shift_close = request.POST.get("auto_shift_close") == "on"
        auto_per_spot = request.POST.get("auto_per_spot") == "on"
        shift_template_name = (request.POST.get("shift_template_name") or "").strip()

        if request.POST.get("action") == "claim_chat":
            chat_id = (request.POST.get("chat_id") or "").strip()
            if chat_id:
                if not settings:
                    settings = TelegramSettings(user=request.user)
                current = [c.strip() for c in (settings.chat_ids or "").split(",") if c.strip()]
                if chat_id not in current:
                    current.append(chat_id)
                settings.chat_ids = ",".join(current)
                settings.save()
                PendingTelegramChat.objects.filter(chat_id=chat_id).delete()
                message = "Чат подключен."
        elif request.POST.get("action") == "save":
            if not settings:
                settings = TelegramSettings(user=request.user)
            settings.chat_ids = chat_ids
            settings.auto_daily = auto_daily
            settings.daily_time = daily_time
            settings.timezone = timezone_name
            settings.template_name = template_name
            settings.include_spots = include_spots
            settings.include_issues = include_issues
            settings.include_returns = include_returns
            settings.metrics = metrics
            settings.spot_ids = spot_ids
            settings.notify_issue_types = notify_issue_types
            settings.auto_shift_close = auto_shift_close
            settings.auto_per_spot = auto_per_spot
            settings.shift_template_name = shift_template_name
            settings.save()
            message = "Настройки Telegram сохранены."

    templates = ReportTemplate.objects.order_by("name")
    spots = Spot.objects.order_by("spot_id")
    pending_chats = PendingTelegramChat.objects.order_by("-created_at")[:20]
    issue_types = [
        ("payment_mismatch", "Несоответствие оплат"),
        ("zero_or_negative_sum", "Нулевая/отрицательная сумма"),
        ("no_transactions", "Нет чеков"),
        ("table_move", "Перенос на другой стол"),
    ]
    return render(
        request,
        "telegram_settings.html",
        {
            "settings": settings,
            "templates": templates,
            "message": message,
            "issue_types": issue_types,
            "spots": spots,
            "pending_chats": pending_chats,
        },
    )


def _notify_issue_alerts(issues: List[DataIssue]) -> None:
    if not issues:
        return
    settings_list = TelegramSettings.objects.all()
    for settings in settings_list:
        if not settings.chat_ids:
            continue
        allowed = set(settings.notify_issue_types or [])
        if not allowed:
            continue
        selected = [i for i in issues if i.issue_type in allowed]
        if not selected:
            continue
        ids = [int(x.strip()) for x in settings.chat_ids.split(",") if x.strip()]
        lines = ["Сомнительные операции:"]
        for issue in selected[:10]:
            lines.append(f"• {issue.message}")
        if len(selected) > 10:
            lines.append(f"… и еще {len(selected) - 10}")
        send_telegram_message_to(ids, "\n".join(lines))


def _require_settings(values: Dict[str, Optional[str]]) -> None:
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise ValueError("Missing settings: " + ", ".join(missing))


@require_GET
def poster_auth_start(request: HttpRequest) -> HttpResponse:
    try:
        _require_settings({"POSTER_APP_ID": settings.POSTER_APP_ID})
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=500)

    redirect_uri = request.build_absolute_uri("/poster/auth/callback/")
    params = {
        "application_id": settings.POSTER_APP_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    url = f"https://joinposter.com/api/auth?{urlencode(params)}"
    return redirect(url)


@require_GET
def poster_auth_callback(request: HttpRequest) -> HttpResponse:
    code = request.GET.get("code")
    account = request.GET.get("account")
    if not code or not account:
        return JsonResponse({"error": "Missing code or account in callback"}, status=400)

    try:
        _require_settings(
            {
                "POSTER_APP_ID": settings.POSTER_APP_ID,
                "POSTER_APP_SECRET": settings.POSTER_APP_SECRET,
            }
        )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=500)

    redirect_uri = request.build_absolute_uri("/poster/auth/callback/")
    token_url = f"https://{account}.joinposter.com/api/v2/auth/access_token"
    payload = {
        "application_id": settings.POSTER_APP_ID,
        "application_secret": settings.POSTER_APP_SECRET,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
        "code": code,
    }

    try:
        response = requests.post(token_url, data=payload, timeout=20)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        return JsonResponse({"error": f"Token request failed: {exc}"}, status=502)
    except ValueError:
        return JsonResponse({"error": "Token response is not JSON"}, status=502)

    access_token = data.get("access_token")
    if not access_token:
        return JsonResponse({"error": "No access_token in response", "details": data}, status=502)

    PosterAccount.objects.update_or_create(
        account=account,
        defaults={
            "access_token": access_token,
            "account_number": data.get("account_number", ""),
            "user_info": data.get("user", {}) or {},
            "owner_info": data.get("ownerInfo", {}) or {},
            "tariff_info": data.get("tariff", {}) or {},
        },
    )

    return JsonResponse({"status": "ok", "account": account})


@csrf_protect
@require_http_methods(["GET", "POST"])
@login_required
def reports_dashboard(request: HttpRequest) -> HttpResponse:
    message = None
    error = None
    account = UserPosterAccount.objects.filter(user=request.user, is_active=True).first()
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_template":
            name = (request.POST.get("template_name") or "").strip()
            config_raw = (request.POST.get("template_config") or "").strip()
            if name and config_raw:
                ReportTemplate.objects.update_or_create(
                    name=name,
                    defaults={"config": {"builder": config_raw}},
                )
                message = "Шаблон сохранен."
        elif action == "import":
            date_from_raw = (request.POST.get("date_from") or "").strip()
            date_to_raw = (request.POST.get("date_to") or "").strip()
            include_products = request.POST.get("include_products") == "on"
            try:
                date_from = dt.date.fromisoformat(date_from_raw)
                date_to = dt.date.fromisoformat(date_to_raw)
            except ValueError:
                error = "Invalid dates. Use calendar picker."
            else:
                if not account:
                    error = "Сначала подключите аккаунт Poster на странице онбординга."
                else:
                    client = PosterClient(
                        base_url=_normalize_base_url(account.account_base_url),
                        token=account.api_token,
                        auth_style=account.auth_style,
                    )
                    if date_from > date_to:
                        date_from, date_to = date_to, date_from
                    current = date_from
                    total = 0
                    while current <= date_to:
                        total += import_daily(
                            client=client,
                            report_date=current,
                            include_products_sales=include_products,
                        )
                        new_issues = scan_anomalies(current)
                        _notify_issue_alerts(new_issues)
                        generate_insights(current)
                        current += dt.timedelta(days=1)
                    message = f"Импортировано. Всего чеков: {total}."
        elif action == "template_quick":
            preset = (request.POST.get("preset") or "").strip()
            presets = {
                "daily_basic": {"metrics": ["transactions_count", "revenue", "avg_check"]},
                "revenue_only": {"metrics": ["revenue"]},
                "checks_only": {"metrics": ["transactions_count"]},
            }
            if preset in presets:
                ReportTemplate.objects.update_or_create(
                    name={"daily_basic": "Дневной обзор", "revenue_only": "Только выручка", "checks_only": "Только чеки"}[preset],
                    defaults={"config": presets[preset]},
                )
                message = "Шаблон создан."

    reports = list(DailyReport.objects.order_by("-date")[:30])
    issues = DataIssue.objects.filter(ignored=False).order_by("-created_at")[:30]
    insights = Insight.objects.order_by("-created_at")[:30]
    templates = ReportTemplate.objects.order_by("name")
    latest_report = reports[0] if reports else None
    chart_points = [
        {
            "date": report.date.isoformat(),
            "revenue_eur": report.revenue / 100,
            "transactions": report.transactions_count,
            "avg_check_eur": report.avg_check / 100,
        }
        for report in reversed(list(reports))
    ]
    return render(
        request,
        "reports/dashboard.html",
        {
            "reports": reports,
            "issues": issues,
            "templates": templates,
            "message": message,
            "error": error,
            "chart_points": chart_points,
            "builder_data": json.dumps(chart_points),
            "account": account,
            "insights": insights,
            "today": dt.date.today().isoformat(),
            "latest_report": latest_report,
        },
    )


def register(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            return redirect("/reports/")
    else:
        form = UserCreationForm()
    return render(request, "registration/register.html", {"form": form})


@login_required
def issue_detail(request: HttpRequest, issue_id: int) -> HttpResponse:
    issue = DataIssue.objects.get(id=issue_id)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "ignore":
            issue.ignored = True
            issue.ignored_at = timezone.now()
            issue.save(update_fields=["ignored", "ignored_at"])
        elif action == "restore":
            issue.ignored = False
            issue.ignored_at = None
            issue.save(update_fields=["ignored", "ignored_at"])
    transaction_id = issue.context.get("transaction_id") if isinstance(issue.context, dict) else None
    transaction = None
    if transaction_id:
        transaction = Transaction.objects.filter(transaction_id=str(transaction_id)).first()
    explanations = {
        "payment_mismatch": {
            "title": "Несоответствие оплат",
            "details": "Сумма оплат не совпадает с суммой чека. Возможна ошибка в оплатах или возвратах.",
            "tip": "Проверьте оплаты по карте/наличными и статус чека.",
        },
        "zero_or_negative_sum": {
            "title": "Нулевая или отрицательная сумма",
            "details": "Чек закрыт, но сумма меньше или равна нулю.",
            "tip": "Проверьте возвраты и корректность закрытия чека.",
        },
        "no_transactions": {
            "title": "Нет продаж",
            "details": "За выбранный день нет чеков.",
            "tip": "Убедитесь, что касса работала и данные загружены.",
        },
    }
    explain = explanations.get(issue.issue_type, None)
    return render(
        request,
        "reports/issue_detail.html",
        {"issue": issue, "transaction": transaction, "explain": explain},
    )


@login_required
def transaction_detail(request: HttpRequest, transaction_id: str) -> HttpResponse:
    transaction = Transaction.objects.filter(transaction_id=transaction_id).first()
    products = []
    if transaction and isinstance(transaction.raw, dict):
        products = transaction.raw.get("products") or []
    payments = None
    if transaction:
        payments = {
            "cash": transaction.payed_cash,
            "card": transaction.payed_card,
            "bonus": transaction.payed_bonus,
            "third_party": transaction.payed_third_party,
            "cert": transaction.payed_cert,
            "total": transaction.payed_sum,
        }
    return render(
        request,
        "reports/transaction_detail.html",
        {"transaction": transaction, "products": products, "payments": payments},
    )


@require_GET
def reports_export(request: HttpRequest) -> HttpResponse:
    date_from_raw = (request.GET.get("date_from") or "").strip()
    date_to_raw = (request.GET.get("date_to") or "").strip()

    try:
        date_from = dt.date.fromisoformat(date_from_raw) if date_from_raw else None
        date_to = dt.date.fromisoformat(date_to_raw) if date_to_raw else None
    except ValueError:
        return JsonResponse({"error": "Invalid date format"}, status=400)

    qs = DailyReport.objects.all().order_by("date")
    if date_from and date_to:
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        qs = qs.filter(date__range=[date_from, date_to])
    elif date_from:
        qs = qs.filter(date__gte=date_from)
    elif date_to:
        qs = qs.filter(date__lte=date_to)

    wb = Workbook()
    ws = wb.active
    ws.title = "Daily Reports"
    ws.append(["Date", "Transactions", "Revenue (EUR)", "Avg Check (EUR)"])

    for report in qs:
        ws.append(
            [
                report.date.isoformat(),
                report.transactions_count,
                report.revenue / 100,
                report.avg_check / 100,
            ]
        )

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = 'attachment; filename="daily_reports.xlsx"'
    wb.save(response)
    return response

def _extract_path(payload: Any, path: Optional[str]) -> Any:
    if not path:
        return None
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
            continue
        if isinstance(current, list):
            try:
                idx = int(part)
            except ValueError:
                return None
            if idx < 0 or idx >= len(current):
                return None
            current = current[idx]
            continue
        return None
    return current


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_money(value: Any, scale: float) -> Optional[float]:
    raw = _safe_float(value)
    if raw is None:
        return None
    if scale == 0:
        return raw
    return raw / scale


def _client_screen_demo_payload() -> Dict[str, Any]:
    return {
        "order_number": "A-1024",
        "items": [
            {"name": "Cappuccino", "qty": 2, "price": 3.5, "sum": 7.0},
            {"name": "Quiche", "qty": 1, "price": 5.9, "sum": 5.9},
            {"name": "Mineralvesi", "qty": 1, "price": 2.2, "sum": 2.2},
        ],
        "total": 15.1,
        "currency": settings.CLIENT_SCREEN_CURRENCY_SYMBOL,
        "demo": True,
        "updated_at": dt.datetime.utcnow().isoformat() + "Z",
    }


_CLIENT_SCREEN_CACHE: Dict[str, Any] = {}


def _client_screen_cache_set(payload: Dict[str, Any]) -> None:
    _CLIENT_SCREEN_CACHE.clear()
    _CLIENT_SCREEN_CACHE.update(payload)


def _client_screen_poster_client(request: HttpRequest) -> Optional[PosterClient]:
    if request.user.is_authenticated:
        account = UserPosterAccount.objects.filter(user=request.user, is_active=True).first()
        if account:
            return PosterClient(
                base_url=_normalize_base_url(account.account_base_url),
                token=account.api_token,
                auth_style=account.auth_style,
            )
    base_url = settings.CLIENT_SCREEN_POSTER_BASE_URL
    token = settings.CLIENT_SCREEN_POSTER_TOKEN
    auth_style = settings.CLIENT_SCREEN_POSTER_AUTH_STYLE
    if base_url and token and auth_style:
        return PosterClient(
            base_url=_normalize_base_url(base_url),
            token=token,
            auth_style=auth_style,
        )
    return None


def _client_screen_items(raw_items: Any) -> list[Dict[str, Any]]:
    if isinstance(raw_items, dict):
        raw_items = list(raw_items.values())
    if not isinstance(raw_items, Iterable):
        return []
    items: list[Dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        name = (
            _extract_path(raw, settings.CLIENT_SCREEN_ITEM_NAME_PATH)
            or raw.get("name")
            or raw.get("product_name")
            or raw.get("id")
        )
        qty = _extract_path(raw, settings.CLIENT_SCREEN_ITEM_QTY_PATH) or raw.get("qty") or raw.get("count")
        price = _extract_path(raw, settings.CLIENT_SCREEN_ITEM_PRICE_PATH) or raw.get("price")
        total = _extract_path(raw, settings.CLIENT_SCREEN_ITEM_TOTAL_PATH) or raw.get("sum") or raw.get("total")

        qty_value = _safe_float(qty) or 0
        price_value = _to_money(price, settings.CLIENT_SCREEN_AMOUNT_SCALE)
        sum_value = _to_money(total, settings.CLIENT_SCREEN_AMOUNT_SCALE)
        if sum_value is None and price_value is not None:
            sum_value = qty_value * price_value

        items.append(
            {
                "name": str(name or "—"),
                "qty": qty_value,
                "price": price_value,
                "sum": sum_value,
            }
        )
    return items


def client_screen(request: HttpRequest) -> HttpResponse:
    context = {
        "poll_seconds": settings.CLIENT_SCREEN_POLL_SECONDS,
        "currency_symbol": settings.CLIENT_SCREEN_CURRENCY_SYMBOL,
    }
    return render(request, "client_screen.html", context)


@require_GET
def client_screen_data(request: HttpRequest) -> HttpResponse:
    order_id = (request.GET.get("order_id") or "").strip()
    if order_id == "" and _CLIENT_SCREEN_CACHE:
        return JsonResponse(_CLIENT_SCREEN_CACHE)

    client = _client_screen_poster_client(request)
    if settings.CLIENT_SCREEN_DEMO_MODE and not client:
        return JsonResponse(_client_screen_demo_payload())

    if not client:
        return JsonResponse({"error": "Poster client is not configured"}, status=500)

    endpoint = settings.CLIENT_SCREEN_ORDER_ENDPOINT
    if not endpoint:
        if settings.CLIENT_SCREEN_DEMO_MODE:
            return JsonResponse(_client_screen_demo_payload())
        return JsonResponse({"error": "CLIENT_SCREEN_ORDER_ENDPOINT is not configured"}, status=500)

    params: Dict[str, Any] = {}
    if order_id and settings.CLIENT_SCREEN_ORDER_ID_PARAM:
        params[settings.CLIENT_SCREEN_ORDER_ID_PARAM] = order_id

    if settings.CLIENT_SCREEN_ORDER_PARAMS_JSON:
        try:
            extra_params = json.loads(settings.CLIENT_SCREEN_ORDER_PARAMS_JSON)
            if isinstance(extra_params, dict):
                params.update(extra_params)
        except json.JSONDecodeError:
            return JsonResponse({"error": "CLIENT_SCREEN_ORDER_PARAMS_JSON is invalid JSON"}, status=500)

    response = _client_screen_fetch_and_format(client=client, endpoint=endpoint, params=params, order_id=order_id)
    return JsonResponse(response)


def _client_screen_fetch_and_format(
    client: PosterClient,
    endpoint: str,
    params: Dict[str, Any],
    order_id: str,
) -> Dict[str, Any]:
    payload = client.get(endpoint, params=params)

    items_raw = _extract_path(payload, settings.CLIENT_SCREEN_ITEMS_PATH)
    items = _client_screen_items(items_raw or [])

    total_value = _to_money(
        _extract_path(payload, settings.CLIENT_SCREEN_TOTAL_PATH),
        settings.CLIENT_SCREEN_AMOUNT_SCALE,
    )
    if total_value is None and items:
        total_value = sum(item.get("sum") or 0 for item in items)

    order_number = _extract_path(payload, settings.CLIENT_SCREEN_ORDER_NUMBER_PATH) or order_id or "—"

    return {
        "order_number": str(order_number),
        "items": items,
        "total": total_value or 0,
        "currency": settings.CLIENT_SCREEN_CURRENCY_SYMBOL,
        "demo": False,
        "updated_at": dt.datetime.utcnow().isoformat() + "Z",
    }


@csrf_protect
@require_http_methods(["POST"])
def poster_webhook(request: HttpRequest) -> HttpResponse:
    secret = settings.CLIENT_SCREEN_WEBHOOK_SECRET or settings.POSTER_APP_SECRET
    if not secret:
        return JsonResponse({"error": "Webhook secret is not configured"}, status=500)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload"}, status=400)

    verify_original = payload.get("verify") or ""
    if not verify_original:
        return JsonResponse({"error": "Missing verify"}, status=400)

    verify_parts = [
        str(payload.get("account") or ""),
        str(payload.get("object") or ""),
        str(payload.get("object_id") or ""),
        str(payload.get("action") or ""),
    ]
    if payload.get("data") is not None:
        verify_parts.append(str(payload.get("data")))
    verify_parts.append(str(payload.get("time") or ""))
    verify_parts.append(str(secret))

    verify_string = ";".join(verify_parts)
    verify_calc = hashlib.md5(verify_string.encode("utf-8")).hexdigest()
    if verify_calc != verify_original:
        return JsonResponse({"error": "Invalid verify signature"}, status=403)

    client = _client_screen_poster_client(request)
    if not client:
        return JsonResponse({"error": "Poster client is not configured"}, status=500)

    endpoint = settings.CLIENT_SCREEN_WEBHOOK_REFRESH_ENDPOINT or settings.CLIENT_SCREEN_ORDER_ENDPOINT
    if not endpoint:
        return JsonResponse({"error": "CLIENT_SCREEN_ORDER_ENDPOINT is not configured"}, status=500)

    params: Dict[str, Any] = {}
    object_id = str(payload.get("object_id") or "").strip()
    id_param = settings.CLIENT_SCREEN_WEBHOOK_ID_PARAM or settings.CLIENT_SCREEN_ORDER_ID_PARAM
    if object_id and id_param:
        params[id_param] = object_id

    try:
        response = _client_screen_fetch_and_format(
            client=client,
            endpoint=endpoint,
            params=params,
            order_id=object_id,
        )
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)

    # Telegram: auto-report on shift close
    obj = str(payload.get("object") or "")
    action = str(payload.get("action") or "")
    if obj == "cash_shift_transaction" and action in {"added", "changed"}:
        settings_list = TelegramSettings.objects.filter(auto_shift_close=True).exclude(chat_ids="")
        for settings in settings_list:
            ids = [int(x.strip()) for x in settings.chat_ids.split(",") if x.strip()]
            date = timezone.localdate()
            template_name = settings.shift_template_name or ""
            if template_name:
                template = ReportTemplate.objects.filter(name=template_name).first()
                if template:
                    text = build_custom_report_text(date, template)
                else:
                    text = build_report_text(
                        date,
                        metrics=settings.metrics or None,
                        include_spots=settings.include_spots,
                        include_issues=settings.include_issues,
                        include_returns=settings.include_returns,
                        spot_ids=settings.spot_ids or None,
                    )
            else:
                text = build_report_text(
                    date,
                    metrics=settings.metrics or None,
                    include_spots=settings.include_spots,
                    include_issues=settings.include_issues,
                    include_returns=settings.include_returns,
                    spot_ids=settings.spot_ids or None,
                )
            send_telegram_message_to(ids, text)
            if settings.auto_per_spot:
                summary = daily_summary_by_spot(date)
                if settings.spot_ids:
                    allowed = {str(s) for s in settings.spot_ids}
                    summary = {k: v for k, v in summary.items() if str(k) in allowed}
                for spot_id, item in summary.items():
                    if item["revenue"] <= 0 and item["transactions"] <= 0:
                        continue
                    name = get_spot_name(spot_id)
                    lines = [
                        f"{name} за {date.isoformat()}",
                        f"Выручка: {item['revenue']/100:.2f} €",
                        f"Чеков: {item['transactions']}",
                    ]
                    if settings.include_returns:
                        lines.append(f"Возвратов: {item['returns']}")
                    send_telegram_message_to(ids, "\n".join(lines))

    _client_screen_cache_set(response)
    return JsonResponse({"status": "accept"})


def _telegram_send(chat_id: int, text: str, reply_markup: Optional[dict] = None) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
    except Exception:
        pass


def _parse_telegram_args(args: List[str]) -> tuple[Optional[dt.date], Optional[str], Dict[str, Any]]:
    date = None
    spot_query = None
    overrides: Dict[str, Any] = {}
    for token in args:
        if token.count("-") == 2 and len(token) >= 8:
            try:
                date = dt.date.fromisoformat(token)
                continue
            except ValueError:
                pass
        if token.startswith("spot="):
            spot_query = token.split("=", 1)[1].strip()
            continue
        if token.startswith("metrics="):
            overrides["metrics"] = [m.strip() for m in token.split("=", 1)[1].split(",") if m.strip()]
            continue
        if token.startswith("issues="):
            overrides["include_issues"] = token.split("=", 1)[1].strip() not in {"0", "false", "no"}
            continue
        if token.startswith("spots="):
            overrides["include_spots"] = token.split("=", 1)[1].strip() not in {"0", "false", "no"}
            continue
        if token.startswith("returns="):
            overrides["include_returns"] = token.split("=", 1)[1].strip() not in {"0", "false", "no"}
            continue
        if token.startswith("template="):
            overrides["template_name"] = token.split("=", 1)[1].strip()
            continue
        if token.startswith("per_spot="):
            overrides["per_spot"] = token.split("=", 1)[1].strip() not in {"0", "false", "no"}
            continue
        if not spot_query:
            spot_query = token
    return date, spot_query, overrides


def telegram_webhook(request: HttpRequest, secret: str) -> HttpResponse:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not token or not webhook_secret or secret != webhook_secret:
        return JsonResponse({"error": "Forbidden"}, status=403)
    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    message = payload.get("message") or payload.get("edited_message")
    callback = payload.get("callback_query")

    if callback:
        data = callback.get("data") or ""
        chat_id = callback.get("message", {}).get("chat", {}).get("id")
        if not chat_id:
            return JsonResponse({"status": "ok"})
        settings = TelegramSettings.objects.filter(chat_ids__icontains=str(chat_id)).first()
        if not settings:
            _telegram_send(chat_id, "Для этого чата нет настроек. Зайдите в /telegram/ и добавьте chat_id.")
            return JsonResponse({"status": "ok"})
        if data.startswith("report|"):
            raw = data.split("|", 1)[1]
            date, spot_query, overrides = _parse_telegram_args([p for p in raw.split(";") if p])
            if date is None:
                date = timezone.localdate()
            if overrides.get("per_spot"):
                summary = daily_summary_by_spot(date)
                if settings.spot_ids:
                    allowed = {str(s) for s in settings.spot_ids}
                    summary = {k: v for k, v in summary.items() if str(k) in allowed}
                for spot_id, item in summary.items():
                    if item["revenue"] <= 0 and item["transactions"] <= 0:
                        continue
                    name = get_spot_name(spot_id)
                    lines = [
                        f"{name} за {date.isoformat()}",
                        f"Выручка: {item['revenue']/100:.2f} €",
                        f"Чеков: {item['transactions']}",
                    ]
                    if settings.include_returns:
                        lines.append(f"Возвратов: {item['returns']}")
                    _telegram_send(chat_id, "\n".join(lines))
            else:
                text = build_report_text(
                    date,
                    metrics=overrides.get("metrics") or (settings.metrics or None),
                    include_spots=overrides.get("include_spots", settings.include_spots),
                    include_issues=overrides.get("include_issues", settings.include_issues),
                    include_returns=overrides.get("include_returns", settings.include_returns),
                    spot_ids=settings.spot_ids or None,
                )
                _telegram_send(chat_id, text)
        return JsonResponse({"status": "ok"})

    if not message:
        return JsonResponse({"status": "ok"})

    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return JsonResponse({"status": "ok"})

    text = (message.get("text") or "").strip()
    settings = TelegramSettings.objects.filter(chat_ids__icontains=str(chat_id)).first()
    if not settings and text not in {"/start", "/help"}:
        _telegram_send(chat_id, "Для этого чата нет настроек. Зайдите в /telegram/ и добавьте chat_id.")
        return JsonResponse({"status": "ok"})

    if text.startswith("/start"):
        if not settings:
            PendingTelegramChat.objects.update_or_create(
                chat_id=chat_id,
                defaults={
                    "title": message.get("chat", {}).get("title") or message.get("chat", {}).get("username") or "",
                    "chat_type": message.get("chat", {}).get("type") or "",
                },
            )
        _telegram_send(
            chat_id,
            f"Ваш chat_id: {chat_id}\nЭтот чат сохранён. Откройте /telegram/ и нажмите «Подключить чат».\nКоманды:\n/report — отчет\n/issues — проблемы\n/spots — точки\n/spot <id|name>\n/settings — настройки",
        )
        return JsonResponse({"status": "ok"})

    if text.startswith("/settings"):
        if not settings:
            _telegram_send(chat_id, "Настройки не найдены. Зайдите в /telegram/ и сохраните настройки.")
            return JsonResponse({"status": "ok"})
        lines = [
            "Настройки Telegram:",
            f"Авто‑отчет: {'да' if settings.auto_daily else 'нет'}",
            f"Отдельно по точкам: {'да' if settings.auto_per_spot else 'нет'}",
            f"Авто при закрытии смены: {'да' if settings.auto_shift_close else 'нет'}",
            f"Время: {settings.daily_time} ({settings.timezone})",
            f"Метрики: {', '.join(settings.metrics or []) or 'по умолчанию'}",
            f"Точки: {', '.join(settings.spot_ids or []) or 'все'}",
        ]
        _telegram_send(chat_id, "\n".join(lines))
        return JsonResponse({"status": "ok"})

    if text.startswith("/spots"):
        spots = Spot.objects.order_by("spot_id")
        if not spots:
            _telegram_send(chat_id, "Нет справочника точек. Импортируйте данные.")
            return JsonResponse({"status": "ok"})
        lines = ["Точки:"] + [f"• {s.spot_id}: {s.name}" for s in spots]
        _telegram_send(chat_id, "\n".join(lines))
        return JsonResponse({"status": "ok"})

    if text.startswith("/spot"):
        parts = text.split()
        if len(parts) < 2:
            _telegram_send(chat_id, "Использование: /spot <id|name> [YYYY-MM-DD]")
            return JsonResponse({"status": "ok"})
        date = timezone.localdate()
        query = " ".join(parts[1:])
        if parts[-1].count("-") == 2:
            try:
                date = dt.date.fromisoformat(parts[-1])
                query = " ".join(parts[1:-1])
            except ValueError:
                pass
        spot = Spot.objects.filter(spot_id=query).first() or Spot.objects.filter(name__icontains=query).first()
        if not spot:
            _telegram_send(chat_id, "Точка не найдена.")
            return JsonResponse({"status": "ok"})
        summary = daily_summary_by_spot(date)
        item = summary.get(spot.spot_id)
        if not item:
            _telegram_send(chat_id, f"За {date.isoformat()} по точке {spot.name} данных нет.")
            return JsonResponse({"status": "ok"})
        lines = [
            f"{spot.name} за {date.isoformat()}",
            f"Выручка: {item['revenue']/100:.2f} €",
            f"Чеков: {item['transactions']}",
            f"Возвратов: {item['returns']}",
        ]
        _telegram_send(chat_id, "\n".join(lines))
        return JsonResponse({"status": "ok"})

    if text.startswith("/issues"):
        parts = text.split()
        date = timezone.localdate()
        if len(parts) >= 2:
            try:
                date = dt.date.fromisoformat(parts[1])
            except ValueError:
                pass
        issues = DataIssue.objects.filter(date=date, ignored=False)
        if not issues:
            _telegram_send(chat_id, f"Проблем за {date.isoformat()} нет.")
            return JsonResponse({"status": "ok"})
        lines = [f"Проблемы за {date.isoformat()}:"] + [f"• {i.message}" for i in issues[:10]]
        if issues.count() > 10:
            lines.append(f"… и еще {issues.count() - 10}")
        _telegram_send(chat_id, "\n".join(lines))
        base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if base_url:
            buttons = []
            for issue in issues[:5]:
                tx_id = (issue.context or {}).get("transaction_id")
                row = [{"text": "Проблема", "url": f"{base_url}/reports/issues/{issue.id}/"}]
                if tx_id:
                    row.append({"text": "Чек", "url": f"{base_url}/reports/transactions/{tx_id}/"})
                buttons.append(row)
            _telegram_send(chat_id, "Открыть детали:", {"inline_keyboard": buttons})
        return JsonResponse({"status": "ok"})

    if text.startswith("/report"):
        args = text.split()[1:]
        date, spot_query, overrides = _parse_telegram_args(args)
        if date is None:
            date = timezone.localdate()
        if spot_query:
            spot = Spot.objects.filter(spot_id=spot_query).first() or Spot.objects.filter(name__icontains=spot_query).first()
            if not spot:
                _telegram_send(chat_id, "Точка не найдена.")
                return JsonResponse({"status": "ok"})
            summary = daily_summary_by_spot(date)
            item = summary.get(spot.spot_id)
            if not item:
                _telegram_send(chat_id, f"За {date.isoformat()} по точке {spot.name} данных нет.")
                return JsonResponse({"status": "ok"})
            lines = [
                f"{spot.name} за {date.isoformat()}",
                f"Выручка: {item['revenue']/100:.2f} €",
                f"Чеков: {item['transactions']}",
            ]
            if overrides.get("include_returns", True):
                lines.append(f"Возвратов: {item['returns']}")
            _telegram_send(chat_id, "\n".join(lines))
        else:
            text = build_report_text(
                date,
                metrics=overrides.get("metrics") or (settings.metrics or None),
                include_spots=overrides.get("include_spots", settings.include_spots),
                include_issues=overrides.get("include_issues", settings.include_issues),
                include_returns=overrides.get("include_returns", settings.include_returns),
                spot_ids=settings.spot_ids or None,
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "Выручка+чеки", "callback_data": "report|metrics=revenue,transactions"},
                        {"text": "Только выручка", "callback_data": "report|metrics=revenue"},
                    ],
                    [
                        {"text": "Без проблем", "callback_data": "report|issues=0"},
                        {"text": "Без возвратов", "callback_data": "report|returns=0"},
                    ],
                    [
                        {"text": "По точкам", "callback_data": "report|per_spot=1"},
                    ],
                ]
            }
            _telegram_send(chat_id, text, keyboard)
        return JsonResponse({"status": "ok"})

    return JsonResponse({"status": "ok"})
