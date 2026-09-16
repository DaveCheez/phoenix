from django.db import migrations, models
from django.db.models import Count, Q, Sum
import django.db.models.deletion
import django.utils.timezone


def merge_duplicate_items(apps, schema_editor):
    CartItem = apps.get_model("cart", "CartItem")

    duplicates = (
        CartItem.objects.values("cart_id", "product_id")
        .annotate(row_count=Count("id"), total_quantity=Sum("quantity"))
        .filter(row_count__gt=1)
    )

    for duplicate in duplicates.iterator():
        rows = CartItem.objects.filter(
            cart_id=duplicate["cart_id"],
            product_id=duplicate["product_id"],
        ).order_by("id")
        keep = rows.first()
        if keep is None:
            continue

        keep.quantity = max(1, duplicate["total_quantity"] or 1)
        keep.save(update_fields=["quantity"])
        rows.exclude(id=keep.id).delete()

    CartItem.objects.filter(quantity__lt=1).update(quantity=1)


class Migration(migrations.Migration):
    dependencies = [
        ("cart", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="cart",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="cartitem",
            name="created_at",
            field=models.DateTimeField(auto_now_add=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="cartitem",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="cart",
            name="user",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="carts",
                to="auth.user",
            ),
        ),
        migrations.AlterField(
            model_name="cartitem",
            name="cart",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="items",
                to="cart.cart",
            ),
        ),
        migrations.RunPython(merge_duplicate_items, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="cartitem",
            name="quantity",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AlterModelOptions(
            name="cart",
            options={"ordering": ("-updated_at", "-created_at")},
        ),
        migrations.AlterModelOptions(
            name="cartitem",
            options={"ordering": ("created_at", "id")},
        ),
        migrations.AddConstraint(
            model_name="cartitem",
            constraint=models.UniqueConstraint(
                fields=("cart", "product"),
                name="unique_product_per_cart",
            ),
        ),
        migrations.AddConstraint(
            model_name="cartitem",
            constraint=models.CheckConstraint(
                check=Q(quantity__gte=1),
                name="cart_item_quantity_gte_1",
            ),
        ),
    ]
