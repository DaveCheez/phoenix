import uuid
from decimal import Decimal, ROUND_HALF_UP

from django.db import models


class Order(models.Model):
    STATUS_CHOICES = [
        ("pending_deposit", "Pending deposit"),
        ("deposit_paid", "Deposit paid"),
        ("balance_due", "Balance due"),
        ("paid", "Paid in full"),
        ("cancelled", "Cancelled"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    cart = models.ForeignKey("cart.Cart", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    balance_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default="pending_deposit")
    currency = models.CharField(max_length=3, default="gbp")
    customer_name = models.CharField(max_length=200)
    customer_email = models.EmailField()
    address_line1 = models.CharField(max_length=255, blank=True)
    address_city = models.CharField(max_length=120, blank=True)
    address_postcode = models.CharField(max_length=30, blank=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    deposit_amount = models.DecimalField(max_digits=10, decimal_places=2)
    balance_amount = models.DecimalField(max_digits=10, decimal_places=2)
    stripe_deposit_session_id = models.CharField(max_length=255, blank=True, null=True, unique=True)
    stripe_deposit_payment_intent_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_balance_session_id = models.CharField(max_length=255, blank=True, null=True)
    deposit_paid_at = models.DateTimeField(blank=True, null=True)
    balance_paid_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-created_at",)

    @staticmethod
    def calculate_deposit(total: Decimal) -> Decimal:
        return (total / Decimal("3")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def is_paid_in_full(self):
        return self.status == "paid"

    def __str__(self):
        return f"Order {self.id} - {self.customer_name} - £{self.total_amount}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, related_name="items", on_delete=models.CASCADE)
    product_id = models.PositiveBigIntegerField()
    product_name = models.CharField(max_length=200)
    sku = models.CharField(max_length=100, blank=True)
    quantity = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    def __str__(self):
        return f"{self.product_name} x {self.quantity}"
