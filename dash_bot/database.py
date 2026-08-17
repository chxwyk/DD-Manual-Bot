from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from .models import (
    CommissionSummary,
    GuildSettings,
    Order,
    PaymentMethod,
    StaffCommissionSummary,
)
from .pricing import DEFAULT_DISCOUNT_BASIS_POINTS, discounted_price_cents

T = TypeVar("T")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER PRIMARY KEY,
    brand_name TEXT NOT NULL,
    ticket_category_id INTEGER NOT NULL,
    staff_role_id INTEGER NOT NULL,
    log_channel_id INTEGER NOT NULL,
    banner_url TEXT,
    customer_ping_role_id INTEGER,
    panel_channel_id INTEGER,
    panel_message_id INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    channel_id INTEGER UNIQUE,
    restaurant_key TEXT NOT NULL,
    restaurant_name TEXT NOT NULL,
    group_order_url TEXT NOT NULL DEFAULT '',
    fulfillment TEXT NOT NULL CHECK (fulfillment IN ('delivery', 'pickup')),
    location TEXT NOT NULL,
    contact_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    notes TEXT,
    submitted_total_cents INTEGER NOT NULL CHECK (submitted_total_cents > 0),
    customer_price_cents INTEGER NOT NULL CHECK (customer_price_cents >= 0),
    discount_basis_points INTEGER NOT NULL DEFAULT 5000,
    assigned_staff_id INTEGER,
    payment_method TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_order_per_customer
ON orders(guild_id, customer_id)
WHERE status NOT IN ('closed', 'cancelled');

CREATE TABLE IF NOT EXISTS payment_methods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    staff_user_id INTEGER NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    instructions TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(guild_id, staff_user_id, name)
);

CREATE TABLE IF NOT EXISTS restaurant_settings (
    guild_id INTEGER NOT NULL,
    restaurant_key TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, restaurant_key)
);

CREATE TABLE IF NOT EXISTS store_status (
    guild_id INTEGER PRIMARY KEY,
    orders_open INTEGER NOT NULL DEFAULT 1 CHECK (orders_open IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owner_commissions (
    order_id INTEGER PRIMARY KEY,
    guild_id INTEGER NOT NULL,
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    status TEXT NOT NULL DEFAULT 'owed' CHECK (status IN ('owed', 'paid')),
    recorded_by INTEGER NOT NULL,
    owed_by_staff_id INTEGER,
    recorded_at TEXT NOT NULL,
    settled_by INTEGER,
    settled_at TEXT,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS owner_commissions_by_guild_status
ON owner_commissions(guild_id, status, recorded_at);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    actor_id INTEGER,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS orders_by_channel ON orders(channel_id);
CREATE INDEX IF NOT EXISTS events_by_order ON order_events(order_id, id);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _settings_from_row(row: sqlite3.Row | None) -> GuildSettings | None:
    if row is None:
        return None
    return GuildSettings(
        guild_id=row["guild_id"],
        brand_name=row["brand_name"],
        ticket_category_id=row["ticket_category_id"],
        staff_role_id=row["staff_role_id"],
        log_channel_id=row["log_channel_id"],
        banner_url=row["banner_url"],
        customer_ping_role_id=row["customer_ping_role_id"],
        panel_channel_id=row["panel_channel_id"],
        panel_message_id=row["panel_message_id"],
    )


def _order_from_row(row: sqlite3.Row | None) -> Order | None:
    if row is None:
        return None
    return Order(
        id=row["id"],
        guild_id=row["guild_id"],
        customer_id=row["customer_id"],
        channel_id=row["channel_id"],
        restaurant_key=row["restaurant_key"],
        restaurant_name=row["restaurant_name"],
        group_order_url=row["group_order_url"],
        fulfillment=row["fulfillment"],
        location=row["location"],
        contact_name=row["contact_name"],
        phone=row["phone"],
        email=row["email"],
        notes=row["notes"],
        submitted_total_cents=row["submitted_total_cents"],
        customer_price_cents=row["customer_price_cents"],
        discount_basis_points=row["discount_basis_points"],
        assigned_staff_id=row["assigned_staff_id"],
        payment_method=row["payment_method"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class ActiveOrderExistsError(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    async def _run(self, operation: Callable[[], T]) -> T:
        return await asyncio.to_thread(operation)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        def operation() -> None:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.executescript(SCHEMA)
                order_columns = {
                    str(row["name"])
                    for row in connection.execute("PRAGMA table_info(orders)").fetchall()
                }
                if "email" not in order_columns:
                    connection.execute("ALTER TABLE orders ADD COLUMN email TEXT")
                if "group_order_url" not in order_columns:
                    connection.execute(
                        "ALTER TABLE orders ADD COLUMN group_order_url TEXT NOT NULL DEFAULT ''"
                    )
                settings_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(guild_settings)"
                    ).fetchall()
                }
                if "customer_ping_role_id" not in settings_columns:
                    connection.execute(
                        "ALTER TABLE guild_settings ADD COLUMN customer_ping_role_id INTEGER"
                    )
                commission_columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(owner_commissions)"
                    ).fetchall()
                }
                if "owed_by_staff_id" not in commission_columns:
                    connection.execute(
                        "ALTER TABLE owner_commissions ADD COLUMN owed_by_staff_id INTEGER"
                    )
                connection.execute(
                    """
                    UPDATE owner_commissions
                    SET owed_by_staff_id = COALESCE(
                        owed_by_staff_id,
                        (SELECT assigned_staff_id FROM orders
                         WHERE orders.id = owner_commissions.order_id),
                        recorded_by
                    )
                    WHERE owed_by_staff_id IS NULL
                    """
                )

        async with self._write_lock:
            await self._run(operation)

    async def upsert_guild_settings(
        self,
        *,
        guild_id: int,
        brand_name: str,
        ticket_category_id: int,
        staff_role_id: int,
        log_channel_id: int,
        banner_url: str | None,
        customer_ping_role_id: int | None = None,
    ) -> None:
        now = _now()

        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO guild_settings (
                        guild_id, brand_name, ticket_category_id, staff_role_id,
                        log_channel_id, banner_url, customer_ping_role_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        brand_name = excluded.brand_name,
                        ticket_category_id = excluded.ticket_category_id,
                        staff_role_id = excluded.staff_role_id,
                        log_channel_id = excluded.log_channel_id,
                        banner_url = excluded.banner_url,
                        customer_ping_role_id = excluded.customer_ping_role_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        guild_id,
                        brand_name,
                        ticket_category_id,
                        staff_role_id,
                        log_channel_id,
                        banner_url,
                        customer_ping_role_id,
                        now,
                        now,
                    ),
                )

        async with self._write_lock:
            await self._run(operation)

    async def get_guild_settings(self, guild_id: int) -> GuildSettings | None:
        def operation() -> GuildSettings | None:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,)
                ).fetchone()
                return _settings_from_row(row)

        return await self._run(operation)

    async def save_panel(self, guild_id: int, channel_id: int, message_id: int) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE guild_settings
                    SET panel_channel_id = ?, panel_message_id = ?, updated_at = ?
                    WHERE guild_id = ?
                    """,
                    (channel_id, message_id, _now(), guild_id),
                )

        async with self._write_lock:
            await self._run(operation)

    async def get_store_open(self, guild_id: int) -> bool:
        def operation() -> bool:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT orders_open FROM store_status WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                return True if row is None else bool(row["orders_open"])

        return await self._run(operation)

    async def set_store_open(self, guild_id: int, orders_open: bool) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO store_status (guild_id, orders_open, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(guild_id) DO UPDATE SET
                        orders_open = excluded.orders_open,
                        updated_at = excluded.updated_at
                    """,
                    (guild_id, int(orders_open), _now()),
                )

        async with self._write_lock:
            await self._run(operation)

    async def create_order(
        self,
        *,
        guild_id: int,
        customer_id: int,
        restaurant_key: str,
        restaurant_name: str,
        group_order_url: str,
        fulfillment: str,
        location: str,
        contact_name: str,
        phone: str,
        email: str,
        notes: str | None,
        submitted_total_cents: int,
    ) -> Order:
        now = _now()
        price = discounted_price_cents(submitted_total_cents, DEFAULT_DISCOUNT_BASIS_POINTS)

        def operation() -> Order:
            try:
                with self._connect() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO orders (
                            guild_id, customer_id, restaurant_key, restaurant_name,
                            group_order_url, fulfillment, location, contact_name, phone, email, notes,
                            submitted_total_cents, customer_price_cents,
                            discount_basis_points, status, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            guild_id,
                            customer_id,
                            restaurant_key,
                            restaurant_name,
                            group_order_url,
                            fulfillment,
                            location,
                            contact_name,
                            phone,
                            email,
                            notes,
                            submitted_total_cents,
                            price,
                            DEFAULT_DISCOUNT_BASIS_POINTS,
                            "creating",
                            now,
                            now,
                        ),
                    )
                    row = connection.execute(
                        "SELECT * FROM orders WHERE id = ?", (cursor.lastrowid,)
                    ).fetchone()
            except sqlite3.IntegrityError as exc:
                error_text = str(exc)
                if (
                    "one_active_order_per_customer" in error_text
                    or "orders.guild_id, orders.customer_id" in error_text
                ):
                    raise ActiveOrderExistsError from exc
                raise

            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            return await self._run(operation)

    async def attach_channel(self, order_id: int, channel_id: int) -> Order:
        def operation() -> Order:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE orders SET channel_id = ?, status = 'open', updated_at = ?
                    WHERE id = ?
                    """,
                    (channel_id, _now(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            return await self._run(operation)

    async def cancel_order_creation(self, order_id: int) -> None:
        await self.set_order_status(order_id, "cancelled", actor_id=None)

    async def get_order(self, order_id: int) -> Order | None:
        def operation() -> Order | None:
            with self._connect() as connection:
                return _order_from_row(
                    connection.execute(
                        "SELECT * FROM orders WHERE id = ?", (order_id,)
                    ).fetchone()
                )

        return await self._run(operation)

    async def get_order_by_channel(self, channel_id: int) -> Order | None:
        def operation() -> Order | None:
            with self._connect() as connection:
                return _order_from_row(
                    connection.execute(
                        "SELECT * FROM orders WHERE channel_id = ?", (channel_id,)
                    ).fetchone()
                )

        return await self._run(operation)

    async def get_active_order_for_customer(
        self, guild_id: int, customer_id: int
    ) -> Order | None:
        def operation() -> Order | None:
            with self._connect() as connection:
                return _order_from_row(
                    connection.execute(
                        """
                        SELECT * FROM orders
                        WHERE guild_id = ? AND customer_id = ?
                          AND status NOT IN ('closed', 'cancelled')
                        ORDER BY id DESC LIMIT 1
                        """,
                        (guild_id, customer_id),
                    ).fetchone()
                )

        return await self._run(operation)

    async def assign_order(self, order_id: int, staff_id: int) -> Order:
        def operation() -> Order:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE orders
                    SET assigned_staff_id = ?, status = CASE
                        WHEN status = 'open' THEN 'claimed' ELSE status END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (staff_id, _now(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            order = await self._run(operation)
        await self.add_event(order_id, staff_id, "order_claimed", {})
        return order

    async def force_assign_order(self, order_id: int, staff_id: int, *, actor_id: int) -> Order:
        def operation() -> tuple[Order, int | None]:
            with self._connect() as connection:
                previous = connection.execute(
                    "SELECT assigned_staff_id FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                if previous is None:
                    raise ValueError(f"Order {order_id} does not exist.")
                previous_staff_id = previous["assigned_staff_id"]
                connection.execute(
                    """
                    UPDATE orders
                    SET assigned_staff_id = ?, status = CASE
                        WHEN status = 'open' THEN 'claimed' ELSE status END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (staff_id, _now(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order, previous_staff_id

        async with self._write_lock:
            order, previous_staff_id = await self._run(operation)
        await self.add_event(
            order_id,
            actor_id,
            "order_force_claimed",
            {
                "previous_staff_id": previous_staff_id,
                "new_staff_id": staff_id,
            },
        )
        return order

    async def update_order_pricing(
        self, order_id: int, total_cents: int, actor_id: int
    ) -> Order:
        price = discounted_price_cents(total_cents, DEFAULT_DISCOUNT_BASIS_POINTS)

        def operation() -> Order:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE orders
                    SET submitted_total_cents = ?, customer_price_cents = ?,
                        discount_basis_points = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        total_cents,
                        price,
                        DEFAULT_DISCOUNT_BASIS_POINTS,
                        _now(),
                        order_id,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            order = await self._run(operation)
        await self.add_event(
            order_id,
            actor_id,
            "price_verified",
            {"total_cents": total_cents, "customer_price_cents": price},
        )
        return order

    async def set_order_status(
        self,
        order_id: int,
        status: str,
        *,
        actor_id: int | None,
        details: dict[str, Any] | None = None,
    ) -> Order:
        now = _now()
        closed_at = now if status in {"closed", "cancelled"} else None

        def operation() -> Order:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE orders
                    SET status = ?, updated_at = ?,
                        closed_at = COALESCE(?, closed_at)
                    WHERE id = ?
                    """,
                    (status, now, closed_at, order_id),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            order = await self._run(operation)
        await self.add_event(order_id, actor_id, f"status_{status}", details or {})
        return order

    async def close_order_with_commission(
        self,
        order_id: int,
        *,
        guild_id: int,
        amount_cents: int,
        actor_id: int,
        owed_by_staff_id: int,
        reason: str,
    ) -> tuple[Order, bool]:
        now = _now()

        def operation() -> tuple[Order, bool]:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT guild_id FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                if existing is None:
                    raise ValueError(f"Order {order_id} does not exist.")
                if int(existing["guild_id"]) != guild_id:
                    raise ValueError("Order does not belong to this Discord server.")

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO owner_commissions (
                        order_id, guild_id, amount_cents, status,
                        recorded_by, owed_by_staff_id, recorded_at
                    ) VALUES (?, ?, ?, 'owed', ?, ?, ?)
                    """,
                    (
                        order_id,
                        guild_id,
                        amount_cents,
                        actor_id,
                        owed_by_staff_id,
                        now,
                    ),
                )
                commission_recorded = cursor.rowcount > 0
                connection.execute(
                    """
                    UPDATE orders
                    SET status = 'closed', updated_at = ?, closed_at = COALESCE(closed_at, ?)
                    WHERE id = ?
                    """,
                    (now, now, order_id),
                )
                connection.execute(
                    """
                    INSERT INTO order_events (
                        order_id, actor_id, event_type, details_json, created_at
                    ) VALUES (?, ?, 'status_closed', ?, ?)
                    """,
                    (
                        order_id,
                        actor_id,
                        json.dumps(
                            {
                                "reason": reason,
                                "owner_commission_cents": amount_cents,
                                "owed_by_staff_id": owed_by_staff_id,
                                "commission_recorded": commission_recorded,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()

            order = _order_from_row(row)
            assert order is not None
            return order, commission_recorded

        async with self._write_lock:
            return await self._run(operation)

    async def record_owner_commission(
        self,
        order_id: int,
        *,
        guild_id: int,
        amount_cents: int,
        actor_id: int,
        owed_by_staff_id: int,
        reason: str,
    ) -> bool:
        """Record a duplicate-safe commission without closing the active ticket."""
        now = _now()

        def operation() -> bool:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT guild_id FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
                if existing is None:
                    raise ValueError(f"Order {order_id} does not exist.")
                if int(existing["guild_id"]) != guild_id:
                    raise ValueError("Order does not belong to this Discord server.")

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO owner_commissions (
                        order_id, guild_id, amount_cents, status,
                        recorded_by, owed_by_staff_id, recorded_at
                    ) VALUES (?, ?, ?, 'owed', ?, ?, ?)
                    """,
                    (
                        order_id,
                        guild_id,
                        amount_cents,
                        actor_id,
                        owed_by_staff_id,
                        now,
                    ),
                )
                commission_recorded = cursor.rowcount > 0
                connection.execute(
                    """
                    INSERT INTO order_events (
                        order_id, actor_id, event_type, details_json, created_at
                    ) VALUES (?, ?, 'commission_recorded', ?, ?)
                    """,
                    (
                        order_id,
                        actor_id,
                        json.dumps(
                            {
                                "reason": reason,
                                "owner_commission_cents": amount_cents,
                                "owed_by_staff_id": owed_by_staff_id,
                                "commission_recorded": commission_recorded,
                            },
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        now,
                    ),
                )
                return commission_recorded

        async with self._write_lock:
            return await self._run(operation)

    async def get_commission_summary(self, guild_id: int) -> CommissionSummary:
        def operation() -> CommissionSummary:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS lifetime_order_count,
                        COALESCE(SUM(amount_cents), 0) AS lifetime_cents,
                        COALESCE(SUM(CASE WHEN status = 'owed' THEN 1 ELSE 0 END), 0)
                            AS owed_order_count,
                        COALESCE(SUM(CASE WHEN status = 'owed' THEN amount_cents ELSE 0 END), 0)
                            AS owed_cents
                    FROM owner_commissions
                    WHERE guild_id = ?
                    """,
                    (guild_id,),
                ).fetchone()
                assert row is not None
                return CommissionSummary(
                    owed_order_count=int(row["owed_order_count"]),
                    owed_cents=int(row["owed_cents"]),
                    lifetime_order_count=int(row["lifetime_order_count"]),
                    lifetime_cents=int(row["lifetime_cents"]),
                )

        return await self._run(operation)

    async def get_commission_summary_by_staff(
        self, guild_id: int
    ) -> list[StaffCommissionSummary]:
        def operation() -> list[StaffCommissionSummary]:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT
                        owed_by_staff_id AS staff_user_id,
                        COUNT(*) AS lifetime_order_count,
                        COALESCE(SUM(amount_cents), 0) AS lifetime_cents,
                        COALESCE(SUM(CASE WHEN status = 'owed' THEN 1 ELSE 0 END), 0)
                            AS owed_order_count,
                        COALESCE(SUM(CASE WHEN status = 'owed' THEN amount_cents ELSE 0 END), 0)
                            AS owed_cents
                    FROM owner_commissions
                    WHERE guild_id = ? AND owed_by_staff_id IS NOT NULL
                    GROUP BY owed_by_staff_id
                    ORDER BY owed_cents DESC, owed_by_staff_id
                    """,
                    (guild_id,),
                ).fetchall()
                return [
                    StaffCommissionSummary(
                        staff_user_id=int(row["staff_user_id"]),
                        owed_order_count=int(row["owed_order_count"]),
                        owed_cents=int(row["owed_cents"]),
                        lifetime_order_count=int(row["lifetime_order_count"]),
                        lifetime_cents=int(row["lifetime_cents"]),
                    )
                    for row in rows
                ]

        return await self._run(operation)

    async def set_payment_method_for_order(
        self, order_id: int, method_name: str, actor_id: int
    ) -> Order:
        def operation() -> Order:
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE orders
                    SET payment_method = ?, status = 'awaiting_payment',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (method_name, _now(), order_id),
                )
                row = connection.execute(
                    "SELECT * FROM orders WHERE id = ?", (order_id,)
                ).fetchone()
            order = _order_from_row(row)
            assert order is not None
            return order

        async with self._write_lock:
            order = await self._run(operation)
        await self.add_event(
            order_id, actor_id, "payment_method_selected", {"method": method_name}
        )
        return order

    async def add_event(
        self,
        order_id: int,
        actor_id: int | None,
        event_type: str,
        details: dict[str, Any],
    ) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO order_events (
                        order_id, actor_id, event_type, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        order_id,
                        actor_id,
                        event_type,
                        json.dumps(details, separators=(",", ":"), sort_keys=True),
                        _now(),
                    ),
                )

        async with self._write_lock:
            await self._run(operation)

    async def upsert_payment_method(
        self,
        *,
        guild_id: int,
        staff_user_id: int,
        name: str,
        instructions: str,
    ) -> None:
        now = _now()

        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO payment_methods (
                        guild_id, staff_user_id, name, instructions,
                        enabled, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(guild_id, staff_user_id, name) DO UPDATE SET
                        instructions = excluded.instructions,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (guild_id, staff_user_id, name, instructions, now, now),
                )

        async with self._write_lock:
            await self._run(operation)

    async def list_payment_methods(
        self, guild_id: int, staff_user_id: int
    ) -> list[PaymentMethod]:
        def operation() -> list[PaymentMethod]:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM payment_methods
                    WHERE guild_id = ? AND staff_user_id = ? AND enabled = 1
                    ORDER BY name COLLATE NOCASE
                    """,
                    (guild_id, staff_user_id),
                ).fetchall()
                return [
                    PaymentMethod(
                        id=row["id"],
                        guild_id=row["guild_id"],
                        staff_user_id=row["staff_user_id"],
                        name=row["name"],
                        instructions=row["instructions"],
                        enabled=bool(row["enabled"]),
                    )
                    for row in rows
                ]

        return await self._run(operation)

    async def remove_payment_method(self, guild_id: int, staff_user_id: int, name: str) -> bool:
        def operation() -> bool:
            with self._connect() as connection:
                cursor = connection.execute(
                    """
                    DELETE FROM payment_methods
                    WHERE guild_id = ? AND staff_user_id = ?
                      AND name = ? COLLATE NOCASE
                    """,
                    (guild_id, staff_user_id, name),
                )
                return cursor.rowcount > 0

        async with self._write_lock:
            return await self._run(operation)

    async def set_restaurant_enabled(
        self, guild_id: int, restaurant_key: str, enabled: bool
    ) -> None:
        def operation() -> None:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO restaurant_settings (
                        guild_id, restaurant_key, enabled, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, restaurant_key) DO UPDATE SET
                        enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (guild_id, restaurant_key, int(enabled), _now()),
                )

        async with self._write_lock:
            await self._run(operation)

    async def disabled_restaurant_keys(self, guild_id: int) -> set[str]:
        def operation() -> set[str]:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT restaurant_key FROM restaurant_settings
                    WHERE guild_id = ? AND enabled = 0
                    """,
                    (guild_id,),
                ).fetchall()
                return {row["restaurant_key"] for row in rows}

        return await self._run(operation)
