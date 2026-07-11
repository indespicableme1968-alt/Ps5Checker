#!/usr/bin/env python3
"""
PS5 stock checker for Indian retailers.

Designed for scheduled execution through GitHub Actions. It uses a real
headless browser so JavaScript-rendered product pages can be checked without
requiring a laptop or phone to remain switched on.

The checker:
- applies the configured delivery pincode where possible
- detects active Buy Now / Add to Cart controls and structured availability
- sends Telegram alerts only when stock appears or a new search result appears
- keeps a small state.json file to prevent duplicate notifications
- treats CAPTCHA/access-denied pages as UNKNOWN, never as stock
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"
STATE_FILE = BASE_DIR / "state.json"

DEFAULT_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "45000"))
PINCODE = os.getenv("DELIVERY_PINCODE", "575006")
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
NOTIFY_OUT_OF_STOCK = os.getenv("NOTIFY_OUT_OF_STOCK", "false").lower() == "true"
SEND_TEST_NOTIFICATION = os.getenv("SEND_TEST_NOTIFICATION", "false").lower() == "true"

POSITIVE_BUTTON_PATTERNS = (
    "add to cart",
    "add to basket",
    "buy now",
    "buy it now",
    "proceed to buy",
    "place order",
)

NEGATIVE_BUTTON_PATTERNS = (
    "notify me",
    "sold out",
    "out of stock",
    "currently unavailable",
    "coming soon",
    "pre-order closed",
)

BLOCKED_PAGE_PATTERNS = (
    "captcha",
    "enter the characters you see below",
    "access denied",
    "automated access",
    "verify you are human",
    "unusual traffic",
    "temporarily blocked",
    "request unsuccessful",
)

OUT_OF_STOCK_TEXT_PATTERNS = (
    "currently unavailable",
    "temporarily out of stock",
    "out of stock",
    "sold out",
    "notify me when available",
    "notify me",
    "coming soon",
)

IN_STOCK_TEXT_PATTERNS = (
    "in stock",
    "standard delivery available",
    "available for delivery",
    "delivery available",
)

PRICE_PATTERN = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", re.I)


class StockStatus(str, Enum):
    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    UNKNOWN = "UNKNOWN"


@dataclass
class Match:
    title: str
    url: str
    price: float | None = None


@dataclass
class CheckResult:
    key: str
    retailer: str
    name: str
    status: StockStatus
    url: str
    detail: str
    price: float | None = None
    confidence: str = "low"
    matches: list[Match] = field(default_factory=list)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Could not read {path.name}: {exc}", file=sys.stderr)
        return default


def save_json_if_changed(path: Path, data: Any) -> bool:
    rendered = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    if old == rendered:
        return False
    path.write_text(rendered, encoding="utf-8")
    return True


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_price(text: str, minimum: float = 20_000, maximum: float = 100_000) -> float | None:
    prices: list[float] = []
    for match in PRICE_PATTERN.finditer(text or ""):
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if minimum <= value <= maximum:
            prices.append(value)
    return min(prices) if prices else None


def format_price(price: float | None) -> str:
    if price is None:
        return "Price not detected"
    return f"₹{price:,.0f}"


def domain_name(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def within_price_range(price: float | None, product: dict[str, Any]) -> bool:
    if price is None:
        return True
    minimum = float(product.get("min_price", 20_000))
    maximum = float(product.get("max_price", 100_000))
    return minimum <= price <= maximum


async def dismiss_common_popups(page: Page) -> None:
    selectors = [
        "button[aria-label='Close']",
        "button[aria-label='close']",
        "button:has-text('No thanks')",
        "button:has-text('Not now')",
        "button:has-text('Skip')",
        "button:has-text('Maybe later')",
        "button:has-text('✕')",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1000)
                await page.wait_for_timeout(300)
        except Exception:
            pass


async def set_amazon_pincode(page: Page, pincode: str) -> bool:
    try:
        trigger = page.locator("#nav-global-location-popover-link").first
        if not await trigger.is_visible(timeout=1500):
            return False
        await trigger.click(timeout=3000)
        field = page.locator("#GLUXZipUpdateInput").first
        await field.wait_for(state="visible", timeout=5000)
        await field.fill(pincode)
        submit = page.locator("#GLUXZipUpdate input[type='submit'], #GLUXZipUpdate").first
        await submit.click(timeout=3000)
        await page.wait_for_timeout(2500)
        return True
    except Exception:
        return False


async def set_generic_pincode(page: Page, pincode: str) -> bool:
    selectors = [
        "input[placeholder*='pincode' i]",
        "input[aria-label*='pincode' i]",
        "input[name*='pincode' i]",
        "input[id*='pincode' i]",
        "input[placeholder*='postal' i]",
    ]
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), 5)
        except Exception:
            continue

        for index in range(count):
            field = locator.nth(index)
            try:
                if not await field.is_visible(timeout=500):
                    continue
                await field.fill(pincode, timeout=2000)

                # Prefer a nearby Apply/Check button, otherwise press Enter.
                nearby = field.locator(
                    "xpath=ancestor::*[self::form or self::div][1]"
                ).locator(
                    "button:has-text('Apply'), button:has-text('Check'), "
                    "button:has-text('Update'), button:has-text('Submit')"
                ).first
                if await nearby.count() and await nearby.is_visible(timeout=500):
                    await nearby.click(timeout=2000)
                else:
                    await field.press("Enter")
                await page.wait_for_timeout(2500)
                return True
            except Exception:
                continue
    return False


async def apply_pincode(page: Page, retailer: str, pincode: str) -> bool:
    retailer_lower = retailer.lower()
    if "amazon" in retailer_lower and await set_amazon_pincode(page, pincode):
        return True
    return await set_generic_pincode(page, pincode)


async def get_visible_controls(page: Page) -> list[dict[str, Any]]:
    script = """
    (nodes) => nodes.map((el) => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const visible =
        style.visibility !== 'hidden' &&
        style.display !== 'none' &&
        rect.width > 0 &&
        rect.height > 0;
      const text = (
        el.innerText ||
        el.value ||
        el.getAttribute('aria-label') ||
        el.getAttribute('title') ||
        ''
      ).replace(/\\s+/g, ' ').trim();
      const disabled =
        Boolean(el.disabled) ||
        el.getAttribute('aria-disabled') === 'true' ||
        el.classList.contains('disabled');
      return { text, visible, disabled, y: rect.top + window.scrollY };
    })
    """
    try:
        return await page.locator(
            "button, input[type='submit'], input[type='button'], a[role='button']"
        ).evaluate_all(script)
    except Exception:
        return []


def find_control(
    controls: list[dict[str, Any]],
    patterns: tuple[str, ...],
    require_enabled: bool,
    max_y: float = 1800,
) -> str | None:
    for control in controls:
        text = clean_text(str(control.get("text", ""))).lower()
        if not text or not control.get("visible", False):
            continue
        if float(control.get("y", 0) or 0) > max_y:
            continue
        if require_enabled and control.get("disabled", False):
            continue
        if any(pattern in text for pattern in patterns):
            return clean_text(str(control.get("text", "")))
    return None


def availability_from_json(value: Any) -> tuple[StockStatus | None, float | None]:
    found_status: StockStatus | None = None
    found_price: float | None = None

    def visit(node: Any) -> None:
        nonlocal found_status, found_price
        if isinstance(node, dict):
            availability = node.get("availability")
            if isinstance(availability, str):
                lowered = availability.lower()
                if any(token in lowered for token in ("instock", "limitedavailability", "preorder")):
                    found_status = StockStatus.IN_STOCK
                elif any(token in lowered for token in ("outofstock", "soldout", "discontinued")):
                    if found_status is None:
                        found_status = StockStatus.OUT_OF_STOCK

            for price_key in ("price", "lowPrice", "highPrice"):
                raw_price = node.get(price_key)
                if raw_price is not None and found_price is None:
                    try:
                        candidate = float(str(raw_price).replace(",", ""))
                        if 20_000 <= candidate <= 100_000:
                            found_price = candidate
                    except ValueError:
                        pass

            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found_status, found_price


async def read_json_ld(page: Page) -> tuple[StockStatus | None, float | None]:
    try:
        contents = await page.locator("script[type='application/ld+json']").all_text_contents()
    except Exception:
        return None, None

    best_status: StockStatus | None = None
    best_price: float | None = None
    for content in contents:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            continue
        status, price = availability_from_json(parsed)
        if status == StockStatus.IN_STOCK:
            best_status = status
        elif status == StockStatus.OUT_OF_STOCK and best_status is None:
            best_status = status
        if best_price is None and price is not None:
            best_price = price
    return best_status, best_price


async def extract_meta_price(page: Page) -> float | None:
    selectors = [
        "meta[property='product:price:amount']",
        "meta[property='og:price:amount']",
        "meta[itemprop='price']",
    ]
    for selector in selectors:
        try:
            content = await page.locator(selector).first.get_attribute("content")
            if content:
                candidate = float(content.replace(",", ""))
                if 20_000 <= candidate <= 100_000:
                    return candidate
        except Exception:
            continue
    return None


async def read_body_text(page: Page) -> str:
    try:
        return clean_text(await page.locator("body").inner_text(timeout=5000))
    except Exception:
        return ""


def blocked_reason(body_text: str) -> str | None:
    lowered = body_text.lower()
    for pattern in BLOCKED_PAGE_PATTERNS:
        if pattern in lowered:
            return pattern
    return None


def exactish_line_match(body_text: str, patterns: tuple[str, ...]) -> str | None:
    lines = [clean_text(line).lower() for line in re.split(r"[\r\n]+", body_text)]
    for line in lines:
        if not line or len(line) > 120:
            continue
        for pattern in patterns:
            if pattern == line or line.startswith(pattern) or line.endswith(pattern):
                return clean_text(line)
    return None


async def check_product_page(
    page: Page,
    product: dict[str, Any],
) -> CheckResult:
    key = product["key"]
    retailer = product["retailer"]
    name = product["name"]
    url = product["url"]

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
        await page.wait_for_timeout(int(product.get("settle_ms", 2500)))
        await dismiss_common_popups(page)

        if product.get("apply_pincode", True):
            await apply_pincode(page, retailer, str(product.get("pincode") or PINCODE))
            await dismiss_common_popups(page)

        body_text = await read_body_text(page)
        block = blocked_reason(body_text)
        if block:
            return CheckResult(
                key, retailer, name, StockStatus.UNKNOWN, page.url or url,
                f"Retailer returned a blocked/CAPTCHA page: {block}",
            )

        controls = await get_visible_controls(page)
        positive = find_control(controls, POSITIVE_BUTTON_PATTERNS, require_enabled=True)
        negative = find_control(controls, NEGATIVE_BUTTON_PATTERNS, require_enabled=False)

        json_status, json_price = await read_json_ld(page)
        price = json_price or await extract_meta_price(page)
        if price is None:
            price = parse_price(
                body_text,
                float(product.get("min_price", 20_000)),
                float(product.get("max_price", 100_000)),
            )

        if positive and within_price_range(price, product):
            return CheckResult(
                key, retailer, name, StockStatus.IN_STOCK, page.url or url,
                f'Active purchase control detected: "{positive}"',
                price=price,
                confidence="high",
            )

        # A disabled Buy button plus a visible Notify/Sold Out control is out of stock.
        if negative:
            return CheckResult(
                key, retailer, name, StockStatus.OUT_OF_STOCK, page.url or url,
                f'Unavailable control detected: "{negative}"',
                price=price,
                confidence="high",
            )

        if json_status is not None:
            if json_status == StockStatus.IN_STOCK and not within_price_range(price, product):
                return CheckResult(
                    key, retailer, name, StockStatus.UNKNOWN, page.url or url,
                    f"Structured data said in stock, but detected price was outside the configured range: {format_price(price)}",
                    price=price,
                    confidence="low",
                )
            return CheckResult(
                key, retailer, name, json_status, page.url or url,
                "Availability detected from Product structured data",
                price=price,
                confidence="medium",
            )

        unavailable_text = exactish_line_match(body_text, OUT_OF_STOCK_TEXT_PATTERNS)
        if unavailable_text:
            return CheckResult(
                key, retailer, name, StockStatus.OUT_OF_STOCK, page.url or url,
                f'Unavailable text detected: "{unavailable_text}"',
                price=price,
                confidence="medium",
            )

        available_text = exactish_line_match(body_text, IN_STOCK_TEXT_PATTERNS)
        if available_text and within_price_range(price, product):
            return CheckResult(
                key, retailer, name, StockStatus.IN_STOCK, page.url or url,
                f'Availability text detected: "{available_text}"',
                price=price,
                confidence="medium",
            )

        return CheckResult(
            key, retailer, name, StockStatus.UNKNOWN, page.url or url,
            "No reliable purchase or availability signal was found",
            price=price,
        )

    except PlaywrightTimeoutError:
        return CheckResult(
            key, retailer, name, StockStatus.UNKNOWN, url,
            f"Page did not finish loading within {DEFAULT_TIMEOUT_MS // 1000} seconds",
        )
    except Exception as exc:
        return CheckResult(
            key, retailer, name, StockStatus.UNKNOWN, url,
            f"{type(exc).__name__}: {exc}",
        )


async def extract_search_candidates(page: Page) -> list[dict[str, str]]:
    script = """
    (anchors) => anchors.map((a) => {
      let node = a;
      let context = '';
      for (let i = 0; i < 7 && node; i += 1, node = node.parentElement) {
        const current = (node.innerText || '').replace(/\\s+/g, ' ').trim();
        if (current.length >= 30 && current.length <= 1800) {
          context = current;
          if (current.includes('₹') || /Rs\\.?\\s*\\d/i.test(current)) break;
        }
      }
      return {
        title: (a.innerText || a.getAttribute('title') || '').replace(/\\s+/g, ' ').trim(),
        href: a.href || '',
        context
      };
    })
    """
    try:
        return await page.locator("a[href]").evaluate_all(script)
    except Exception:
        return []


def candidate_matches(candidate: dict[str, str], product: dict[str, Any]) -> bool:
    title = clean_text(candidate.get("title", ""))
    context = clean_text(candidate.get("context", ""))
    combined = f"{title} {context}".lower()
    href = candidate.get("href", "")

    if not href.startswith(("http://", "https://")):
        return False

    include_all = [str(x).lower() for x in product.get("include_all", [])]
    include_any = [str(x).lower() for x in product.get("include_any", [])]
    exclude = [str(x).lower() for x in product.get("exclude", [])]

    if include_all and not all(token in combined for token in include_all):
        return False
    if include_any and not any(token in combined for token in include_any):
        return False
    if any(token in combined for token in exclude):
        return False
    if any(token in combined for token in ("currently unavailable", "out of stock", "sold out")):
        return False

    price = parse_price(
        combined,
        float(product.get("min_price", 20_000)),
        float(product.get("max_price", 100_000)),
    )
    if price is None and product.get("require_price", True):
        return False
    return True


async def check_search_page(page: Page, product: dict[str, Any]) -> CheckResult:
    key = product["key"]
    retailer = product["retailer"]
    name = product["name"]
    url = product["url"]

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT_MS)
        await page.wait_for_timeout(int(product.get("settle_ms", 3500)))
        await dismiss_common_popups(page)

        body_text = await read_body_text(page)
        block = blocked_reason(body_text)
        if block:
            return CheckResult(
                key, retailer, name, StockStatus.UNKNOWN, page.url or url,
                f"Retailer returned a blocked/CAPTCHA page: {block}",
            )

        raw_candidates = await extract_search_candidates(page)
        seen_urls: set[str] = set()
        matches: list[Match] = []

        for candidate in raw_candidates:
            if not candidate_matches(candidate, product):
                continue
            href = candidate["href"].split("#", 1)[0]
            if href in seen_urls:
                continue
            seen_urls.add(href)
            title = clean_text(candidate.get("title") or candidate.get("context") or name)
            if len(title) > 180:
                title = title[:177] + "..."
            combined = f"{candidate.get('title', '')} {candidate.get('context', '')}"
            price = parse_price(
                combined,
                float(product.get("min_price", 20_000)),
                float(product.get("max_price", 100_000)),
            )
            matches.append(Match(title=title or name, url=href, price=price))
            if len(matches) >= int(product.get("max_matches", 5)):
                break

        if matches:
            best = min(matches, key=lambda match: match.price if match.price is not None else 10**9)
            return CheckResult(
                key, retailer, name, StockStatus.IN_STOCK, best.url,
                f"{len(matches)} matching console listing(s) found on the search page",
                price=best.price,
                confidence="medium",
                matches=matches,
            )

        return CheckResult(
            key, retailer, name, StockStatus.OUT_OF_STOCK, page.url or url,
            "No matching console listing in the configured price range was found",
            confidence="medium",
        )
    except PlaywrightTimeoutError:
        return CheckResult(
            key, retailer, name, StockStatus.UNKNOWN, url,
            f"Search page did not finish loading within {DEFAULT_TIMEOUT_MS // 1000} seconds",
        )
    except Exception as exc:
        return CheckResult(
            key, retailer, name, StockStatus.UNKNOWN, url,
            f"{type(exc).__name__}: {exc}",
        )


async def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured as GitHub Actions secrets"
        )

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(endpoint, json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram rejected the message: {data}")


def build_stock_message(result: CheckResult) -> str:
    title = html.escape(result.name)
    retailer = html.escape(result.retailer)
    detail = html.escape(result.detail)
    url = html.escape(result.url, quote=True)

    lines = [
        "🎮 <b>PS5 STOCK FOUND</b>",
        "",
        f"<b>{title}</b>",
        f"Store: <b>{retailer}</b>",
        f"Price: <b>{html.escape(format_price(result.price))}</b>",
        f"Confidence: {html.escape(result.confidence)}",
        f'<a href="{url}">Open product page</a>',
    ]

    if len(result.matches) > 1:
        lines.extend(["", "<b>Other matching listings:</b>"])
        for match in result.matches[:5]:
            match_title = html.escape(match.title)
            match_url = html.escape(match.url, quote=True)
            lines.append(
                f'• <a href="{match_url}">{match_title}</a> — {html.escape(format_price(match.price))}'
            )

    lines.extend(["", f"<i>{detail}</i>", f"Checked: {html.escape(now_iso())}"])
    return "\n".join(lines)


def build_out_of_stock_message(result: CheckResult) -> str:
    return "\n".join(
        [
            "⚪ <b>PS5 is no longer detected in stock</b>",
            "",
            f"<b>{html.escape(result.name)}</b>",
            f"Store: <b>{html.escape(result.retailer)}</b>",
            f"<i>{html.escape(result.detail)}</i>",
        ]
    )


def fingerprint(result: CheckResult) -> str:
    canonical_url = result.url.split("?", 1)[0].rstrip("/")
    return f"{result.status.value}|{canonical_url}"


async def make_context(browser: Browser) -> BrowserContext:
    return await browser.new_context(
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1440, "height": 1000},
        extra_http_headers={
            "Accept-Language": "en-IN,en;q=0.9",
            "DNT": "1",
        },
    )


async def run_checks(products: list[dict[str, Any]]) -> list[CheckResult]:
    results: list[CheckResult] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )
        try:
            for product in products:
                if not product.get("enabled", True):
                    continue

                context = await make_context(browser)
                page = await context.new_page()
                page.set_default_timeout(10_000)

                mode = str(product.get("mode", "product")).lower()
                print(f"\nChecking {product['retailer']} — {product['name']}")
                if mode == "search":
                    result = await check_search_page(page, product)
                else:
                    result = await check_product_page(page, product)

                print(
                    f"{result.status.value}: {result.detail}; "
                    f"{format_price(result.price)}; {result.url}"
                )
                results.append(result)
                await context.close()

                # Stay polite to retailer sites and avoid burst traffic.
                await asyncio.sleep(float(product.get("delay_after_seconds", 1.5)))
        finally:
            await browser.close()

    return results


async def async_main() -> int:
    products_data = load_json(PRODUCTS_FILE, {})
    products = products_data.get("products", []) if isinstance(products_data, dict) else []
    if not products:
        print("No products are enabled in products.json", file=sys.stderr)
        return 2

    if SEND_TEST_NOTIFICATION:
        await send_telegram(
            "✅ <b>PS5 stock checker test successful</b>\n"
            f"Delivery pincode: <b>{html.escape(PINCODE)}</b>\n"
            f"Time: {html.escape(now_iso())}"
        )
        print("Telegram test notification sent.")

    state = load_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}

    results = await run_checks(products)
    alert_errors = 0

    for result in results:
        previous = state.get(result.key, {})
        previous_status = previous.get("status")
        previous_fingerprint = previous.get("last_alerted_fingerprint")
        current_fingerprint = fingerprint(result)

        # UNKNOWN never overwrites a known stock state.
        if result.status == StockStatus.UNKNOWN:
            continue

        should_alert_stock = (
            result.status == StockStatus.IN_STOCK
            and (
                previous_status != StockStatus.IN_STOCK.value
                or previous_fingerprint != current_fingerprint
            )
        )

        if should_alert_stock:
            try:
                await send_telegram(build_stock_message(result))
                print(f"Telegram stock alert sent for {result.key}")
                previous_fingerprint = current_fingerprint
            except Exception as exc:
                alert_errors += 1
                print(f"Telegram alert failed for {result.key}: {exc}", file=sys.stderr)

        if (
            NOTIFY_OUT_OF_STOCK
            and result.status == StockStatus.OUT_OF_STOCK
            and previous_status == StockStatus.IN_STOCK.value
        ):
            try:
                await send_telegram(build_out_of_stock_message(result))
            except Exception as exc:
                alert_errors += 1
                print(f"Out-of-stock alert failed for {result.key}: {exc}", file=sys.stderr)

        new_entry = {
            "status": result.status.value,
            "url": result.url,
        }
        if previous_fingerprint:
            new_entry["last_alerted_fingerprint"] = previous_fingerprint
        state[result.key] = new_entry

    changed = save_json_if_changed(STATE_FILE, state)
    print(f"\nState file changed: {changed}")

    in_stock = sum(result.status == StockStatus.IN_STOCK for result in results)
    out_stock = sum(result.status == StockStatus.OUT_OF_STOCK for result in results)
    unknown = sum(result.status == StockStatus.UNKNOWN for result in results)
    print(f"Summary: {in_stock} in stock, {out_stock} out of stock, {unknown} unknown")

    return 1 if alert_errors else 0


def main() -> None:
    try:
        raise SystemExit(asyncio.run(async_main()))
    except KeyboardInterrupt:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
