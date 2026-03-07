"""
Biedawkobot FastAPI service.

Provides a read-only HTTP API over the MongoDB sales collection.

Endpoints:
    GET /health
    GET /sales?provider=&category=&active=true
"""

import os
from datetime import date, datetime
from typing import Any

from fastapi import FastAPI, Query
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27018")
MONGO_DB_NAME = os.getenv("MONGO_DB", "biedawkobot")

app = FastAPI(title="Biedawkobot Sales API", version="1.0.0")

_motor_client: AsyncIOMotorClient | None = None


def get_motor_db():
    global _motor_client
    if _motor_client is None:
        _motor_client = AsyncIOMotorClient(MONGO_URI)
    return _motor_client[MONGO_DB_NAME]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/sales")
async def get_sales(
    provider: str | None = Query(default=None, description="Filter by shop: biedronka or lidl"),
    category: str | None = Query(default=None, description="Partial case-insensitive category match"),
    active: bool = Query(default=False, description="Only return promotions valid today"),
) -> list[dict[str, Any]]:
    db = get_motor_db()
    query: dict[str, Any] = {}

    if provider:
        query["provider"] = provider

    if category:
        query["category"] = {"$regex": category, "$options": "i"}

    if active:
        today = datetime.now()
        query["valid_from"] = {"$lte": today}
        query["valid_to"] = {"$gt": today}

    cursor = db["sales"].find(query, {"_id": 0})
    return await cursor.to_list(length=None)
