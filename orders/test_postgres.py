"""PostgreSQL-only evidence for constraints, errors, and competing writers.

These tests are skipped on SQLite. They use the disposable test database
created by the isolated PostgreSQL settings. Database errors are not mocked.
"""

import threading
import time
import unittest
import uuid
from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase

from cart.models import Cart, CartItem
from store.models import Category, Product

from .creation import REFERENCE_CONSTRAINT_NAME, _is_reference_integrity_error, create_order_from_cart
from .factories import create_snapshot_order
from .models import Order, OrderItem, OrderItemOption
from .test_creation import _customer


POSTGRES_ONLY = "PostgreSQL verification only. Skipped on the normal SQLite run."


def _driver(exc):
    return exc.__cause__


@unittest.skipUnless(connection.vendor == "postgresql", POSTGRES_ONLY)
class PostgresReferenceMetadataTests(TestCase):
    def test_live_reference_constraint_and_real_duplicate_error(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT con.conname, att.attname
                FROM pg_constraint con
                JOIN pg_class rel ON rel.oid = con.conrelid
                JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                JOIN pg_attribute att
                  ON att.attrelid = rel.oid
                 AND att.attnum = ANY (con.conkey)
                WHERE nsp.nspname = 'public'
                  AND rel.relname = 'orders_order'
                  AND con.contype = 'u'
                  AND att.attname = 'reference'
                """
            )
            rows = cursor.fetchall()
        self.assertEqual(
            rows,
            [(REFERENCE_CONSTRAINT_NAME, "reference")],
            msg=f"pg_constraint rows for orders_order.reference: {rows}",
        )

        existing, _catalogue = create_snapshot_order()
        values = {
            field.attname: getattr(existing, field.attname)
            for field in existing._meta.concrete_fields
            if not field.primary_key
        }
        with self.assertRaises(IntegrityError) as caught:
            with transaction.atomic():
                Order.objects.bulk_create([Order(id=uuid.uuid4(), **values)])
        cause = _driver(caught.exception)
        self.assertEqual(cause.pgcode, "23505")
        self.assertEqual(cause.diag.constraint_name, REFERENCE_CONSTRAINT_NAME)
        self.assertEqual(cause.diag.table_name, "orders_order")
        self.assertTrue(_is_reference_integrity_error(caught.exception))
        self.assertEqual(Order.objects.count(), 1)

    def test_other_integrity_errors_are_not_reference_collisions(self):
        order, _catalogue = create_snapshot_order(finalise=False)
        item = order.items.get()

        with self.assertRaises(IntegrityError) as check_error:
            with transaction.atomic():
                Order.objects.filter(pk=order.pk).update(
                    deposit_required=order.deposit_required + Decimal("0.01")
                )
        check_cause = _driver(check_error.exception)
        self.assertEqual(check_cause.pgcode, "23514")
        self.assertFalse(_is_reference_integrity_error(check_error.exception))

        with self.assertRaises(IntegrityError) as unique_error:
            with transaction.atomic():
                OrderItem.objects.bulk_create(
                    [
                        OrderItem(
                            order=order,
                            position=item.position,
                            original_product_id=item.original_product_id,
                            product_name=item.product_name,
                            product_slug=item.product_slug,
                            sku="",
                            configuration_signature=item.configuration_signature,
                            quantity=item.quantity,
                            base_unit_price=item.base_unit_price,
                            options_total=item.options_total,
                            configured_unit_price=item.configured_unit_price,
                            line_total=item.line_total,
                        )
                    ]
                )
        unique_cause = _driver(unique_error.exception)
        self.assertEqual(unique_cause.pgcode, "23505")
        self.assertNotEqual(unique_cause.diag.constraint_name, REFERENCE_CONSTRAINT_NAME)
        self.assertFalse(_is_reference_integrity_error(unique_error.exception))

        with self.assertRaises(IntegrityError) as foreign_key_error:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                OrderItem.objects.bulk_create(
                    [
                        OrderItem(
                            order_id=uuid.uuid4(),
                            position=2,
                            original_product_id=item.original_product_id,
                            product_name=item.product_name,
                            product_slug=item.product_slug,
                            sku="",
                            configuration_signature=item.configuration_signature,
                            quantity=item.quantity,
                            base_unit_price=item.base_unit_price,
                            options_total=item.options_total,
                            configured_unit_price=item.configured_unit_price,
                            line_total=item.line_total,
                        )
                    ]
                )
        foreign_cause = _driver(foreign_key_error.exception)
        self.assertEqual(foreign_cause.pgcode, "23503")
        self.assertFalse(_is_reference_integrity_error(foreign_key_error.exception))
        self.assertEqual(order.items.count(), 1)


class _CollisionReferences:
    def __init__(self):
        self._lock = threading.Lock()
        self.calls = []

    def __call__(self):
        with self._lock:
            ident = threading.get_ident()
            previous = sum(1 for call_ident, _reference in self.calls if call_ident == ident)
            reference = "PV-RACECOLLIDE01" if previous == 0 else f"PV-RACE{ident:x}{previous}"
            reference = reference[:32]
            self.calls.append((ident, reference))
            return reference


@unittest.skipUnless(connection.vendor == "postgresql", POSTGRES_ONLY)
class PostgresConcurrencyTests(TransactionTestCase):
    def _cart(self, session_key):
        category = Category.objects.create(
            name=f"Race {session_key}",
            slug=f"race-{session_key}",
            type="product",
        )
        product = Product.objects.create(
            category=category,
            name=f"Clip {session_key}",
            slug=f"clip-{session_key}",
            price=Decimal("1.00"),
        )
        cart = Cart.objects.create(session_key=session_key)
        CartItem.objects.create(
            cart=cart,
            product=product,
            quantity=1,
            configuration_signature="",
        )
        return cart

    def test_competing_reference_inserts_retry_without_partial_orders(self):
        carts = [self._cart("race-a"), self._cart("race-b")]
        barrier = threading.Barrier(2, timeout=15)
        generator = _CollisionReferences()
        results = []
        errors = []

        def create(cart):
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                barrier.wait()
                order = create_order_from_cart(cart_id=cart.id, **_customer())
                results.append(order.pk)
            except Exception as exc:
                errors.append(repr(exc))
            finally:
                thread_connection.close()

        with patch("orders.creation.generate_order_reference", generator):
            workers = [threading.Thread(target=create, args=(cart,)) for cart in carts]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(30)
                self.assertFalse(worker.is_alive())

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertGreaterEqual(len(generator.calls), 3)
        orders = list(Order.objects.filter(pk__in=results))
        references = {order.reference for order in orders}
        self.assertEqual(len(references), 2)
        self.assertIn("PV-RACECOLLIDE01", references)
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(OrderItem.objects.count(), 2)
        for order in orders:
            self.assertTrue(order.is_finalised)
            self.assertEqual(order.items.count(), 1)
            self.assertEqual(order.items.get().selected_options.count(), 0)

    def test_stale_parent_cannot_gain_a_child_after_finalisation(self):
        order, _catalogue = create_snapshot_order(finalise=False)
        existing = order.items.get()
        loaded = threading.Event()
        finalised = threading.Event()
        outcome = {}

        def load_then_insert():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                stale = Order.objects.get(pk=order.pk)
                self.assertFalse(stale.is_finalised)
                loaded.set()
                self.assertTrue(finalised.wait(15))
                OrderItem(
                    order=stale,
                    position=2,
                    original_product_id=existing.original_product_id,
                    product_name=existing.product_name,
                    product_slug=existing.product_slug,
                    sku="",
                    configuration_signature=existing.configuration_signature,
                    quantity=existing.quantity,
                    base_unit_price=existing.base_unit_price,
                    options_total=existing.options_total,
                    configured_unit_price=existing.configured_unit_price,
                    line_total=existing.line_total,
                ).save()
                outcome["saved"] = True
            except ValidationError as exc:
                outcome["error"] = exc
            except Exception as exc:
                outcome["unexpected"] = exc
            finally:
                thread_connection.close()

        def finalise():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                if not loaded.wait(15):
                    outcome["finalise_error"] = "parent was not loaded"
                    return
                Order.objects.get(pk=order.pk)._mark_finalised()
                finalised.set()
            except Exception as exc:
                outcome["finalise_error"] = exc
            finally:
                thread_connection.close()

        workers = [
            threading.Thread(target=load_then_insert),
            threading.Thread(target=finalise),
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
            self.assertFalse(worker.is_alive())

        self.assertNotIn("unexpected", outcome)
        self.assertNotIn("finalise_error", outcome)
        self.assertNotIn("saved", outcome)
        self.assertIn("error", outcome)
        order.refresh_from_db()
        self.assertTrue(order.is_finalised)
        self.assertEqual(order.items.count(), 1)

    def test_parent_lock_controls_competing_finalisation(self):
        order, _catalogue = create_snapshot_order(finalise=False)
        lock_held = threading.Event()
        state = {}

        def finalise():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                thread_connection.ensure_connection()
                state["pid"] = thread_connection.connection.get_backend_pid()
                self.assertTrue(lock_held.wait(15))
                Order.objects.get(pk=order.pk)._mark_finalised()
                state["finalised"] = True
            except Exception as exc:
                state["error"] = exc
            finally:
                thread_connection.close()

        worker = threading.Thread(target=finalise)
        worker.start()
        try:
            deadline = time.monotonic() + 15
            while "pid" not in state and "error" not in state and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIn("pid", state)

            with transaction.atomic():
                Order.objects.select_for_update().get(pk=order.pk)
                lock_held.set()
                seen_lock_wait = False
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            """
                            SELECT wait_event_type
                            FROM pg_stat_activity
                            WHERE pid = %s
                            """,
                            [state["pid"]],
                        )
                        row = cursor.fetchone()
                    if row and row[0] == "Lock":
                        seen_lock_wait = True
                        break
                    time.sleep(0.05)
                self.assertTrue(
                    seen_lock_wait,
                    msg=f"finaliser was not waiting on the parent lock: {state}",
                )
        finally:
            lock_held.set()
            worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", state)
        self.assertTrue(state.get("finalised"))
        order.refresh_from_db()
        self.assertTrue(order.is_finalised)

    def test_finalised_queryset_delete_is_blocked_on_another_connection(self):
        order, _catalogue = create_snapshot_order()
        item_ids = list(order.items.values_list("id", flat=True))
        option_ids = list(
            OrderItemOption.objects.filter(order_item__order=order).values_list("id", flat=True)
        )
        outcome = {}

        def delete_order():
            from django.db import connection as thread_connection

            thread_connection.close()
            try:
                Order.objects.filter(pk=order.pk).delete()
                outcome["deleted"] = True
            except ValidationError as exc:
                outcome["error"] = exc
            except Exception as exc:
                outcome["unexpected"] = exc
            finally:
                thread_connection.close()

        worker = threading.Thread(target=delete_order)
        worker.start()
        worker.join(20)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("unexpected", outcome)
        self.assertNotIn("deleted", outcome)
        self.assertIn("error", outcome)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertEqual(OrderItem.objects.filter(pk__in=item_ids).count(), len(item_ids))
        self.assertEqual(
            OrderItemOption.objects.filter(pk__in=option_ids).count(),
            len(option_ids),
        )
