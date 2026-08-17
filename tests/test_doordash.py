import unittest

from dash_bot.doordash import (
    DoorDashLinkError,
    extract_public_metadata,
    normalize_group_order_url,
)


class DoorDashLinkTests(unittest.TestCase):
    def test_accepts_supported_https_links(self) -> None:
        self.assertEqual(
            normalize_group_order_url("<https://drd.sh/cart/abc#invite>"),
            "https://drd.sh/cart/abc",
        )
        self.assertEqual(
            normalize_group_order_url("https://www.doordash.com/orders/group/abc"),
            "https://www.doordash.com/orders/group/abc",
        )

    def test_rejects_non_doordash_or_insecure_links(self) -> None:
        for value in (
            "http://drd.sh/cart/abc",
            "https://example.com/cart/abc",
            "https://doordash.com.evil.test/cart/abc",
        ):
            with self.subTest(value=value), self.assertRaises(DoorDashLinkError):
                normalize_group_order_url(value)

    def test_extracts_public_store_and_subtotal_metadata(self) -> None:
        page = (
            '<meta property="og:title" content="Test Burger – DoorDash">'
            '<script>{"subtotalDisplayString":"$25.75"}</script>'
        )
        self.assertEqual(extract_public_metadata(page), ("Test Burger", 2575))


if __name__ == "__main__":
    unittest.main()
