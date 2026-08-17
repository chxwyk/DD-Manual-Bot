import unittest

from dash_bot.pricing import (
    MoneyError,
    discounted_price_cents,
    format_cents,
    parse_money,
)


class PricingTests(unittest.TestCase):
    def test_parse_common_money_formats(self) -> None:
        self.assertEqual(parse_money("30"), 3000)
        self.assertEqual(parse_money("30.5"), 3050)
        self.assertEqual(parse_money("$30.50"), 3050)
        self.assertEqual(parse_money("1,024.50"), 102450)

    def test_rejects_invalid_or_unsafe_totals(self) -> None:
        for value in ("", "free", "-1", "12.345", "$0.00", "29.99", "5000.01"):
            with self.subTest(value=value), self.assertRaises(MoneyError):
                parse_money(value)

    def test_exact_half_off_rounds_half_cents_up(self) -> None:
        self.assertEqual(discounted_price_cents(4278), 2139)
        self.assertEqual(discounted_price_cents(2499), 1250)
        self.assertEqual(discounted_price_cents(1), 1)

    def test_currency_format(self) -> None:
        self.assertEqual(format_cents(2139), "$21.39")
        self.assertEqual(format_cents(102450), "$1,024.50")


if __name__ == "__main__":
    unittest.main()
