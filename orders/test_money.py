from decimal import Decimal
from unittest import TestCase

from .money import OrderMoneyError, calculate_deposit


class DepositCalculationTests(TestCase):
    def test_exact_third_total(self):
        split = calculate_deposit(Decimal("825.00"))
        self.assertEqual(split.deposit, Decimal("275.00"))
        self.assertEqual(split.balance, Decimal("550.00"))

    def test_remainder_classes_when_pennies_divided_by_three(self):
        cases = (
            (Decimal("100.00"), Decimal("33.33"), Decimal("66.67")),
            (Decimal("100.01"), Decimal("33.34"), Decimal("66.67")),
            (Decimal("100.02"), Decimal("33.34"), Decimal("66.68")),
        )
        for total, deposit, balance in cases:
            with self.subTest(total=total):
                split = calculate_deposit(total)
                self.assertEqual(split.deposit, deposit)
                self.assertEqual(split.balance, balance)

    def test_deposit_plus_balance_equals_original_total(self):
        totals = (
            Decimal("0.00"),
            Decimal("0.01"),
            Decimal("0.02"),
            Decimal("1.00"),
            Decimal("2.00"),
            Decimal("100.00"),
            Decimal("100.01"),
            Decimal("100.02"),
            Decimal("825.00"),
            Decimal("9999.99"),
            Decimal("123456.78"),
        )
        for total in totals:
            with self.subTest(total=total):
                split = calculate_deposit(total)
                self.assertEqual(split.deposit + split.balance, total)

    def test_whole_order_rounding_not_per_line(self):
        first = calculate_deposit(Decimal("1.00"))
        second = calculate_deposit(Decimal("1.00"))
        combined = calculate_deposit(Decimal("2.00"))
        self.assertEqual(first.deposit + second.deposit, Decimal("0.66"))
        self.assertEqual(combined.deposit, Decimal("0.67"))
        self.assertEqual(combined.balance, Decimal("1.33"))

    def test_large_and_tiny_amounts(self):
        tiny = calculate_deposit(Decimal("0.01"))
        self.assertEqual(tiny.deposit, Decimal("0.00"))
        self.assertEqual(tiny.balance, Decimal("0.01"))
        large = calculate_deposit(Decimal("1000000.00"))
        self.assertEqual(large.deposit, Decimal("333333.33"))
        self.assertEqual(large.balance, Decimal("666666.67"))

    def test_invalid_money_inputs_are_rejected(self):
        invalid = (
            825.00,
            True,
            False,
            100,
            "825.00",
            None,
            Decimal("NaN"),
            Decimal("Infinity"),
            Decimal("-Infinity"),
            Decimal("-0.01"),
            Decimal("1.001"),
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(OrderMoneyError):
                    calculate_deposit(value)
