from django.db import migrations, models
import uuid


class Migration(migrations.Migration):
    initial = True
    dependencies = [("cart", "0002_cartitem_constraints_and_timestamps")]

    operations = [
        migrations.CreateModel(
            name="Order",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("cart", models.ForeignKey(blank=True, null=True, on_delete=models.deletion.SET_NULL, related_name="orders", to="cart.cart")),
                ("balance_token", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("status", models.CharField(choices=[("pending_deposit", "Pending deposit"), ("deposit_paid", "Deposit paid"), ("balance_due", "Balance due"), ("paid", "Paid in full"), ("cancelled", "Cancelled")], default="pending_deposit", max_length=30)),
                ("currency", models.CharField(default="gbp", max_length=3)),
                ("customer_name", models.CharField(max_length=200)),
                ("customer_email", models.EmailField(max_length=254)),
                ("address_line1", models.CharField(blank=True, max_length=255)),
                ("address_city", models.CharField(blank=True, max_length=120)),
                ("address_postcode", models.CharField(blank=True, max_length=30)),
                ("total_amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("deposit_amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("balance_amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("stripe_deposit_session_id", models.CharField(blank=True, max_length=255, null=True, unique=True)),
                ("stripe_deposit_payment_intent_id", models.CharField(blank=True, max_length=255, null=True)),
                ("stripe_balance_session_id", models.CharField(blank=True, max_length=255, null=True)),
                ("deposit_paid_at", models.DateTimeField(blank=True, null=True)),
                ("balance_paid_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="OrderItem",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("product_id", models.PositiveBigIntegerField()),
                ("product_name", models.CharField(max_length=200)),
                ("sku", models.CharField(blank=True, max_length=100)),
                ("quantity", models.PositiveIntegerField()),
                ("unit_price", models.DecimalField(decimal_places=2, max_digits=10)),
                ("line_total", models.DecimalField(decimal_places=2, max_digits=10)),
                ("order", models.ForeignKey(on_delete=models.deletion.CASCADE, related_name="items", to="orders.order")),
            ],
        ),
    ]
