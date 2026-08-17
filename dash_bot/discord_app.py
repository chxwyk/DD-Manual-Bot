from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from .config import ConfigError, Settings
from .database import ActiveOrderExistsError, Database
from .doordash import (
    DoorDashLinkError,
    inspect_group_order_link,
    normalize_group_order_url,
)
from .models import GuildSettings, Order, PaymentMethod
from .pricing import MoneyError, format_cents, parse_money
from .transcripts import (
    TranscriptAttachment,
    TranscriptMessage,
    render_transcript_html,
    save_transcript,
)

LOGGER = logging.getLogger("bobs_doordash_manual")
EMBED_COLOR = discord.Color.from_rgb(88, 101, 242)
SUCCESS_COLOR = discord.Color.from_rgb(46, 204, 113)
WARNING_COLOR = discord.Color.from_rgb(241, 196, 15)
ERROR_COLOR = discord.Color.from_rgb(231, 76, 60)
KNOWN_PAYMENT_METHODS = (
    "Cash App",
    "Apple Pay",
    "Zelle",
    "Venmo",
    "PayPal",
    "Stripe Payment Link",
    "Cryptocurrency",
)
DEFAULT_BANNER_FILENAME = "bobs-burger-doordash-manual-30-total.gif"
DEFAULT_BANNER_PATH = Path(__file__).with_name("assets") / DEFAULT_BANNER_FILENAME
BRAND_AVATAR_FILENAME = "bobs-burger-doordash-manual-pfp.png"
BRAND_AVATAR_PATH = Path(__file__).with_name("assets") / BRAND_AVATAR_FILENAME
EMAIL_PATTERN = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE)
DONE_CLOSE_DELAY_SECONDS = 30 * 60
MINIMUM_TOTAL_CENTS = 3_000


def _safe_channel_fragment(value: str) -> str:
    fragment = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return fragment[:24] or "customer"


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _parse_contact_details(value: str) -> tuple[str, str] | None:
    email_match = EMAIL_PATTERN.search(value)
    if email_match is None or len(email_match.group(0)) > 254:
        return None

    email = email_match.group(0)
    phone = f"{value[: email_match.start()]} {value[email_match.end() :]}"
    phone = re.sub(
        r"(?i)\b(?:email|e-mail|phone|phone number|number)\b\s*[:=-]?",
        " ",
        phone,
    )
    phone = re.sub(r"[|,;\n]+", " ", phone)
    phone = re.sub(r"\s+", " ", phone).strip(" -:")
    digit_count = len(re.sub(r"\D", "", phone))
    if digit_count < 7 or digit_count > 15:
        return None
    return email, phone[:80]


def _is_administrator(member: discord.Member | discord.User) -> bool:
    return isinstance(member, discord.Member) and member.guild_permissions.administrator


def _is_staff(member: discord.Member | discord.User, settings: GuildSettings) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.administrator or any(
        role.id == settings.staff_role_id for role in member.roles
    )


async def _ephemeral(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    kwargs: dict[str, Any] = {
        "content": content,
        "embed": embed,
        "view": view,
        "ephemeral": True,
        "allowed_mentions": discord.AllowedMentions.none(),
    }
    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)
    else:
        await interaction.response.send_message(**kwargs)


async def _configured_settings(
    bot: DashManualBot, interaction: discord.Interaction
) -> GuildSettings | None:
    if interaction.guild_id is None:
        await _ephemeral(interaction, "This can only be used inside the Discord server.")
        return None
    settings = await bot.db.get_guild_settings(interaction.guild_id)
    if settings is None:
        await _ephemeral(
            interaction,
            "The bot has not been configured yet. An administrator must run `/setup`.",
        )
    return settings


async def _ticket_order(bot: DashManualBot, interaction: discord.Interaction) -> Order | None:
    if interaction.channel_id is None:
        await _ephemeral(interaction, "Use this inside an order ticket.")
        return None
    order = await bot.db.get_order_by_channel(interaction.channel_id)
    if order is None:
        await _ephemeral(interaction, "This channel is not an order ticket.")
    return order


def _claimed_ticket_overwrites(
    channel: discord.TextChannel,
    staff_role: discord.Role,
    claimant: discord.Member,
) -> tuple[discord.PermissionOverwrite, discord.PermissionOverwrite]:
    staff_overwrite = channel.overwrites_for(staff_role)
    staff_overwrite.update(
        view_channel=True,
        read_message_history=True,
        send_messages=False,
        send_messages_in_threads=False,
        create_public_threads=False,
        create_private_threads=False,
        add_reactions=False,
        use_application_commands=False,
        use_external_apps=False,
    )

    claimant_overwrite = channel.overwrites_for(claimant)
    claimant_overwrite.update(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        send_messages_in_threads=True,
        create_public_threads=True,
        create_private_threads=True,
        attach_files=True,
        embed_links=True,
        add_reactions=True,
        use_application_commands=True,
        use_external_apps=True,
    )
    return staff_overwrite, claimant_overwrite


async def _lock_ticket_to_claimant(
    interaction: discord.Interaction,
    settings: GuildSettings,
    claimant: discord.Member,
) -> bool:
    if interaction.guild is None or not isinstance(interaction.channel, discord.TextChannel):
        return False

    staff_role = interaction.guild.get_role(settings.staff_role_id)
    if staff_role is None:
        return False

    staff_overwrite, claimant_overwrite = _claimed_ticket_overwrites(
        interaction.channel, staff_role, claimant
    )
    try:
        # Apply the claimant allow first so a partial Discord API failure never
        # leaves the assigned chef unable to speak in their own ticket.
        await interaction.channel.set_permissions(
            claimant,
            overwrite=claimant_overwrite,
            reason="DoorDash Manual ticket claimed",
        )
        await interaction.channel.set_permissions(
            staff_role,
            overwrite=staff_overwrite,
            reason="DoorDash Manual ticket claimed; unassigned chefs are read-only",
        )
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.exception("Could not lock claimed ticket %s", interaction.channel.id)
        return False
    return True


async def _claim_ticket(bot: DashManualBot, interaction: discord.Interaction) -> None:
    order = await _ticket_order(bot, interaction)
    settings = await _configured_settings(bot, interaction)
    if order is None or settings is None:
        return
    if not _is_staff(interaction.user, settings) or not isinstance(
        interaction.user, discord.Member
    ):
        await _ephemeral(interaction, "Only Manual Chefs can claim tickets.")
        return

    claim_lock = bot.claim_locks.setdefault(order.id, asyncio.Lock())
    async with claim_lock:
        current_order = await bot.db.get_order_by_channel(interaction.channel_id)
        if current_order is None:
            await _ephemeral(interaction, "This channel is not an order ticket.")
            return
        if (
            current_order.assigned_staff_id
            and current_order.assigned_staff_id != interaction.user.id
        ):
            await _ephemeral(
                interaction,
                f"This ticket is already claimed by <@{current_order.assigned_staff_id}>.",
            )
            return

        await interaction.response.defer(thinking=True)
        if current_order.assigned_staff_id is None:
            current_order = await bot.db.assign_order(current_order.id, interaction.user.id)
        permissions_locked = await _lock_ticket_to_claimant(
            interaction, settings, interaction.user
        )

    description = (
        f"{interaction.user.mention} is now handling this order.\n\n"
        "Other chefs can still view the ticket, but they cannot message, react, "
        "use commands, or create threads here."
    )
    if not permissions_locked:
        description += (
            "\n\n⚠️ The ticket was claimed, but Discord would not apply the chat lock. "
            "An administrator should check the bot's **Manage Channels** permission."
        )
    await interaction.followup.send(
        embed=discord.Embed(
            title="Ticket Claimed & Locked",
            description=description,
            color=SUCCESS_COLOR if permissions_locked else WARNING_COLOR,
        ),
        allowed_mentions=discord.AllowedMentions(users=[interaction.user]),
    )


def _panel_embed(settings: GuildSettings, orders_open: bool) -> discord.Embed:
    status_line = (
        "🟢 **ORDERS ARE OPEN** — New order tickets are being accepted."
        if orders_open
        else "🔴 **ORDERS ARE CLOSED** — New order tickets are temporarily paused."
    )
    embed = discord.Embed(
        title=f"🛵 {settings.brand_name}",
        description=(
            f"{status_line}\n\n"
            "Send a **DoorDash group-order cart link** and a Manual Chef will handle "
            "the order inside a private ticket.\n\n"
            "Choose **Place Order**, complete the quick form, and keep the group cart "
            "available for your chef.\n\n"
            "**Minimum final total: $30 after taxes and fees.**"
        ),
        color=SUCCESS_COLOR if orders_open else ERROR_COLOR,
    )
    embed.add_field(
        name="🚗 Delivery or Pickup",
        value="For pickup, type **PICKUP** in the form exactly as shown.",
        inline=True,
    )
    embed.add_field(
        name="🧾 Final Total",
        value=(
            "Enter at least **$30 after taxes and fees** from the DoorDash checkout "
            "screen."
        ),
        inline=True,
    )
    embed.add_field(
        name="🔗 Group Cart",
        value="Use an HTTPS link from **drd.sh** or **doordash.com**.",
        inline=False,
    )
    embed.set_footer(text="Never share passwords, card numbers, or login codes.")
    if settings.banner_url and _valid_http_url(settings.banner_url):
        embed.set_image(url=settings.banner_url)
    elif DEFAULT_BANNER_PATH.is_file():
        embed.set_image(url=f"attachment://{DEFAULT_BANNER_FILENAME}")
    return embed


def _default_banner_file(settings: GuildSettings) -> discord.File | None:
    if settings.banner_url or not DEFAULT_BANNER_PATH.is_file():
        return None
    return discord.File(DEFAULT_BANNER_PATH, filename=DEFAULT_BANNER_FILENAME)


def _how_to_order_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🛵 How DoorDash Manual Works",
        description="Four quick steps. Your chef sees everything important at the top.",
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="1 — Start a DoorDash group order",
        value=(
            "Choose a restaurant in DoorDash, create a group order, add your items, "
            "and copy the group-cart link."
        ),
        inline=False,
    )
    embed.add_field(
        name="2 — Check the final total",
        value=(
            "Your DoorDash total must be **$30 or more after taxes and fees**. "
            "Enter the final amount shown at checkout."
        ),
        inline=False,
    )
    embed.add_field(
        name="3 — Open your private ticket",
        value=(
            "Press **Place Order**, paste the group link and address, and add any "
            "Dasher note. For pickup, type **PICKUP** in all caps."
        ),
        inline=False,
    )
    embed.add_field(
        name="4 — Chef review and payment",
        value=(
            "A Manual Chef claims the ticket, opens the clickable group cart, verifies "
            "the order, sends payment instructions, and posts progress updates."
        ),
        inline=False,
    )
    embed.add_field(
        name="Keep the link active",
        value="Do not delete or close the DoorDash group cart until staff confirms completion.",
        inline=False,
    )
    embed.set_footer(
        text="Never send account passwords, bank logins, full card numbers, or security codes."
    )
    return embed


def _order_summary_embed(order: Order) -> discord.Embed:
    fulfillment = order.fulfillment.title()
    fulfillment_emoji = "🛍️" if order.fulfillment == "pickup" else "🚗"
    embed = discord.Embed(
        title=f"🛵 DoorDash Manual #{order.id:06d} • {order.restaurant_name}",
        description=(
            f"## {fulfillment_emoji} **{fulfillment.upper()}**\n"
            f"### [OPEN DOORDASH GROUP CART]({order.group_order_url})\n"
            "The assigned chef should open the cart first and verify all items."
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="Order Type",
        value=f"{fulfillment_emoji} **{fulfillment.upper()}**",
        inline=True,
    )
    embed.add_field(
        name="Customer-Entered Final Total",
        value=format_cents(order.submitted_total_cents),
        inline=True,
    )
    embed.add_field(name="Status", value="Waiting for staff", inline=True)
    embed.add_field(
        name="Estimated Customer Price (50% off final total)",
        value=f"**{format_cents(order.customer_price_cents)}**",
        inline=True,
    )
    embed.add_field(
        name=(
            "Pickup Instructions"
            if order.fulfillment == "pickup"
            else "Full Delivery Address"
        ),
        value=order.location[:1024],
        inline=False,
    )
    embed.add_field(name="Customer", value=f"<@{order.customer_id}>", inline=False)
    if order.notes:
        embed.add_field(name="Notes", value=order.notes[:1024], inline=False)
    embed.set_footer(text="The final total is customer-entered and must include taxes and fees.")
    return embed


class OwnedView(discord.ui.View):
    def __init__(self, owner_id: int, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await _ephemeral(interaction, "This menu belongs to another customer.")
            return False
        return True


class MainPanelView(discord.ui.View):
    def __init__(self, bot: DashManualBot, *, orders_open: bool = True) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.open_order.disabled = not orders_open
        self.open_order.emoji = "🟢" if orders_open else "🔴"
        self.open_order.style = (
            discord.ButtonStyle.success if orders_open else discord.ButtonStyle.danger
        )
        self.open_order.label = "Place Order" if orders_open else "Orders Closed"

    @discord.ui.button(
        label="Place Order",
        emoji="🛵",
        style=discord.ButtonStyle.primary,
        custom_id="dashmanual:v1:open_order",
    )
    async def open_order(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild_id is None:
            return

        if not await self.bot.db.get_store_open(interaction.guild_id):
            await _ephemeral(
                interaction,
                "🔴 Orders are currently closed. Please check back when the storefront reopens.",
            )
            return

        existing = await self.bot.db.get_active_order_for_customer(
            interaction.guild_id, interaction.user.id
        )
        if existing:
            channel_text = (
                f"<#{existing.channel_id}>" if existing.channel_id else "your pending ticket"
            )
            await _ephemeral(
                interaction,
                f"You already have an active order: {channel_text}. Close it before opening another.",
            )
            return

        await interaction.response.send_modal(DashOrderModal(self.bot))

    @discord.ui.button(
        label="How It Works",
        emoji="🛒",
        style=discord.ButtonStyle.secondary,
        custom_id="dashmanual:v1:how_to_order",
    )
    async def how_to_order(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await _ephemeral(interaction, embed=_how_to_order_embed())


class DashOrderModal(discord.ui.Modal, title="🛵 Start Your DoorDash Order"):
    def __init__(self, bot: DashManualBot) -> None:
        super().__init__()
        self.bot = bot
        self.group_link = discord.ui.TextInput(
            label="DoorDash group cart link",
            placeholder="https://drd.sh/cart/...",
            required=True,
            min_length=12,
            max_length=500,
        )
        self.final_total = discord.ui.TextInput(
            label="Final total after taxes and fees",
            placeholder="$30.00 minimum — checkout total",
            required=True,
            min_length=1,
            max_length=20,
        )
        self.delivery_address = discord.ui.TextInput(
            label="Delivery address (ignored for pickup)",
            placeholder="Street, apt/unit, City, State, ZIP",
            required=True,
            min_length=3,
            max_length=350,
            style=discord.TextStyle.paragraph,
        )
        self.dasher_note = discord.ui.TextInput(
            label="Note for the Dasher (optional)",
            placeholder="Gate code, leave at side door, hotel room, etc.",
            required=False,
            max_length=500,
            style=discord.TextStyle.paragraph,
        )
        self.pickup_code = discord.ui.TextInput(
            label="Pickup instead of delivery?",
            placeholder="Type PICKUP for pickup — leave blank for delivery",
            required=False,
            max_length=20,
        )
        self.add_item(self.group_link)
        self.add_item(self.final_total)
        self.add_item(self.delivery_address)
        self.add_item(self.dasher_note)
        self.add_item(self.pickup_code)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if (
            settings is None
            or interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
        ):
            return
        if not await self.bot.db.get_store_open(interaction.guild.id):
            await _ephemeral(
                interaction,
                "🔴 Orders closed while you were completing the form. Try again when open.",
            )
            return

        try:
            group_url = normalize_group_order_url(str(self.group_link))
        except DoorDashLinkError as exc:
            await _ephemeral(interaction, str(exc))
            return
        try:
            final_total_cents = parse_money(str(self.final_total))
        except MoneyError as exc:
            await _ephemeral(interaction, str(exc))
            return
        if final_total_cents < MINIMUM_TOTAL_CENTS:
            await _ephemeral(
                interaction,
                "The final DoorDash total must be **$30.00 or more after taxes and fees**.",
            )
            return

        pickup_value = str(self.pickup_code).strip()
        if pickup_value and pickup_value != "PICKUP":
            await _ephemeral(
                interaction,
                "For pickup, type **PICKUP** in all caps. Otherwise leave that field blank.",
            )
            return
        fulfillment = "pickup" if pickup_value == "PICKUP" else "delivery"
        location = (
            "PICKUP — open the DoorDash group cart to confirm the restaurant location"
            if fulfillment == "pickup"
            else str(self.delivery_address).strip()
        )

        await interaction.response.defer(ephemeral=True, thinking=True)
        existing = await self.bot.db.get_active_order_for_customer(
            interaction.guild.id, interaction.user.id
        )
        if existing:
            await interaction.followup.send(
                f"You already have an active ticket: <#{existing.channel_id}>.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        store_name = "DoorDash Group Order"
        detected_subtotal_cents: int | None = None
        resolved_url = group_url
        inspection_note: str | None = None
        try:
            preview = await inspect_group_order_link(group_url)
            resolved_url = preview.resolved_url
            store_name = preview.store_name or store_name
            detected_subtotal_cents = preview.detected_subtotal_cents
        except DoorDashLinkError as exc:
            inspection_note = str(exc)
        except Exception:
            LOGGER.exception("Unexpected DoorDash link inspection failure")
            inspection_note = "DoorDash did not expose public cart details; open the link manually."

        try:
            order = await self.bot.db.create_order(
                guild_id=interaction.guild.id,
                customer_id=interaction.user.id,
                restaurant_key="doordash",
                restaurant_name=store_name,
                group_order_url=resolved_url,
                fulfillment=fulfillment,
                location=location,
                contact_name=interaction.user.display_name,
                phone="Not requested",
                email="",
                notes=str(self.dasher_note).strip() or None,
                submitted_total_cents=final_total_cents,
            )
        except ActiveOrderExistsError:
            await interaction.followup.send(
                "You already have an active DoorDash Manual ticket.", ephemeral=True
            )
            return

        category = interaction.guild.get_channel(settings.ticket_category_id)
        staff_role = interaction.guild.get_role(settings.staff_role_id)
        bot_member = interaction.guild.me
        if (
            not isinstance(category, discord.CategoryChannel)
            or staff_role is None
            or bot_member is None
        ):
            await self.bot.db.cancel_order_creation(order.id)
            await interaction.followup.send(
                "Ticket setup is incomplete. Ask an administrator to rerun `/setup`.",
                ephemeral=True,
            )
            return

        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                use_application_commands=True,
                use_external_apps=True,
            ),
            staff_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                use_application_commands=True,
                use_external_apps=True,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }
        channel_name = (
            f"dash-{order.id:06d}-{_safe_channel_fragment(interaction.user.display_name)}"
        )
        try:
            channel = await interaction.guild.create_text_channel(
                channel_name,
                category=category,
                overwrites=overwrites,
                topic=(
                    f"DoorDash Manual #{order.id:06d} • Customer {interaction.user.id} • "
                    f"{fulfillment.upper()}"
                )[:1024],
                reason=f"DoorDash Manual #{order.id:06d}",
            )
            order = await self.bot.db.attach_channel(order.id, channel.id)
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not create DoorDash Manual ticket %s", order.id)
            await self.bot.db.cancel_order_creation(order.id)
            await interaction.followup.send(
                "I could not create the ticket. Check my Manage Channels permission.",
                ephemeral=True,
            )
            return

        await channel.send(
            content=f"{interaction.user.mention} {staff_role.mention}",
            embed=_order_summary_embed(order),
            view=TicketControlsView(self.bot),
            allowed_mentions=discord.AllowedMentions(
                users=[interaction.user], roles=[staff_role], everyone=False
            ),
        )
        chef_embed = discord.Embed(
            title=f"{('🛍️ PICKUP' if fulfillment == 'pickup' else '🚗 DELIVERY')} — CHEF START HERE",
            description=(
                f"### [OPEN THE CLICKABLE GROUP CART]({order.group_order_url})\n"
                f"**Customer-entered final total:** {format_cents(final_total_cents)}\n"
                f"**Store detected:** {store_name}\n"
                f"**Address / pickup:** {location}"
            ),
            color=SUCCESS_COLOR if fulfillment == "pickup" else EMBED_COLOR,
        )
        if detected_subtotal_cents is not None:
            chef_embed.add_field(
                name="Publicly Detected Subtotal (Reference Only)",
                value=(
                    f"{format_cents(detected_subtotal_cents)} • This is before taxes and fees; "
                    "compare it with the live checkout manually."
                ),
                inline=False,
            )
        elif inspection_note:
            chef_embed.add_field(
                name="Automatic Link Read",
                value=f"{inspection_note} The link is still ready to open manually.",
                inline=False,
            )
        if order.notes:
            chef_embed.add_field(name="Dasher Note", value=order.notes[:1024], inline=False)
        chef_embed.set_footer(text="Claim the ticket before sending payment or progress updates.")
        await channel.send(embed=chef_embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(
            f"Your private DoorDash Manual ticket is ready: {channel.mention}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class TicketControlsView(discord.ui.View):
    def __init__(self, bot: DashManualBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Claim",
        emoji="🙋",
        style=discord.ButtonStyle.success,
        custom_id="dashmanual:v1:claim",
    )
    async def claim(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _claim_ticket(self.bot, interaction)

    @discord.ui.button(
        label="Close Ticket",
        emoji="🔒",
        style=discord.ButtonStyle.danger,
        custom_id="dashmanual:v1:close",
    )
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only staff can close and archive tickets.")
            return
        await _ephemeral(
            interaction,
            "Save the transcript and close this ticket?",
            view=ConfirmCloseView(self.bot, interaction.user.id),
        )


class ConfirmCloseView(OwnedView):
    def __init__(self, bot: DashManualBot, owner_id: int) -> None:
        super().__init__(owner_id, timeout=60)
        self.bot = bot

    @discord.ui.button(label="Save & Close", emoji="🔒", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await close_ticket(self.bot, interaction, reason="Closed with the ticket button")

    @discord.ui.button(label="Keep Open", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Ticket left open.", embed=None, view=None
        )


class InvoiceView(discord.ui.View):
    def __init__(self, bot: DashManualBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Choose Payment Method",
        emoji="💳",
        style=discord.ButtonStyle.primary,
        custom_id="dashmanual:v1:choose_payment",
    )
    async def choose_payment(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        if order is None:
            return
        if interaction.user.id != order.customer_id:
            await _ephemeral(interaction, "Only the customer can choose how to pay.")
            return
        if order.assigned_staff_id is None or interaction.guild_id is None:
            await _ephemeral(interaction, "A staff member must claim this ticket first.")
            return
        methods = await self.bot.db.list_payment_methods(
            interaction.guild_id, order.assigned_staff_id
        )
        if not methods:
            await _ephemeral(
                interaction, "The assigned staff member has no payment methods available."
            )
            return
        embed = discord.Embed(
            title="Choose a Payment Method",
            description=f"Amount due: **{format_cents(order.customer_price_cents)}**",
            color=EMBED_COLOR,
        )
        await _ephemeral(
            interaction,
            embed=embed,
            view=PaymentPickerView(self.bot, interaction.user.id, order.id, methods),
        )


class PaymentSelect(discord.ui.Select):
    def __init__(
        self,
        bot: DashManualBot,
        owner_id: int,
        order_id: int,
        methods: list[PaymentMethod],
    ) -> None:
        self.bot = bot
        self.owner_id = owner_id
        self.order_id = order_id
        self.methods = {str(method.id): method for method in methods[:25]}
        options = [
            discord.SelectOption(
                label=method.name[:100],
                value=str(method.id),
                emoji="💳",
            )
            for method in methods[:25]
        ]
        super().__init__(
            placeholder="Select how you want to pay…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        order = await self.bot.db.get_order(self.order_id)
        method = self.methods[self.values[0]]
        if order is None or order.channel_id != interaction.channel_id:
            await _ephemeral(interaction, "This invoice is no longer available.")
            return
        if order.customer_id != interaction.user.id:
            await _ephemeral(interaction, "Only the customer can select payment.")
            return
        if order.assigned_staff_id != method.staff_user_id:
            await _ephemeral(
                interaction,
                "The assigned staff member changed. Ask staff to send a new invoice.",
            )
            return

        order = await self.bot.db.set_payment_method_for_order(
            order.id, method.name, interaction.user.id
        )
        embed = discord.Embed(
            title=f"💳 Pay with {method.name}",
            description=(
                f"Send exactly **{format_cents(order.customer_price_cents)}** using "
                "the instructions below."
            ),
            color=WARNING_COLOR,
        )
        embed.add_field(
            name="Payment Instructions",
            value=method.instructions[:1024],
            inline=False,
        )
        embed.add_field(
            name="After Paying",
            value=(
                "Select **I've Paid** and upload a clear payment screenshot. "
                "Staff must confirm it before the order is placed."
            ),
            inline=False,
        )
        embed.set_footer(
            text="Only use the details shown in this private ticket. Never share banking passwords or security codes."
        )
        await interaction.response.edit_message(
            content="Payment method selected.",
            embed=None,
            view=None,
        )
        if interaction.channel is not None:
            await interaction.channel.send(
                content=f"<@{order.customer_id}>",
                embed=embed,
                view=PaymentSubmittedView(self.bot),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )


class PaymentPickerView(OwnedView):
    def __init__(
        self,
        bot: DashManualBot,
        owner_id: int,
        order_id: int,
        methods: list[PaymentMethod],
    ) -> None:
        super().__init__(owner_id)
        self.add_item(PaymentSelect(bot, owner_id, order_id, methods))


class PaymentSubmittedView(discord.ui.View):
    def __init__(self, bot: DashManualBot) -> None:
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="I've Paid",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="dashmanual:v1:payment_submitted",
    )
    async def submitted(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        order = await _ticket_order(self.bot, interaction)
        if order is None:
            return
        if interaction.user.id != order.customer_id:
            await _ephemeral(interaction, "Only the customer can submit payment.")
            return
        order = await self.bot.db.set_order_status(
            order.id,
            "payment_submitted",
            actor_id=interaction.user.id,
        )
        staff_mention = f"<@{order.assigned_staff_id}>" if order.assigned_staff_id else "Staff"
        await interaction.response.send_message(
            content=staff_mention,
            embed=discord.Embed(
                title="Payment Submitted",
                description=(
                    "Please upload the payment screenshot now. Staff will verify it "
                    "before placing the DoorDash order."
                ),
                color=WARNING_COLOR,
            ),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )


def _message_text(message: discord.Message) -> str:
    parts = [message.content] if message.content else []
    for embed in message.embeds:
        if embed.title:
            parts.append(f"[Embed] {embed.title}")
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")
    for sticker in message.stickers:
        parts.append(f"[Sticker] {sticker.name}")
    return "\n".join(parts)


async def _collect_transcript(channel: discord.TextChannel) -> list[TranscriptMessage]:
    messages: list[TranscriptMessage] = []
    async for message in channel.history(limit=5000, oldest_first=True):
        avatar_url = (
            str(message.author.display_avatar.url)
            if getattr(message.author, "display_avatar", None)
            else None
        )
        messages.append(
            TranscriptMessage(
                author_name=str(message.author),
                author_id=message.author.id,
                avatar_url=avatar_url,
                created_at=message.created_at,
                content=_message_text(message),
                attachments=[
                    TranscriptAttachment(
                        filename=attachment.filename,
                        url=attachment.url,
                    )
                    for attachment in message.attachments
                ],
            )
        )
    return messages


async def close_ticket(
    bot: DashManualBot,
    interaction: discord.Interaction,
    *,
    reason: str,
    commission_cents: int | None = None,
    close_delay_seconds: int = 0,
) -> None:
    order = await _ticket_order(bot, interaction)
    settings = await _configured_settings(bot, interaction)
    if (
        order is None
        or settings is None
        or interaction.guild is None
        or not isinstance(interaction.channel, discord.TextChannel)
    ):
        return
    if not _is_staff(interaction.user, settings):
        await _ephemeral(interaction, "Only staff can close and archive tickets.")
        return
    if order.assigned_staff_id not in {None, interaction.user.id} and not _is_administrator(
        interaction.user
    ):
        await _ephemeral(
            interaction,
            f"Only the claiming chef <@{order.assigned_staff_id}> or an administrator "
            "can close this ticket.",
        )
        return
    if interaction.channel.id in bot.closing_channels:
        await _ephemeral(interaction, "This ticket is already being archived.")
        return

    bot.closing_channels.add(interaction.channel.id)
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        commission_staff_id: int | None = None
        if commission_cents is not None:
            if order.assigned_staff_id is None:
                order = await bot.db.assign_order(order.id, interaction.user.id)
                if isinstance(interaction.user, discord.Member):
                    await _lock_ticket_to_claimant(interaction, settings, interaction.user)
            commission_staff_id = order.assigned_staff_id or interaction.user.id
        commission_recorded: bool | None = None
        if commission_cents is not None and close_delay_seconds > 0:
            try:
                commission_recorded = await bot.db.record_owner_commission(
                    order.id,
                    guild_id=interaction.guild.id,
                    amount_cents=commission_cents,
                    actor_id=interaction.user.id,
                    owed_by_staff_id=commission_staff_id,
                    reason=reason,
                )
                await bot.db.set_order_status(
                    order.id,
                    "completed",
                    actor_id=interaction.user.id,
                    details={
                        "reason": reason,
                        "scheduled_close_delay_seconds": close_delay_seconds,
                    },
                )
            except Exception:
                LOGGER.exception("Could not complete order %s", order.id)
                await interaction.followup.send(
                    "The order could not be completed in the database, so the ticket "
                    "was left open. Please try `/done` again.",
                    ephemeral=True,
                )
                return

        if close_delay_seconds > 0:
            close_at = discord.utils.utcnow() + timedelta(seconds=close_delay_seconds)
            close_timestamp = int(close_at.timestamp())
            commission_message = ""
            if commission_cents is not None:
                assert commission_staff_id is not None
                commission_message = (
                    f" {format_cents(commission_cents)} was recorded as owed by "
                    f"<@{commission_staff_id}>."
                    if commission_recorded
                    else " The owner commission for this order was already recorded."
                )
            await interaction.followup.send(
                "Order completed."
                f"{commission_message} The ticket will remain open for 30 minutes and "
                f"close automatically <t:{close_timestamp}:R>.",
                ephemeral=True,
            )
            await interaction.channel.send(
                content=f"<@{order.customer_id}>",
                embed=discord.Embed(
                    title="✅ Order Completed — 30-Minute Access Window",
                    description=(
                        "This ticket will stay open for **30 minutes** so you can open, "
                        "copy, or save your order link and any important information.\n\n"
                        f"**Automatic close:** <t:{close_timestamp}:F> "
                        f"(<t:{close_timestamp}:R>)"
                    ),
                    color=SUCCESS_COLOR,
                ),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
            await asyncio.sleep(close_delay_seconds)

        transcript_messages = await _collect_transcript(interaction.channel)
        archived_order = replace(order, status="completed") if commission_cents else order
        content = render_transcript_html(
            guild_name=interaction.guild.name,
            channel_name=interaction.channel.name,
            order=archived_order,
            messages=transcript_messages,
        )
        transcript_path = save_transcript(
            bot.settings.transcript_dir / f"order-{order.id:06d}.html",
            content,
        )

        log_channel = interaction.guild.get_channel(settings.log_channel_id)
        if not isinstance(log_channel, discord.TextChannel):
            message = (
                "I created the transcript but could not find the configured transcript "
                "channel. The ticket was left open."
            )
            if close_delay_seconds > 0:
                await interaction.channel.send(f"⚠️ {message}")
            else:
                await interaction.followup.send(message, ephemeral=True)
            return

        log_embed = discord.Embed(
            title=f"Archived Order #{order.id:06d}",
            description=reason[:4096],
            color=discord.Color.dark_grey(),
        )
        log_embed.add_field(
            name="Customer", value=f"<@{order.customer_id}> (`{order.customer_id}`)"
        )
        log_embed.add_field(name="DoorDash Store", value=order.restaurant_name)
        log_embed.add_field(
            name="Group Cart Link",
            value=f"[Open DoorDash Cart]({order.group_order_url})",
            inline=False,
        )
        log_embed.add_field(
            name="Final Status",
            value=archived_order.status.replace("_", " ").title(),
        )
        if order.assigned_staff_id:
            log_embed.add_field(
                name="Assigned Staff",
                value=f"<@{order.assigned_staff_id}> (`{order.assigned_staff_id}`)",
            )

        try:
            log_message = await log_channel.send(
                embed=log_embed,
                file=discord.File(transcript_path),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.HTTPException):
            LOGGER.exception("Could not upload transcript for order %s", order.id)
            message = (
                "The transcript could not be uploaded, so the ticket was left open. "
                "Check the bot's permissions in the transcript channel."
            )
            if close_delay_seconds > 0:
                await interaction.channel.send(f"⚠️ {message}")
            else:
                await interaction.followup.send(message, ephemeral=True)
            return

        try:
            if commission_cents is not None:
                if commission_recorded is None:
                    assert commission_staff_id is not None
                    _, commission_recorded = await bot.db.close_order_with_commission(
                        order.id,
                        guild_id=interaction.guild.id,
                        amount_cents=commission_cents,
                        actor_id=interaction.user.id,
                        owed_by_staff_id=commission_staff_id,
                        reason=reason,
                    )
                else:
                    await bot.db.set_order_status(
                        order.id,
                        "closed",
                        actor_id=interaction.user.id,
                        details={"reason": reason, "owner_commission_cents": commission_cents},
                    )
                log_embed.add_field(
                    name="Owner Commission Owed",
                    value=(
                        f"{format_cents(commission_cents)} recorded"
                        if commission_recorded
                        else f"{format_cents(commission_cents)} was already recorded"
                    ),
                    inline=False,
                )
                log_embed.add_field(
                    name="Owed By Manual Chef",
                    value=f"<@{commission_staff_id}> (`{commission_staff_id}`)",
                    inline=False,
                )
                with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                    await log_message.edit(embed=log_embed)
            else:
                await bot.db.set_order_status(
                    order.id,
                    "closed",
                    actor_id=interaction.user.id,
                    details={"reason": reason},
                )
        except Exception:
            LOGGER.exception("Could not finalize order %s in the database", order.id)
            message = (
                "The transcript was uploaded, but the order could not be finalized in the "
                "database. The ticket was left open; please try again."
            )
            if close_delay_seconds > 0:
                await interaction.channel.send(f"⚠️ {message}")
            else:
                await interaction.followup.send(message, ephemeral=True)
            return

        customer = interaction.guild.get_member(order.customer_id)
        if customer is not None:
            with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                await customer.send(
                    content=f"Your order ticket #{order.id:06d} was closed.",
                    file=discord.File(transcript_path),
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        confirmation = "Transcript saved."
        if commission_cents is not None:
            assert commission_staff_id is not None
            confirmation += (
                f" {format_cents(commission_cents)} was recorded as owed by "
                f"<@{commission_staff_id}>."
                if commission_recorded
                else " The owner commission for this order was already recorded."
            )
        confirmation += " This ticket will close in 5 seconds."
        if close_delay_seconds == 0:
            await interaction.followup.send(confirmation, ephemeral=True)
        await interaction.channel.send(
            "🔒 The access window has ended. Transcript saved; closing this ticket "
            "in 5 seconds."
            if close_delay_seconds > 0
            else "🔒 Transcript saved. Closing this ticket in 5 seconds.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await asyncio.sleep(5)
        await interaction.channel.delete(
            reason=f"Order #{order.id:06d} archived by {interaction.user}"
        )
    finally:
        bot.closing_channels.discard(interaction.channel.id)


async def _refresh_saved_panel(
    bot: DashManualBot, guild: discord.Guild, settings: GuildSettings
) -> bool:
    if not settings.panel_channel_id or not settings.panel_message_id:
        return False
    channel = guild.get_channel(settings.panel_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return False
    try:
        message = await channel.fetch_message(settings.panel_message_id)
        orders_open = await bot.db.get_store_open(guild.id)
        if settings.banner_url:
            attachments: list[discord.Attachment | discord.File] = []
        else:
            matching = [
                attachment
                for attachment in message.attachments
                if attachment.filename == DEFAULT_BANNER_FILENAME
            ]
            attachments = matching or [discord.File(DEFAULT_BANNER_PATH)]
        await message.edit(
            embed=_panel_embed(settings, orders_open),
            view=MainPanelView(bot, orders_open=orders_open),
            attachments=attachments,
        )
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False


async def _announce_store_open(
    bot: DashManualBot, guild: discord.Guild, settings: GuildSettings
) -> str:
    role_id = settings.customer_ping_role_id or bot.settings.customer_ping_role_id
    if role_id is None:
        return (
            "No customer role was pinged. Run `/setup` and choose the customer role."
        )

    role = guild.get_role(role_id)
    if role is None or role.is_default():
        return (
            "No customer role was pinged because the saved customer role no longer exists. "
            "Run `/setup` to choose it again."
        )

    channel = (
        guild.get_channel(settings.panel_channel_id)
        if settings.panel_channel_id is not None
        else None
    )
    if not isinstance(channel, discord.TextChannel):
        return "No customer role was pinged because the saved storefront channel is missing."

    bot_member = guild.me
    if bot_member is None:
        return "No customer role was pinged because the bot member could not be found."
    permissions = channel.permissions_for(bot_member)
    if not permissions.send_messages:
        return (
            f"No customer role was pinged because I cannot send messages in {channel.mention}."
        )
    if not role.mentionable and not permissions.mention_everyone:
        return (
            f"No customer role was pinged because {role.mention} is not mentionable. "
            "Make the role mentionable or give the bot **Mention @everyone, @here, and All Roles**."
        )

    try:
        await channel.send(
            content=(
                f"{role.mention} 🟢 **BOB'S BURGERS DOORDASH MANUAL IS OPEN!**\n"
                "DoorDash Manual orders are available. Your cart must have a "
                "**$30+ final total after taxes and fees**. Use **Place Order** above "
                "to open a private ticket."
            ),
            allowed_mentions=discord.AllowedMentions(
                users=False,
                roles=[role],
                everyone=False,
                replied_user=False,
            ),
        )
    except (discord.Forbidden, discord.HTTPException):
        LOGGER.exception("Could not send the storefront-open ping in guild %s", guild.id)
        return f"The store opened, but I could not ping customers in {channel.mention}."

    return f"Customer notification sent to {role.mention} in {channel.mention}."


@dataclass(slots=True)
class SetupSelections:
    panel_channel_id: int | None = None
    ticket_category_id: int | None = None
    staff_role_id: int | None = None
    transcript_channel_id: int | None = None
    customer_role_id: int | None = None

    @classmethod
    def from_settings(
        cls,
        guild: discord.Guild,
        settings: GuildSettings | None,
        fallback_customer_role_id: int | None,
    ) -> SetupSelections:
        if settings is None:
            customer_role_id = fallback_customer_role_id
            if customer_role_id is not None and guild.get_role(customer_role_id) is None:
                customer_role_id = None
            return cls(customer_role_id=customer_role_id)

        customer_role_id = settings.customer_ping_role_id or fallback_customer_role_id
        if customer_role_id is not None and guild.get_role(customer_role_id) is None:
            customer_role_id = None
        return cls(
            panel_channel_id=settings.panel_channel_id,
            ticket_category_id=settings.ticket_category_id,
            staff_role_id=settings.staff_role_id,
            transcript_channel_id=settings.log_channel_id,
            customer_role_id=customer_role_id,
        )

    def core_complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.panel_channel_id,
                self.ticket_category_id,
                self.staff_role_id,
                self.transcript_channel_id,
            )
        )


def _channel_default(
    guild: discord.Guild,
    channel_id: int | None,
    channel_type: type[discord.abc.GuildChannel],
) -> list[discord.abc.GuildChannel]:
    if channel_id is None:
        return []
    channel = guild.get_channel(channel_id)
    return [channel] if isinstance(channel, channel_type) else []


def _role_default(guild: discord.Guild, role_id: int | None) -> list[discord.Role]:
    if role_id is None:
        return []
    role = guild.get_role(role_id)
    return [role] if role is not None and not role.is_default() else []


def _setup_summary_embed(
    guild: discord.Guild,
    state: SetupSelections,
    *,
    step: int,
) -> discord.Embed:
    category = guild.get_channel(state.ticket_category_id) if state.ticket_category_id else None
    embed = discord.Embed(
        title=f"DoorDash Manual Setup • Step {step} of 2",
        description=(
            "Choose from your existing Discord channels and roles below. "
            "Your selections are saved only after pressing **Save Setup**."
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(
        name="Storefront Channel",
        value=f"<#{state.panel_channel_id}>" if state.panel_channel_id else "Not selected",
        inline=True,
    )
    embed.add_field(
        name="Ticket Category",
        value=(
            f"**{category.name}**"
            if isinstance(category, discord.CategoryChannel)
            else "Not selected"
        ),
        inline=True,
    )
    embed.add_field(
        name="Manual Chef Role",
        value=f"<@&{state.staff_role_id}>" if state.staff_role_id else "Not selected",
        inline=True,
    )
    embed.add_field(
        name="Transcript Channel",
        value=(
            f"<#{state.transcript_channel_id}>"
            if state.transcript_channel_id
            else "Not selected"
        ),
        inline=True,
    )
    embed.add_field(
        name="Customer Ping Role",
        value=f"<@&{state.customer_role_id}>" if state.customer_role_id else "Not selected",
        inline=True,
    )
    embed.add_field(
        name="Storefront Banner",
        value="Included **$30 minimum final-total** banner",
        inline=False,
    )
    embed.set_footer(text="Run /setup again whenever you want to change these settings.")
    return embed


class _SetupChannelSelect(discord.ui.ChannelSelect):
    def __init__(
        self,
        *,
        guild: discord.Guild,
        state_key: str,
        channel_type: discord.ChannelType,
        resolved_type: type[discord.abc.GuildChannel],
        current_id: int | None,
        placeholder: str,
        row: int,
    ) -> None:
        super().__init__(
            custom_id=f"dash_setup_{state_key}",
            channel_types=[channel_type],
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            required=True,
            default_values=_channel_default(guild, current_id, resolved_type),
            row=row,
        )
        self.state_key = state_key

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, SetupCoreView):
            return
        setattr(view.state, self.state_key, self.values[0].id)
        await interaction.response.edit_message(
            embed=_setup_summary_embed(view.guild, view.state, step=1),
            view=view,
        )


class _SetupRoleSelect(discord.ui.RoleSelect):
    def __init__(
        self,
        *,
        guild: discord.Guild,
        state_key: str,
        current_id: int | None,
        placeholder: str,
        row: int,
    ) -> None:
        super().__init__(
            custom_id=f"dash_setup_{state_key}",
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            required=True,
            default_values=_role_default(guild, current_id),
            row=row,
        )
        self.state_key = state_key

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, SetupCoreView):
            setattr(view.state, self.state_key, self.values[0].id)
            embed = _setup_summary_embed(view.guild, view.state, step=1)
        elif isinstance(view, SetupFinishView):
            setattr(view.state, self.state_key, self.values[0].id)
            embed = _setup_summary_embed(view.guild, view.state, step=2)
        else:
            return
        await interaction.response.edit_message(embed=embed, view=view)


class _SetupOwnedView(discord.ui.View):
    def __init__(
        self,
        bot: DashManualBot,
        guild: discord.Guild,
        owner_id: int,
        state: SetupSelections,
    ) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.guild = guild
        self.owner_id = owner_id
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await _ephemeral(interaction, "Only the administrator who opened `/setup` can use it.")
        return False

    async def _cancel(self, interaction: discord.Interaction) -> None:
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Setup Cancelled",
                description="Nothing was changed.",
                color=WARNING_COLOR,
            ),
            view=None,
        )


class SetupCoreView(_SetupOwnedView):
    def __init__(
        self,
        bot: DashManualBot,
        guild: discord.Guild,
        owner_id: int,
        state: SetupSelections,
    ) -> None:
        super().__init__(bot, guild, owner_id, state)
        self.add_item(
            _SetupChannelSelect(
                guild=guild,
                state_key="panel_channel_id",
                channel_type=discord.ChannelType.text,
                resolved_type=discord.TextChannel,
                current_id=state.panel_channel_id,
                placeholder="Choose the existing storefront channel",
                row=0,
            )
        )
        self.add_item(
            _SetupChannelSelect(
                guild=guild,
                state_key="ticket_category_id",
                channel_type=discord.ChannelType.category,
                resolved_type=discord.CategoryChannel,
                current_id=state.ticket_category_id,
                placeholder="Choose the existing ticket category",
                row=1,
            )
        )
        self.add_item(
            _SetupRoleSelect(
                guild=guild,
                state_key="staff_role_id",
                current_id=state.staff_role_id,
                placeholder="Choose the existing Manual Chef role",
                row=2,
            )
        )
        self.add_item(
            _SetupChannelSelect(
                guild=guild,
                state_key="transcript_channel_id",
                channel_type=discord.ChannelType.text,
                resolved_type=discord.TextChannel,
                current_id=state.transcript_channel_id,
                placeholder="Choose the existing transcript channel",
                row=3,
            )
        )

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.primary, row=4)
    async def continue_setup(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not self.state.core_complete():
            await _ephemeral(interaction, "Select all four channels, categories, and roles first.")
            return
        next_view = SetupFinishView(
            self.bot,
            self.guild,
            self.owner_id,
            self.state,
        )
        await interaction.response.edit_message(
            embed=_setup_summary_embed(self.guild, self.state, step=2),
            view=next_view,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=4)
    async def cancel_setup(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._cancel(interaction)


class SetupFinishView(_SetupOwnedView):
    def __init__(
        self,
        bot: DashManualBot,
        guild: discord.Guild,
        owner_id: int,
        state: SetupSelections,
    ) -> None:
        super().__init__(bot, guild, owner_id, state)
        self.add_item(
            _SetupRoleSelect(
                guild=guild,
                state_key="customer_role_id",
                current_id=state.customer_role_id,
                placeholder="Choose your existing customer role",
                row=0,
            )
        )

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        previous_view = SetupCoreView(
            self.bot,
            self.guild,
            self.owner_id,
            self.state,
        )
        await interaction.response.edit_message(
            embed=_setup_summary_embed(self.guild, self.state, step=1),
            view=previous_view,
        )
        self.stop()

    @discord.ui.button(label="Save Setup", style=discord.ButtonStyle.success, row=1)
    async def save_setup(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not self.state.core_complete() or self.state.customer_role_id is None:
            await _ephemeral(interaction, "Choose the customer role before saving.")
            return

        panel_channel = self.guild.get_channel(self.state.panel_channel_id)
        ticket_category = self.guild.get_channel(self.state.ticket_category_id)
        staff_role = self.guild.get_role(self.state.staff_role_id)
        transcript_channel = self.guild.get_channel(self.state.transcript_channel_id)
        customer_role = self.guild.get_role(self.state.customer_role_id)
        bot_member = self.guild.me

        if not isinstance(panel_channel, discord.TextChannel):
            await _ephemeral(interaction, "The selected storefront channel no longer exists.")
            return
        if not isinstance(ticket_category, discord.CategoryChannel):
            await _ephemeral(interaction, "The selected ticket category no longer exists.")
            return
        if staff_role is None or staff_role.is_default():
            await _ephemeral(interaction, "Choose a private Manual Chef role, not @everyone.")
            return
        if not isinstance(transcript_channel, discord.TextChannel):
            await _ephemeral(interaction, "The selected transcript channel no longer exists.")
            return
        if customer_role is None or customer_role.is_default():
            await _ephemeral(interaction, "Choose your customer role, not @everyone.")
            return
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await _ephemeral(interaction, "Give the bot **Manage Channels** permission first.")
            return
        if not panel_channel.permissions_for(bot_member).send_messages:
            await _ephemeral(interaction, "I cannot send messages in the storefront channel.")
            return
        if not panel_channel.permissions_for(bot_member).attach_files:
            await _ephemeral(interaction, "I need **Attach Files** in the storefront channel.")
            return
        if not transcript_channel.permissions_for(bot_member).attach_files:
            await _ephemeral(interaction, "I need **Attach Files** in the transcript channel.")
            return

        await interaction.response.defer()
        previous = await self.bot.db.get_guild_settings(self.guild.id)
        can_refresh_existing = (
            previous is not None
            and previous.panel_channel_id == panel_channel.id
            and previous.panel_message_id is not None
        )
        await self.bot.db.upsert_guild_settings(
            guild_id=self.guild.id,
            brand_name="Bob's Burgers DoorDash Manual",
            ticket_category_id=ticket_category.id,
            staff_role_id=staff_role.id,
            log_channel_id=transcript_channel.id,
            banner_url=None,
            customer_ping_role_id=customer_role.id,
        )
        settings = await self.bot.db.get_guild_settings(self.guild.id)
        assert settings is not None

        panel_updated = can_refresh_existing and await _refresh_saved_panel(
            self.bot, self.guild, settings
        )
        if not panel_updated:
            orders_open = await self.bot.db.get_store_open(self.guild.id)
            panel_kwargs: dict[str, Any] = {
                "embed": _panel_embed(settings, orders_open),
                "view": MainPanelView(self.bot, orders_open=orders_open),
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if banner_file := _default_banner_file(settings):
                panel_kwargs["file"] = banner_file
            try:
                panel_message = await panel_channel.send(**panel_kwargs)
            except (discord.Forbidden, discord.HTTPException):
                await interaction.edit_original_response(
                    embed=discord.Embed(
                        title="Setup Could Not Post the Storefront",
                        description=(
                            "The selections were saved, but Discord blocked the storefront "
                            "message. Check the bot permissions and run `/panel`."
                        ),
                        color=ERROR_COLOR,
                    ),
                    view=None,
                )
                self.stop()
                return
            await self.bot.db.save_panel(self.guild.id, panel_channel.id, panel_message.id)

        self.stop()
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="✅ DoorDash Manual Setup Saved",
                description=(
                    f"Storefront: {panel_channel.mention}\n"
                    f"Tickets: **{ticket_category.name}**\n"
                    f"Manual Chef: {staff_role.mention}\n"
                    f"Transcripts: {transcript_channel.mention}\n"
                    f"Customer ping: {customer_role.mention}\n\n"
                    "The $30 final-total storefront banner was applied automatically."
                ),
                color=SUCCESS_COLOR,
            ),
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger, row=1)
    async def cancel_setup(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self._cancel(interaction)


class DashCommands(commands.Cog):
    manual = app_commands.Group(
        name="manual", description="Control the DoorDash Manual storefront"
    )
    payments = app_commands.Group(
        name="payments", description="Configure your staff payment methods"
    )

    def __init__(self, bot: DashManualBot) -> None:
        self.bot = bot

    @app_commands.command(name="setup", description="Open the interactive DoorDash setup menu")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setup(self, interaction: discord.Interaction) -> None:
        if not _is_administrator(interaction.user) or interaction.guild is None:
            await _ephemeral(interaction, "Only a server administrator can run setup.")
            return
        existing = await self.bot.db.get_guild_settings(interaction.guild.id)
        state = SetupSelections.from_settings(
            interaction.guild,
            existing,
            self.bot.settings.customer_ping_role_id,
        )
        view = SetupCoreView(
            self.bot,
            interaction.guild,
            interaction.user.id,
            state,
        )
        await _ephemeral(
            interaction,
            embed=_setup_summary_embed(interaction.guild, state, step=1),
            view=view,
        )

    @app_commands.command(name="panel", description="Refresh or repost the order panel")
    @app_commands.guild_only()
    @app_commands.describe(channel="Optional new channel for the customer panel")
    async def panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild is None:
            return
        if not _is_administrator(interaction.user):
            await _ephemeral(interaction, "Only a server administrator can manage the panel.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        if channel is None and await _refresh_saved_panel(
            self.bot, interaction.guild, settings
        ):
            await interaction.followup.send("Panel refreshed.", ephemeral=True)
            return

        target = channel or (
            interaction.channel
            if isinstance(interaction.channel, discord.TextChannel)
            else None
        )
        if target is None:
            await interaction.followup.send(
                "Choose a text channel for the panel.", ephemeral=True
            )
            return
        orders_open = await self.bot.db.get_store_open(interaction.guild.id)
        panel_kwargs: dict[str, Any] = {
            "embed": _panel_embed(settings, orders_open),
            "view": MainPanelView(self.bot, orders_open=orders_open),
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if banner_file := _default_banner_file(settings):
            panel_kwargs["file"] = banner_file
        message = await target.send(**panel_kwargs)
        await self.bot.db.save_panel(interaction.guild.id, target.id, message.id)
        await interaction.followup.send(
            f"Panel posted in {target.mention}.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _set_store_status(
        self, interaction: discord.Interaction, *, orders_open: bool
    ) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only staff can open or close orders.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        was_open = await self.bot.db.get_store_open(interaction.guild.id)
        await self.bot.db.set_store_open(interaction.guild.id, orders_open)
        panel_updated = await _refresh_saved_panel(self.bot, interaction.guild, settings)
        status_message = (
            "🟢 Orders are now **OPEN**. Customers can create new tickets."
            if orders_open
            else "🔴 Orders are now **CLOSED**. New tickets are blocked; "
            "existing tickets remain open."
        )
        if not panel_updated:
            status_message += " Run `/panel` to repost the storefront status."
        if orders_open and not was_open:
            status_message += (
                f"\n{await _announce_store_open(self.bot, interaction.guild, settings)}"
            )
        elif orders_open:
            status_message += "\nThe store was already open, so no new customer ping was sent."
        await interaction.followup.send(status_message, ephemeral=True)

    @manual.command(name="open", description="Open DoorDash Manual for new order tickets")
    @app_commands.guild_only()
    async def store_open(self, interaction: discord.Interaction) -> None:
        await self._set_store_status(interaction, orders_open=True)

    @manual.command(name="close", description="Close DoorDash Manual to new order tickets")
    @app_commands.guild_only()
    async def store_close(self, interaction: discord.Interaction) -> None:
        await self._set_store_status(interaction, orders_open=False)

    @app_commands.command(name="claim", description="Assign this order ticket to yourself")
    @app_commands.guild_only()
    async def claim_command(self, interaction: discord.Interaction) -> None:
        await _claim_ticket(self.bot, interaction)

    @app_commands.command(
        name="force_claim",
        description="Admin: reassign this ticket to a different DoorDash Manual staff member",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(staff="DoorDash Manual staff member who should handle this ticket")
    async def force_claim(
        self, interaction: discord.Interaction, staff: discord.Member
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if (
            order is None
            or settings is None
            or interaction.guild is None
            or not isinstance(interaction.channel, discord.TextChannel)
        ):
            return
        if not _is_administrator(interaction.user):
            await _ephemeral(
                interaction,
                "Only the server owner or an administrator can force-claim tickets.",
            )
            return
        if not _is_staff(staff, settings):
            await _ephemeral(
                interaction,
                f"{staff.mention} does not have the configured DoorDash Manual staff role.",
            )
            return
        if interaction.channel.id in self.bot.closing_channels:
            await _ephemeral(
                interaction,
                "This ticket has already been completed and is waiting to close.",
            )
            return

        claim_lock = self.bot.claim_locks.setdefault(order.id, asyncio.Lock())
        async with claim_lock:
            current_order = await self.bot.db.get_order_by_channel(interaction.channel.id)
            if current_order is None:
                await _ephemeral(interaction, "This channel is not an order ticket.")
                return

            await interaction.response.defer(thinking=True)
            previous_staff_id = current_order.assigned_staff_id
            permissions_locked = await _lock_ticket_to_claimant(interaction, settings, staff)
            if not permissions_locked:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="Force Claim Failed",
                        description=(
                            "Discord would not apply the new ticket permissions. Check that "
                            "the bot has **Manage Channels** and its role is above the staff role."
                        ),
                        color=ERROR_COLOR,
                    ),
                    ephemeral=True,
                )
                return

            if previous_staff_id and previous_staff_id != staff.id:
                previous_staff = interaction.guild.get_member(previous_staff_id)
                if previous_staff is None:
                    with contextlib.suppress(discord.NotFound, discord.Forbidden):
                        previous_staff = await interaction.guild.fetch_member(previous_staff_id)
                if previous_staff is not None:
                    try:
                        await interaction.channel.set_permissions(
                            previous_staff,
                            overwrite=None,
                            reason=(
                                f"DoorDash Manual force-claimed by {interaction.user}; "
                                "removing previous claimant access"
                            ),
                        )
                    except (discord.Forbidden, discord.HTTPException):
                        LOGGER.exception(
                            "Could not remove previous claimant permissions in ticket %s",
                            interaction.channel.id,
                        )
                        with contextlib.suppress(discord.Forbidden, discord.HTTPException):
                            await interaction.channel.set_permissions(
                                staff,
                                overwrite=None,
                                reason="Rolling back failed DoorDash Manual force claim",
                            )
                        await interaction.followup.send(
                            "I could not remove the previous chef's ticket access, so the "
                            "reassignment was cancelled. Check **Manage Channels** permission.",
                            ephemeral=True,
                        )
                        return

            await self.bot.db.force_assign_order(
                current_order.id,
                staff.id,
                actor_id=interaction.user.id,
            )

        previous_text = (
            f"Previous chef: <@{previous_staff_id}>\n"
            if previous_staff_id and previous_staff_id != staff.id
            else ""
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title="Ticket Reassigned",
                description=(
                    f"{previous_text}New chef: {staff.mention}\n\n"
                    "The previous chef is now read-only. Only the selected chef, customer, "
                    "and administrators can continue handling this order."
                ),
                color=SUCCESS_COLOR,
            ),
            allowed_mentions=discord.AllowedMentions(users=[staff]),
        )

    @app_commands.command(
        name="pay", description="Verify the final total and send the customer invoice"
    )
    @app_commands.guild_only()
    @app_commands.describe(
        final_total="Optional corrected DoorDash total after taxes and fees"
    )
    async def pay(
        self, interaction: discord.Interaction, final_total: str | None = None
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None or interaction.guild_id is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only staff can send an invoice.")
            return
        if order.assigned_staff_id not in {None, interaction.user.id} and not _is_administrator(
            interaction.user
        ):
            await _ephemeral(
                interaction, f"This order is assigned to <@{order.assigned_staff_id}>."
            )
            return

        if final_total is not None:
            try:
                total_cents = parse_money(final_total)
            except MoneyError as exc:
                await _ephemeral(interaction, str(exc))
                return
        else:
            total_cents = order.submitted_total_cents
        if total_cents < MINIMUM_TOTAL_CENTS:
            await _ephemeral(
                interaction,
                "The verified DoorDash total must be at least **$30.00 after taxes and fees**.",
            )
            return

        methods = await self.bot.db.list_payment_methods(
            interaction.guild_id, interaction.user.id
        )
        if not methods:
            await _ephemeral(
                interaction,
                "Add at least one payment method first with `/payments set`.",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        if order.assigned_staff_id is None or order.assigned_staff_id != interaction.user.id:
            order = await self.bot.db.assign_order(order.id, interaction.user.id)
            if isinstance(interaction.user, discord.Member):
                await _lock_ticket_to_claimant(interaction, settings, interaction.user)
        order = await self.bot.db.update_order_pricing(
            order.id, total_cents, interaction.user.id
        )
        order = await self.bot.db.set_order_status(
            order.id, "awaiting_payment_selection", actor_id=interaction.user.id
        )

        invoice = discord.Embed(
            title=f"Invoice • Order #{order.id:06d}",
            description=(
                "The final checkout amount was verified. Choose a payment method "
                "below to receive the assigned staff member's instructions."
            ),
            color=EMBED_COLOR,
        )
        invoice.add_field(
            name="Verified DoorDash Final Total",
            value=format_cents(order.submitted_total_cents),
            inline=True,
        )
        invoice.add_field(
            name="You Pay (50% Off)",
            value=f"**{format_cents(order.customer_price_cents)}**",
            inline=True,
        )
        invoice.add_field(name="Handled By", value=interaction.user.mention, inline=False)
        invoice.set_footer(text="Do not pay until you select a method from this invoice.")
        if interaction.channel is not None:
            await interaction.channel.send(
                content=f"<@{order.customer_id}>",
                embed=invoice,
                view=InvoiceView(self.bot),
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        await interaction.followup.send(
            f"Invoice sent. Customer price: {format_cents(order.customer_price_cents)}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="paid", description="Staff: confirm that the customer's payment was received"
    )
    @app_commands.guild_only()
    async def paid(self, interaction: discord.Interaction) -> None:
        await self._staff_status(
            interaction,
            status="paid",
            title="Payment Confirmed",
            description="Payment was verified. Staff can now place the DoorDash order.",
            color=SUCCESS_COLOR,
        )

    @app_commands.command(
        name="ordered", description="Staff: mark the DoorDash order as placed"
    )
    @app_commands.guild_only()
    @app_commands.describe(details="Optional ETA, pickup name, or confirmation number")
    async def ordered(
        self, interaction: discord.Interaction, details: str | None = None
    ) -> None:
        description = "The DoorDash order has been placed."
        if details:
            description += f"\n\n**Update:** {details[:1500]}"
        await self._staff_status(
            interaction,
            status="ordered",
            title="Order Placed",
            description=description,
            color=SUCCESS_COLOR,
            details={"update": details} if details else {},
        )

    @app_commands.command(
        name="complete", description="Staff: mark the food order as completed"
    )
    @app_commands.guild_only()
    async def complete(self, interaction: discord.Interaction) -> None:
        await self._staff_status(
            interaction,
            status="completed",
            title="Order Completed",
            description=(
                "The order is complete. Use `/done` to record the owner commission, "
                "save the transcript, and close this ticket."
            ),
            color=SUCCESS_COLOR,
        )

    @app_commands.command(
        name="done",
        description="Staff: finish the order, record commission, and close the ticket",
    )
    @app_commands.guild_only()
    async def done(self, interaction: discord.Interaction) -> None:
        await close_ticket(
            self.bot,
            interaction,
            reason="Order completed with /done",
            commission_cents=self.bot.settings.owner_commission_cents,
            close_delay_seconds=DONE_CLOSE_DELAY_SECONDS,
        )

    async def _staff_status(
        self,
        interaction: discord.Interaction,
        *,
        status: str,
        title: str,
        description: str,
        color: discord.Color,
        details: dict[str, Any] | None = None,
    ) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only staff can update order status.")
            return
        if order.assigned_staff_id not in {None, interaction.user.id} and not _is_administrator(
            interaction.user
        ):
            await _ephemeral(
                interaction, f"This order is assigned to <@{order.assigned_staff_id}>."
            )
            return
        auto_claimed = order.assigned_staff_id is None
        if auto_claimed:
            await interaction.response.defer(thinking=True)
            order = await self.bot.db.assign_order(order.id, interaction.user.id)
            if isinstance(interaction.user, discord.Member):
                await _lock_ticket_to_claimant(interaction, settings, interaction.user)
        await self.bot.db.set_order_status(
            order.id, status, actor_id=interaction.user.id, details=details
        )
        response_kwargs: dict[str, Any] = {
            "content": f"<@{order.customer_id}>",
            "embed": discord.Embed(title=title, description=description, color=color),
            "allowed_mentions": discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        }
        if interaction.response.is_done():
            await interaction.followup.send(**response_kwargs)
        else:
            await interaction.response.send_message(**response_kwargs)

    @app_commands.command(
        name="order_info", description="Show the stored information for this order"
    )
    @app_commands.guild_only()
    async def order_info(self, interaction: discord.Interaction) -> None:
        order = await _ticket_order(self.bot, interaction)
        settings = await _configured_settings(self.bot, interaction)
        if order is None or settings is None:
            return
        if interaction.user.id != order.customer_id and not _is_staff(
            interaction.user, settings
        ):
            await _ephemeral(interaction, "You do not have access to this order.")
            return
        embed = _order_summary_embed(order)
        embed.title = f"Order #{order.id:06d} Information"
        embed.set_field_at(
            2, name="Status", value=order.status.replace("_", " ").title(), inline=True
        )
        if order.assigned_staff_id:
            embed.add_field(
                name="Assigned Staff", value=f"<@{order.assigned_staff_id}>", inline=False
            )
        await _ephemeral(interaction, embed=embed)

    @app_commands.command(
        name="close", description="Save the transcript and close this order ticket"
    )
    @app_commands.guild_only()
    @app_commands.describe(reason="Reason saved with the transcript")
    async def close(
        self,
        interaction: discord.Interaction,
        reason: str = "Order ticket closed",
    ) -> None:
        await close_ticket(self.bot, interaction, reason=reason)

    @app_commands.command(
        name="earnings",
        description="Admin: view completed-order commission currently owed",
    )
    @app_commands.guild_only()
    async def earnings(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None or not _is_administrator(interaction.user):
            await _ephemeral(
                interaction,
                "Only a server administrator can view owner commission totals.",
            )
            return

        summary = await self.bot.db.get_commission_summary(interaction.guild_id)
        staff_summaries = await self.bot.db.get_commission_summary_by_staff(
            interaction.guild_id
        )
        embed = discord.Embed(
            title="Owner Commission Owed",
            description=(
                f"**{format_cents(summary.owed_cents)}** is currently owed across "
                f"**{summary.owed_order_count}** completed order(s)."
            ),
            color=SUCCESS_COLOR,
        )
        embed.add_field(
            name="Per Completed Order",
            value=format_cents(self.bot.settings.owner_commission_cents),
            inline=True,
        )
        embed.add_field(
            name="Lifetime Recorded",
            value=(
                f"{format_cents(summary.lifetime_cents)} across "
                f"{summary.lifetime_order_count} order(s)"
            ),
            inline=True,
        )
        if staff_summaries:
            embed.add_field(
                name="Who Owes It",
                value="\n".join(
                    f"<@{item.staff_user_id}> — **{format_cents(item.owed_cents)} owed** "
                    f"({item.owed_order_count} order(s)); "
                    f"{format_cents(item.lifetime_cents)} lifetime"
                    for item in staff_summaries
                )[:1024],
                inline=False,
            )
        embed.set_footer(text="Only /done records commission. /close does not.")
        await _ephemeral(interaction, embed=embed)

    @payments.command(name="set", description="Add or update one of your payment methods")
    @app_commands.guild_only()
    @app_commands.describe(
        method="Payment method name",
        instructions="Your payment tag, recipient, link, and any short instructions",
    )
    async def payments_set(
        self,
        interaction: discord.Interaction,
        method: app_commands.Range[str, 2, 60],
        instructions: app_commands.Range[str, 2, 900],
    ) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild_id is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only staff can configure payment methods.")
            return
        existing = await self.bot.db.list_payment_methods(
            interaction.guild_id, interaction.user.id
        )
        is_existing = any(item.name.casefold() == method.casefold() for item in existing)
        if len(existing) >= 10 and not is_existing:
            await _ephemeral(interaction, "You can configure up to 10 payment methods.")
            return
        lowered = instructions.casefold()
        forbidden = ("password", "seed phrase", "private key", "security code")
        if any(term in lowered for term in forbidden):
            await _ephemeral(
                interaction,
                "Do not store passwords, seed phrases, private keys, or security codes. "
                "Enter only a payment tag, recipient, or payment link.",
            )
            return
        await self.bot.db.upsert_payment_method(
            guild_id=interaction.guild_id,
            staff_user_id=interaction.user.id,
            name=method.strip(),
            instructions=instructions.strip(),
        )
        await _ephemeral(
            interaction,
            f"**{method.strip()}** is ready. Customers will see its instructions only "
            "inside private tickets assigned to you.",
        )

    @payments_set.autocomplete("method")
    async def payment_method_autocomplete(
        self, _: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        needle = current.casefold()
        return [
            app_commands.Choice(name=name, value=name)
            for name in KNOWN_PAYMENT_METHODS
            if needle in name.casefold()
        ][:25]

    @payments.command(name="list", description="List your configured payment methods")
    @app_commands.guild_only()
    async def payments_list(self, interaction: discord.Interaction) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild_id is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only staff can view payment methods.")
            return
        methods = await self.bot.db.list_payment_methods(
            interaction.guild_id, interaction.user.id
        )
        if not methods:
            await _ephemeral(interaction, "You have no payment methods configured.")
            return
        embed = discord.Embed(
            title="Your Payment Methods",
            description="\n".join(f"• **{method.name}**" for method in methods),
            color=EMBED_COLOR,
        )
        embed.set_footer(text="Use /payments set to update one.")
        await _ephemeral(interaction, embed=embed)

    @payments.command(name="remove", description="Remove one of your payment methods")
    @app_commands.guild_only()
    async def payments_remove(self, interaction: discord.Interaction, method: str) -> None:
        settings = await _configured_settings(self.bot, interaction)
        if settings is None or interaction.guild_id is None:
            return
        if not _is_staff(interaction.user, settings):
            await _ephemeral(interaction, "Only staff can change payment methods.")
            return
        removed = await self.bot.db.remove_payment_method(
            interaction.guild_id, interaction.user.id, method.strip()
        )
        await _ephemeral(
            interaction,
            (
                f"Removed **{method.strip()}**."
                if removed
                else f"No payment method named **{method.strip()}** was found."
            ),
        )

    @payments_remove.autocomplete("method")
    async def existing_payment_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        methods = await self.bot.db.list_payment_methods(
            interaction.guild_id, interaction.user.id
        )
        needle = current.casefold()
        return [
            app_commands.Choice(name=method.name, value=method.name)
            for method in methods
            if needle in method.name.casefold()
        ][:25]

class DashManualBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings = settings
        self.db = Database(settings.database_path)
        self.closing_channels: set[int] = set()
        self.claim_locks: dict[int, asyncio.Lock] = {}
        self._avatar_sync_attempted = False

    async def _sync_brand_avatar(self) -> None:
        if self._avatar_sync_attempted or self.user is None:
            return
        self._avatar_sync_attempted = True

        try:
            avatar_bytes = await asyncio.to_thread(BRAND_AVATAR_PATH.read_bytes)
        except OSError:
            LOGGER.exception("Could not read the bundled DoorDash Manual avatar")
            return

        try:
            current_avatar_bytes = (
                await self.user.avatar.read() if self.user.avatar is not None else b""
            )
        except discord.HTTPException:
            LOGGER.exception("Could not read the current Discord bot avatar")
            current_avatar_bytes = b""

        current_digest = hashlib.sha256(current_avatar_bytes).digest()
        bundled_digest = hashlib.sha256(avatar_bytes).digest()
        if current_avatar_bytes and current_digest == bundled_digest:
            LOGGER.info("DoorDash Manual avatar is already applied")
            return

        try:
            await self.user.edit(avatar=avatar_bytes)
        except (discord.HTTPException, ValueError):
            LOGGER.exception("Discord rejected the bundled DoorDash Manual avatar update")
            return

        LOGGER.info("Applied the Bob's Burgers DoorDash Manual bot avatar")

    async def setup_hook(self) -> None:
        await self.db.initialize()
        await self.add_cog(DashCommands(self))
        self.add_view(MainPanelView(self))
        self.add_view(TicketControlsView(self))
        self.add_view(InvoiceView(self))
        self.add_view(PaymentSubmittedView(self))
        self.tree.on_error = self.on_tree_error

        if self.settings.dev_guild_id:
            guild = discord.Object(id=self.settings.dev_guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOGGER.info(
                "Synced %s command(s) to development guild %s",
                len(synced),
                self.settings.dev_guild_id,
            )
        else:
            synced = await self.tree.sync()
            LOGGER.info("Synced %s global command(s)", len(synced))

    async def on_ready(self) -> None:
        if self.user:
            await self._sync_brand_avatar()
            LOGGER.info(
                "Ready as %s (%s) in %s guild(s)", self.user, self.user.id, len(self.guilds)
            )
            await self.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name="DoorDash group-cart tickets",
                )
            )

    async def on_tree_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        LOGGER.exception("Application command failed", exc_info=error)
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Try again in {error.retry_after:.1f} seconds."
        elif isinstance(error, app_commands.BotMissingPermissions):
            message = "I am missing required Discord permissions for that action."
        else:
            message = "Something went wrong while handling that command. Please try again."
        with contextlib.suppress(discord.HTTPException):
            await _ephemeral(interaction, message)


def run() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings.transcript_dir.mkdir(parents=True, exist_ok=True)
    bot = DashManualBot(settings)
    bot.run(settings.token, log_handler=None)
