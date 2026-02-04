import datetime as dt
import os
from typing import List, Optional, Tuple

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

from reports.models import DailyReport, DataIssue, Spot, TelegramSettings, ReportTemplate
from reports.services import daily_summary_by_spot, get_spot_name, build_report_text, build_custom_report_text


def _get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise CommandError(f"Missing env var: {name}")
    return value


def _parse_chat_ids(value: str) -> List[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


def _format_currency(cents: int) -> str:
    return f"{cents/100:.2f} €"


async def _send_per_spot(context: ContextTypes.DEFAULT_TYPE, settings: TelegramSettings, date: dt.date):
    summary = await _get_summary(date)
    if settings.spot_ids:
        allowed = {str(s) for s in settings.spot_ids}
        summary = {k: v for k, v in summary.items() if str(k) in allowed}
    if not summary:
        return
    ids = _parse_chat_ids(settings.chat_ids)
    for spot_id, item in summary.items():
        if item["revenue"] <= 0 and item["transactions"] <= 0:
            continue
        name = get_spot_name(spot_id)
        lines = [
            f"{name} за {date.isoformat()}",
            f"Выручка: {_format_currency(item['revenue'])}",
            f"Чеков: {item['transactions']}",
        ]
        if settings.include_returns:
            lines.append(f"Возвратов: {item['returns']}")
        text = "\n".join(lines)
        for cid in ids:
            await context.bot.send_message(chat_id=cid, text=text)


@sync_to_async
def _get_report(date: dt.date):
    return DailyReport.objects.filter(date=date).first()


@sync_to_async
def _get_issues(date: dt.date):
    return list(DataIssue.objects.filter(date=date, ignored=False))


@sync_to_async
def _get_summary(date: dt.date):
    return daily_summary_by_spot(date)


@sync_to_async
def _get_spots():
    return list(Spot.objects.all().order_by("spot_id"))


@sync_to_async
def _find_spot(query: str):
    return Spot.objects.filter(spot_id=query).first() or Spot.objects.filter(name__icontains=query).first()


@sync_to_async
def _get_settings_by_chat(chat_id: int):
    return TelegramSettings.objects.filter(chat_ids__icontains=str(chat_id)).first()


@sync_to_async
def _get_all_settings():
    return list(TelegramSettings.objects.all())


def _parse_report_args(args: List[str]) -> Tuple[Optional[dt.date], Optional[str], dict]:
    date = None
    spot_query = None
    overrides = {}
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
        if token.startswith("per_spot="):
            overrides["per_spot"] = token.split("=", 1)[1].strip() not in {"0", "false", "no"}
            continue
        if token.startswith("template="):
            overrides["template_name"] = token.split("=", 1)[1].strip()
            continue
        if not spot_query:
            spot_query = token
    return date, spot_query, overrides


def _parse_overrides_string(raw: str) -> Tuple[Optional[dt.date], Optional[str], dict]:
    args = [part for part in raw.split(";") if part]
    return _parse_report_args(args)


async def _build_report(date: dt.date, chat_id: Optional[int] = None, overrides: Optional[dict] = None) -> str:
    overrides = overrides or {}
    settings = None
    if chat_id is not None:
        settings = await _get_settings_by_chat(chat_id)
    template_name = overrides.get("template_name") or (settings.template_name if settings else "")
    if template_name:
        template = await sync_to_async(lambda: ReportTemplate.objects.filter(name=template_name).first())()
        if template:
            return await sync_to_async(build_custom_report_text)(date, template)
    metrics = overrides.get("metrics") or (settings.metrics if settings and settings.metrics else None)
    include_spots = overrides.get("include_spots", settings.include_spots if settings else True)
    include_issues = overrides.get("include_issues", settings.include_issues if settings else True)
    include_returns = overrides.get("include_returns", settings.include_returns if settings else True)
    return await sync_to_async(build_report_text)(
        date,
        metrics=metrics,
        include_spots=include_spots,
        include_issues=include_issues,
        include_returns=include_returns,
        spot_ids=settings.spot_ids if settings else None,
    )


class Command(BaseCommand):
    help = "Run Telegram bot for daily reports"

    def handle(self, *args, **options):
        if os.getenv("TELEGRAM_WEBHOOK_SECRET"):
            raise CommandError("Webhook mode enabled. Do not run polling bot.")
        token = _get_env("TELEGRAM_BOT_TOKEN")

        async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
            date, spot_query, overrides = _parse_report_args(context.args or [])
            if date is None:
                date = timezone.localdate()
            settings = await _get_settings_by_chat(update.effective_chat.id)
            if not settings:
                await update.message.reply_text(
                    "Для этого чата нет настроек. Зайдите в /telegram/ и добавьте chat_id."
                )
                return
            if spot_query:
                spot = await _find_spot(spot_query)
                if not spot:
                    await update.message.reply_text("Точка не найдена.")
                    return
                summary = await _get_summary(date)
                item = summary.get(spot.spot_id)
                if not item:
                    await update.message.reply_text(f"За {date.isoformat()} по точке {spot.name} данных нет.")
                    return
                lines = [
                    f"{spot.name} за {date.isoformat()}",
                    f"Выручка: {_format_currency(item['revenue'])}",
                    f"Чеков: {item['transactions']}",
                ]
                if overrides.get("include_returns", True):
                    lines.append(f"Возвратов: {item['returns']}")
                await update.message.reply_text("\n".join(lines))
            else:
                text = await _build_report(date, update.effective_chat.id, overrides=overrides)
                keyboard = InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("Выручка+чеки", callback_data="report|metrics=revenue,transactions"),
                            InlineKeyboardButton("Только выручка", callback_data="report|metrics=revenue"),
                        ],
                        [
                            InlineKeyboardButton("Без проблем", callback_data="report|issues=0"),
                            InlineKeyboardButton("Без возвратов", callback_data="report|returns=0"),
                        ],
                        [
                            InlineKeyboardButton("По точкам", callback_data="report|per_spot=1"),
                        ],
                    ]
                )
                await update.message.reply_text(text, reply_markup=keyboard)

            issues = await _get_issues(date)
            base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
            if issues and base_url:
                buttons = []
                for issue in issues[:5]:
                    tx_id = (issue.context or {}).get("transaction_id")
                    row = []
                    row.append(InlineKeyboardButton("Проблема", url=f"{base_url}/reports/issues/{issue.id}/"))
                    if tx_id:
                        row.append(InlineKeyboardButton("Чек", url=f"{base_url}/reports/transactions/{tx_id}/"))
                    buttons.append(row)
                await update.message.reply_text("Открыть детали:", reply_markup=InlineKeyboardMarkup(buttons))

        async def cmd_spots(update: Update, context: ContextTypes.DEFAULT_TYPE):
            settings = await _get_settings_by_chat(update.effective_chat.id)
            if not settings:
                await update.message.reply_text(
                    "Для этого чата нет настроек. Зайдите в /telegram/ и добавьте chat_id."
                )
                return
            spots = await _get_spots()
            if not spots:
                await update.message.reply_text("Нет справочника точек. Добавьте в админке.")
                return
            lines = ["Точки:"]
            for s in spots:
                lines.append(f"• {s.spot_id}: {s.name}")
            await update.message.reply_text("\n".join(lines))

        async def cmd_spot(update: Update, context: ContextTypes.DEFAULT_TYPE):
            settings = await _get_settings_by_chat(update.effective_chat.id)
            if not settings:
                await update.message.reply_text(
                    "Для этого чата нет настроек. Зайдите в /telegram/ и добавьте chat_id."
                )
                return
            if not context.args:
                await update.message.reply_text("Использование: /spot <id> или /spot <name>")
                return
            date = timezone.localdate()
            if len(context.args) >= 2 and context.args[-1].count("-") == 2:
                try:
                    date = dt.date.fromisoformat(context.args[-1])
                    query = " ".join(context.args[:-1])
                except ValueError:
                    query = " ".join(context.args)
            else:
                query = " ".join(context.args)

            spot = await _find_spot(query)
            if not spot:
                await update.message.reply_text("Точка не найдена.")
                return

            summary = await _get_summary(date)
            item = summary.get(spot.spot_id)
            if not item:
                await update.message.reply_text(f"За {date.isoformat()} по точке {spot.name} данных нет.")
                return
            await update.message.reply_text(
                f"{spot.name} за {date.isoformat()}:\n"
                f"Выручка: {_format_currency(item['revenue'])}\n"
                f"Чеков: {item['transactions']}\n"
                f"Возвратов: {item['returns']}"
            )

        async def cmd_issues(update: Update, context: ContextTypes.DEFAULT_TYPE):
            settings = await _get_settings_by_chat(update.effective_chat.id)
            if not settings:
                await update.message.reply_text(
                    "Для этого чата нет настроек. Зайдите в /telegram/ и добавьте chat_id."
                )
                return
            date = timezone.localdate()
            if context.args:
                try:
                    date = dt.date.fromisoformat(context.args[0])
                except ValueError:
                    await update.message.reply_text("Формат даты: YYYY-MM-DD")
                    return
            issues = await _get_issues(date)
            if not issues:
                await update.message.reply_text(f"Проблем за {date.isoformat()} нет.")
                return
            lines = [f"Проблемы за {date.isoformat()}:"]
            for issue in issues[:10]:
                lines.append(f"• {issue.message}")
            if len(issues) > 10:
                lines.append(f"… и еще {len(issues) - 10}")
            await update.message.reply_text("\n".join(lines))

            base_url = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
            if issues and base_url:
                buttons = []
                for issue in issues[:5]:
                    tx_id = (issue.context or {}).get("transaction_id")
                    row = [InlineKeyboardButton("Проблема", url=f"{base_url}/reports/issues/{issue.id}/")]
                    if tx_id:
                        row.append(InlineKeyboardButton("Чек", url=f"{base_url}/reports/transactions/{tx_id}/"))
                    buttons.append(row)
                await update.message.reply_text("Открыть детали:", reply_markup=InlineKeyboardMarkup(buttons))

        async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
            await update.message.reply_text(
                "/report — отчет за сегодня\n"
                "/report YYYY-MM-DD — отчет за дату\n"
                "/report spot=ID — отчет по точке\n"
                "/report spot=ID metrics=revenue,avg_check — выбор метрик\n"
                "/report issues=0 returns=0 — скрыть разделы\n"
                "После /report доступны кнопки быстрых фильтров\n"
                "/spots — список точек\n"
                "/spot <id|name> [YYYY-MM-DD] — отчет по точке\n"
                "/issues [YYYY-MM-DD] — сомнительные операции\n"
                "/settings — текущие настройки"
            )

        async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            chat_id = update.effective_chat.id
            await update.message.reply_text(
                f"Ваш chat_id: {chat_id}\n"
                "Команды:\n"
                "/report — отчет за сегодня\n"
                "/report YYYY-MM-DD — отчет за дату\n"
                "/spots — список точек\n"
                "/spot <id|name> [YYYY-MM-DD] — отчет по точке\n"
                "/issues [YYYY-MM-DD] — сомнительные операции\n"
                "/settings — текущие настройки"
            )

        async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
            settings = await _get_settings_by_chat(update.effective_chat.id)
            if not settings:
                await update.message.reply_text("Настройки не найдены. Зайдите в /telegram/ и сохраните настройки.")
                return
            lines = [
                "Настройки Telegram:",
                f"Авто‑отчет: {'да' if settings.auto_daily else 'нет'}",
                f"Отдельно по точкам: {'да' if settings.auto_per_spot else 'нет'}",
                f"Авто при закрытии смены: {'да' if settings.auto_shift_close else 'нет'}",
                f"Время: {settings.daily_time} ({settings.timezone})",
                f"Метрики: {', '.join(settings.metrics or []) or 'по умолчанию'}",
                f"Точки: {', '.join(settings.spot_ids or []) or 'все'}",
                f"Проблемы: {'да' if settings.include_issues else 'нет'}",
                f"Возвраты: {'да' if settings.include_returns else 'нет'}",
            ]
            await update.message.reply_text("\n".join(lines))

        async def cmd_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
            query = update.callback_query
            if not query or not query.data:
                return
            await query.answer()
            if not query.data.startswith("report|"):
                return
            raw = query.data.split("|", 1)[1]
            date, spot_query, overrides = _parse_overrides_string(raw)
            if date is None:
                date = timezone.localdate()
            if overrides.get("per_spot"):
                settings = await _get_settings_by_chat(query.message.chat_id)
                if settings:
                    await _send_per_spot(context, settings, date)
                return
            text = await _build_report(date, query.message.chat_id, overrides=overrides)
            await query.message.reply_text(text)

        async def send_daily_for(settings: TelegramSettings, context: ContextTypes.DEFAULT_TYPE):
            date = timezone.localdate()
            if settings.last_sent_date == date:
                return
            ids = _parse_chat_ids(settings.chat_ids)
            if settings.auto_per_spot:
                await _send_per_spot(context, settings, date)
            text = await _build_report(date, ids[0] if ids else None)
            for cid in ids:
                await context.bot.send_message(chat_id=cid, text=text)
            settings.last_sent_date = date
            await sync_to_async(settings.save)()

        app = ApplicationBuilder().token(token).build()
        app.add_handler(CommandHandler("start", cmd_start))
        app.add_handler(CommandHandler("help", cmd_help))
        app.add_handler(CommandHandler("report", cmd_report))
        app.add_handler(CommandHandler("spots", cmd_spots))
        app.add_handler(CommandHandler("spot", cmd_spot))
        app.add_handler(CommandHandler("issues", cmd_issues))
        app.add_handler(CommandHandler("settings", cmd_settings))
        app.add_handler(CallbackQueryHandler(cmd_callback))

        # schedule daily report
        try:
            from zoneinfo import ZoneInfo
        except Exception:
            ZoneInfo = None

        async def tick(context: ContextTypes.DEFAULT_TYPE):
            settings_list = await _get_all_settings()
            for settings in settings_list:
                if not settings.auto_daily or not settings.chat_ids:
                    continue
                tz = None
                if ZoneInfo and settings.timezone:
                    try:
                        tz = ZoneInfo(settings.timezone)
                    except Exception:
                        tz = None
                now = dt.datetime.now(tz or timezone.get_current_timezone())
                try:
                    hour, minute = [int(x) for x in (settings.daily_time or "23:59").split(":")]
                except Exception:
                    hour, minute = (23, 59)
                if now.hour == hour and now.minute == minute:
                    await send_daily_for(settings, context)

        app.job_queue.run_repeating(tick, interval=60, first=0)

        self.stdout.write(self.style.SUCCESS("Telegram bot running"))
        app.run_polling()
