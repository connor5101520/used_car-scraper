"""Z-score based deal scoring engine for used car listings."""

import logging
from collections import defaultdict
from statistics import mean, stdev

from database.db import Database

logger = logging.getLogger(__name__)

DEAL_TIERS = {
    "Exceptional Deal": (-float("inf"), -2.0),
    "Great Deal": (-2.0, -1.0),
    "Good Deal": (-1.0, -0.5),
    "Fair Price": (-0.5, 0.5),
    "Overpriced": (0.5, float("inf")),
}


def deal_label(score: float) -> str:
    for label, (lo, hi) in DEAL_TIERS.items():
        if lo <= score < hi:
            return label
    return "Fair Price"


def _mileage_adjusted_price(price: int, mileage: int | None) -> float:
    """Adjust price based on mileage to make comparisons fairer.

    Uses a simple linear depreciation model:
    - Baseline: 12,000 miles/year is "average"
    - Each 10,000 miles above/below average shifts perceived value by ~$500
    - This is a rough heuristic, not a precise valuation model
    """
    if mileage is None or mileage <= 0:
        return float(price)

    # Assume average mileage for a used car is ~60,000
    avg_mileage = 60_000
    mileage_diff = mileage - avg_mileage
    adjustment = (mileage_diff / 10_000) * 500

    return float(price) + adjustment


def score_listings(db: Database, min_sample_size: int = 5):
    """Score all active listings using z-score method.

    Groups listings by make + model + year (±1 year range).
    For each group with enough samples, computes z-scores.
    """
    listings = db.get_listings_for_scoring()
    if not listings:
        logger.info("No scorable listings found")
        return

    # Group by make + model (case-insensitive)
    groups: dict[str, list[dict]] = defaultdict(list)
    for listing in listings:
        make = (listing["make"] or "").lower().strip()
        model = (listing["model"] or "").lower().strip()
        if make and model:
            key = f"{make}|{model}"
            groups[key].append(listing)

    scored_count = 0
    skipped_count = 0

    for group_key, group_listings in groups.items():
        for listing in group_listings:
            # Find comparable listings: same make+model, year ±1
            year = listing["year"]
            comparables = [
                l for l in group_listings
                if l["id"] != listing["id"]
                and abs(l["year"] - year) <= 1
            ]

            # Include the listing itself in the sample
            all_in_group = comparables + [listing]

            if len(all_in_group) < min_sample_size:
                # Try widening year range to ±2
                comparables_wide = [
                    l for l in group_listings
                    if l["id"] != listing["id"]
                    and abs(l["year"] - year) <= 2
                ]
                all_in_group = comparables_wide + [listing]

                if len(all_in_group) < min_sample_size:
                    skipped_count += 1
                    continue

            # Compute adjusted prices
            prices = [
                _mileage_adjusted_price(l["price"], l["mileage"])
                for l in all_in_group
            ]
            listing_price = _mileage_adjusted_price(listing["price"], listing["mileage"])

            avg = mean(prices)
            sd = stdev(prices) if len(prices) > 1 else 1.0

            if sd == 0:
                score = 0.0
            else:
                score = (listing_price - avg) / sd

            # Clamp to reasonable range
            score = max(-5.0, min(5.0, score))

            db.update_deal_score(listing["id"], round(score, 3))
            scored_count += 1

    logger.info(f"Scored {scored_count} listings, skipped {skipped_count} (insufficient comparables)")
