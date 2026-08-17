# Security and privacy

This bot stores order details, delivery/pickup locations, customer names, email
addresses, phone numbers, payment instructions, Discord IDs, and ticket
transcripts. Keep ticket and transcript channels private, grant access only to
trusted staff, and use customer contact details only to fulfill that order.

## Never store these

- Discord, restaurant, email, banking, or payment-account passwords
- Full card numbers, CVVs, PINs, login codes, or recovery codes
- Cryptocurrency seed phrases or private keys

`/payments set` is designed only for a payment tag, recipient email/phone number,
or an HTTPS payment link. It does not connect to or sign into a financial account.

## Operational requirements

- Use a dedicated Discord bot token and never commit `.env`.
- Regenerate the token immediately if it is exposed.
- Give the bot only the permissions listed in `README.md`.
- Keep the transcript channel hidden from customers and unrelated members.
- Attach a persistent Railway volume at `/app/data`.
- Use the service only for offers and DoorDash orders you are authorized to
  fulfill. The bot does not bypass restaurant systems or grant authorization.
