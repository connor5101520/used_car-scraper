#!/usr/bin/env python3
"""Used Car Deal Scraper — CLI entry point.

Usage:
    python main.py scrape              Run a one-shot scrape of Craigslist
    python main.py deals               Show top deals in the terminal
    python main.py deals --limit 10    Show top N deals
    python main.py stats               Show database statistics
"""

import argparse
import logging
import os
import sys

import yaml

from database.db import Database
from scrapers import craigslist
from scoring.deal_scorer import deal_label, score_listings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def load_config(path: str = "config.yaml") -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"Config file {path} not found, using defaults")
        return {}


def cmd_scrape(config: dict, db: Database):
    """Scrape Craigslist and score the results."""
    token = config.get("apify", {}).get("token") or os.environ.get("APIFY_TOKEN")
    if not token:
        print("\n" + "=" * 60)
        print(" ERROR: No Apify API token configured!")
        print("=" * 60)
        print()
        print(" To fix this, open config.yaml and paste your token")
        print(" on the line that says:  token: \"\"")
        print()
        print(" Don't have a token yet?")
        print("   1. Go to https://apify.com (free signup)")
        print("   2. Profile icon -> Settings -> Integrations")
        print("   3. Copy your Personal API token")
        print()
        sys.exit(1)

    logger.info("Starting Craigslist scrape...")
    run_id = db.log_scrape_start("craigslist")
    errors = None
    new_count = 0
    listings = []

    try:
        listings = craigslist.scrape(config)
        source_ids = set()

        for listing in listings:
            is_new = db.upsert_listing(listing)
            if is_new:
                new_count += 1
            source_ids.add(listing["source_id"])

        # Mark listings not seen in this scrape as inactive
        db.mark_inactive("craigslist", source_ids)

        logger.info(f"Scrape complete: {len(listings)} found, {new_count} new")

        # Score all active listings
        min_sample = config.get("scoring", {}).get("min_sample_size", 5)
        score_listings(db, min_sample_size=min_sample)

    except Exception as e:
        errors = str(e)
        logger.error(f"Scrape failed: {e}")

    db.log_scrape_end(run_id, len(listings), new_count, errors)


def cmd_deals(config: dict, db: Database, limit: int = 20):
    """Display top deals in the terminal."""
    deals = db.get_top_deals(limit=limit)

    if not deals:
        print("\nNo scored deals found yet. Run 'python main.py scrape' first.\n")
        return

    print(f"\n{'='*80}")
    print(f" TOP {len(deals)} DEALS")
    print(f"{'='*80}\n")

    for i, d in enumerate(deals, 1):
        score = d["deal_score"]
        label = deal_label(score)
        year = d["year"] or "?"
        make = d["make"] or "Unknown"
        model = d["model"] or ""
        price = f"${d['price']:,}" if d["price"] else "N/A"
        mileage = f"{d['mileage']:,} mi" if d["mileage"] else "N/A"
        location = d["location"] or ""

        print(f"  {i:>2}. [{label:^18}] {year} {make} {model}")
        print(f"      Price: {price:<12} Mileage: {mileage:<14} Score: {score:+.2f}")
        if location:
            print(f"      Location: {location}")
        if d["url"]:
            print(f"      {d['url']}")
        print()


def cmd_stats(config: dict, db: Database):
    """Show database statistics."""
    stats = db.get_stats()
    print(f"\n{'='*40}")
    print(f" DATABASE STATS")
    print(f"{'='*40}")
    print(f"  Total listings:  {stats['total_listings']}")
    print(f"  Active listings: {stats['active_listings']}")
    print(f"  Scored listings: {stats['scored_listings']}")
    if stats["by_source"]:
        print(f"  By source:")
        for source, count in stats["by_source"].items():
            print(f"    {source}: {count}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Used Car Deal Scraper")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--token", help="Apify API token (or set APIFY_TOKEN env var, or put in config.yaml)")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    subparsers.add_parser("scrape", help="Run a one-shot Craigslist scrape")

    deals_parser = subparsers.add_parser("deals", help="Show top deals")
    deals_parser.add_argument("--limit", type=int, default=20, help="Number of deals to show")

    subparsers.add_parser("stats", help="Show database statistics")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    config = load_config(args.config)

    # Allow token from --token flag, env var, or config.yaml
    if args.token:
        config.setdefault("apify", {})["token"] = args.token

    db_path = config.get("database", {}).get("path", "./car_deals.db")
    db = Database(db_path)

    if args.command == "scrape":
        cmd_scrape(config, db)
    elif args.command == "deals":
        cmd_deals(config, db, limit=args.limit)
    elif args.command == "stats":
        cmd_stats(config, db)


if __name__ == "__main__":
    main()
