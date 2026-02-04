from django.contrib import admin

from reports.models import (
    DailyReport,
    DataIssue,
    PaymentsReport,
    PosterAccount,
    ProductsSalesReport,
    SpotsSalesReport,
    ReportTemplate,
    Transaction,
    UserPosterAccount,
    Insight,
    Spot,
    TelegramSettings,
)


@admin.register(PosterAccount)
class PosterAccountAdmin(admin.ModelAdmin):
    list_display = ("account", "account_number", "created_at", "updated_at")


@admin.register(UserPosterAccount)
class UserPosterAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "account_base_url", "auth_style", "is_active", "updated_at")


@admin.register(Insight)
class InsightAdmin(admin.ModelAdmin):
    list_display = ("date", "title", "severity", "created_at")
    list_filter = ("severity",)


@admin.register(TelegramSettings)
class TelegramSettingsAdmin(admin.ModelAdmin):
    list_display = ("user", "chat_ids", "auto_daily", "daily_time", "timezone")


@admin.register(DailyReport)
class DailyReportAdmin(admin.ModelAdmin):
    list_display = ("date", "transactions_count", "revenue", "avg_check", "updated_at")
    list_filter = ("date",)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("transaction_id", "status", "sum", "payed_sum", "date_start", "date_close")
    list_filter = ("status",)
    search_fields = ("transaction_id", "user_id", "spot_id", "table_id")


@admin.register(Spot)
class SpotAdmin(admin.ModelAdmin):
    list_display = ("spot_id", "name")


@admin.register(PaymentsReport)
class PaymentsReportAdmin(admin.ModelAdmin):
    list_display = ("date", "updated_at")


@admin.register(ProductsSalesReport)
class ProductsSalesReportAdmin(admin.ModelAdmin):
    list_display = ("date", "updated_at")


@admin.register(SpotsSalesReport)
class SpotsSalesReportAdmin(admin.ModelAdmin):
    list_display = ("date", "updated_at")


@admin.register(DataIssue)
class DataIssueAdmin(admin.ModelAdmin):
    list_display = ("date", "issue_type", "severity", "ignored", "created_at")
    list_filter = ("issue_type", "severity", "ignored")


@admin.register(ReportTemplate)
class ReportTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at", "updated_at")

# Register your models here.
