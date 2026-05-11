import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, List, Dict, Any
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv


STATE_FILE = Path("data/seen.json")
CONFIG_FILE = Path("config.yaml")


@dataclass
class Listing:
    source: str
    site: str
    title: str
    url: str
    price: str = ""
    location: str = ""
    layout: str = ""
    area: str = ""

    @property
    def uid(self) -> str:
        raw = f"{self.site}|{self.url}|{self.title}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def load_config() -> Dict[str, Any]:
    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen() -> set:
    if not STATE_FILE.exists():
        return set()
    with STATE_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return set(data.get("seen_ids", []))


def save_seen(seen: set) -> None:
    STATE_FILE.parent.mkdir(exist_ok=True)
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump({"seen_ids": sorted(seen)}, f, ensure_ascii=False, indent=2)


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.7,uk;q=0.6",
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    return response.text


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def text_has_any(text: str, values: Iterable[str]) -> bool:
    text_norm = text.lower()
    return any(v.lower() in text_norm for v in values)


def normalize_url(base_url: str, href: str) -> str:
    if not href:
        return base_url
    return urljoin(base_url, href)


def extract_listing_cards(soup: BeautifulSoup) -> list:
    """
    Універсальна евристика для карток оголошень.
    Сайти можуть міняти HTML, тому тут кілька варіантів.
    """
    selectors = [
        "article",
        "[data-testid*=property]",
        "[data-testid*=estate]",
        "[class*=property]",
        "[class*=estate]",
        "[class*=advert]",
        "[class*=listing]",
        "[class*=card]",
    ]

    cards = []
    for selector in selectors:
        found = soup.select(selector)
        if len(found) >= 2:
            cards.extend(found)

    # прибираємо дублікати по тексту
    unique = []
    used = set()
    for card in cards:
        txt = clean_text(card.get_text(" "))
        key = hashlib.md5(txt[:500].encode("utf-8")).hexdigest()
        if txt and key not in used:
            used.add(key)
            unique.append(card)

    return unique


def listing_from_card(card, base_url: str, source_name: str, site: str) -> Listing | None:
    text = clean_text(card.get_text(" "))

    link = None
    for a in card.find_all("a", href=True):
        href = a.get("href", "")
        if any(x in href for x in ["/detail", "/nemovitost", "/property", "/inzerat", "/pronajem"]):
            link = normalize_url(base_url, href)
            break

    if not link:
        first_link = card.find("a", href=True)
        if first_link:
            link = normalize_url(base_url, first_link["href"])

    if not link:
        return None

    title = ""
    heading = card.find(["h1", "h2", "h3", "h4"])
    if heading:
        title = clean_text(heading.get_text(" "))
    if not title:
        title = text[:120]

    price_match = re.search(r"(\d[\d\s]{2,}\s*Kč(?:\s*\+\s*\d[\d\s]{2,}\s*Kč)?)", text)
    area_match = re.search(r"(\d{2,4}\s*m²)", text)
    layout_match = re.search(r"([1-9]\+(?:kk|1))", text, flags=re.IGNORECASE)

    return Listing(
        source=source_name,
        site=site,
        title=title,
        url=link,
        price=clean_text(price_match.group(1)) if price_match else "",
        area=clean_text(area_match.group(1)) if area_match else "",
        layout=clean_text(layout_match.group(1)) if layout_match else "",
        location=text[:250],
    )


def parse_sreality_html(html: str, source_name: str, base_url: str) -> List[Listing]:
    soup = BeautifulSoup(html, "lxml")
    listings = []

    # Sreality часто має посилання на detail у HTML навіть якщо картки складні.
    links = soup.find_all("a", href=True)

    for a in links:
        href = a.get("href", "")

        if "/detail/pronajem/byt/" not in href:
            continue

        url = normalize_url("https://www.sreality.cz", href.split("?")[0].split("#")[0])

        # Беремо текст навколо лінка
        parent = a
        for _ in range(5):
            if parent.parent:
                parent = parent.parent

        text = clean_text(parent.get_text(" "))

        if not text:
            text = clean_text(a.get_text(" "))

        layout_match = re.search(r"([1-9]\+(?:kk|1))", text, flags=re.IGNORECASE)
        area_match = re.search(r"(\d{2,4}\s*m²)", text)
        price_match = re.search(r"(\d[\d\s]{2,}\s*Kč(?:/měsíc| měsíčně|)?)", text)

        title = text[:160] if text else "Sreality listing"

        listings.append(
            Listing(
                source=source_name,
                site="sreality",
                title=title,
                url=url,
                price=clean_text(price_match.group(1)) if price_match else "",
                area=clean_text(area_match.group(1)) if area_match else "",
                layout=clean_text(layout_match.group(1)) if layout_match else "",
                location=text[:300],
            )
        )

    return deduplicate_listings(listings)


def parse_bezrealitky_html(html: str, source_name: str, base_url: str) -> List[Listing]:
    soup = BeautifulSoup(html, "lxml")
    cards = extract_listing_cards(soup)
    listings = []
    for card in cards:
        item = listing_from_card(card, base_url, source_name, "bezrealitky")
        if item:
            listings.append(item)
    return deduplicate_listings(listings)


def deduplicate_listings(listings: List[Listing]) -> List[Listing]:
    result = []
    used = set()
    for item in listings:
        if item.url not in used:
            used.add(item.url)
            result.append(item)
    return result


def passes_filters(item: Listing, filters: Dict[str, Any]) -> bool:
    full_text = " ".join([
        item.title,
        item.location,
        item.layout,
        item.area,
        item.price,
        item.url,
    ]).lower()

    allowed_layouts = [x.lower() for x in filters.get("layouts", [])]
    allowed_locations = [x.lower() for x in filters.get("locations", [])]
    excluded_locations = [x.lower() for x in filters.get("excluded_locations", [])]

    found_layout = re.search(r"\b([1-9]\+(?:kk|1))\b", full_text, flags=re.IGNORECASE)
    if not found_layout:
        return False

    layout = found_layout.group(1).lower()
    if layout not in allowed_layouts:
        return False

    for bad_location in excluded_locations:
        if bad_location in full_text:
            return False

    if allowed_locations:
        if not any(location in full_text for location in allowed_locations):
            return False

    return True


def format_telegram_message(item: Listing) -> str:
    parts = [
        "🏠 <b>Нова квартира</b>",
        "",
        f"📌 <b>{escape_html(item.title)}</b>",
    ]

    if item.price:
        parts.append(f"💰 {escape_html(item.price)}")
    if item.area:
        parts.append(f"📐 {escape_html(item.area)}")
    if item.layout:
        parts.append(f"🏘 {escape_html(item.layout)}")

    parts.extend([
        f"🌐 Джерело: {escape_html(item.source)}",
        "",
        f"🔗 {escape_html(item.url)}",
    ])

    return "\n".join(parts)


def escape_html(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def send_telegram(token: str, chat_id: str, message: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()


def collect_source(source: Dict[str, Any]) -> List[Listing]:
    html = fetch_html(source["url"])
    site = source["site"].lower()

    if site == "sreality":
        return parse_sreality_html(html, source["name"], source["url"])
    if site == "bezrealitky":
        return parse_bezrealitky_html(html, source["name"], source["url"])

    raise ValueError(f"Unsupported site: {site}")


def main() -> None:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")

    config = load_config()
    filters = config.get("filters", {})
    seen = load_seen()

    new_items: List[Listing] = []

    for source in config.get("sources", []):
        if not source.get("enabled", True):
            continue

        print(f"Checking: {source['name']}")

        try:
            listings = collect_source(source)
        except Exception as e:
            print(f"ERROR while checking {source['name']}: {e}")
            continue

        print(f"Found raw listings: {len(listings)}")

        for item in listings:
            if not passes_filters(item, filters):
                continue
            if item.uid in seen:
                continue

            new_items.append(item)
            seen.add(item.uid)

        # пауза між сайтами
        time.sleep(2)

    for item in new_items[:20]:
        message = format_telegram_message(item)
        send_telegram(token, chat_id, message)
        time.sleep(1)

    save_seen(seen)

    print(f"New items sent: {len(new_items)}")


if __name__ == "__main__":
    main()
