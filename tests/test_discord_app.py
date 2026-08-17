import unittest
from unittest.mock import Mock

import discord

from dash_bot.discord_app import (
    DashCommands,
    SetupSelections,
    _claimed_ticket_overwrites,
    _order_summary_embed,
    _parse_contact_details,
)
from dash_bot.models import Order


class ContactDetailsTests(unittest.TestCase):
    def test_parses_required_email_and_phone(self) -> None:
        parsed = _parse_contact_details("Email: customer@example.com | Phone: (555) 555-0100")
        self.assertEqual(parsed, ("customer@example.com", "(555) 555-0100"))

    def test_rejects_missing_email_or_phone(self) -> None:
        self.assertIsNone(_parse_contact_details("Phone: (555) 555-0100"))
        self.assertIsNone(_parse_contact_details("Email: customer@example.com"))

    def test_order_summary_shows_estimated_price_and_notes(self) -> None:
        order = Order(
            id=1,
            guild_id=2,
            customer_id=3,
            channel_id=4,
            restaurant_key="dominos",
            restaurant_name="Domino's",
            group_order_url="https://drd.sh/cart/summary-test",
            fulfillment="pickup",
            location="123 Main St",
            contact_name="Customer",
            phone="(555) 555-0100",
            email="customer@example.com",
            notes="Pickup after 6 PM",
            submitted_total_cents=4278,
            customer_price_cents=2139,
            discount_basis_points=5000,
            assigned_staff_id=None,
            payment_method=None,
            status="open",
            created_at="2026-08-06T12:00:00+00:00",
            updated_at="2026-08-06T12:00:00+00:00",
        )

        embed = _order_summary_embed(order)
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(fields["Order Type"], "🛍️ **PICKUP**")
        self.assertEqual(fields["Pickup Instructions"], "123 Main St")
        self.assertEqual(
            fields["Estimated Customer Price (50% off final total)"], "**$21.39**"
        )
        self.assertEqual(fields["Notes"], "Pickup after 6 PM")


class ClaimedTicketPermissionTests(unittest.TestCase):
    def test_other_staff_become_read_only_and_claimant_can_speak(self) -> None:
        channel = Mock()
        channel.overwrites_for.side_effect = [
            discord.PermissionOverwrite(),
            discord.PermissionOverwrite(),
        ]

        staff_overwrite, claimant_overwrite = _claimed_ticket_overwrites(
            channel,
            Mock(spec=discord.Role),
            Mock(spec=discord.Member),
        )

        self.assertFalse(staff_overwrite.send_messages)
        self.assertFalse(staff_overwrite.send_messages_in_threads)
        self.assertFalse(staff_overwrite.use_application_commands)
        self.assertFalse(staff_overwrite.use_external_apps)
        self.assertFalse(staff_overwrite.add_reactions)
        self.assertTrue(staff_overwrite.view_channel)
        self.assertTrue(staff_overwrite.read_message_history)

        self.assertTrue(claimant_overwrite.send_messages)
        self.assertTrue(claimant_overwrite.send_messages_in_threads)
        self.assertTrue(claimant_overwrite.use_application_commands)
        self.assertTrue(claimant_overwrite.use_external_apps)
        self.assertTrue(claimant_overwrite.add_reactions)
        self.assertTrue(claimant_overwrite.attach_files)


class InteractiveSetupTests(unittest.TestCase):
    def test_manual_storefront_commands_have_a_unique_group(self) -> None:
        self.assertEqual(DashCommands.manual.name, "manual")
        self.assertEqual(
            {command.name for command in DashCommands.manual.commands}, {"open", "close"}
        )

    def test_setup_slash_command_has_no_typed_options(self) -> None:
        self.assertEqual(DashCommands.setup.parameters, [])

    def test_core_selection_requires_every_dropdown(self) -> None:
        selections = SetupSelections()
        self.assertFalse(selections.core_complete())
        selections.panel_channel_id = 1
        selections.ticket_category_id = 2
        selections.staff_role_id = 3
        selections.transcript_channel_id = 4
        self.assertTrue(selections.core_complete())


if __name__ == "__main__":
    unittest.main()
