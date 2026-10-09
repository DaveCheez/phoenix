"""Upgrade an orders database from committed 0001 through the draft chain.

Uses the disposable test database and historical migration models. It does
not touch db.sqlite3. The latest schema is restored even when an assertion
fails.
"""

from decimal import Decimal

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from .models import Order, OrderItem


class OrderMigrationUpgradeTests(TransactionTestCase):
    def test_upgrade_from_0001_preserves_a_large_whole_pound_order(self):
        executor = MigrationExecutor(connection)
        self.assertIn(
            ("orders", "0005_orderitem_money_constraints"),
            executor.loader.graph.nodes,
        )
        try:
            self._upgrade_from_initial(executor)
        finally:
            MigrationExecutor(connection).migrate(
                MigrationExecutor(connection).loader.graph.leaf_nodes()
            )

    def _upgrade_from_initial(self, executor):
        executor.migrate([("orders", "0001_initial")])
        apps = executor.loader.project_state([("orders", "0001_initial")]).apps
        HistoricalOrder = apps.get_model("orders", "Order")
        HistoricalItem = apps.get_model("orders", "OrderItem")
        HistoricalOption = apps.get_model("orders", "OrderItemOption")

        order = HistoricalOrder.objects.create(
            reference="PV-UPGRADE30000000",
            customer_name="Alex Farmer",
            customer_email="alex@example.com",
            customer_phone="01254 000000",
            vehicle_make="Volkswagen",
            vehicle_model="T6.1",
            vehicle_year=2021,
            vehicle_wheelbase="LWB",
            currency="GBP",
            full_total=Decimal("30000000.00"),
            deposit_required=Decimal("10000000.00"),
            balance_on_completion=Decimal("20000000.00"),
            amount_paid=Decimal("0.00"),
            fulfilment_method="workshop_fitting",
            fitting_charge=Decimal("0.00"),
            tax_treatment="not_vat_registered",
            tax_amount=Decimal("0.00"),
            payment_terms="one_third_deposit_balance_on_completion",
            payment_terms_text=(
                "One-third deposit. Balance due on completion of the work and fitting."
            ),
            payment_status="pending_deposit",
            job_status="new",
        )
        item = HistoricalItem.objects.create(
            order=order,
            position=1,
            original_product_id=42,
            product_name="Large whole-pound rack",
            product_slug="large-whole-pound-rack",
            sku="",
            configuration_signature="large-whole-pounds",
            quantity=1,
            base_unit_price=Decimal("30000000.00"),
            options_total=Decimal("0.00"),
            configured_unit_price=Decimal("30000000.00"),
            line_total=Decimal("30000000.00"),
        )
        option = HistoricalOption.objects.create(
            order_item=item,
            position=1,
            original_group_id=7,
            original_option_id=8,
            group_name="Fitting",
            option_name="Workshop fitting",
            option_sku="",
            price_adjustment=Decimal("0.00"),
        )

        executor.loader.build_graph()
        executor.migrate([("orders", "0005_orderitem_money_constraints")])

        stored = Order.objects.get(reference="PV-UPGRADE30000000")
        self.assertEqual(stored.pk, order.pk)
        self.assertFalse(stored.is_finalised)
        self.assertEqual(stored.full_total, Decimal("30000000.00"))
        self.assertEqual(stored.deposit_required, Decimal("10000000.00"))
        self.assertEqual(stored.balance_on_completion, Decimal("20000000.00"))
        self.assertEqual(stored.customer_name, "Alex Farmer")
        stored_item = OrderItem.objects.get(pk=item.pk)
        self.assertEqual(stored_item.order_id, stored.pk)
        self.assertEqual(stored_item.product_name, "Large whole-pound rack")
        self.assertEqual(stored_item.line_total, Decimal("30000000.00"))
        self.assertEqual(stored_item.configuration_signature, "large-whole-pounds")
        stored_option = stored_item.selected_options.get()
        self.assertEqual(stored_option.pk, option.pk)
        self.assertEqual(stored_option.option_name, "Workshop fitting")
        self.assertEqual(stored_option.group_name, "Fitting")
        self.assertEqual(stored_option.price_adjustment, Decimal("0.00"))

        if connection.vendor == "sqlite":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders_order'"
                )
                order_sql = cursor.fetchone()[0]
                cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'orders_orderitem'"
                )
                item_sql = cursor.fetchone()[0]
            self.assertIn("order_deposit_plus_balance_equals_total", order_sql)
            self.assertIn("orderitem_configured_unit_matches_parts", item_sql)
            self.assertIn("orderitem_line_total_matches_quantity", item_sql)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Order.objects.filter(pk=stored.pk).update(
                    deposit_required=Decimal("10000000.01")
                )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OrderItem.objects.filter(pk=stored_item.pk).update(
                    line_total=Decimal("30000000.01")
                )
        stored.refresh_from_db()
        stored_item.refresh_from_db()
        self.assertEqual(stored.deposit_required, Decimal("10000000.00"))
        self.assertEqual(stored_item.line_total, Decimal("30000000.00"))
