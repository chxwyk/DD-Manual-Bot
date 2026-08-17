# Bob's Burgers DoorDash Manual

A Railway-ready Discord bot for private DoorDash group-cart tickets.

## Customer flow

1. Customer presses **Place Order**.
2. Customer submits an HTTPS DoorDash group-cart link, a **$30+ final total after taxes and fees**, delivery address, optional Dasher note, and optionally types `PICKUP`.
3. The bot safely checks public DoorDash metadata when available, creates a private ticket, pings the configured Manual Chef role, and posts a clickable group-cart link.
4. **PICKUP** or **DELIVERY** appears in large text at the top of the chef summary.
5. The chef claims the ticket, sends payment, and posts order updates.

DoorDash may hide live cart data behind a login or JavaScript. That never blocks ticket creation: the customer-entered final total remains visible and the chef gets a one-click cart link. Any publicly detected subtotal is shown only as a pre-tax reference. The bot never asks for or stores DoorDash credentials.

## Main commands

- `/setup` — open a private dropdown/button menu for the existing storefront channel, ticket category, Manual Chef role, transcript channel, and customer ping role; saved choices preload the next time `/setup` is opened
- `/panel` — refresh or repost the storefront
- `/manual open` / `/manual close` — control DoorDash Manual orders without conflicting with the other bots; only opening pings customers
- `/claim` — claim and lock a ticket so other chefs become read-only
- `/force_claim staff:@Chef` — administrator reassignment
- `/pay [final_total]` — verify the final total after taxes and fees and send the invoice
- `/paid` — confirm customer payment
- `/ordered [details]` — announce that the DoorDash order was placed
- `/done` — record **$1.75 owed by the chef who claimed the ticket**, then keep the ticket open for 30 minutes before transcript/archive
- `/close` — cancel/archive without recording commission
- `/earnings` — administrator view of total owed plus a per-chef breakdown
- `/payments set`, `/payments list`, `/payments remove` — chef payment methods

## Railway variables

```env
DISCORD_TOKEN=your_new_bot_token
DEV_GUILD_ID=your_server_id
CUSTOMER_PING_ROLE_ID=optional_legacy_fallback_role_id
DATABASE_PATH=/app/data/doordash_manual.db
TRANSCRIPT_DIR=/app/data/transcripts
OWNER_COMMISSION_CENTS=175
LOG_LEVEL=INFO
```

The customer role is normally selected and saved through `/setup`; the Railway role variable is only a fallback for an older database. Attach a Railway volume at `/app/data` so settings, tickets, commission records, and transcripts survive redeploys.

## Required Discord permissions

- View Channels
- Send Messages
- Embed Links
- Attach Files
- Read Message History
- Manage Channels
- Use Application Commands
- Mention Everyone / Roles only if the customer notification role is not mentionable

The bot role must be above the Manual Chef role so claim locks and force reassignments work.

## GitHub and deployment

Upload the ZIP contents to a new GitHub repository. Do not upload a real `.env` or bot token. Connect the repository to Railway, add the variables above, mount `/app/data`, and wait for the log line showing the bot is ready. Then run `/setup`, choose the existing channels and roles from the menus, and press **Save Setup**. The included animated $30 storefront banner is applied automatically.
