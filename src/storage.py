"""SQLite storage: price history (for medians) and posted deals (for dedupe).

The database file (data/prices.db) is committed back to the repo by the
GitHub Actions workflow, so the history survives between runs.
"""
from __future__ import annotations

import sqlite3
import statistics
from datetime import date, datetime, timezone
from pathlib import Path

from src.scanner import Quote

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    depart_date  TEXT NOT NULL,      -- YYYY-MM-DD
    price        REAL NOT NULL,      -- EUR
    airline      TEXT,               -- IATA code, e.g. W6
    transfers    INTEGER,            -- 0 = direct
    duration_min INTEGER,
    link         TEXT,               -- no longer filled (kept so older DBs still match)
    fetched_at   TEXT NOT NULL       -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_prices_route ON prices (origin, destination, fetched_at);

CREATE TABLE IF NOT EXISTS posted_deals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    channel      TEXT NOT NULL,      -- "telegram" today; Instagram/premium/English later
    origin       TEXT NOT NULL,
    destination  TEXT NOT NULL,
    depart_date  TEXT NOT NULL,
    price        REAL NOT NULL,
    price_band   INTEGER NOT NULL,   -- see deals.price_band()
    posted_at    TEXT NOT NULL       -- ISO-8601 UTC
);
CREATE INDEX IF NOT EXISTS idx_posted_route ON posted_deals (channel, origin, destination, depart_date);
"""


def to_iso(moment: datetime) -> str:
    """UTC ISO-8601 text. One format everywhere lets SQLite compare timestamps as strings."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


class Storage:
    """Usage:  with Storage("data/prices.db") as storage: ...  (closes the file at the end)."""

    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # --- prices --------------------------------------------------------------

    def save_quotes(self, quotes: list[Quote]) -> int:
        rows = [
            # The link isn't saved: it's ~400 characters, only needed for this run's
            # posts, and would make the committed DB grow ~5x faster.
            (q.origin, q.destination, q.depart_date.isoformat(), q.price, q.airline,
             q.transfers, q.duration_min, None, to_iso(q.fetched_at))
            for q in quotes
        ]
        with self.conn:  # commits when the block ends
            self.conn.executemany(
                "INSERT INTO prices (origin, destination, depart_date, price, airline,"
                " transfers, duration_min, link, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def route_median(self, origin: str, destination: str, since: datetime) -> tuple[float | None, int]:
        """Median price of every observation for the route since `since`, plus how many there were."""
        rows = self.conn.execute(
            "SELECT price FROM prices WHERE origin = ? AND destination = ? AND fetched_at >= ?",
            (origin, destination, to_iso(since)),
        ).fetchall()
        prices = [row[0] for row in rows]
        return (statistics.median(prices) if prices else None), len(prices)

    def prune_prices(self, older_than: datetime) -> int:
        """Delete old observations and shrink the file, so the committed DB stays small."""
        with self.conn:
            deleted = self.conn.execute(
                "DELETE FROM prices WHERE fetched_at < ?", (to_iso(older_than),)
            ).rowcount
        if deleted:
            self.conn.execute("VACUUM")
        return deleted

    # --- posted deals (dedupe) ----------------------------------------------

    def was_posted(self, channel: str, origin: str, destination: str, depart_date: date,
                   price_band: int, since: datetime) -> bool:
        """Did we post this route + date since `since` at the same or a cheaper price band?

        Same band = same deal, so skip it. A cheaper band means the price fell
        further, which is news, so it may be posted again. A pricier band is
        never worth a repost.
        """
        row = self.conn.execute(
            "SELECT 1 FROM posted_deals WHERE channel = ? AND origin = ? AND destination = ?"
            " AND depart_date = ? AND posted_at >= ? AND price_band <= ? LIMIT 1",
            (channel, origin, destination, depart_date.isoformat(), to_iso(since), price_band),
        ).fetchone()
        return row is not None

    def first_posted_at(self, channel: str, origin: str, destination: str, depart_date: date,
                        since: datetime, until: datetime) -> datetime | None:
        """When this route + date was first posted on `channel` between since and until, or None."""
        row = self.conn.execute(
            "SELECT MIN(posted_at) FROM posted_deals WHERE channel = ? AND origin = ? AND destination = ?"
            " AND depart_date = ? AND posted_at >= ? AND posted_at <= ?",
            (channel, origin, destination, depart_date.isoformat(), to_iso(since), to_iso(until)),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def record_post(self, channel: str, quote: Quote, price_band: int, posted_at: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO posted_deals (channel, origin, destination, depart_date, price,"
                " price_band, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (channel, quote.origin, quote.destination, quote.depart_date.isoformat(),
                 quote.price, price_band, to_iso(posted_at)),
            )
