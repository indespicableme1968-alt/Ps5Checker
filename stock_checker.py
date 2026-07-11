#!/usr/bin/env python3
"""Reliable PS5 stock checker for Indian retailers.

This version is designed for GitHub Actions and improves the original checker by:
- continuing to inspect partially loaded pages after navigation timeouts
- preserving line breaks in page text so stock phrases can be detected
- using retailer-specific selectors before generic detection
- checking Flipkart result links instead of treating a search result as stock
- blocking only images/fonts/media to reduce page load time
- retrying uncertain product pages once
- avoiding false alerts when a site blocks automation or changes its page
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
from urllib.parse import urlparse

import httpx
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"
STATE_FILE = BASE_DIR / "state.json"

PAGE_TIMEOUT_MS = int(os.getenv("PAGE_TIMEOUT_MS", "30000"))
PINCODE = os.getenv("DELIVERY_PINCODE", "575006").strip()
HEADLESS = os.getenv("HEADLESS", "true").lower() != "false"
NOTIFY_OUT_OF_STOCK = os.getenv("NOTIFY_OUT_OF_STOCK", "false").lower() == "true"
SEND_TEST_NOTIFICATION = os.getenv("SEND_TEST_NOTIFICATION", "false").lower() == "true"
MAX_SEARCH_RESULTS_TO_VERIFY = int(os.getenv("MAX_SEARCH_RESULTS_TO_VERIFY", "3"))

PRICE_PATTERN = re.compile(r"(?:₹|rs\.?|inr)\s*([0-9][0-9,]*(?:\.\d{1,2})?)", re.I)

BLOCKED_PATTERNS = (
    "captcha",
    "enter the characters you see below",
    "verify you are human",
    "unusual traffic",
    "automated access",
    "access denied",
    "temporarily blocked",
    "request unsuccessful",
    "your request has been blocked",
)

REMOVED_PATTERNS = (
    "page not found",
    "product not found",
    "we couldn't find the page",
    "we could not find the page",
    "this page does not exist",
    "the page you requested could not be found",
)

POSITIVE_TEXT_PATTERNS = (
    "add to cart",
    "add to basket",
    "buy now",
    "buy it now",
    "proceed to buy",
)

NEGATIVE_TEXT_PATTERNS = (
    "currently unavailable",
    "temporarily out of stock",
    "out of stock",
    "sold out",
    "notify me when available",
    "notify me",
    "coming soon",
    "pre-order closed",
)

IN_STOCK_TEXT_PATTERNS = (
    "in stock",
    "available for delivery",
    "delivery available",
    "standard delivery available",
    "available to order",
)

# Retailer-specific selectors are checked before generic text/button detection.
RETAILER_SELECTORS: dict[str, dict[str, list[str]]] = {
    "amazon": {
        "positive": [
            "#add-to-cart-button",
            "#buy-now-button",
            "input[name='submit.add-to-cart']",
            "input[name='submit.buy-now']",
        ],
        "negative": [
            "#outOfStock",
            "#availability .a-color-state",
            "#availability .a-color-price",
            "#availability span",
        ],
        "price": [
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_feature_div .a-price .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            ".a-price .a-offscreen",
        ],
    },
    "flipkart": {
        "positive": [
            "button:has-text('Add to cart')",
            "button:has-text('ADD TO CART')",
            "button:has-text('Buy Now')",
            "button:has-text('BUY NOW')",
        ],
        "negative": [
            "button:has-text('Notify Me')",
            "button:has-text('NOTIFY ME')",
            "text=Currently unavailable",
            "text=Sold Out",
        ],
        "price": [
            "div:has-text('₹')",
        ],
    },
    "croma": {
        "positive": [
            "button:has-text('Add to cart')",
            "button:has-text('Add to Cart')",
            "button:has-text('Buy Now')",
        ],
        "negative": [
            "button:has-text('Notify Me')",
            "text=Out of Stock",
            "text=Currently unavailable",
        ],
        "price": [
            "[class*='amount']",
            "[class*='price']",
        ],
    },
    "reliance": {
        "positive": [
            "button:has-text('Add to Cart')",
            "button:has-text('Buy Now')",
            "button:has-text('Add to cart')",
        ],
        "negative": [
            "button:has-text('Notify Me')",
            "text=Out of Stock",
            "text=Currently unavailable",
        ],
        "price": [
            "[class*='price']",
            "[class*='Price']",
        ],
    },
    "shopatsc": {
        "positive": [
            "button:has-text('Add to cart')",
            "button:has-text('Buy now')",
            "button:has-text('Buy Now')",
        ],
        "negative": [
            "button:has-text('Sold out')",
            "button:has-text('Notify me')",
            "text=Out of stock",
        ],
        "price": ["[class*='price']"],
    },
    "vijay": {
        "positive": [
            "button:has-text('Add to Cart')",
            "button:has-text('Buy Now')",
        ],
        "negative": [
            "button:has-text('Notify Me')",
            "text=Out of Stock",
            "text=Sold Out",
        ],
        "price": ["[class*='price']", "[class*='Price']"],
    },
    "games": {
        "positive": [
            "button:has-text('Add to cart')",
            "button:has-text('Add To Cart')",
            "button:has-text('Buy Now')",
        ],
        "negative": [
            "button:has-text('Notify Me')",
            "text=Out of Stock",
            "text=Sold Out",
        ],
        "price": ["[class*='price']", "[class*='Price']"],
    },
}


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
    except (OSError, json.JSONDecodeError) as exc:
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


def retailer_key(retailer: str, url: str = "") -> str:
    value = f"{retailer} {url}".lower()
    if "amazon" in value:
        return "amazon"
    if "flipkart" in value:
        return "flipkart"
    if "croma" in value:
        return "croma"
    if "reliance" in value:
        return "reliance"
    if "shopatsc" in value or "sony" in value:
        return "shopatsc"
    if "vijay" in value:
        return "vijay"
    if "games" in value:
        return "games"
    return "generic"


def parse_price(text: str, minimum: float = 20_000, maximum: float = 100_000) -> float | None:
    values: list[float] = []
    for match in PRICE_PATTERN.finditer(text or ""):
        try:
            value = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        if minimum <= value <= maximum:
            values.append(value)
    return min(values) if values else None


def format_price(price: float | None) -> str:
    return "Price not detected" if price is None else f"₹{price:,.0f}"


def within_price_range(price: float | None, product: dict[str, Any]) -> bool:
    if price is None:
        return True
    return float(product.get("min_price", 20_000)) <= price <= float(product.get("max_price", 100_000))


def contains_pattern(text: str, patterns: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for pattern in patterns:
        if pattern in lowered:
            return pattern
    return None


def line_pattern(text: str, patterns: tuple[str, ...]) -> str | None:
    """Find short, stock-related lines while preserving the page's line breaks."""
    for raw_line in re.split(r"[\r\n]+", text):
        line = clean_text(raw_line)
        lowered = line.lower()
        if not line or len(line) > 180:
            continue
        for pattern in patterns:
            if pattern in lowered:
                return line
    return None


async def dismiss_popups(page: Page) -> None:
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
            if await locator.is_visible(timeout=350):
                await locator.click(timeout=1000)
                await page.wait_for_timeout(200)
        except Exception:
            pass


async def body_text(page: Page) -> str:
    try:
        # Do not call clean_text here. Preserving new lines is important.
        return await page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


async def best_effort_goto(page: Page, url: str, timeout_ms: int = PAGE_TIMEOUT_MS) -> tuple[bool, str]:
    """Navigate without discarding a partially loaded but usable page."""
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        status = response.status if response else 0
        return True, f"HTTP {status}" if status else "loaded"
    except PlaywrightTimeoutError:
        text = await body_text(page)
        if len(clean_text(text)) >= 120:
            return True, "navigation timed out, but partial page content was available"

    # One lighter retry. `commit` succeeds as soon as the server responds.
    try:
        response = await page.goto(url, wait_until="commit", timeout=max(12_000, timeout_ms // 2))
        await page.wait_for_timeout(2500)
        status = response.status if response else 0
        text = await body_text(page)
        if len(clean_text(text)) >= 120:
            return True, f"retry loaded HTTP {status}" if status else "retry loaded"
    except Exception:
        pass

    return False, f"page did not provide usable content within {timeout_ms // 1000} seconds"


async def set_amazon_pincode(page: Page, pincode: str) -> bool:
    try:
        trigger = page.locator("#nav-global-location-popover-link").first
        if not await trigger.is_visible(timeout=1200):
            return False
        await trigger.click(timeout=2500)
        field = page.locator("#GLUXZipUpdateInput").first
        await field.wait_for(state="visible", timeout=4000)
        await field.fill(pincode)
        await page.locator("#GLUXZipUpdate input[type='submit'], #GLUXZipUpdate").first.click(timeout=2500)
        await page.wait_for_timeout(1800)
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
            count = min(await locator.count(), 4)
        except Exception:
            continue
        for index in range(count):
            field = locator.nth(index)
            try:
                if not await field.is_visible(timeout=350):
                    continue
                await field.fill(pincode, timeout=1500)
                container = field.locator("xpath=ancestor::*[self::form or self::div][1]")
                button = container.locator(
                    "button:has-text('Apply'), button:has-text('Check'), "
                    "button:has-text('Update'), button:has-text('Submit')"
                ).first
                if await button.count() and await button.is_visible(timeout=350):
                    await button.click(timeout=1500)
                else:
                    await field.press("Enter")
                await page.wait_for_timeout(1500)
                return True
            except Exception:
                continue
    return False


async def apply_pincode(page: Page, product: dict[str, Any]) -> bool:
    if not product.get("apply_pincode", True) or not PINCODE:
        return False
    key = retailer_key(product.get("retailer", ""), product.get("url", ""))
    if key == "amazon":
        return await set_amazon_pincode(page, str(product.get("pincode") or PINCODE))
    return await set_generic_pincode(page, str(product.get("pincode") or PINCODE))


async def locator_text(locator: Locator) -> str:
    try:
        return clean_text(await locator.inner_text(timeout=1200))
    except Exception:
        try:
            return clean_text(await locator.get_attribute("value") or "")
        except Exception:
            return ""


async def first_visible_selector(
    page: Page,
    selectors: list[str],
    require_enabled: bool = False,
    required_patterns: tuple[str, ...] | None = None,
) -> tuple[str, str] | None:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if not await locator.is_visible(timeout=500):
                continue
            if require_enabled and await locator.is_disabled(timeout=300):
                continue
            text = await locator_text(locator)
            if required_patterns and not any(pattern in text.lower() for pattern in required_patterns):
                continue
            return selector, text
        except Exception:
            continue
    return None


async def generic_visible_control(page: Page, patterns: tuple[str, ...], require_enabled: bool) -> str | None:
    try:
        controls = page.locator("button, input[type='submit'], input[type='button'], a[role='button']")
        count = min(await controls.count(), 120)
        for index in range(count):
            locator = controls.nth(index)
            try:
                if not await locator.is_visible(timeout=120):
                    continue
                if require_enabled and await locator.is_disabled(timeout=120):
                    continue
                text = (await locator_text(locator)).lower()
                if text and any(pattern in text for pattern in patterns):
                    return text
            except Exception:
                continue
    except Exception:
        pass
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
                    except ValueError:
                        continue
                    if 20_000 <= candidate <= 100_000:
                        found_price = candidate
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found_status, found_price


async def read_json_ld(page: Page) -> tuple[StockStatus | None, float | None]:
    try:
        scripts = await page.locator("script[type='application/ld+json']").all_text_contents()
    except Exception:
        return None, None

    best_status: StockStatus | None = None
    best_price: float | None = None
    for script in scripts:
        try:
            parsed = json.loads(script)
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


async def extract_price(page: Page, text: str, product: dict[str, Any]) -> float | None:
    key = retailer_key(product.get("retailer", ""), page.url)
    selectors = RETAILER_SELECTORS.get(key, {}).get("price", [])
    minimum = float(product.get("min_price", 20_000))
    maximum = float(product.get("max_price", 100_000))

    for selector in selectors:
        try:
            locators = page.locator(selector)
            count = min(await locators.count(), 20)
            for index in range(count):
                value = await locator_text(locators.nth(index))
                price = parse_price(value, minimum, maximum)
                if price is not None:
                    return price
        except Exception:
            continue

    for selector in (
        "meta[property='product:price:amount']",
        "meta[property='og:price:amount']",
        "meta[itemprop='price']",
    ):
        try:
            content = await page.locator(selector).first.get_attribute("content")
            if content:
                candidate = float(content.replace(",", ""))
                if minimum <= candidate <= maximum:
                    return candidate
        except Exception:
            continue

    return parse_price(text, minimum, maximum)


async def inspect_loaded_product_page(page: Page, product: dict[str, Any], navigation_detail: str) -> CheckResult:
    key = product["key"]
    retailer = product["retailer"]
    name = product["name"]
    url = page.url or product["url"]

    await dismiss_popups(page)
    await apply_pincode(page, product)
    await dismiss_popups(page)
    await page.wait_for_timeout(int(product.get("settle_ms", 1800)))

    text = await body_text(page)
    compact = clean_text(text)
    title = clean_text(await page.title()) if compact else ""

    block = contains_pattern(f"{title}\n{text}", BLOCKED_PATTERNS)
    if block:
        return CheckResult(key, retailer, name, StockStatus.UNKNOWN, url, f"Retailer blocked the cloud browser: {block}")

    removed = contains_pattern(f"{title}\n{text}", REMOVED_PATTERNS)
    if removed:
        return CheckResult(key, retailer, name, StockStatus.UNKNOWN, url, f"Product URL appears removed or invalid: {removed}")

    rkey = retailer_key(retailer, url)
    selectors = RETAILER_SELECTORS.get(rkey, {})

    positive = await first_visible_selector(page, selectors.get("positive", []), require_enabled=True)
    negative = await first_visible_selector(
        page,
        selectors.get("negative", []),
        require_enabled=False,
        required_patterns=NEGATIVE_TEXT_PATTERNS,
    )

    if positive is None:
        generic_positive = await generic_visible_control(page, POSITIVE_TEXT_PATTERNS, require_enabled=True)
        if generic_positive:
            positive = ("generic purchase control", generic_positive)

    if negative is None:
        generic_negative = await generic_visible_control(page, NEGATIVE_TEXT_PATTERNS, require_enabled=False)
        if generic_negative:
            negative = ("generic unavailable control", generic_negative)

    structured_status, structured_price = await read_json_ld(page)
    price = structured_price or await extract_price(page, text, product)

    # Strongest signal: an enabled purchase control on the product page.
    if positive and within_price_range(price, product):
        label = positive[1] or positive[0]
        return CheckResult(
            key, retailer, name, StockStatus.IN_STOCK, url,
            f'Active purchase control detected: "{label}" ({navigation_detail})',
            price=price, confidence="high",
        )

    # Explicit unavailable controls override stale structured data.
    if negative:
        label = negative[1] or negative[0]
        return CheckResult(
            key, retailer, name, StockStatus.OUT_OF_STOCK, url,
            f'Unavailable control detected: "{label}" ({navigation_detail})',
            price=price, confidence="high",
        )

    # Structured data is useful, but only after checking visible controls.
    if structured_status is not None:
        if structured_status == StockStatus.IN_STOCK and not within_price_range(price, product):
            return CheckResult(
                key, retailer, name, StockStatus.UNKNOWN, url,
                f"Structured data reported stock, but the detected price was outside the configured range: {format_price(price)}",
                price=price,
            )
        return CheckResult(
            key, retailer, name, structured_status, url,
            f"Availability detected from product structured data ({navigation_detail})",
            price=price, confidence="medium",
        )

    unavailable_line = line_pattern(text, NEGATIVE_TEXT_PATTERNS)
    if unavailable_line:
        return CheckResult(
            key, retailer, name, StockStatus.OUT_OF_STOCK, url,
            f'Unavailable text detected: "{unavailable_line}" ({navigation_detail})',
            price=price, confidence="medium",
        )

    available_line = line_pattern(text, IN_STOCK_TEXT_PATTERNS)
    if available_line and within_price_range(price, product):
        return CheckResult(
            key, retailer, name, StockStatus.IN_STOCK, url,
            f'Availability text detected: "{available_line}" ({navigation_detail})',
            price=price, confidence="medium",
        )

    snippet = compact[:180]
    return CheckResult(
        key, retailer, name, StockStatus.UNKNOWN, url,
        f"No reliable stock signal was found ({navigation_detail}). Page: {title or 'untitled'}. Snippet: {snippet}",
        price=price,
    )


async def check_product_page(page: Page, product: dict[str, Any]) -> CheckResult:
    loaded, navigation_detail = await best_effort_goto(page, product["url"])
    if not loaded:
        return CheckResult(
            product["key"], product["retailer"], product["name"],
            StockStatus.UNKNOWN, product["url"], navigation_detail,
        )

    result = await inspect_loaded_product_page(page, product, navigation_detail)
    if result.status != StockStatus.UNKNOWN:
        return result

    # Retry only uncertain pages, not explicit blocks/removals.
    if "blocked" in result.detail.lower() or "removed" in result.detail.lower():
        return result

    try:
        await page.reload(wait_until="domcontentloaded", timeout=max(15_000, PAGE_TIMEOUT_MS // 2))
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(1500)
    retry_result = await inspect_loaded_product_page(page, product, "one retry")
    return retry_result


async def extract_search_candidates(page: Page, product: dict[str, Any]) -> list[Match]:
    script = """
    (anchors) => anchors.map((a) => {
      let node = a;
      let context = '';
      for (let i = 0; i < 7 && node; i += 1, node = node.parentElement) {
        const current = (node.innerText || '').replace(/\\s+/g, ' ').trim();
        if (current.length >= 25 && current.length <= 1600) {
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
        candidates = await page.locator("a[href]").evaluate_all(script)
    except Exception:
        return []

    include_all = [str(value).lower() for value in product.get("include_all", [])]
    include_any = [str(value).lower() for value in product.get("include_any", [])]
    exclude = [str(value).lower() for value in product.get("exclude", [])]
    minimum = float(product.get("min_price", 20_000))
    maximum = float(product.get("max_price", 100_000))

    results: list[Match] = []
    seen: set[str] = set()
    for candidate in candidates:
        title = clean_text(str(candidate.get("title", "")))
        context = clean_text(str(candidate.get("context", "")))
        combined = f"{title} {context}".lower()
        href = str(candidate.get("href", "")).split("#", 1)[0]

        if not href.startswith(("http://", "https://")) or href in seen:
            continue
        if include_all and not all(token in combined for token in include_all):
            continue
        if include_any and not any(token in combined for token in include_any):
            continue
        if any(token in combined for token in exclude):
            continue

        price = parse_price(combined, minimum, maximum)
        if product.get("require_price", True) and price is None:
            continue

        seen.add(href)
        results.append(Match(title=title or product["name"], url=href, price=price))
        if len(results) >= max(5, MAX_SEARCH_RESULTS_TO_VERIFY):
            break
    return results


async def check_search_page(browser: Browser, page: Page, product: dict[str, Any]) -> CheckResult:
    loaded, navigation_detail = await best_effort_goto(page, product["url"], timeout_ms=25_000)
    if not loaded:
        return CheckResult(
            product["key"], product["retailer"], product["name"],
            StockStatus.UNKNOWN, product["url"], navigation_detail,
        )

    await dismiss_popups(page)
    await page.wait_for_timeout(int(product.get("settle_ms", 2500)))
    text = await body_text(page)
    block = contains_pattern(text, BLOCKED_PATTERNS)
    if block:
        return CheckResult(
            product["key"], product["retailer"], product["name"],
            StockStatus.UNKNOWN, page.url or product["url"],
            f"Search page blocked the cloud browser: {block}",
        )

    candidates = await extract_search_candidates(page, product)
    if not candidates:
        snippet = clean_text(text)[:180]
        return CheckResult(
            product["key"], product["retailer"], product["name"],
            StockStatus.UNKNOWN, page.url or product["url"],
            f"No verifiable console result link was found ({navigation_detail}). Snippet: {snippet}",
        )

    verified_out = 0
    unknown_details: list[str] = []
    for index, match in enumerate(candidates[:MAX_SEARCH_RESULTS_TO_VERIFY], start=1):
        candidate_product = dict(product)
        candidate_product["url"] = match.url
        candidate_product["name"] = match.title
        candidate_product["mode"] = "product"
        candidate_product["apply_pincode"] = product.get("apply_pincode", False)

        context = await make_context(browser)
        candidate_page = await context.new_page()
        candidate_page.set_default_timeout(8000)
        try:
            result = await check_product_page(candidate_page, candidate_product)
        finally:
            await context.close()

        if result.status == StockStatus.IN_STOCK:
            result.key = product["key"]
            result.name = product["name"]
            result.matches = [match]
            result.detail = f"Verified result {index}: {result.detail}"
            return result
        if result.status == StockStatus.OUT_OF_STOCK:
            verified_out += 1
        else:
            unknown_details.append(result.detail[:120])

    if verified_out == min(len(candidates), MAX_SEARCH_RESULTS_TO_VERIFY):
        return CheckResult(
            product["key"], product["retailer"], product["name"],
            StockStatus.OUT_OF_STOCK, page.url or product["url"],
            f"Checked {verified_out} matching console result(s); none had an active purchase signal",
            confidence="medium",
        )

    return CheckResult(
        product["key"], product["retailer"], product["name"],
        StockStatus.UNKNOWN, page.url or product["url"],
        "Matching console results were found, but they could not all be verified: " + " | ".join(unknown_details[:2]),
    )


async def send_telegram(message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be configured as GitHub Actions secrets")

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
    lines = [
        "🎮 <b>PS5 STOCK FOUND</b>",
        "",
        f"<b>{html.escape(result.name)}</b>",
        f"Store: <b>{html.escape(result.retailer)}</b>",
        f"Price: <b>{html.escape(format_price(result.price))}</b>",
        f"Confidence: {html.escape(result.confidence)}",
        f'<a href="{html.escape(result.url, quote=True)}">Open product page</a>',
        "",
        f"<i>{html.escape(result.detail)}</i>",
        f"Checked: {html.escape(now_iso())}",
    ]
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
    context = await browser.new_context(
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        viewport={"width": 1440, "height": 1000},
        extra_http_headers={"Accept-Language": "en-IN,en;q=0.9", "DNT": "1"},
    )

    # Images, videos and fonts are not needed for stock detection and often
    # cause retail pages to exceed the GitHub Actions navigation timeout.
    async def route_handler(route: Any) -> None:
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
        else:
            await route.continue_()

    await context.route("**/*", route_handler)
    return context


async def run_checks(products: list[dict[str, Any]]) -> list[CheckResult]:
    results: list[CheckResult] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=HEADLESS,
            args=["--disable-dev-shm-usage", "--no-sandbox"],
        )
        try:
            for product in products:
                if not product.get("enabled", True):
                    continue

                context = await make_context(browser)
                page = await context.new_page()
                page.set_default_timeout(8000)

                print(f"\nChecking {product['retailer']} — {product['name']}")
                try:
                    if str(product.get("mode", "product")).lower() == "search":
                        result = await check_search_page(browser, page, product)
                    else:
                        result = await check_product_page(page, product)
                except Exception as exc:
                    result = CheckResult(
                        product["key"], product["retailer"], product["name"],
                        StockStatus.UNKNOWN, product["url"], f"Unexpected {type(exc).__name__}: {exc}",
                    )
                finally:
                    await context.close()

                print(f"{result.status.value}: {result.detail}; {format_price(result.price)}; {result.url}")
                results.append(result)
                await asyncio.sleep(float(product.get("delay_after_seconds", 1.0)))
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

        # An uncertain page must not overwrite a known state or trigger alerts.
        if result.status == StockStatus.UNKNOWN:
            continue

        should_alert_stock = (
            result.status == StockStatus.IN_STOCK
            and (previous_status != StockStatus.IN_STOCK.value or previous_fingerprint != current_fingerprint)
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

        new_entry: dict[str, str] = {"status": result.status.value, "url": result.url}
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
