from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "orders"
    verbose_name = "Orders"

    def ready(self):
        from django.db.models.signals import pre_delete

        from .models import Order, OrderItem, OrderItemOption
        from .signals import prevent_finalised_snapshot_delete

        for model in (Order, OrderItem, OrderItemOption):
            pre_delete.connect(
                prevent_finalised_snapshot_delete,
                sender=model,
                dispatch_uid=f"orders.prevent_finalised_delete.{model._meta.label_lower}",
            )
