from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .models import Order
from .pricing import format_cents


@dataclass(frozen=True, slots=True)
class TranscriptAttachment:
    filename: str
    url: str


@dataclass(frozen=True, slots=True)
class TranscriptMessage:
    author_name: str
    author_id: int
    avatar_url: str | None
    created_at: datetime
    content: str
    attachments: list[TranscriptAttachment] = field(default_factory=list)


def render_transcript_html(
    *,
    guild_name: str,
    channel_name: str,
    order: Order,
    messages: list[TranscriptMessage],
) -> str:
    rendered_messages: list[str] = []
    for message in messages:
        safe_name = html.escape(message.author_name)
        safe_time = html.escape(message.created_at.isoformat())
        safe_content = html.escape(message.content).replace("\n", "<br>")
        avatar = (
            f'<img class="avatar" src="{html.escape(message.avatar_url, quote=True)}" alt="">'
            if message.avatar_url
            else '<div class="avatar fallback"></div>'
        )
        attachments = "".join(
            (
                '<a class="attachment" target="_blank" rel="noreferrer" '
                f'href="{html.escape(item.url, quote=True)}">'
                f"📎 {html.escape(item.filename)}</a>"
            )
            for item in message.attachments
        )
        rendered_messages.append(
            f"""
            <article class="message">
              {avatar}
              <div class="message-body">
                <div><strong>{safe_name}</strong>
                  <span class="meta">User {message.author_id} • {safe_time}</span>
                </div>
                <div class="content">{safe_content or "<em>No text content</em>"}</div>
                <div>{attachments}</div>
              </div>
            </article>
            """
        )

    safe_guild = html.escape(guild_name)
    safe_channel = html.escape(channel_name)
    safe_restaurant = html.escape(order.restaurant_name)
    safe_status = html.escape(order.status.replace("_", " ").title())
    safe_contact_name = html.escape(order.contact_name)
    safe_email = html.escape(order.email or "Not provided (legacy order)")
    safe_phone = html.escape(order.phone)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Order #{order.id:06d} Transcript</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; background: #111318; color: #e7e9ee; }}
    header {{ position: sticky; top: 0; padding: 20px; background: #1b1e25;
      border-bottom: 1px solid #313640; z-index: 1; }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    .summary {{ color: #aeb4c0; font-size: 14px; }}
    .contact {{ margin-top: 6px; color: #d9dce3; font-size: 14px; }}
    main {{ max-width: 900px; margin: auto; padding: 14px 20px 40px; }}
    .message {{ display: flex; gap: 12px; padding: 12px; border-radius: 10px; }}
    .message:hover {{ background: #191c23; }}
    .avatar {{ width: 42px; height: 42px; border-radius: 50%; object-fit: cover; }}
    .fallback {{ flex: 0 0 42px; background: #5865f2; }}
    .message-body {{ min-width: 0; }}
    .meta {{ color: #89909c; font-size: 12px; margin-left: 7px; }}
    .content {{ margin: 4px 0; overflow-wrap: anywhere; line-height: 1.45; }}
    .attachment {{ display: inline-block; color: #8ea1ff; margin: 4px 8px 0 0; }}
  </style>
</head>
<body>
  <header>
    <h1>{safe_guild} / #{safe_channel}</h1>
    <div class="summary">
      Order #{order.id:06d} • {safe_restaurant} • {safe_status} •
      {html.escape(order.fulfillment.title())} •
      Cart {format_cents(order.submitted_total_cents)} •
      Customer price {format_cents(order.customer_price_cents)}
    </div>
    <div class="contact">
      Customer: {safe_contact_name} • Email: {safe_email} • Phone: {safe_phone}
    </div>
  </header>
  <main>
    {"".join(rendered_messages)}
  </main>
</body>
</html>
"""


def save_transcript(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
