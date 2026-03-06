"""
MongoDB client helpers for Biedawkobot.

All functions are synchronous (pymongo) — used by the scraper and parser scripts
that run as cron jobs outside of any async runtime.

Environment variables:
    MONGO_URI  — MongoDB connection string (default: mongodb://localhost:27017)
    MONGO_DB   — Database name (default: biedawkobot)
"""

import os
from datetime import date, datetime, timezone
from typing import Any

from pymongo import MongoClient, ASCENDING
from pymongo.collection import Collection
from pymongo.database import Database

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB_NAME = os.getenv("MONGO_DB", "biedawkobot")

_client: MongoClient | None = None


def get_db() -> Database:
    """Return the shared MongoClient database instance (lazy init)."""
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI)
        _ensure_indexes(_client[MONGO_DB_NAME])
    return _client[MONGO_DB_NAME]


def _ensure_indexes(db: Database) -> None:
    """Create required indexes if they don't already exist."""
    db["leaflets"].create_index(
        [("provider", ASCENDING), ("uuid", ASCENDING)],
        unique=True,
        name="provider_uuid_unique",
    )
    db["sales"].create_index(
        [("provider", ASCENDING), ("leaflet_id", ASCENDING)],
        name="provider_leaflet",
    )
    db["sales"].create_index(
        [("valid_from", ASCENDING), ("valid_to", ASCENDING)],
        name="validity_range",
    )


# ---------------------------------------------------------------------------
# Leaflet helpers
# ---------------------------------------------------------------------------


def is_leaflet_done(provider: str, uuid: str) -> bool:
    """Return True if the leaflet has already been fully processed."""
    db = get_db()
    doc = db["leaflets"].find_one(
        {"provider": provider, "uuid": uuid},
        {"status": 1},
    )
    return doc is not None and doc.get("status") == "done"


def upsert_leaflet(
    provider: str,
    uuid: str,
    identifier: str,
    status: str,
    page_count: int = 0,
) -> None:
    """Insert or update a leaflet document."""
    db = get_db()
    now = datetime.now(tz=timezone.utc)
    db["leaflets"].update_one(
        {"provider": provider, "uuid": uuid},
        {
            "$set": {
                "identifier": identifier,
                "status": status,
                "page_count": page_count,
            },
            "$setOnInsert": {"scraped_at": now},
        },
        upsert=True,
    )


def set_leaflet_status(provider: str, uuid: str, status: str) -> None:
    """Update only the status field of a leaflet document."""
    db = get_db()
    update: dict[str, Any] = {"$set": {"status": status}}
    if status == "done":
        update["$set"]["processed_at"] = datetime.now(tz=timezone.utc)
    db["leaflets"].update_one({"provider": provider, "uuid": uuid}, update)


# ---------------------------------------------------------------------------
# Sales helpers
# ---------------------------------------------------------------------------


def insert_sales(items: list[dict]) -> int:
    """
    Bulk-insert a list of sale dicts into the sales collection.
    Returns the number of documents inserted.
    """
    if not items:
        return 0
    db = get_db()
    result = db["sales"].insert_many(items, ordered=False)
    return len(result.inserted_ids)


def query_sales(
    provider: str | None = None,
    category: str | None = None,
    active_today: bool = False,
) -> list[dict]:
    """
    Query sales with optional filters.

    Args:
        provider:     Filter by shop name (exact match).
        category:     Filter by category (case-insensitive partial match).
        active_today: If True, only return promotions valid today.
    """
    db = get_db()
    query: dict[str, Any] = {}

    if provider:
        query["provider"] = provider

    if category:
        query["category"] = {"$regex": category, "$options": "i"}

    if active_today:
        today = datetime.combine(date.today(), datetime.min.time())
        query["valid_from"] = {"$lte": today}
        query["valid_to"] = {"$gte": today}

    cursor = db["sales"].find(query, {"_id": 0})
    return list(cursor)
