from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when required bot configuration is missing or invalid."""


def _load_local_env(path: Path = Path(".env")) -> None:
    """Load a small, dependency-free .env file without overwriting real env vars."""
    if not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    database_path: Path
    transcript_dir: Path
    dev_guild_id: int | None
    customer_ping_role_id: int | None
    owner_commission_cents: int
    log_level: int

    @classmethod
    def from_env(cls) -> Settings:
        _load_local_env()

        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigError(
                "DISCORD_TOKEN is missing. Copy .env.example to .env and add the bot token."
            )

        database_path = Path(os.getenv("DATABASE_PATH", "data/doordash_manual.db"))
        transcript_dir = Path(os.getenv("TRANSCRIPT_DIR", "data/transcripts"))

        raw_guild_id = os.getenv("DEV_GUILD_ID", "").strip()
        try:
            dev_guild_id = int(raw_guild_id) if raw_guild_id else None
        except ValueError as exc:
            raise ConfigError("DEV_GUILD_ID must be a Discord server ID.") from exc

        raw_ping_role_id = os.getenv("CUSTOMER_PING_ROLE_ID", "").strip()
        try:
            customer_ping_role_id = int(raw_ping_role_id) if raw_ping_role_id else None
        except ValueError as exc:
            raise ConfigError("CUSTOMER_PING_ROLE_ID must be a plain Discord role ID.") from exc
        if customer_ping_role_id is not None and customer_ping_role_id <= 0:
            raise ConfigError("CUSTOMER_PING_ROLE_ID must be a positive Discord role ID.")

        raw_commission_cents = os.getenv("OWNER_COMMISSION_CENTS", "175").strip()
        try:
            owner_commission_cents = int(raw_commission_cents)
        except ValueError as exc:
            raise ConfigError(
                "OWNER_COMMISSION_CENTS must be a whole number of cents."
            ) from exc
        if owner_commission_cents <= 0:
            raise ConfigError("OWNER_COMMISSION_CENTS must be greater than zero.")

        log_name = os.getenv("LOG_LEVEL", "INFO").upper()
        log_level = getattr(logging, log_name, None)
        if not isinstance(log_level, int):
            raise ConfigError(f"Unknown LOG_LEVEL: {log_name}")

        return cls(
            token=token,
            database_path=database_path,
            transcript_dir=transcript_dir,
            dev_guild_id=dev_guild_id,
            customer_ping_role_id=customer_ping_role_id,
            owner_commission_cents=owner_commission_cents,
            log_level=log_level,
        )
