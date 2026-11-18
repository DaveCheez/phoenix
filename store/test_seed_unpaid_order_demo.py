"""The unpaid-order demo seed refuses every database except its local file."""

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings


class SeedUnpaidOrderDemoTests(TestCase):
    def test_refuses_the_test_database(self):
        with self.assertRaises(CommandError):
            call_command("seed_unpaid_order_demo")

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "phoenix",
                "HOST": "ep-example.neon.tech",
            }
        }
    )
    def test_refuses_a_remote_database(self):
        with self.assertRaises(CommandError):
            call_command("seed_unpaid_order_demo")
