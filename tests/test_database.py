import sqlite3
import tempfile
import unittest
from pathlib import Path

from dash_bot.database import SCHEMA, ActiveOrderExistsError, Database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp_dir.name) / "test.db")
        await self.db.initialize()

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_order_lifecycle_and_duplicate_protection(self) -> None:
        order = await self.db.create_order(
            guild_id=1,
            customer_id=2,
            restaurant_key="dominos",
            restaurant_name="Domino's",
            group_order_url="https://drd.sh/cart/test-one",
            fulfillment="pickup",
            location="123 Main St",
            contact_name="Luis",
            phone="555-0100",
            email="luis@example.com",
            notes=None,
            submitted_total_cents=4278,
        )
        self.assertEqual(order.customer_price_cents, 2139)
        self.assertEqual(order.status, "creating")

        with self.assertRaises(ActiveOrderExistsError):
            await self.db.create_order(
                guild_id=1,
                customer_id=2,
                restaurant_key="ihop",
                restaurant_name="IHOP",
                group_order_url="https://drd.sh/cart/test-two",
                fulfillment="delivery",
                location="456 Main St",
                contact_name="Luis",
                phone="555-0100",
                email="luis@example.com",
                notes=None,
                submitted_total_cents=2000,
            )

        order = await self.db.attach_channel(order.id, 99)
        self.assertEqual(order.status, "open")
        order = await self.db.assign_order(order.id, 5)
        self.assertEqual(order.assigned_staff_id, 5)
        order = await self.db.force_assign_order(order.id, 6, actor_id=99)
        self.assertEqual(order.assigned_staff_id, 6)
        order = await self.db.set_order_status(order.id, "closed", actor_id=5)
        self.assertEqual(order.status, "closed")

        next_order = await self.db.create_order(
            guild_id=1,
            customer_id=2,
            restaurant_key="ihop",
            restaurant_name="IHOP",
            group_order_url="https://drd.sh/cart/test-three",
            fulfillment="pickup",
            location="456 Main St",
            contact_name="Luis",
            phone="555-0100",
            email="luis@example.com",
            notes=None,
            submitted_total_cents=2000,
        )
        self.assertNotEqual(next_order.id, order.id)

    async def test_completed_order_records_commission_only_once(self) -> None:
        order = await self.db.create_order(
            guild_id=1,
            customer_id=20,
            restaurant_key="dominos",
            restaurant_name="Domino's",
            group_order_url="https://drd.sh/cart/test-four",
            fulfillment="pickup",
            location="123 Main St",
            contact_name="Customer",
            phone="555-0100",
            email="customer@example.com",
            notes=None,
            submitted_total_cents=3000,
        )
        order = await self.db.attach_channel(order.id, 199)

        closed, first_recorded = await self.db.close_order_with_commission(
            order.id,
            guild_id=1,
            amount_cents=175,
            actor_id=5,
            owed_by_staff_id=77,
            reason="Order completed with /done",
        )
        _, second_recorded = await self.db.close_order_with_commission(
            order.id,
            guild_id=1,
            amount_cents=175,
            actor_id=5,
            owed_by_staff_id=77,
            reason="Duplicate /done",
        )

        self.assertEqual(closed.status, "closed")
        self.assertTrue(first_recorded)
        self.assertFalse(second_recorded)
        summary = await self.db.get_commission_summary(1)
        self.assertEqual(summary.owed_order_count, 1)
        self.assertEqual(summary.owed_cents, 175)
        self.assertEqual(summary.lifetime_order_count, 1)
        self.assertEqual(summary.lifetime_cents, 175)
        by_staff = await self.db.get_commission_summary_by_staff(1)
        self.assertEqual(by_staff[0].staff_user_id, 77)
        self.assertEqual(by_staff[0].owed_cents, 175)

    async def test_commission_can_be_recorded_before_ticket_closes(self) -> None:
        order = await self.db.create_order(
            guild_id=1,
            customer_id=21,
            restaurant_key="dominos",
            restaurant_name="Domino's",
            group_order_url="https://drd.sh/cart/test-five",
            fulfillment="pickup",
            location="123 Main St",
            contact_name="Customer",
            phone="555-0100",
            email="customer@example.com",
            notes=None,
            submitted_total_cents=3000,
        )
        order = await self.db.attach_channel(order.id, 200)

        first_recorded = await self.db.record_owner_commission(
            order.id,
            guild_id=1,
            amount_cents=175,
            actor_id=5,
            owed_by_staff_id=88,
            reason="Order completed with /done",
        )
        second_recorded = await self.db.record_owner_commission(
            order.id,
            guild_id=1,
            amount_cents=175,
            actor_id=5,
            owed_by_staff_id=88,
            reason="Duplicate /done",
        )

        still_open = await self.db.get_order(order.id)
        self.assertIsNotNone(still_open)
        assert still_open is not None
        self.assertEqual(still_open.status, "open")
        self.assertTrue(first_recorded)
        self.assertFalse(second_recorded)
        summary = await self.db.get_commission_summary(1)
        self.assertEqual(summary.owed_order_count, 1)
        self.assertEqual(summary.owed_cents, 175)

    async def test_existing_database_gets_email_column_migration(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy.db"
        legacy_schema = SCHEMA.replace("    email TEXT,\n", "")
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(legacy_schema)

        legacy_db = Database(legacy_path)
        await legacy_db.initialize()
        with sqlite3.connect(legacy_path) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(orders)").fetchall()
            }

        self.assertIn("email", columns)

    async def test_existing_database_gets_customer_role_setting_migration(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy-settings.db"
        legacy_schema = SCHEMA.replace("    customer_ping_role_id INTEGER,\n", "")
        with sqlite3.connect(legacy_path) as connection:
            connection.executescript(legacy_schema)

        legacy_db = Database(legacy_path)
        await legacy_db.initialize()
        with sqlite3.connect(legacy_path) as connection:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(guild_settings)").fetchall()
            }

        self.assertIn("customer_ping_role_id", columns)

    async def test_settings_payments_and_restaurant_overrides(self) -> None:
        await self.db.upsert_guild_settings(
            guild_id=1,
            brand_name="Test Direct",
            ticket_category_id=2,
            staff_role_id=3,
            log_channel_id=4,
            banner_url=None,
            customer_ping_role_id=5,
        )
        settings = await self.db.get_guild_settings(1)
        self.assertIsNotNone(settings)
        assert settings is not None
        self.assertEqual(settings.brand_name, "Test Direct")
        self.assertEqual(settings.customer_ping_role_id, 5)

        self.assertTrue(await self.db.get_store_open(1))
        await self.db.set_store_open(1, False)
        self.assertFalse(await self.db.get_store_open(1))
        await self.db.set_store_open(1, True)
        self.assertTrue(await self.db.get_store_open(1))

        await self.db.upsert_payment_method(
            guild_id=1,
            staff_user_id=5,
            name="Cash App",
            instructions="Send to $test",
        )
        methods = await self.db.list_payment_methods(1, 5)
        self.assertEqual([item.name for item in methods], ["Cash App"])
        self.assertTrue(await self.db.remove_payment_method(1, 5, "cash app"))

        await self.db.set_restaurant_enabled(1, "dominos", False)
        self.assertEqual(await self.db.disabled_restaurant_keys(1), {"dominos"})


if __name__ == "__main__":
    unittest.main()
