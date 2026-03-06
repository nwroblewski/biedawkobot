#!/usr/bin/env python3
"""
Parse sale records from leaflet page images using a local Ollama vision model.

Usage:
    python parse_sales.py                    # traverse all of leaflets/
    python parse_sales.py path/to/image.png  # single image
    python parse_sales.py path/to/folder/    # all images in folder

Outputs:
    approved.txt — JSONL, one valid SaleItem per line
    failed.txt   — blocks for items that could not be validated after retry

Environment variables (optional):
    OLLAMA_BASE_URL  — Ollama host (default: http://localhost:11434)
    OLLAMA_MODEL     — Model name (default: qwen3-vl)
"""

import argparse
import base64
import json
import os
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from db.client import insert_sales, is_leaflet_done, set_leaflet_status, upsert_leaflet

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3-vl")

APPROVED_FILE = Path("approved.txt")
FAILED_FILE = Path("failed.txt")
LEAFLETS_DIR = Path("leaflets")

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class SaleItem(BaseModel):
    product_name: str
    discounted_price: float
    original_price: float
    discount_pct: int | None = None
    unit: str | None = None
    valid_from: date
    valid_to: date
    category: str
    leaflet_id: str
    provider: str

    @field_validator("product_name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("product_name must not be empty")
        return v.strip()

    @field_validator("discounted_price")
    @classmethod
    def price_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("discounted_price must be > 0")
        return v

    @field_validator("discount_pct")
    @classmethod
    def pct_range(cls, v: int | None) -> int | None:
        if v is not None and not (-100 <= v <= 100):
            raise ValueError("discount_pct must be between 0 and 100")
        return v

    @model_validator(mode="after")
    def original_gte_discounted(self) -> "SaleItem":
        if self.original_price < self.discounted_price:
            raise ValueError(
                f"original_price ({self.original_price}) must be >= "
                f"discounted_price ({self.discounted_price})"
            )
        return self


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------


def resolve_dates(raw: dict) -> dict:
    """
    GPT-4o returns valid_from / valid_to as MM-DD strings.
    Inject the current year into both; if valid_from > valid_to after injection,
    bump valid_to to the next year (handles Dec→Jan promotions).
    Mutates and returns the raw dict.
    """
    current_year = datetime.now().year

    for key in ("valid_from", "valid_to"):
        val = raw.get(key)
        if isinstance(val, str):
            # Accept both MM-DD and any string that looks like a partial date
            stripped = val.strip()
            if len(stripped) == 5 and stripped[2] == "-":
                raw[key] = f"{current_year}-{stripped}"

    try:
        vf = date.fromisoformat(raw["valid_from"])
        vt = date.fromisoformat(raw["valid_to"])
        if vf > vt:
            mm_dd = raw["valid_to"][5:]  # keep the MM-DD part
            raw["valid_to"] = f"{current_year + 1}-{mm_dd}"
    except (KeyError, ValueError):
        pass

    return raw


# ---------------------------------------------------------------------------
# LLM HELPERS
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert at reading Polish supermarket promotional leaflets.
Your task is to extract every sale/discount item visible in the provided image.

For each product on sale, return a JSON object with these exact fields:
- product_name (string): full product name as shown, non-empty
- discounted_price (number): the sale/promotional price in PLN, must be > 0
- original_price (number): the regular/crossed-out price in PLN, must be >= discounted_price
- discount_pct (integer or null): the discount percentage if explicitly shown (e.g. -30%), else null
- unit (string or null): unit of measure if shown (e.g. "szt.", "kg", "l", "opak."), else null
- valid_from (string): promotion start date as MM-DD (day and month only, no year)
- valid_to (string): promotion end date as MM-DD (day and month only, no year)
- category (string): product category (e.g. "nabiał", "mięso", "napoje", "chemia", "pieczywo")

Return ONLY a JSON object in this exact format:
{"sales": [<item1>, <item2>, ...]}

If a required field cannot be determined from the image, do your best to infer it.
Do not include any explanation or text outside the JSON.\

REMEMBER THAT A PART OF PRICE MAY BE WRITTEN IN SMALLER FONT. THE SMALLER FONT - USUALLY 2 LAST DIGITS MEANS GROSZES (CENTS)
AND SHOULD BE PLACED AS DECIMAL PART OF PRICE.
"""


def encode_image(image_path: Path) -> str:
    """Return base64-encoded image data."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def call_vision_model(image_path: Path, extra_hint: str = "") -> list[dict]:
    """Call the local Ollama /api/chat endpoint and return a list of raw sale dicts."""
    b64 = encode_image(image_path)

    system = SYSTEM_PROMPT
    if extra_hint:
        system += (
            "\n\nIMPORTANT — your previous response contained validation errors. "
            "Fix only the fields mentioned below and return the full corrected JSON:\n"
            + extra_hint
        )

    payload = {
        "model": MODEL,
        "images": [b64],
        "prompt": f"{SYSTEM_PROMPT}  Extract this data from image. Return ONLY JSON WITH OBJECTS - NO EXPLANATIONS",
        "format": "json",
        "stream": False
    }


    print("calling ollama")
    response = httpx.post(
        f"{OLLAMA_BASE_URL}/api/generate",
        json=payload,
        timeout=360,
    )

    print(response.json()['thinking'])

    content = response.json()['thinking']
    parsed = json.loads(content)
    return parsed.get("sales", [])


# ---------------------------------------------------------------------------
# File output
# ---------------------------------------------------------------------------


def append_approved(item: SaleItem) -> None:
    with APPROVED_FILE.open("a", encoding="utf-8") as f:
        f.write(item.model_dump_json() + "\n")


def append_failed(raw: dict, error: str, image_path: Path) -> None:
    with FAILED_FILE.open("a", encoding="utf-8") as f:
        f.write(f"=== FAILED: {image_path} ===\n")
        f.write(json.dumps(raw, ensure_ascii=False, default=str) + "\n")
        f.write(f"Validation error: {error}\n")
        f.write("===\n\n")


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------


def get_leaflet_id(image_path: Path) -> str:
    """Return the immediate parent folder name as the leaflet ID."""
    return image_path.parent.name


def get_provider(image_path: Path) -> str:
    """Infer shop/provider name from the folder structure: leaflets/<provider>/<uuid>/image."""
    return image_path.parent.parent.name


def process_image(image_path: Path, provider: str, debug: bool = False) -> list[SaleItem]:
    """Process a single image and return validated SaleItem list."""
    print(f"\nProcessing: {image_path}")
    leaflet_id = get_leaflet_id(image_path)

    try:
        raw_items = call_vision_model(image_path)
    except Exception as e:
        print(f"  ERROR calling vision model: {e}")
        return []

    print(f"  Model returned {len(raw_items)} item(s)")

    approved: list[SaleItem] = []

    for i, raw in enumerate(raw_items):
        raw = resolve_dates(raw)
        raw["leaflet_id"] = leaflet_id
        raw["provider"] = provider

        # --- First attempt ---------------------------------------------------
        try:
            item = SaleItem(**raw)
            approved.append(item)
            if debug:
                append_approved(item)
            print(f"  [OK]    item {i + 1}: {raw.get('product_name', '?')}")
            continue
        except (ValidationError, Exception) as first_err:
            first_error_str = str(first_err)

        # --- Retry once with error hint --------------------------------------
        print(
            f"  [RETRY] item {i + 1}: {raw.get('product_name', '?')} "
            f"— {first_error_str[:100]}"
        )

        retry_raw = raw
        retry_error_str = "retry did not produce output"

        try:
            retry_items = call_vision_model(image_path, extra_hint=first_error_str)
            if i < len(retry_items):
                retry_raw = resolve_dates(retry_items[i])
                retry_raw["leaflet_id"] = leaflet_id
                retry_raw["provider"] = provider
            item = SaleItem(**retry_raw)
            approved.append(item)
            if debug:
                append_approved(item)
            print(f"  [OK]    item {i + 1} after retry")
            continue
        except (ValidationError, Exception) as retry_err:
            retry_error_str = str(retry_err)

        # --- Permanently failed ----------------------------------------------
        if debug:
            append_failed(
                retry_raw,
                f"first attempt: {first_error_str} | retry: {retry_error_str}",
                image_path,
            )
        print(f"  [FAIL]  item {i + 1}")

    return approved


def discover_images(root: Path) -> list[Path]:
    """Recursively find all image files under root."""
    extensions = ("*.png", "*.jpg", "*.jpeg")
    images: list[Path] = []
    for pattern in extensions:
        images.extend(root.rglob(pattern))
    return sorted(set(images))


def discover_leaflet_dirs(root: Path) -> list[tuple[str, str, Path]]:
    """
    Walk leaflets/<provider>/<uuid>/ and return (provider, uuid, path) tuples
    for directories that have at least one image and are not yet done in the DB.
    """
    result = []
    if not root.is_dir():
        return result
    for provider_dir in sorted(root.iterdir()):
        if not provider_dir.is_dir():
            continue
        provider = provider_dir.name
        for uuid_dir in sorted(provider_dir.iterdir()):
            if not uuid_dir.is_dir():
                continue
            uuid = uuid_dir.name
            images = discover_images(uuid_dir)
            if not images:
                continue
            if is_leaflet_done(provider, uuid):
                print(f"  [{provider}/{uuid}] already done, skipping.")
                continue
            result.append((provider, uuid, uuid_dir))
    return result


def process_leaflet_dir(provider: str, uuid: str, uuid_dir: Path, debug: bool) -> None:
    """Process all images in a single leaflet directory, write to DB, then delete images."""
    print(f"\n{'='*60}")
    print(f"Processing leaflet: {provider}/{uuid}")

    set_leaflet_status(provider, uuid, "processing")

    images = discover_images(uuid_dir)
    print(f"  Found {len(images)} image(s)")

    all_items: list[SaleItem] = []
    for image_path in images:
        items = process_image(image_path, provider, debug=debug)
        all_items.extend(items)

    if all_items:
        records = [item.model_dump(mode="json") for item in all_items]
        # Convert date objects to datetime for MongoDB
        for rec in records:
            for field in ("valid_from", "valid_to"):
                if isinstance(rec.get(field), str):
                    from datetime import datetime as dt
                    rec[field] = dt.fromisoformat(rec[field])
        inserted = insert_sales(records)
        print(f"  Inserted {inserted} sale record(s) into MongoDB")
    else:
        print("  No valid sale records extracted")

    shutil.rmtree(uuid_dir)
    print(f"  Deleted image directory: {uuid_dir}")

    set_leaflet_status(provider, uuid, "done")
    print(f"  Marked {provider}/{uuid} as done")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract sale records from leaflet images using a local vision model."
    )
    parser.add_argument(
        "path",
        nargs="?",
        help=(
            "Path to an image file or directory. "
            "If omitted, traverses all unprocessed leaflets under leaflets/."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=f"Also write approved/failed records to {APPROVED_FILE} / {FAILED_FILE}",
    )
    args = parser.parse_args()

    if args.path is None:
        if not LEAFLETS_DIR.is_dir():
            sys.exit(
                f"ERROR: Default leaflets directory '{LEAFLETS_DIR}' not found. "
                "Provide a path argument or run from the project root."
            )
        leaflets = discover_leaflet_dirs(LEAFLETS_DIR)
        if not leaflets:
            print("No unprocessed leaflets found.")
            return
        print(f"Found {len(leaflets)} unprocessed leaflet(s) to process.")
        for provider, uuid, uuid_dir in leaflets:
            process_leaflet_dir(provider, uuid, uuid_dir, debug=args.debug)
    else:
        target = Path(args.path)
        if target.is_file():
            provider = get_provider(target)
            items = process_image(target, provider, debug=True)
            print(f"\nExtracted {len(items)} item(s).")
        elif target.is_dir():
            images = discover_images(target)
            if not images:
                sys.exit("No images found in directory.")
            provider = get_provider(images[0]) if images else "unknown"
            all_items: list[SaleItem] = []
            for image_path in images:
                all_items.extend(process_image(image_path, provider, debug=True))
            print(f"\nExtracted {len(all_items)} item(s) total.")
        else:
            sys.exit(f"ERROR: Path not found: {target}")

    print("\nDone.")


if __name__ == "__main__":
    main()
