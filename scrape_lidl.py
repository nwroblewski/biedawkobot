"""
Lidl gazetki (leaflet) image scraper.

Discovers all leaflets on the Lidl gazetki index page and downloads every
page image for each one.  Images are saved as:

    lidl/<flyer_uuid>/page_NNN.jpg

where NNN is the zero-padded page number.  Already-downloaded pages are
skipped automatically.

Usage:
    python scrape_lidl.py              # scrape all leaflets
    python scrape_lidl.py <flyer-identifier>   # single leaflet by identifier
        e.g. oferta-wazna-od-2-03-do-4-03-gazetka-pon-kw10
"""

import asyncio
import re
import argparse
from pathlib import Path
import httpx
from playwright.async_api import async_playwright

from db.client import is_leaflet_done, upsert_leaflet

PROVIDER = "lidl"

GAZETKI_URL = "https://www.lidl.pl/c/nasze-gazetki/s10008614"
SCHWARZ_API = "https://endpoints.leaflets.schwarz/v4/flyer"

# Matches Lidl leaflet URLs: /l/pl/gazetki/<identifier>/ar/<N>
_LEAFLET_URL_RE = re.compile(r"/l/[a-z]{2}/gazetki/([^/]+)/")


def get_existing_pages(output_dir: Path) -> set[int]:
    """Return set of page numbers already downloaded in output_dir."""
    result: set[int] = set()
    if not output_dir.exists():
        return result
    for f in output_dir.iterdir():
        m = re.match(r"page_(\d+)\.", f.name)
        if m:
            result.add(int(m.group(1)))
    return result


async def fetch_flyer_data(identifier: str) -> dict | None:
    """
    Call the Schwarz leaflet API for the given flyer identifier.
    Returns the parsed JSON or None on failure.
    """
    url = f"{SCHWARZ_API}?flyer_identifier={identifier}&region_id=0&region_code=0"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(url, headers={"Accept": "application/json"})
            if r.status_code == 200:
                data = r.json()
                if data.get("success") and "flyer" in data:
                    return data["flyer"]
                print(f"  API error: {data.get('message', 'unknown')}")
            else:
                print(f"  ERROR: API returned HTTP {r.status_code} for {identifier}")
    except Exception as e:
        print(f"  ERROR: failed to fetch flyer data: {e}")
    return None


async def download_page(page_entry: dict, output_dir: Path, existing: set[int], client: httpx.AsyncClient) -> None:
    """Download the zoom image for a single page entry if not already present."""
    page_num: int = page_entry["number"]
    if page_num in existing:
        print(f"  page_{page_num:03d} already exists, skipping.")
        return

    # Prefer zoom (highest res), fall back to image
    img_url: str = page_entry.get("zoom") or page_entry.get("image")
    if not img_url:
        print(f"  No image URL for page {page_num}, skipping.")
        return

    clean_url = img_url.split("?")[0]
    ext = Path(clean_url).suffix or ".jpg"
    filename = f"page_{page_num:03d}{ext}"
    dest = output_dir / filename

    try:
        r = await client.get(img_url, timeout=60)
        if r.status_code == 200:
            dest.write_bytes(r.content)
            print(f"  Saved: {filename} ({len(r.content) // 1024} KB)")
        else:
            print(f"  Warning: HTTP {r.status_code} for page {page_num}: {img_url}")
    except Exception as e:
        print(f"  Warning: could not download page {page_num}: {e}")


async def scrape_flyer(identifier: str) -> str | None:
    """
    Scrape a single Lidl leaflet by its flyer identifier.
    Returns the UUID folder name or None on failure.
    """
    print(f"\nFetching flyer: {identifier}")
    flyer = await fetch_flyer_data(identifier)
    if not flyer:
        print("  ERROR: Could not retrieve flyer data. Skipping.")
        return None

    uuid: str = flyer["id"]
    pages: list[dict] = flyer.get("pages", [])
    name: str = flyer.get("name", identifier)

    print(f"  Flyer: {name}")
    print(f"  UUID:  {uuid}")
    print(f"  Pages: {len(pages)}")

    # -- DB check: skip if already fully processed ----------------------------
    if is_leaflet_done(PROVIDER, uuid):
        print(f"  Leaflet {uuid} already processed, skipping.")
        return uuid

    output_dir = Path("leaflets") / PROVIDER / uuid
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"  Output: {output_dir}")

    existing = get_existing_pages(output_dir)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for page_entry in pages:
            await download_page(page_entry, output_dir, existing, client)

    print(f"  Done. {len(pages)} page(s) processed → {output_dir}")
    upsert_leaflet(PROVIDER, uuid, identifier, "images_ready", len(pages))
    return uuid


async def discover_identifiers(browser) -> list[str]:
    """
    Open the Lidl gazetki index page, intercept JSON responses from the
    Schwarz leaflets API that contain flyer listings, and return a list of
    unique flyer identifiers.  Falls back to scraping DOM links if no API
    response is captured within the timeout.
    """
    page = await browser.new_page()
    captured: list[dict] = []

    async def on_response(response):
        if "leaflets.schwarz" in response.url and response.status == 200:
            try:
                data = await response.json()
                if isinstance(data.get("flyers"), list):
                    captured.extend(data["flyers"])
            except Exception:
                pass

    page.on("response", on_response)

    print(f"Loading gazetki index: {GAZETKI_URL}")
    try:
        await page.goto(GAZETKI_URL, wait_until="load", timeout=60000)
    except Exception:
        pass  # proceed even if load times out

    # Give the page a few seconds to fire the flyer-list XHR
    await page.wait_for_timeout(5000)

    seen: set[str] = set()
    identifiers: list[str] = []

    if captured:
        # Identifiers come from the intercepted API response
        for flyer in captured:
            slug = flyer.get("flyerIdentifier") or flyer.get("identifier")
            if not slug:
                # Try to extract from flyerUrlAbsolute or similar fields
                url_field = flyer.get("flyerUrlAbsolute", "")
                m = _LEAFLET_URL_RE.search(url_field)
                slug = m.group(1) if m else None
            if slug and slug not in seen:
                seen.add(slug)
                identifiers.append(slug)
    else:
        # Fallback: scrape <a> hrefs from the rendered DOM
        hrefs: list[str] = await page.evaluate(
            "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"
        )
        for href in hrefs:
            m = _LEAFLET_URL_RE.search(href)
            if m:
                slug = m.group(1)
                if slug not in seen:
                    seen.add(slug)
                    identifiers.append(slug)

    await page.close()
    print(f"Found {len(identifiers)} leaflet(s) on the index page.")
    return identifiers


async def scrape_all() -> None:
    """Discover all leaflets on the Lidl gazetki index and scrape each one."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        identifiers = await discover_identifiers(context)
        if not identifiers:
            print("No leaflets found on the index page.")
            await browser.close()
            return

        await browser.close()

    for i, identifier in enumerate(identifiers, 1):
        print(f"\n{'='*60}")
        print(f"Leaflet {i}/{len(identifiers)}: {identifier}")
        await scrape_flyer(identifier)

    print(f"\n{'='*60}")
    print(f"All {len(identifiers)} leaflet(s) processed.")


async def scrape_single(identifier: str) -> None:
    """Scrape a single leaflet by its flyer identifier (no browser needed)."""
    await scrape_flyer(identifier)


def main():
    parser = argparse.ArgumentParser(
        description="Download images from Lidl gazetki leaflets."
    )
    parser.add_argument(
        "identifier",
        nargs="?",
        help=(
            "Flyer identifier slug (e.g. oferta-wazna-od-2-03-do-4-03-gazetka-pon-kw10). "
            "If omitted, all leaflets from the gazetki index are scraped."
        ),
    )
    args = parser.parse_args()

    if args.identifier:
        asyncio.run(scrape_single(args.identifier))
    else:
        asyncio.run(scrape_all())


if __name__ == "__main__":
    main()
