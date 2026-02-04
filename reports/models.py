from django.db import models
from django.contrib.auth.models import User


class PosterAccount(models.Model):
    account = models.CharField(max_length=100, unique=True)
    access_token = models.CharField(max_length=255)
    account_number = models.CharField(max_length=50, blank=True)
    user_info = models.JSONField(default=dict, blank=True)
    owner_info = models.JSONField(default=dict, blank=True)
    tariff_info = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.account


class UserPosterAccount(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="poster_accounts")
    account_base_url = models.CharField(max_length=255)
    api_token = models.CharField(max_length=255)
    auth_style = models.CharField(max_length=32, default="query_token")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("user", "account_base_url")

    def __str__(self) -> str:
        return f"{self.user.username} → {self.account_base_url}"


class DailyReport(models.Model):
    date = models.DateField(unique=True)
    transactions_count = models.IntegerField(default=0)
    revenue = models.BigIntegerField(default=0)
    avg_check = models.BigIntegerField(default=0)
    raw_transactions = models.JSONField(default=dict, blank=True)
    raw_payments = models.JSONField(default=dict, blank=True)
    raw_products_sales = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return str(self.date)

    @property
    def revenue_eur(self) -> float:
        return self.revenue / 100

    @property
    def avg_check_eur(self) -> float:
        return self.avg_check / 100


class Transaction(models.Model):
    transaction_id = models.CharField(max_length=32, unique=True)
    date_start = models.DateTimeField(null=True, blank=True)
    date_close = models.DateTimeField(null=True, blank=True)
    status = models.IntegerField(null=True, blank=True)
    sum = models.BigIntegerField(default=0)
    payed_sum = models.BigIntegerField(default=0)
    payed_cash = models.BigIntegerField(default=0)
    payed_card = models.BigIntegerField(default=0)
    payed_bonus = models.BigIntegerField(default=0)
    payed_third_party = models.BigIntegerField(default=0)
    payed_cert = models.BigIntegerField(default=0)
    spot_id = models.CharField(max_length=32, blank=True)
    table_id = models.CharField(max_length=32, blank=True)
    user_id = models.CharField(max_length=32, blank=True)
    client_id = models.CharField(max_length=32, blank=True)
    service_mode = models.CharField(max_length=16, blank=True)
    processing_status = models.CharField(max_length=16, blank=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.transaction_id


class Spot(models.Model):
    spot_id = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)

    def __str__(self) -> str:
        return f"{self.spot_id}: {self.name}"


class PaymentsReport(models.Model):
    date = models.DateField(unique=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return str(self.date)


class ProductsSalesReport(models.Model):
    date = models.DateField(unique=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return str(self.date)


class SpotsSalesReport(models.Model):
    date = models.DateField(unique=True)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return str(self.date)


class DataIssue(models.Model):
    date = models.DateField()
    issue_type = models.CharField(max_length=80)
    severity = models.IntegerField(default=1)
    message = models.TextField()
    context = models.JSONField(default=dict, blank=True)
    ignored = models.BooleanField(default=False)
    ignored_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.date}: {self.issue_type}"


class ReportTemplate(models.Model):
    name = models.CharField(max_length=120, unique=True)
    config = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.name


class Insight(models.Model):
    date = models.DateField()
    title = models.CharField(max_length=120)
    severity = models.IntegerField(default=1)
    details = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"{self.date}: {self.title}"


class TelegramSettings(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="telegram_settings")
    chat_ids = models.CharField(max_length=255, default="")
    auto_daily = models.BooleanField(default=True)
    auto_shift_close = models.BooleanField(default=False)
    auto_per_spot = models.BooleanField(default=False)
    daily_time = models.CharField(max_length=5, default="23:59")
    timezone = models.CharField(max_length=40, default="UTC")
    metrics = models.JSONField(default=list, blank=True)
    spot_ids = models.JSONField(default=list, blank=True)
    include_spots = models.BooleanField(default=True)
    include_issues = models.BooleanField(default=True)
    include_returns = models.BooleanField(default=True)
    template_name = models.CharField(max_length=120, blank=True, default="")
    shift_template_name = models.CharField(max_length=120, blank=True, default="")
    notify_issue_types = models.JSONField(default=list, blank=True)
    last_sent_date = models.DateField(null=True, blank=True)

    def __str__(self) -> str:
        return f"TelegramSettings({self.user.username})"
