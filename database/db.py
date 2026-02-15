"""SQLite database layer for car listings and scrape tracking."""

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: str = "./car_deals.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_tables()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def _ensure_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS listings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source          TEXT NOT NULL,
                source_id       TEXT NOT NULL,
                url             TEXT,
                title           TEXT,
                make            TEXT,
                model           TEXT,
                year            INTEGER,
                price           INTEGER,
                mileage         INTEGER,
                location        TEXT,
                zip_code        TEXT,
                transmission    TEXT,
                drive_type      TEXT,
                fuel_type       TEXT,
                title_status    TEXT,
                seller_type     TEXT,
                description     TEXT,
                image_url       TEXT,
                deal_score      REAL,
                first_seen      TEXT NOT NULL,
                last_seen       TEXT NOT NULL,
                price_history   TEXT DEFAULT '[]',
                active          INTEGER DEFAULT 1,
                UNIQUE(source, source_id)
            );

            CREATE TABLE IF NOT EXISTS scrape_runs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                source          TEXT NOT NULL,
                started_at      TEXT NOT NULL,
                finished_at     TEXT,
                listings_found  INTEGER DEFAULT 0,
                new_listings    INTEGER DEFAULT 0,
                errors          TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
            CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(active);
            CREATE INDEX IF NOT EXISTS idx_listings_make_model ON listings(make, model);
            CREATE INDEX IF NOT EXISTS idx_listings_deal_score ON listings(deal_score);
        """)

    def upsert_listing(self, listing: dict) -> bool:
        """Insert or update a listing. Returns True if this is a new listing."""
        now = _now()
        conn = self._conn

        existing = conn.execute(
            "SELECT id, price, price_history FROM listings WHERE source = ? AND source_id = ?",
            (listing["source"], listing["source_id"]),
        ).fetchone()

        if existing:
            price_history = json.loads(existing["price_history"])
            if listing.get("price") and listing["price"] != existing["price"]:
                price_history.append({"price": listing["price"], "date": now})

            conn.execute(
                """UPDATE listings SET
                    url=?, title=?, make=?, model=?, year=?, price=?, mileage=?,
                    location=?, zip_code=?, transmission=?, drive_type=?, fuel_type=?,
                    title_status=?, seller_type=?, description=?, image_url=?,
                    last_seen=?, price_history=?, active=1
                WHERE source=? AND source_id=?""",
                (
                    listing.get("url"), listing.get("title"),
                    listing.get("make"), listing.get("model"),
                    listing.get("year"), listing.get("price"),
                    listing.get("mileage"), listing.get("location"),
                    listing.get("zip_code"), listing.get("transmission"),
                    listing.get("drive_type"), listing.get("fuel_type"),
                    listing.get("title_status"), listing.get("seller_type"),
                    listing.get("description"), listing.get("image_url"),
                    now, json.dumps(price_history),
                    listing["source"], listing["source_id"],
                ),
            )
            conn.commit()
            return False
        else:
            price_history = []
            if listing.get("price"):
                price_history.append({"price": listing["price"], "date": now})

            conn.execute(
                """INSERT INTO listings
                    (source, source_id, url, title, make, model, year, price, mileage,
                     location, zip_code, transmission, drive_type, fuel_type,
                     title_status, seller_type, description, image_url,
                     first_seen, last_seen, price_history, active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    listing["source"], listing["source_id"],
                    listing.get("url"), listing.get("title"),
                    listing.get("make"), listing.get("model"),
                    listing.get("year"), listing.get("price"),
                    listing.get("mileage"), listing.get("location"),
                    listing.get("zip_code"), listing.get("transmission"),
                    listing.get("drive_type"), listing.get("fuel_type"),
                    listing.get("title_status"), listing.get("seller_type"),
                    listing.get("description"), listing.get("image_url"),
                    now, now, json.dumps(price_history),
                ),
            )
            conn.commit()
            return True

    def get_active_listings(self, source: str | None = None) -> list[dict]:
        """Fetch all active listings, optionally filtered by source."""
        conn = self._conn
        if source:
            rows = conn.execute(
                "SELECT * FROM listings WHERE active = 1 AND source = ? ORDER BY deal_score ASC",
                (source,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM listings WHERE active = 1 ORDER BY deal_score ASC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_listings_for_scoring(self) -> list[dict]:
        """Fetch active listings that have make, model, year, and price for scoring."""
        rows = self._conn.execute(
            """SELECT * FROM listings
               WHERE active = 1 AND make IS NOT NULL AND model IS NOT NULL
                 AND year IS NOT NULL AND price IS NOT NULL"""
        ).fetchall()
        return [dict(r) for r in rows]

    def update_deal_score(self, listing_id: int, score: float):
        self._conn.execute(
            "UPDATE listings SET deal_score = ? WHERE id = ?",
            (score, listing_id),
        )
        self._conn.commit()

    def mark_inactive(self, source: str, active_source_ids: set[str]):
        """Mark listings as inactive if they weren't seen in the latest scrape."""
        conn = self._conn
        rows = conn.execute(
            "SELECT id, source_id FROM listings WHERE source = ? AND active = 1",
            (source,),
        ).fetchall()
        for row in rows:
            if row["source_id"] not in active_source_ids:
                conn.execute(
                    "UPDATE listings SET active = 0 WHERE id = ?", (row["id"],)
                )
        conn.commit()

    def log_scrape_start(self, source: str) -> int:
        now = _now()
        cursor = self._conn.execute(
            "INSERT INTO scrape_runs (source, started_at) VALUES (?, ?)",
            (source, now),
        )
        self._conn.commit()
        return cursor.lastrowid

    def log_scrape_end(self, run_id: int, listings_found: int, new_listings: int, errors: str | None = None):
        now = _now()
        self._conn.execute(
            """UPDATE scrape_runs SET finished_at=?, listings_found=?, new_listings=?, errors=?
               WHERE id=?""",
            (now, listings_found, new_listings, errors, run_id),
        )
        self._conn.commit()

    def get_top_deals(self, limit: int = 20, source: str | None = None) -> list[dict]:
        """Get the best deals (lowest deal_score) from active listings."""
        conn = self._conn
        if source:
            rows = conn.execute(
                """SELECT * FROM listings
                   WHERE active = 1 AND deal_score IS NOT NULL AND source = ?
                   ORDER BY deal_score ASC LIMIT ?""",
                (source, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM listings
                   WHERE active = 1 AND deal_score IS NOT NULL
                   ORDER BY deal_score ASC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self) -> dict:
        """Get summary stats about the database."""
        conn = self._conn
        total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM listings WHERE active = 1").fetchone()[0]
        scored = conn.execute(
            "SELECT COUNT(*) FROM listings WHERE active = 1 AND deal_score IS NOT NULL"
        ).fetchone()[0]
        sources = conn.execute(
            "SELECT source, COUNT(*) as cnt FROM listings WHERE active = 1 GROUP BY source"
        ).fetchall()
        return {
            "total_listings": total,
            "active_listings": active,
            "scored_listings": scored,
            "by_source": {r["source"]: r["cnt"] for r in sources},
        }
