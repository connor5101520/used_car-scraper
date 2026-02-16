"""Craigslist scraper using the Apify actor fatihtahta/craigslist-scraper."""

import hashlib
import logging
import os
import re

from apify_client import ApifyClient

logger = logging.getLogger(__name__)

ACTOR_ID = "fatihtahta/craigslist-scraper"

# Common car makes for parsing titles
CAR_MAKES = {
    "acura", "alfa romeo", "aston martin", "audi", "bentley", "bmw", "buick",
    "cadillac", "chevrolet", "chevy", "chrysler", "dodge", "ferrari", "fiat",
    "ford", "genesis", "gmc", "honda", "hyundai", "infiniti", "jaguar", "jeep",
    "kia", "lamborghini", "land rover", "lexus", "lincoln", "maserati", "mazda",
    "mclaren", "mercedes-benz", "mercedes", "mini", "mitsubishi", "nissan",
    "porsche", "ram", "rivian", "rolls-royce", "subaru", "tesla", "toyota",
    "volkswagen", "vw", "volvo",
}

# Normalize common abbreviations
MAKE_ALIASES = {
    "chevy": "Chevrolet",
    "vw": "Volkswagen",
    "mercedes": "Mercedes-Benz",
}


def _parse_year(text: str) -> int | None:
    """Extract a 4-digit year (1990-2029) from text."""
    match = re.search(r"\b(19[9]\d|20[0-2]\d)\b", text)
    return int(match.group(1)) if match else None


def _parse_make_model(title: str) -> tuple[str | None, str | None]:
    """Best-effort extraction of make and model from a Craigslist title."""
    title_lower = title.lower()

    found_make = None
    make_end = 0
    # Try multi-word makes first (e.g. "land rover", "alfa romeo")
    for make in sorted(CAR_MAKES, key=len, reverse=True):
        idx = title_lower.find(make)
        if idx != -1:
            found_make = make
            make_end = idx + len(make)
            break

    if not found_make:
        return None, None

    # Normalize the make
    canonical = MAKE_ALIASES.get(found_make, found_make.title())

    # Try to grab the next word(s) as the model
    remainder = title[make_end:].strip().lstrip("-").strip()
    # Take the first 1-2 words as model (stop at common noise words)
    noise = {"for", "sale", "with", "only", "low", "miles", "clean", "title", "no", "-", "|"}
    parts = remainder.split()
    model_parts = []
    for part in parts[:3]:
        if part.lower() in noise or part.startswith("$") or part.startswith("("):
            break
        # Stop if it looks like a year
        if re.match(r"^(19|20)\d{2}$", part):
            break
        model_parts.append(part)

    model = " ".join(model_parts).strip(" -|,") if model_parts else None
    return canonical, model


def _parse_mileage(text: str) -> int | None:
    """Try to extract mileage from description text."""
    if not text:
        return None
    patterns = [
        r"(\d[\d,]+)\s*(?:miles|mi\b)",
        r"(?:mileage|odometer)[:\s]*(\d[\d,]+)",
        r"(\d{2,3})[kK]\s*(?:miles|mi\b)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            val = match.group(1).replace(",", "")
            num = int(val)
            # If matched the "123K miles" pattern, multiply
            if "k" in pattern.lower() and num < 1000:
                num *= 1000
            if 100 < num < 500_000:
                return num
    return None


def _extract_source_id(item: dict) -> str:
    """Generate a stable source ID from the listing URL or data."""
    url = item.get("url") or item.get("link") or ""
    if url:
        # Craigslist URLs contain a unique post ID like /12345678.html
        match = re.search(r"/(\d{8,12})\.html", url)
        if match:
            return match.group(1)
    # Fallback: hash of title + price
    key = f"{item.get('title', '')}-{item.get('price', '')}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _normalize_listing(item: dict) -> dict:
    """Map a raw Apify actor result item to our standard listing schema."""
    title = item.get("title") or item.get("name") or ""
    description = item.get("description") or item.get("body") or ""
    full_text = f"{title} {description}"

    year = _parse_year(title) or _parse_year(description)
    make, model = _parse_make_model(title)

    # Price: try various field names the actor might use
    price = None
    for key in ("price", "salePrice", "amount"):
        val = item.get(key)
        if val is not None:
            if isinstance(val, str):
                val = val.replace("$", "").replace(",", "").strip()
                try:
                    price = int(float(val))
                except ValueError:
                    continue
            else:
                price = int(val)
            break

    # Location
    location = item.get("location") or item.get("city") or item.get("area") or ""

    # Image
    images = item.get("images") or item.get("photos") or []
    image_url = None
    if isinstance(images, list) and images:
        image_url = images[0] if isinstance(images[0], str) else images[0].get("url")
    elif isinstance(images, str):
        image_url = images
    if not image_url:
        image_url = item.get("image") or item.get("imageUrl") or item.get("thumbnail")

    return {
        "source": "craigslist",
        "source_id": _extract_source_id(item),
        "url": item.get("url") or item.get("link") or "",
        "title": title,
        "make": make,
        "model": model,
        "year": year,
        "price": price,
        "mileage": _parse_mileage(full_text),
        "location": location,
        "zip_code": item.get("zipCode") or item.get("zip_code"),
        "transmission": None,  # Craigslist doesn't always structure this
        "drive_type": None,
        "fuel_type": None,
        "title_status": None,
        "seller_type": None,
        "description": description[:2000] if description else None,
        "image_url": image_url,
    }


def build_search_url(
    city: str = "newyork",
    min_price: int | None = None,
    max_price: int | None = None,
    min_year: int | None = None,
    max_year: int | None = None,
    max_miles: int | None = None,
    make_model: str | None = None,
    postal: str | None = None,
    search_distance: int | None = None,
    owner_only: bool = False,
) -> str:
    """Build a Craigslist cars & trucks search URL from filter params."""
    base = f"https://{city}.craigslist.org/search/cta"
    params = []
    if min_price:
        params.append(f"min_price={min_price}")
    if max_price:
        params.append(f"max_price={max_price}")
    if min_year:
        params.append(f"min_auto_year={min_year}")
    if max_year:
        params.append(f"max_auto_year={max_year}")
    if max_miles:
        params.append(f"max_auto_miles={max_miles}")
    if make_model:
        params.append(f"auto_make_model={make_model}")
    if postal:
        params.append(f"postal={postal}")
    if search_distance:
        params.append(f"search_distance={search_distance}")
    if owner_only:
        params.append("purveyor=owner")

    return f"{base}?{'&'.join(params)}" if params else base


def scrape(config: dict) -> list[dict]:
    """Run the Craigslist Apify actor and return normalized listings.

    Args:
        config: Full app config dict (must have apify.token and craigslist section).

    Returns:
        List of normalized listing dicts ready for database upsert.
    """
    token = config.get("apify", {}).get("token") or os.environ.get("APIFY_TOKEN")
    if not token:
        raise ValueError(
            "Apify token required. Set APIFY_TOKEN env var or apify.token in config.yaml"
        )

    cl_config = config.get("craigslist", {})
    actor_id = cl_config.get("actor_id", ACTOR_ID)
    search_urls = cl_config.get("search_urls", [])
    limit = cl_config.get("limit", 100)

    if not search_urls:
        raise ValueError("No Craigslist search_urls configured in config.yaml")

    client = ApifyClient(token)
    all_listings = []

    for url in search_urls:
        logger.info(f"Scraping Craigslist: {url}")

        run_input = {
            "startUrls": [url],
            "limit": limit,
        }

        try:
            run = client.actor(actor_id).call(run_input=run_input, timeout_secs=300)
            dataset_items = (
                client.dataset(run["defaultDatasetId"]).list_items().items
            )
            logger.info(f"Got {len(dataset_items)} raw items from Apify")

            # Log the first raw item so we can see the actual field names
            if dataset_items:
                first = dataset_items[0]
                logger.info(f"Sample raw item keys: {list(first.keys())}")
                logger.info(f"Sample raw item: {first}")

            for item in dataset_items:
                normalized = _normalize_listing(item)
                # Only keep listings that have at least a title and price
                if normalized["title"] and normalized["price"]:
                    all_listings.append(normalized)
                else:
                    logger.debug(f"Skipping listing without title/price: {item}")

        except Exception as e:
            logger.error(f"Error scraping {url}: {e}")
            raise

    logger.info(f"Total normalized Craigslist listings: {len(all_listings)}")
    return all_listings
