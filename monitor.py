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

def fetch_json(url: str, params: dict | None = None) -> dict:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        ),
        "Accept": "application/json",
        "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.7,uk;q=0.6",
    }
    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    return response.json()


def in_bbox(lat, lon, bbox: dict) -> bool:
    if lat is None or lon is None:
        return False

    return (
        bbox["lat_min"] <= float(lat) <= bbox["lat_max"]
        and bbox["lon_min"] <= float(lon) <= bbox["lon_max"]
    )


def get_nested(data: dict, path: list, default=None):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current

def slugify_sreality(value: str) -> str:
    value = clean_text(value).lower()

    replacements = {
        "á": "a", "č": "c", "ď": "d", "é": "e", "ě": "e",
        "í": "i", "ň": "n", "ó": "o", "ř": "r", "š": "s",
        "ť": "t", "ú": "u", "ů": "u", "ý": "y", "ž": "z",
        "ä": "a", "ö": "o", "ü": "u",
    }

    for src, dst in replacements.items():
        value = value.replace(src, dst)

    value = value.replace("+", "+")
    value = re.sub(r"[^a-z0-9+]+", "-", value)
    value = re.sub(r"-+", "-", value)
    value = value.strip("-")

    return value or "praha"

def build_sreality_web_url(detail: dict, source: Dict[str, Any], layout: str, locality: str, estate_id: str) -> str:
    category_type = str(source.get("api_params", {}).get("category_type_cb", ""))

    if category_type == "1":
        deal_type = "prodej"
    elif category_type == "2":
        deal_type = "pronajem"
    else:
        deal_type = "pronajem"

    estate_type = "byt"

    layout_slug = slugify_sreality(layout) if layout else "byt"
    locality_slug = slugify_sreality(locality) if locality else "praha"

    return f"https://www.sreality.cz/detail/{deal_type}/{estate_type}/{layout_slug}/{locality_slug}/{estate_id}"

def collect_sreality_api(source: Dict[str, Any]) -> List[Listing]:
    api_url = "https://www.sreality.cz/api/cs/v2/estates"
    detail_base_url = "https://www.sreality.cz/api"
    web_base_url = "https://www.sreality.cz"

    max_pages = int(source.get("max_pages", 3))
    bbox = source.get("bbox", {})
    api_params = source.get("api_params", {}).copy()

    listings: List[Listing] = []

    for page in range(1, max_pages + 1):
        params = {
            **api_params,
            "per_page": 60,
            "page": page,
            "sort": 0,
        }

        data = fetch_json(api_url, params=params)
        estates = get_nested(data, ["_embedded", "estates"], [])

        if not estates:
            break

        for estate in estates:
            detail_href = get_nested(estate, ["_links", "self", "href"])
            if not detail_href:
                continue

            try:
                detail = fetch_json(detail_base_url + detail_href)
            except Exception as e:
                print(f"Sreality detail error: {e}")
                continue

            lat = get_nested(detail, ["map", "lat"])
            lon = get_nested(detail, ["map", "lon"])

            if bbox and not in_bbox(lat, lon, bbox):
                continue

            title = clean_text(get_nested(detail, ["name", "value"], "") or estate.get("name", ""))
            locality = clean_text(get_nested(detail, ["locality", "value"], "") or estate.get("locality", ""))
            price = str(get_nested(detail, ["price_czk", "value"], "") or estate.get("price", ""))
            area = ""
            layout = ""

            for param in detail.get("items", []):
                name = clean_text(str(param.get("name", ""))).lower()
                value = param.get("value", "")

                if isinstance(value, dict):
                    value = value.get("value", "")
                elif isinstance(value, list):
                    value = ", ".join(clean_text(str(x.get("value", x))) for x in value)

                value = clean_text(str(value))

                if "užitná plocha" in name or "podlahová plocha" in name:
                    area = value
                if "typ bytu" in name or "dispozice" in name:
                    layout = value

            if not layout:
                found_layout = re.search(r"([1-9]\+(?:kk|1))", title + " " + locality, flags=re.IGNORECASE)
                layout = found_layout.group(1) if found_layout else ""

            estate_id = str(
                detail.get("hash_id")
                or estate.get("hash_id")
                or detail.get("id")
                or estate.get("id")
                or detail_href.split("/")[-1]
            )

            url = build_sreality_web_url(
                detail=detail,
                source=source,
                layout=layout,
                locality=locality,
                estate_id=estate_id,
            )

            if url.startswith("/"):
                url = "https://www.sreality.cz" + url

            listings.append(
                Listing(
                    source=source["name"],
                    site="sreality",
                    title=title or "Sreality listing",
                    url=url,
                    price=price,
                    area=area,
                    layout=layout,
                    location=locality,
                )
            )

            time.sleep(0.3)

        time.sleep(1)

    return deduplicate_listings(listings)

def collect_source(source: Dict[str, Any]) -> List[Listing]:
    site = source["site"].lower()

    if site == "sreality_api":
        return collect_sreality_api(source)

    html = fetch_html(source["url"])

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

