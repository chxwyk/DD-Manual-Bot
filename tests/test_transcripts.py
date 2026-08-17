import unittest
from datetime import UTC, datetime

from dash_bot.models import Order
from dash_bot.transcripts import TranscriptMessage, render_transcript_html


def sample_order() -> Order:
    return Order(
        id=12,
        guild_id=1,
        customer_id=2,
        channel_id=3,
        restaurant_key="dominos",
        restaurant_name="Domino's",
        group_order_url="https://drd.sh/cart/transcript-test",
        fulfillment="pickup",
        location="123 Main St",
        contact_name="Customer",
        phone="555-0100",
        email="customer@example.com",
        notes=None,
        submitted_total_cents=4278,
        customer_price_cents=2139,
        discount_basis_points=5000,
        assigned_staff_id=4,
        payment_method="Cash App",
        status="completed",
        created_at="2026-07-30T12:00:00+00:00",
        updated_at="2026-07-30T12:10:00+00:00",
    )


class TranscriptTests(unittest.TestCase):
    def test_escapes_message_and_metadata(self) -> None:
        rendered = render_transcript_html(
            guild_name="<Server>",
            channel_name="order-12",
            order=sample_order(),
            messages=[
                TranscriptMessage(
                    author_name="<script>alert(1)</script>",
                    author_id=7,
                    avatar_url=None,
                    created_at=datetime.now(UTC),
                    content="<img src=x onerror=alert(1)>",
                )
            ],
        )
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertNotIn("<img src=x onerror=alert(1)>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("&lt;img", rendered)

    def test_contains_order_totals(self) -> None:
        rendered = render_transcript_html(
            guild_name="Server",
            channel_name="ticket",
            order=sample_order(),
            messages=[],
        )
        self.assertIn("$42.78", rendered)
        self.assertIn("$21.39", rendered)
        self.assertIn("Pickup", rendered)
        self.assertIn("customer@example.com", rendered)
        self.assertIn("555-0100", rendered)


if __name__ == "__main__":
    unittest.main()
