"""SQLite storage: price history (for medians) and posted deals (for dedupe).

On the server the database file (data/prices.db) lives in a Docker volume, so
the history survives between runs and redeploys.
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

-- The Telegram channels' numbers, for the owner's weekly report (src/growth.py).
CREATE TABLE IF NOT EXISTS channel_members (
    channel      TEXT NOT NULL,      -- "free" or "premium"
    members      INTEGER NOT NULL,   -- Telegram's count, the bot and the admins included
    counted_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS channel_joins (
    update_id    INTEGER PRIMARY KEY,   -- Telegram's number for the update: never stored twice
    channel      TEXT NOT NULL,
    source       TEXT NOT NULL,      -- the invite link's place ("instagram", ...); "" = no link of ours
    joined       INTEGER NOT NULL,   -- 1 = joined, 0 = left (who it was is not kept)
    at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reports_sent (
    kind         TEXT NOT NULL,      -- "growth": the weekly report
    sent_at      TEXT NOT NULL
);
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
            # posts, and would make the database grow ~5x faster.
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
        """Delete old observations and shrink the file, so the database stays small."""
        with self.conn:
            deleted = self.conn.execute(
                "DELETE FROM prices WHERE fetched_at < ?", (to_iso(older_than),)
            ).rowcount
        if deleted:
            self.conn.execute("VACUUM")
        return deleted

    def latest_quotes(self, origin: str, destination: str) -> list[Quote]:
        """The prices from the most recent scan of this route, one per departure date.

        Every quote of a run shares the same fetched_at, so the newest fetched_at
        for the route picks out that whole scan.
        """
        rows = self.conn.execute(
            "SELECT origin, destination, depart_date, price, airline, transfers, duration_min, link, fetched_at"
            " FROM prices WHERE origin = ? AND destination = ?"
            " AND fetched_at = (SELECT MAX(fetched_at) FROM prices WHERE origin = ? AND destination = ?)"
            " ORDER BY depart_date",
            (origin, destination, origin, destination),
        ).fetchall()
        return [
            Quote(origin=row[0], destination=row[1], depart_date=date.fromisoformat(row[2]), price=row[3],
                  airline=row[4] or "", transfers=row[5] or 0, duration_min=row[6], link=row[7],
                  fetched_at=datetime.fromisoformat(row[8]))
            for row in rows
        ]

    def last_fetched_at(self) -> datetime | None:
        """When the newest price in the database was fetched (UTC), or None if it's empty."""
        row = self.conn.execute("SELECT MAX(fetched_at) FROM prices").fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    # --- posted deals (dedupe) ----------------------------------------------

    def was_posted(self, channel: str, origin: str, destination: str, depart_date: date | None,
                   price_band: int, since: datetime) -> bool:
        """Did we post this route + date since `since` at the same or a cheaper price band?

        Same band = same deal, so skip it. A cheaper band means the price fell
        further, which is news, so it may be posted again. A pricier band is
        never worth a repost. depart_date None = any date of the route (the
        social posts show one destination at a price, whatever the date).
        """
        sql = ("SELECT 1 FROM posted_deals WHERE channel = ? AND origin = ? AND destination = ?"
               " AND posted_at >= ? AND price_band <= ?")
        values: list = [channel, origin, destination, to_iso(since), price_band]
        if depart_date is not None:
            sql += " AND depart_date = ?"
            values.append(depart_date.isoformat())
        return self.conn.execute(sql + " LIMIT 1", values).fetchone() is not None

    def first_posted_at(self, channel: str, origin: str, destination: str, depart_date: date,
                        price_band: int, since: datetime, until: datetime) -> datetime | None:
        """When this route + date was first posted on `channel` at this price band or a cheaper
        one, between since and until, or None.

        A post at a pricier band doesn't count: the channel had the date then, but not this price.
        """
        row = self.conn.execute(
            "SELECT MIN(posted_at) FROM posted_deals WHERE channel = ? AND origin = ? AND destination = ?"
            " AND depart_date = ? AND price_band <= ? AND posted_at >= ? AND posted_at <= ?",
            (channel, origin, destination, depart_date.isoformat(), price_band, to_iso(since), to_iso(until)),
        ).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def count_posts(self, channel: str, since: datetime) -> int:
        """How many posts went to `channel` since `since`.

        A post lists several dates, each one its own row, but they all share the
        route and the moment of posting: counting those pairs counts posts.
        """
        row = self.conn.execute(
            "SELECT COUNT(*) FROM (SELECT DISTINCT origin, destination, posted_at FROM posted_deals"
            " WHERE channel = ? AND posted_at >= ?)",
            (channel, to_iso(since)),
        ).fetchone()
        return row[0]

    def last_posts(self, channel: str) -> dict[str, datetime]:
        """When each destination was last posted on `channel`: {"FCO": datetime, ...} (UTC)."""
        rows = self.conn.execute(
            "SELECT destination, MAX(posted_at) FROM posted_deals WHERE channel = ? GROUP BY destination",
            (channel,),
        ).fetchall()
        return {row[0]: datetime.fromisoformat(row[1]) for row in rows}

    def record_post(self, channel: str, quote: Quote, price_band: int, posted_at: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO posted_deals (channel, origin, destination, depart_date, price,"
                " price_band, posted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (channel, quote.origin, quote.destination, quote.depart_date.isoformat(),
                 quote.price, price_band, to_iso(posted_at)),
            )

    # --- the channels' numbers (src/growth.py) --------------------------------

    def save_member_count(self, channel: str, members: int, counted_at: datetime) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO channel_members (channel, members, counted_at) VALUES (?, ?, ?)",
                              (channel, members, to_iso(counted_at)))

    def member_count_since(self, channel: str, since: datetime) -> tuple[int, datetime] | None:
        """The first count of `channel` at or after `since`: (members, when it was counted), or None."""
        row = self.conn.execute(
            "SELECT members, counted_at FROM channel_members WHERE channel = ? AND counted_at >= ?"
            " ORDER BY counted_at LIMIT 1",
            (channel, to_iso(since)),
        ).fetchone()
        return (row[0], datetime.fromisoformat(row[1])) if row else None

    def save_join(self, update_id: int, channel: str, source: str, joined: bool, at: datetime) -> None:
        """Note that someone joined (or left) a channel. The same update twice is kept once."""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO channel_joins (update_id, channel, source, joined, at) VALUES (?, ?, ?, ?, ?)",
                (update_id, channel, source, 1 if joined else 0, to_iso(at)),
            )

    def joins_by_source(self, channel: str, since: datetime) -> dict[str, int]:
        """How many joined `channel` since `since`, per place: {"instagram": 5, "": 2, ...}."""
        rows = self.conn.execute(
            "SELECT source, COUNT(*) FROM channel_joins WHERE channel = ? AND joined = 1 AND at >= ? GROUP BY source",
            (channel, to_iso(since)),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def count_leaves(self, channel: str, since: datetime) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM channel_joins WHERE channel = ? AND joined = 0 AND at >= ?",
            (channel, to_iso(since)),
        ).fetchone()
        return row[0]

    def last_report(self, kind: str) -> datetime | None:
        """When the last report of this kind went out, or None."""
        row = self.conn.execute("SELECT MAX(sent_at) FROM reports_sent WHERE kind = ?", (kind,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def record_report(self, kind: str, sent_at: datetime) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO reports_sent (kind, sent_at) VALUES (?, ?)", (kind, to_iso(sent_at)))
