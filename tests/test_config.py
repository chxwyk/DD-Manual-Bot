import os
import unittest
from unittest.mock import patch

from dash_bot.config import ConfigError, Settings


class ConfigTests(unittest.TestCase):
    def test_optional_customer_ping_role_id(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "CUSTOMER_PING_ROLE_ID": "123456789012345678",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.customer_ping_role_id, 123456789012345678)
        self.assertEqual(settings.owner_commission_cents, 175)

    def test_customer_ping_role_id_must_be_numeric(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "CUSTOMER_PING_ROLE_ID": "@Customers",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigError),
        ):
            Settings.from_env()

    def test_owner_commission_can_be_configured_in_cents(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "OWNER_COMMISSION_CENTS": "225",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.owner_commission_cents, 225)

    def test_owner_commission_must_be_positive_cents(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "OWNER_COMMISSION_CENTS": "0",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigError),
        ):
            Settings.from_env()


if __name__ == "__main__":
    unittest.main()
