# Used Car Deal Scraper — Implementation Plan

## Overview

A Python-based agent that scrapes Craigslist, CarGurus, and Autotrader for used car
listings, identifies statistical outlier deals, and sends email digests to subscribers
on a configurable schedule.

---

## Project Structure

```
used_car-scraper/
├── requirements.txt
├── config.yaml                  # Default config (SMTP, scrape settings, delays)
├── main.py                      # Entry point — starts scheduler & CLI
├── scrapers/
│   ├── __init__.py
│   ├── base.py                  # Abstract base scraper class
│   ├── craigslist.py            # Craigslist scraper
│   ├── cargurus.py              # CarGurus scraper
│   └── autotrader.py            # Autotrader scraper
├── database/
│   ├── __init__.py
│   ├── models.py                # SQLite schema & ORM (using sqlite3 stdlib)
│   └── db.py                    # Connection management, queries
├── scoring/
│   ├── __init__.py
│   └── deal_scorer.py           # Statistical outlier detection
├── notifications/
│   ├── __init__.py
│   ├── email_service.py         # SMTP email sending
│   └── templates/
│       ├── deal_digest.html     # Email template for deal alerts
│       └── unsubscribe.html     # Unsubscribe confirmation page/email
├── scheduler/
│   ├── __init__.py
│   └── jobs.py                  # APScheduler job definitions
└── tests/
    ├── __init__.py
    ├── test_scrapers.py
    ├── test_deal_scorer.py
    ├── test_email_service.py
    └── test_database.py
```

---

## Phase 1: Database Layer

### SQLite Schema

**`listings` table** — stores every scraped listing

| Column          | Type    | Description                          |
|-----------------|---------|--------------------------------------|
| id              | INTEGER | Primary key                          |
| source          | TEXT    | "craigslist", "cargurus", "autotrader"|
| source_id       | TEXT    | Unique ID from the source site       |
| url             | TEXT    | Link to the listing                  |
| title           | TEXT    | Listing title                        |
| make            | TEXT    | e.g., "Toyota"                       |
| model           | TEXT    | e.g., "Camry"                        |
| year            | INTEGER | e.g., 2019                           |
| price           | INTEGER | Listed price in USD                  |
| mileage         | INTEGER | Odometer reading                     |
| location        | TEXT    | City/region                          |
| zip_code        | TEXT    | ZIP code if available                |
| transmission    | TEXT    | "automatic" or "manual"              |
| drive_type      | TEXT    | "AWD", "FWD", "RWD", "4WD"          |
| fuel_type       | TEXT    | "gas", "diesel", "hybrid", "electric"|
| title_status    | TEXT    | "clean", "salvage", "rebuilt"        |
| seller_type     | TEXT    | "dealer" or "private"                |
| description     | TEXT    | Full listing description             |
| image_url       | TEXT    | Primary image URL                    |
| deal_score      | REAL    | Computed deal score (lower = better) |
| first_seen      | TEXT    | ISO timestamp of first scrape        |
| last_seen       | TEXT    | ISO timestamp of most recent scrape  |
| price_history   | TEXT    | JSON array of {price, date} objects  |
| active          | INTEGER | 1 = still listed, 0 = removed       |

**`subscribers` table** — email subscribers and their preferences

| Column          | Type    | Description                          |
|-----------------|---------|--------------------------------------|
| id              | INTEGER | Primary key                          |
| email           | TEXT    | Subscriber email (unique)            |
| frequency       | TEXT    | "hourly", "daily", "weekly"          |
| filters         | TEXT    | JSON blob of filter preferences      |
| unsubscribe_token| TEXT   | UUID for unsubscribe link            |
| active          | INTEGER | 1 = subscribed, 0 = unsubscribed    |
| created_at      | TEXT    | ISO timestamp                        |
| last_notified   | TEXT    | ISO timestamp of last email sent     |

**`scrape_runs` table** — tracks scrape history for debugging

| Column          | Type    | Description                          |
|-----------------|---------|--------------------------------------|
| id              | INTEGER | Primary key                          |
| source          | TEXT    | Which scraper ran                    |
| started_at      | TEXT    | ISO timestamp                        |
| finished_at     | TEXT    | ISO timestamp                        |
| listings_found  | INTEGER | Count of listings scraped            |
| new_listings    | INTEGER | Count of new listings (not seen before)|
| errors          | TEXT    | Any error messages                   |

### Key Operations
- `upsert_listing()` — insert or update a listing by (source, source_id)
- `get_listings_for_scoring()` — fetch listings grouped by make/model/year
- `get_subscribers_due()` — fetch subscribers whose next notification is due
- `mark_inactive()` — flag listings not seen in the latest scrape as inactive

---

## Phase 2: Scrapers

### Base Scraper (`scrapers/base.py`)

Abstract class that all scrapers inherit from:

```python
class BaseScraper(ABC):
    def __init__(self, filters: dict, delay: float = 2.0):
        """filters = {make, model, year_min, year_max, price_min, price_max,
                      mileage_max, zip_code, radius, transmission, drive_type,
                      fuel_type, title_status, seller_type}"""

    @abstractmethod
    def build_search_url(self, filters: dict, page: int) -> str: ...

    @abstractmethod
    def parse_listings(self, html: str) -> list[dict]: ...

    def scrape(self) -> list[dict]:
        """Paginate through results, respect delay, return parsed listings."""

    def _request(self, url: str) -> str:
        """HTTP GET with retry, user-agent rotation, and delay."""
```

### Craigslist Scraper (`scrapers/craigslist.py`)

- **URL pattern**: `https://{city}.craigslist.org/search/cta?...` with query params
- **Pagination**: Offset-based (`s=0`, `s=120`, etc.)
- **Parsing**: BeautifulSoup on `.result-row` elements
- **Filters mapped to URL params**: `auto_make_model`, `min_price`, `max_price`,
  `min_auto_year`, `max_auto_year`, `max_auto_miles`, `postal`, `search_distance`
- **Note**: Craigslist is region-based, so we'll need to either target specific cities
  or aggregate across multiple regions based on user's zip + radius

### CarGurus Scraper (`scrapers/cargurus.py`)

- **URL pattern**: `https://www.cargurus.com/Cars/inventorylisting/viewDetailsFilterViewInventoryListing.action?...`
- **Pagination**: Page-based
- **Parsing**: BeautifulSoup on listing card elements; CarGurus also has structured
  JSON-LD data in `<script>` tags that we can extract
- **Bonus**: CarGurus already computes a deal rating (Great/Good/Fair/Overpriced) —
  we can use this as an additional signal alongside our own scoring

### Autotrader Scraper (`scrapers/autotrader.py`)

- **URL pattern**: `https://www.autotrader.com/cars-for-sale/all-cars/...`
- **Pagination**: `firstRecord` offset parameter
- **Parsing**: Autotrader renders some content via JS, but listing data is often
  embedded in JSON within `<script>` tags (`window.__BONNET_DATA__`) — we'll extract
  that directly from the HTML without needing a browser
- **Filters**: Make/model in URL path, query params for price/year/mileage/zip/radius

### Shared Scraping Behavior
- **Rate limiting**: Configurable delay between requests (default 2 seconds)
- **User-agent rotation**: Pool of common browser user-agent strings
- **Retry logic**: Exponential backoff on 429/5xx responses (max 3 retries)
- **Logging**: All requests/responses logged for debugging
- **Deduplication**: source + source_id used as unique key to avoid duplicates

---

## Phase 3: Deal Scoring Engine

### Approach: Z-Score Based Outlier Detection

```
For each listing:
  1. Find all listings with the same make + model + year range (±1 year)
  2. Normalize price by mileage:
       adjusted_price = price / (1 - mileage_depreciation_factor)
     or use simple $/mile ratio
  3. Compute mean and standard deviation of adjusted prices in the group
  4. deal_score = (adjusted_price - mean) / std_deviation
  5. A deal_score < -1.0 means the listing is 1+ std devs below average = DEAL
```

### Deal Tiers

| Score Range     | Label           |
|-----------------|-----------------|
| < -2.0          | Exceptional Deal|
| -2.0 to -1.0   | Great Deal      |
| -1.0 to -0.5   | Good Deal       |
| -0.5 to 0.5    | Fair Price      |
| > 0.5           | Overpriced      |

### Minimum Sample Size
- Need at least **5 comparable listings** to compute a meaningful score
- If fewer than 5 exist, widen the year range (±2, ±3) or skip scoring
- Listings without a score won't be included in deal alerts

### Price History Tracking
- Each time a listing is re-scraped, log the current price to `price_history`
- Flag listings with price drops as additional deal signals

---

## Phase 4: Email Notification System

### Setup
- Use Python's `smtplib` + `email.mime` for sending
- SMTP provider configured in `config.yaml` (Gmail app password, SendGrid, etc.)
- HTML email templates using Python string formatting (no heavy template engine needed)

### Email Digest Contents
- Subject: "🚗 {N} Used Car Deals Found — {date}"
- Body sections:
  - **Exceptional Deals** (score < -2.0) highlighted at top
  - **Great Deals** (score -2.0 to -1.0)
  - Each listing shows: year/make/model, price, mileage, location, deal score, link
  - Price drop badge if price decreased since last scrape
- Footer: unsubscribe link using unique token

### Unsubscribe Flow
- Each email contains: `https://{host}/unsubscribe?token={uuid}`
- For v1 (no web server), unsubscribe link triggers a `mailto:` reply or the CLI
  can manage subscriptions manually
- For v2, a lightweight Flask endpoint could handle this

### Subscriber Management (CLI)
```bash
python main.py subscribe --email user@example.com --frequency daily \
  --make Toyota --model Camry --year-min 2018 --year-max 2022 \
  --price-max 25000 --mileage-max 80000 --zip 90210 --radius 50

python main.py unsubscribe --email user@example.com
python main.py list-subscribers
```

---

## Phase 5: Scheduler

### APScheduler Jobs

```python
# Hourly: scrape all sources, score deals, email hourly subscribers
scheduler.add_job(run_scrape_and_notify, 'interval', hours=1, args=['hourly'])

# Daily: email daily subscribers (scrape data already fresh from hourly runs)
scheduler.add_job(send_digest, 'cron', hour=8, args=['daily'])

# Weekly: email weekly subscribers every Monday at 8am
scheduler.add_job(send_digest, 'cron', day_of_week='mon', hour=8, args=['weekly'])
```

### Job Flow
1. **`run_scrape_and_notify(frequency)`**:
   - Run all scrapers with each subscriber's filters
   - Upsert listings into DB
   - Run deal scorer on new/updated listings
   - For subscribers matching `frequency`, build and send digest email
   - Log scrape run to `scrape_runs` table

---

## Phase 6: Main Entry Point & CLI

### `main.py` Commands

| Command                        | Description                              |
|--------------------------------|------------------------------------------|
| `python main.py run`           | Start the scheduler (runs continuously)  |
| `python main.py scrape`        | One-shot scrape (no scheduling)          |
| `python main.py subscribe ...` | Add a subscriber with filters            |
| `python main.py unsubscribe ..`| Remove a subscriber                      |
| `python main.py list-subscribers`| Show all active subscribers             |
| `python main.py deals ...`     | Show current top deals in terminal       |

Uses `argparse` for CLI parsing.

---

## Dependencies (`requirements.txt`)

```
requests>=2.31.0        # HTTP requests
beautifulsoup4>=4.12.0  # HTML parsing
lxml>=4.9.0             # Fast HTML parser backend
apscheduler>=3.10.0     # Job scheduling
pyyaml>=6.0             # Config file parsing
```

All other modules (`smtplib`, `sqlite3`, `email`, `argparse`, `json`, `statistics`)
are Python standard library — no additional installs needed.

---

## Implementation Order

| Step | Task                                  | Est. Complexity |
|------|---------------------------------------|-----------------|
| 1    | Database schema & models              | Low             |
| 2    | Base scraper class                    | Low             |
| 3    | Craigslist scraper                    | Medium          |
| 4    | CarGurus scraper                      | Medium          |
| 5    | Autotrader scraper                    | Medium          |
| 6    | Deal scoring engine                   | Medium          |
| 7    | Email service & templates             | Medium          |
| 8    | Subscriber management (CLI)           | Low             |
| 9    | Scheduler setup                       | Low             |
| 10   | Main entry point & CLI                | Low             |
| 11   | Config file & environment handling    | Low             |
| 12   | Tests                                 | Medium          |

---

## Config File (`config.yaml`)

```yaml
smtp:
  host: smtp.gmail.com
  port: 587
  username: ""          # Set via environment variable or here
  password: ""          # Set via environment variable or here
  from_address: ""

scraping:
  delay_seconds: 2.0
  max_retries: 3
  user_agents:
    - "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36..."
    - "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36..."

scoring:
  min_sample_size: 5
  deal_threshold: -1.0        # Z-score below this = deal
  exceptional_threshold: -2.0 # Z-score below this = exceptional deal

database:
  path: "./car_deals.db"
```

---

## Open Questions / Future Enhancements (v2)

- **Web UI**: Flask/FastAPI dashboard for browsing deals + managing subscriptions
- **Facebook Marketplace**: Add if/when feasible
- **VIN decoding**: Cross-reference VIN for accident/title history
- **Image analysis**: Flag listings with suspiciously few or stock photos
- **Proxy support**: For high-volume scraping if rate limits become an issue
- **Cloud deployment**: Move from local SQLite to hosted Postgres + cloud scheduler
