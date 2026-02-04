import datetime as dt
from typing import Any, Dict, List, Optional

from django.utils import timezone

import os
import requests

from reports.models import (
    DailyReport,
    PaymentsReport,
    ProductsSalesReport,
    SpotsSalesReport,
    Transaction,
    DataIssue,
    Insight,
    Spot,
    ReportTemplate,
)
from reports.poster_client import PosterAPIError, PosterClient


def _yyyymmdd(value: dt.date) -> str:
    return value.strftime("%Y%m%d")


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_ms(value: Any):
    try:
        ms = int(value)
    except (TypeError, ValueError):
        return None
    if ms <= 0:
        return None
    return dt.datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _extract_transactions(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("response"), list):
        return data["response"]
    return []


def import_daily(
    client: PosterClient,
    report_date: dt.date,
    include_products_sales: bool = False,
) -> int:
    date_from = _yyyymmdd(report_date)
    date_to = _yyyymmdd(report_date)

    transactions = client.get(
        "dash.getTransactions",
        params={"dateFrom": date_from, "dateTo": date_to},
    )
    payments = client.get(
        "dash.getPaymentsReport",
        params={"date_from": date_from, "date_to": date_to},
    )

    products_sales: Dict[str, Any] = {}
    if include_products_sales:
        try:
            products_sales = client.get(
                "dash.getProductsSales",
                params={"dateFrom": date_from, "dateTo": date_to},
            )
        except PosterAPIError:
            products_sales = {}

    # Spots sales for точек (matches Poster Asutused view)
    spots_sales: Dict[str, Any] = {}
    try:
        spots_sales = client.get(
            "dash.getSpotsSales",
            params={"dateFrom": date_from, "dateTo": date_to},
        )
    except PosterAPIError:
        spots_sales = {}

    # Sync spots list
    try:
        spots_list = client.get("spots.getSpots")
        if isinstance(spots_list, dict) and isinstance(spots_list.get("response"), list):
            for row in spots_list["response"]:
                spot_id = str(row.get("spot_id") or "")
                name = str(row.get("name") or "")
                if spot_id and name:
                    Spot.objects.update_or_create(spot_id=spot_id, defaults={"name": name})
    except PosterAPIError:
        pass

    items = _extract_transactions(transactions)
    transactions_count = len(items)
    revenue = sum(_safe_int(item.get("sum")) for item in items)
    avg_check = int(revenue / transactions_count) if transactions_count else 0

    for item in items:
        transaction_id = str(item.get("transaction_id") or "")
        if not transaction_id:
            continue
        Transaction.objects.update_or_create(
            transaction_id=transaction_id,
            defaults={
                "date_start": _parse_ms(item.get("date_start")),
                "date_close": _parse_ms(item.get("date_close")),
                "status": _safe_int(item.get("status")),
                "sum": _safe_int(item.get("sum")),
                "payed_sum": _safe_int(item.get("payed_sum")),
                "payed_cash": _safe_int(item.get("payed_cash")),
                "payed_card": _safe_int(item.get("payed_card")),
                "payed_bonus": _safe_int(item.get("payed_bonus")),
                "payed_third_party": _safe_int(item.get("payed_third_party")),
                "payed_cert": _safe_int(item.get("payed_cert")),
                "spot_id": str(item.get("spot_id") or ""),
                "table_id": str(item.get("table_id") or ""),
                "user_id": str(item.get("user_id") or ""),
                "client_id": str(item.get("client_id") or ""),
                "service_mode": str(item.get("service_mode") or ""),
                "processing_status": str(item.get("processing_status") or ""),
                "raw": item,
            },
        )

    PaymentsReport.objects.update_or_create(
        date=report_date, defaults={"raw": payments}
    )
    if products_sales:
        ProductsSalesReport.objects.update_or_create(
            date=report_date, defaults={"raw": products_sales}
        )
    if spots_sales:
        SpotsSalesReport.objects.update_or_create(
            date=report_date, defaults={"raw": spots_sales}
        )

    # Prefer official spots sales totals for daily revenue/transactions if available
    if isinstance(spots_sales, dict):
        response = spots_sales.get("response")
        # Case 1: aggregate dict (values already in EUR)
        if isinstance(response, dict) and response.get("revenue") is not None:
            total_revenue_eur = float(response.get("revenue") or 0)
            total_tx = _safe_int(response.get("clients") or response.get("transactions_count"))
            if total_revenue_eur >= 0:
                revenue = int(round(total_revenue_eur * 100))
                transactions_count = total_tx
                middle_invoice = response.get("middle_invoice")
                if middle_invoice is not None:
                    try:
                        avg_check = int(round(float(middle_invoice) * 100))
                    except (TypeError, ValueError):
                        avg_check = int(revenue / transactions_count) if transactions_count else 0
                else:
                    avg_check = int(revenue / transactions_count) if transactions_count else 0
        # Case 2: list of spots (values typically in cents)
        elif isinstance(response, list):
            total_revenue = 0
            total_tx = 0
            for row in response:
                total_revenue += _safe_int(row.get("revenue") or row.get("sum"))
                total_tx += _safe_int(row.get("transactions_count") or row.get("count") or row.get("clients"))
            if total_revenue >= 0:
                revenue = total_revenue
                transactions_count = total_tx
                avg_check = int(revenue / transactions_count) if transactions_count else 0

    DailyReport.objects.update_or_create(
        date=report_date,
        defaults={
            "transactions_count": transactions_count,
            "revenue": revenue,
            "avg_check": avg_check,
            "raw_transactions": transactions,
            "raw_payments": payments,
            "raw_products_sales": products_sales,
        },
    )

    return transactions_count


def scan_anomalies(report_date: dt.date) -> List[DataIssue]:
    transactions = Transaction.objects.filter(date_start__date=report_date)
    existing_ignored = {
        (
            issue.issue_type,
            (issue.context or {}).get("transaction_id"),
        )
        for issue in DataIssue.objects.filter(date=report_date, ignored=True)
    }
    issues = []

    for tx in transactions:
        if tx.status == 2 and tx.sum <= 0:
            if ("zero_or_negative_sum", tx.transaction_id) not in existing_ignored:
                issues.append(
                    DataIssue(
                        date=report_date,
                        issue_type="zero_or_negative_sum",
                        severity=2,
                        message=f"Чек закрыт с нулевой/отрицательной суммой: {tx.transaction_id}",
                        context={"transaction_id": tx.transaction_id, "sum": tx.sum},
                    )
                )

        if tx.payed_sum > 0:
            total = (
                tx.payed_cash
                + tx.payed_card
                + tx.payed_bonus
                + tx.payed_third_party
                + tx.payed_cert
            )
            if total != tx.payed_sum:
                if ("payment_mismatch", tx.transaction_id) not in existing_ignored:
                    issues.append(
                        DataIssue(
                            date=report_date,
                            issue_type="payment_mismatch",
                            severity=2,
                            message=f"Несоответствие оплат по чеку: {tx.transaction_id}",
                            context={
                                "transaction_id": tx.transaction_id,
                                "payed_sum": tx.payed_sum,
                                "payed_parts": total,
                            },
                        )
                    )
        # Suspicious: table moved
        history = None
        if isinstance(tx.raw, dict):
            history = tx.raw.get("history")
        if isinstance(history, list):
            moved = False
            for h in history:
                t = str(h.get("type_history") or "").lower()
                if any(k in t for k in ["transfer", "move", "change_table", "change table", "change_table_id"]):
                    moved = True
                    break
            if moved and ("table_move", tx.transaction_id) not in existing_ignored:
                waiter = None
                if isinstance(tx.raw, dict):
                    waiter = tx.raw.get("name") or tx.raw.get("waiter") or tx.raw.get("user_name")
                when = tx.date_close or tx.date_start
                when_text = timezone.localtime(when).strftime("%Y-%m-%d %H:%M") if when else "—"
                sum_eur = f"{tx.sum/100:.2f} €"
                issues.append(
                    DataIssue(
                        date=report_date,
                        issue_type="table_move",
                        severity=1,
                        message=(
                            f"Перенос на другой стол: чек {tx.transaction_id}, "
                            f"официант {waiter or '—'}, время {when_text}, сумма {sum_eur}"
                        ),
                        context={
                            "transaction_id": tx.transaction_id,
                            "waiter": waiter,
                            "time": when_text,
                            "sum": tx.sum,
                        },
                    )
                )

    report = DailyReport.objects.filter(date=report_date).first()
    if report and report.transactions_count == 0:
        if ("no_transactions", None) not in existing_ignored:
            issues.append(
            DataIssue(
                date=report_date,
                issue_type="no_transactions",
                severity=1,
                message="Нет чеков за день",
                context={},
            )
            )

    DataIssue.objects.filter(date=report_date, ignored=False).delete()
    DataIssue.objects.bulk_create(issues)
    return issues


def generate_insights(report_date: dt.date) -> int:
    # Simple insights: revenue drop, zero transactions, avg check drop
    current = DailyReport.objects.filter(date=report_date).first()
    if not current:
        return 0

    prev = DailyReport.objects.filter(date__lt=report_date).order_by("-date").first()
    insights = []

    if current.transactions_count == 0:
        insights.append(
            Insight(
                date=report_date,
                title="Нет продаж",
                severity=2,
                details="За выбранный день нет чеков. Проверьте, работала ли касса.",
            )
        )

    if prev and prev.revenue > 0:
        drop = (prev.revenue - current.revenue) / prev.revenue
        if drop >= 0.3:
            insights.append(
                Insight(
                    date=report_date,
                    title="Падение выручки",
                    severity=2,
                    details=f"Выручка упала на {int(drop*100)}% по сравнению с предыдущим днем.",
                )
            )

    if prev and prev.avg_check > 0:
        drop = (prev.avg_check - current.avg_check) / prev.avg_check
        if drop >= 0.3:
            insights.append(
                Insight(
                    date=report_date,
                    title="Падение среднего чека",
                    severity=1,
                    details=f"Средний чек упал на {int(drop*100)}% по сравнению с предыдущим днем.",
                )
            )

    Insight.objects.filter(date=report_date).delete()
    Insight.objects.bulk_create(insights)
    return len(insights)


def daily_summary_by_spot(report_date: dt.date):
    # Prefer official spots sales report if available
    spots_report = SpotsSalesReport.objects.filter(date=report_date).first()
    if spots_report and isinstance(spots_report.raw, dict):
        rows = spots_report.raw.get("response", [])
        if isinstance(rows, dict):
            # Some responses return totals dict, not per-spot rows
            rows = rows.get("spots", [])
        summary = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            spot_id = str(row.get("spot_id") or row.get("id") or "")
            if not spot_id:
                continue
            summary[spot_id] = {
                "transactions": int(row.get("transactions_count") or row.get("count") or 0),
                "revenue": int(row.get("revenue") or row.get("sum") or 0),
                "returns": int(row.get("returns_count") or 0),
                "returns_sum": int(row.get("returns_sum") or 0),
            }
        if summary:
            return summary

    # Fallback: compute from transactions
    transactions = Transaction.objects.filter(date_start__date=report_date)
    summary = {}
    for tx in transactions:
        key = tx.spot_id or "unknown"
        item = summary.setdefault(
            key,
            {"transactions": 0, "revenue": 0, "returns": 0, "returns_sum": 0},
        )
        item["transactions"] += 1
        item["revenue"] += tx.sum
        if tx.status == 3 or tx.sum < 0:
            item["returns"] += 1
            item["returns_sum"] += tx.sum
    return summary


def get_spot_name(spot_id: str) -> str:
    if not spot_id:
        return "Неизвестная точка"
    spot = Spot.objects.filter(spot_id=str(spot_id)).first()
    return spot.name if spot else f"Точка {spot_id}"


def send_telegram_message(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = os.getenv("TELEGRAM_CHAT_IDS", "")
    if not token or not chat_ids:
        return
    ids = [c.strip() for c in chat_ids.split(",") if c.strip()]
    for cid in ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": int(cid), "text": text},
                timeout=10,
            )
        except Exception:
            pass


def send_telegram_message_to(chat_ids: List[int], text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or not chat_ids:
        return
    for cid in chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": int(cid), "text": text},
                timeout=10,
            )
        except Exception:
            pass


def build_report_text(
    report_date: dt.date,
    metrics: Optional[List[str]] = None,
    include_spots: bool = True,
    include_issues: bool = True,
    include_returns: bool = True,
    spot_ids: Optional[List[str]] = None,
) -> str:
    report = DailyReport.objects.filter(date=report_date).first()
    if not report:
        return f"Отчет за {report_date.isoformat()}: данных нет."

    summary = daily_summary_by_spot(report_date)
    if spot_ids:
        allowed = {str(s) for s in spot_ids}
        summary = {k: v for k, v in summary.items() if str(k) in allowed}
    issues = DataIssue.objects.filter(date=report_date, ignored=False)

    metrics = metrics or ["revenue", "transactions", "avg_check", "issues"]
    lines = [f"Отчет за {report_date.isoformat()}"]
    if "revenue" in metrics:
        lines.append(f"Выручка: {report.revenue/100:.2f} €")
    if "transactions" in metrics:
        lines.append(f"Чеков: {report.transactions_count}")
    if "avg_check" in metrics:
        lines.append(f"Средний чек: {report.avg_check/100:.2f} €")
    if "issues" in metrics:
        lines.append(f"Проблем: {issues.count()}")

    if include_spots:
        lines.append("")
        lines.append("По точкам:")
        if not summary:
            lines.append("— нет данных по точкам")
        else:
            for spot_id, item in summary.items():
                name = get_spot_name(spot_id)
                if include_returns:
                    lines.append(
                        f"• {name}: выручка {item['revenue']/100:.2f} €, чеков {item['transactions']}, возвратов {item['returns']}"
                    )
                else:
                    lines.append(
                        f"• {name}: выручка {item['revenue']/100:.2f} €, чеков {item['transactions']}"
                    )

    if include_issues and issues.exists():
        lines.append("")
        lines.append("Сомнительные операции:")
        for issue in issues[:5]:
            lines.append(f"• {issue.message}")
        if issues.count() > 5:
            lines.append(f"… и еще {issues.count() - 5}")
    return "\n".join(lines)


def build_custom_report_text(report_date: dt.date, template: ReportTemplate) -> str:
    config = template.config or {}
    metrics = config.get("metrics") or ["transactions_count", "revenue", "avg_check"]
    report = DailyReport.objects.filter(date=report_date).first()
    if not report:
        return f"Отчет за {report_date.isoformat()}: данных нет."
    values = {
        "transactions_count": report.transactions_count,
        "revenue": f"{report.revenue/100:.2f} €",
        "avg_check": f"{report.avg_check/100:.2f} €",
    }
    lines = [f"{template.name} за {report_date.isoformat()}"]
    for m in metrics:
        label = {
            "transactions_count": "Чеков",
            "revenue": "Выручка",
            "avg_check": "Средний чек",
        }.get(m, m)
        lines.append(f"{label}: {values.get(m, '')}")
    return "\n".join(lines)
