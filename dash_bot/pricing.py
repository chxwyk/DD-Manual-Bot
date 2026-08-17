from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_PATTERN = re.compile(r"^\$?\s*(\d{1,7}(?:,\d{3})*|\d+)(?:\.(\d{1,2}))?\s*$")
MIN_CART_TOTAL_CENTS = 3_000  # $30.00 final total after taxes and fees
MAX_CART_TOTAL_CENTS = 500_000  # $5,000.00
DEFAULT_DISCOUNT_BASIS_POINTS = 5_000  # 50.00%


class MoneyError(ValueError):
    """Raised when a cart total cannot be safely interpreted as money."""


def parse_money(value: str) -> int:
    """Parse a dollar amount into integer cents.

    Accepted examples: ``30``, ``30.5``, ``$30.50``, and ``1,024.50``.
    Floating point numbers are intentionally never used.
    """
    cleaned = value.strip()
    if not MONEY_PATTERN.fullmatch(cleaned):
        raise MoneyError("Enter a valid total such as 24.50 or $24.50.")

    try:
        amount = Decimal(cleaned.replace("$", "").replace(",", "").strip())
        cents = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyError("Enter a valid dollar amount.") from exc

    if cents < MIN_CART_TOTAL_CENTS:
        raise MoneyError("The final DoorDash total must be $30.00 or more after taxes and fees.")
    if cents > MAX_CART_TOTAL_CENTS:
        raise MoneyError("The final total cannot be greater than $5,000.00.")
    return cents


def discounted_price_cents(
    total_cents: int,
    discount_basis_points: int = DEFAULT_DISCOUNT_BASIS_POINTS,
) -> int:
    """Return the customer's price, rounded to the nearest cent (half up)."""
    if total_cents < 0:
        raise MoneyError("The total cannot be negative.")
    if not 0 <= discount_basis_points <= 10_000:
        raise ValueError("Discount basis points must be between 0 and 10,000.")

    customer_basis_points = 10_000 - discount_basis_points
    return (total_cents * customer_basis_points + 5_000) // 10_000


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    absolute = abs(cents)
    return f"{sign}${absolute // 100:,}.{absolute % 100:02d}"


def format_discount(discount_basis_points: int) -> str:
    percent = Decimal(discount_basis_points) / Decimal(100)
    return f"{percent.quantize(Decimal('0.01')).normalize()}%"
