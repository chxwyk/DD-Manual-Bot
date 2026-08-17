from __future__ import annotations

import html
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp

ALLOWED_HOSTS = {"doordash.com", "www.doordash.com", "drd.sh"}
MAX_PAGE_BYTES = 1_000_000


class DoorDashLinkError(ValueError):
    """Raised when a submitted group-cart link is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class DoorDashPreview:
    submitted_url: str
    resolved_url: str
    store_name: str | None
    detected_subtotal_cents: int | None


def _allowed_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    host = hostname.casefold().rstrip(".")
    return host in ALLOWED_HOSTS or host.endswith(".doordash.com")


def normalize_group_order_url(value: str) -> str:
    raw = value.strip().strip("<>")
    parsed = urlparse(raw)
    if parsed.scheme.casefold() != "https" or not _allowed_host(parsed.hostname):
        raise DoorDashLinkError(
            "Use a valid HTTPS DoorDash group-cart link from `drd.sh` or `doordash.com`."
        )
    if parsed.username or parsed.password:
        raise DoorDashLinkError("DoorDash links cannot contain a username or password.")
    cleaned = parsed._replace(fragment="")
    return urlunparse(cleaned)


def _clean_store_name(value: str) -> str | None:
    cleaned = html.unescape(value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -|\n\t")
    cleaned = re.sub(r"\s*[|–—-]\s*DoorDash.*$", "", cleaned, flags=re.IGNORECASE)
    if not cleaned or cleaned.casefold() in {"doordash", "group order"}:
        return None
    return cleaned[:120]


def extract_public_metadata(page: str) -> tuple[str | None, int | None]:
    store_name: str | None = None
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r'"(?:storeName|merchantName)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
        r"<title[^>]*>(.*?)</title>",
    )
    for pattern in patterns:
        match = re.search(pattern, page, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        candidate = match.group(1)
        if "\\" in candidate:
            with suppress(json.JSONDecodeError):
                candidate = json.loads(f'"{candidate}"')
        store_name = _clean_store_name(candidate)
        if store_name:
            break

    subtotal_cents: int | None = None
    cents_match = re.search(
        r'"(?:subtotalCents|subTotalCents)"\s*:\s*(\d{2,7})', page
    )
    if cents_match:
        subtotal_cents = int(cents_match.group(1))
    else:
        money_match = re.search(
            r'"(?:subtotalDisplayString|subtotalFormatted)"\s*:\s*"\$([0-9,]+\.\d{2})"',
            page,
        )
        if money_match:
            subtotal_cents = int(round(float(money_match.group(1).replace(",", "")) * 100))

    return store_name, subtotal_cents


async def inspect_group_order_link(url: str) -> DoorDashPreview:
    submitted = normalize_group_order_url(url)
    current = submitted
    timeout = aiohttp.ClientTimeout(total=8, connect=4, sock_read=5)
    headers = {"User-Agent": "Bobs-DoorDash-Manual/1.0 (+Discord ticket helper)"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for _ in range(6):
            current = normalize_group_order_url(current)
            async with session.get(current, allow_redirects=False) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise DoorDashLinkError("DoorDash returned an empty redirect.")
                    current = urljoin(current, location)
                    continue
                if response.status >= 400:
                    raise DoorDashLinkError(
                        f"DoorDash returned HTTP {response.status}; staff can still open the link."
                    )
                raw = await response.content.read(MAX_PAGE_BYTES + 1)
                if len(raw) > MAX_PAGE_BYTES:
                    raise DoorDashLinkError("The public DoorDash page was too large to inspect.")
                page = raw.decode(response.charset or "utf-8", errors="replace")
                store_name, subtotal_cents = extract_public_metadata(page)
                return DoorDashPreview(
                    submitted_url=submitted,
                    resolved_url=current,
                    store_name=store_name,
                    detected_subtotal_cents=subtotal_cents,
                )

    raise DoorDashLinkError("The DoorDash link redirected too many times.")
