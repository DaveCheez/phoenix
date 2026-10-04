"""Whole-order GBP deposit arithmetic.

These helpers do not read catalogue prices, carts or payment-provider limits.
They accept an already-agreed order total in pounds and pence.
"""

from collections import namedtuple
from decimal import Decimal, InvalidOperation


PENNY = Decimal("0.01")
DepositSplit = namedtuple("DepositSplit", ("deposit", "balance"))


class OrderMoneyError(ValueError):
    """Raised when a value cannot be used as an order total."""


def calculate_deposit(total):
    """Return the one-third deposit and remaining balance for a GBP total.

    The total must be a finite, non-negative Decimal with at most two decimal
    places. Float, bool, NaN, Infinity, negatives and fractional pennies are
    rejected. The helper does not round malformed input into a valid total.

    deposit_pence = (total_pence + 1) // 3
    balance_pence = total_pence - deposit_pence
    """
    total_pence = _total_pence(total)
    deposit_pence = (total_pence + 1) // 3
    balance_pence = total_pence - deposit_pence
    return DepositSplit(
        deposit=_pence_to_pounds(deposit_pence),
        balance=_pence_to_pounds(balance_pence),
    )


def _total_pence(total) -> int:
    if isinstance(total, bool):
        raise OrderMoneyError("Order totals must be Decimal pounds and pence.")
    if isinstance(total, float):
        raise OrderMoneyError(
            "Order totals cannot be calculated from floating-point numbers."
        )
    if not isinstance(total, Decimal):
        raise OrderMoneyError("Order totals must be Decimal pounds and pence.")
    if not total.is_finite():
        raise OrderMoneyError("Order totals must be finite.")
    if total < Decimal("0.00"):
        raise OrderMoneyError("Order totals cannot be negative.")
    try:
        pennies = (total / PENNY).to_integral_value()
    except (InvalidOperation, ValueError) as exc:
        raise OrderMoneyError("Order totals must be whole pennies.") from exc
    if pennies * PENNY != total:
        raise OrderMoneyError("Order totals must be whole pennies.")
    return int(pennies)


def _pence_to_pounds(pence: int) -> Decimal:
    return (Decimal(pence) * PENNY).quantize(PENNY)
