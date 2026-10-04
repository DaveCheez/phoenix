"""Delete guards for finalised order snapshots.

Registered from OrdersConfig.ready(). Importing this module does not query
the database. The receiver runs only when a delete is attempted.

QuerySet.update(), bulk_create(), bulk_update() and raw SQL do not emit
pre_delete and are not covered here.
"""

from django.core.exceptions import ValidationError
from django.db import transaction


def prevent_finalised_snapshot_delete(sender, instance, using=None, **kwargs):
    """Block deletion of a finalised order, line or selected option.

    ``using`` is the database alias from ``pre_delete``. The parent order is
    resolved and locked on that alias inside one transaction. A cached
    ``instance.order`` is not used for the finalised check.

    Source-cart and catalogue deletes use SET_NULL and do not send this
    signal for the snapshot row. Raising here aborts Django's delete
    collector, which runs inside a transaction, so a blocked cascade does
    not leave a partial delete.
    """
    from .models import Order, OrderItem, OrderItemOption

    if not isinstance(instance, (Order, OrderItem, OrderItemOption)):
        return

    with transaction.atomic(using=using):
        if isinstance(instance, Order):
            order_id = instance.pk
        elif isinstance(instance, OrderItem):
            order_id = instance.order_id
        else:
            order_id = (
                OrderItem.objects.using(using)
                .filter(pk=instance.order_item_id)
                .values_list("order_id", flat=True)
                .first()
            )
        if not order_id:
            return
        order = (
            Order.objects.using(using)
            .select_for_update()
            .filter(pk=order_id)
            .first()
        )
        if order is not None and order.is_finalised:
            raise ValidationError("Finalised order snapshots cannot be deleted.")
