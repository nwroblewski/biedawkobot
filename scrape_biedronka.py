"""
Biedronka press page image scraper.

Can scrape a single leaflet URL or automatically discover and scrape all
leaflets listed on the Biedronka gazetki index page.

Single leaflet usage:
    python scrape_biedronka.py [URL]

Scrape all leaflets:
    python scrape_biedronka.py --all

Images are saved under leaflets/<leaflet_uuid>/page_NNN_I.ext where I is the
image slot index (0, 1, ...) for pages that span two images.
Already-downloaded image slots are skipped automatically.
"""

import asyncio
import re
import argparse
from pathlib import Path
from playwright.async_api import async_playwright

from db.client import is_leaflet_downloaded, upsert_leaflet

PROVIDER = "biedronka"

GAZETKI_URL = "https://www.biedronka.pl/pl/gazetki"
LEAFLET_API_BASE = "https://leaflet-api.prod.biedronka.cloud/api/leaflets"


def build_page_url(base_url: str, page_num: int) -> str:
    """Return the URL with #page=N fragment set."""
    base = base_url.split("#")[0]
    return f"{base}#page={page_num}"


async def intercept_leaflet_uuid(page, url) -> str | None:
    """
    Navigate to `url`, intercept the leaflet API call and return the leaflet
    UUID extracted from the intercepted response URL.
    URL pattern: leaflet-api.prod.biedronka.cloud/api/leaflets/<uuid>
    """
    future: asyncio.Future = asyncio.get_event_loop().create_future()

    async def on_response(response):
        if (
            "leaflet-api" in response.url
            and "/api/leaflets/" in response.url
            and not future.done()
        ):
            m = re.search(r"/api/leaflets/([^/?]+)", response.url)
            if m:
                future.set_result(m.group(1))
            else:
                future.set_exception(ValueError(f"Cannot extract UUID from {response.url}"))

    page.on("response", on_response)
    await page.goto(url, wait_until="networkidle", timeout=60000)

    try:
        uuid = await asyncio.wait_for(future, timeout=20)
        print(f"  Leaflet UUID: {uuid}")
        return uuid
    except asyncio.TimeoutError:
        print("  WARNING: leaflet API response not intercepted within timeout.")
        return None
    finally:
        page.remove_listener("response", on_response)


async def fetch_leaflet_data(uuid: str, request_context) -> dict | None:
    """
    Fetch full leaflet data from the API using the UUID.
    Returns the parsed JSON or None on failure.
    """
    url = f"{LEAFLET_API_BASE}/{uuid}?ctx=web"
    try:
        response = await request_context.get(url)
        if response.ok:
            data = await response.json()
            pages = data.get("images_desktop", [])
            print(f"  Fetched leaflet data ({len(pages)} pages) from API")
            return data
        else:
            print(f"  ERROR: API returned status {response.status} for {url}")
            return None
    except Exception as e:
        print(f"  ERROR: Failed to fetch leaflet data: {e}")
        return None


def get_total_pages_from_dom(page_spans: list[str]) -> int | None:
    """
    Parse the page counter from the leaflet widget span elements.
    Looks for a span with '/' immediately followed by a span with a digit string.
    """
    for i, text in enumerate(page_spans):
        if text.strip() == "/" and i + 1 < len(page_spans):
            nxt = page_spans[i + 1].strip()
            if nxt.isdigit():
                return int(nxt)
    return None


async def get_dom_total_pages(page) -> int | None:
    """Try to read total page count from the rendered widget spans."""
    try:
        await page.wait_for_selector("#gallery-leaflet span", timeout=10000)
        spans = await page.evaluate(
            "() => Array.from(document.querySelectorAll('#gallery-leaflet span')).map(s => s.innerText)"
        )
        return get_total_pages_from_dom(spans)
    except Exception:
        pass
    return None


def get_existing_slots(output_dir: Path) -> dict[int, set[int]]:
    """
    Scan output_dir for already-downloaded files matching page_NNN_I.ext.
    Returns a dict mapping page_num -> set of downloaded slot indices.
    """
    result: dict[int, set[int]] = {}
    if not output_dir.exists():
        return result
    for f in output_dir.iterdir():
        m = re.match(r"page_(\d+)_(\d+)\.", f.name)
        if m:
            page_num = int(m.group(1))
            slot = int(m.group(2))
            result.setdefault(page_num, set()).add(slot)
    return result


async def download_page_images(
    page_entry: dict,
    page_num: int,
    output_dir: Path,
    request_context,
    existing_slots: set[int],
) -> None:
    """
    Download all images for a single page entry.
    Each image is saved as page_NNN_I.ext where I is its index in the images list.
    Slots already present in existing_slots are skipped.
    """
    images = [u for u in page_entry.get("images", []) if u]
    if not images:
        print(f"  No images listed for page {page_num}, skipping.")
        return

    for slot, img_url in enumerate(images):
        if slot in existing_slots:
            print(f"  page_{page_num:03d}_{slot} already exists, skipping.")
            continue

        clean_url = img_url.split("?")[0]
        try:
            response = await request_context.get(clean_url)
            if response.ok:
                data = await response.body()
                ext = Path(clean_url).suffix or ".jpg"
                filename = f"page_{page_num:03d}_{slot}{ext}"
                dest = output_dir / filename
                dest.write_bytes(data)
                print(f"  Saved: {filename} ({len(data) // 1024} KB)")
            else:
                print(f"  Warning: HTTP {response.status} for slot {slot}: {clean_url}")
        except Exception as e:
            print(f"  Warning: could not fetch slot {slot} {clean_url}: {e}")


def extract_leaflet_slug(url: str) -> str | None:
    """Extract the leaflet slug from a Biedronka press URL (used for display only)."""
    m = re.search(r"press,id,([^,/#]+)", url)
    return m.group(1) if m else None


async def close_popup_if_present(page) -> None:
    """Dismiss the store-selection popup if it appears."""
    try:
        close_btn = page.locator(
            "button:has(img[src*='ico_close']), "
            "[class*='close']:visible, "
            "button[class*='close']:visible"
        ).first
        await close_btn.click(timeout=5000)
        await page.wait_for_timeout(800)
    except Exception:
        pass


async def scrape_gazetki_index(browser) -> list[tuple[str, str]]:
    """
    Open the gazetki index page, close any popup, and return a list of
    (slug, leaflet_url) for every leaflet found on the page.
    """
    page = await browser.new_page()
    print(f"Loading gazetki index: {GAZETKI_URL}")
    await page.goto(GAZETKI_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)

    await close_popup_if_present(page)

    hrefs = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href*="press,id,"]'))
                   .map(a => a.href)
    """)

    seen = set()
    leaflets = []
    for href in hrefs:
        slug = extract_leaflet_slug(href)
        if slug and slug not in seen:
            seen.add(slug)
            clean = href.split("#")[0]
            leaflets.append((slug, clean))

    await page.close()
    print(f"Found {len(leaflets)} leaflet(s) on the index page.")
    return leaflets


async def _scrape_leaflet(context, base_url: str, slug: str = "") -> str | None:
    """
    Core per-leaflet scraping logic.  Reuses an existing Playwright context.
    Returns the UUID used as the folder name, or None on failure.
    """
    browser_page = await context.new_page()

    # -- Step 1: open press page, intercept API call to get the UUID ----------
    url_p1 = build_page_url(base_url, 1)
    print(f"Loading page 1: {url_p1}")

    uuid = await intercept_leaflet_uuid(browser_page, url_p1)
    if not uuid:
        print("ERROR: Could not determine leaflet UUID. Skipping.")
        await browser_page.close()
        return None

    # -- DB check: skip if already fully processed ----------------------------
    if is_leaflet_downloaded(PROVIDER, uuid):
        print(f"  Leaflet {uuid} already processed, skipping.")
        await browser_page.close()
        return uuid

    output_dir = Path("leaflets") / PROVIDER / uuid
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {output_dir}")

    # -- Step 2: fetch full image list directly from the API ------------------
    leaflet_data = await fetch_leaflet_data(uuid, context.request)
    if not leaflet_data:
        print("ERROR: Could not retrieve leaflet data. Skipping.")
        await browser_page.close()
        return None

    pages = leaflet_data.get("images_desktop", [])
    if not pages:
        print("ERROR: No pages found in leaflet data. Skipping.")
        await browser_page.close()
        return None

    # -- Step 3: determine total pages ----------------------------------------
    await browser_page.wait_for_timeout(3000)
    total_pages = await get_dom_total_pages(browser_page)
    if total_pages:
        print(f"Total pages (from widget spans): {total_pages}")
    else:
        total_pages = len(pages)
        print(f"Total pages (from API): {total_pages}")

    # Browser no longer needed -- all image URLs are already known
    await browser_page.close()

    # -- Step 4: check which slots are already on disk ------------------------
    existing = get_existing_slots(output_dir)

    # -- Step 5: download all image slots for each page -----------------------
    for page_num in range(1, total_pages + 1):
        page_idx = page_num - 1
        if page_idx >= len(pages):
            break

        page_entry = pages[page_idx]
        total_slots = len([u for u in page_entry.get("images", []) if u])
        existing_slots = existing.get(page_num, set())

        if total_slots > 0 and len(existing_slots) >= total_slots:
            print(f"  Page {page_num}: all {total_slots} image(s) already downloaded, skipping.")
            continue

        print(f"\nPage {page_num}/{total_pages} ({total_slots} image(s))")
        await download_page_images(
            page_entry, page_num, output_dir, context.request, existing_slots
        )

    print(f"\nDone. Images saved to: {output_dir}")
    upsert_leaflet(PROVIDER, uuid, slug or uuid, "images_ready", total_pages)
    return uuid


async def scrape(base_url: str) -> None:
    """Scrape a single leaflet URL."""
    slug = extract_leaflet_slug(base_url) or ""
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
        await _scrape_leaflet(context, base_url, slug)
        await browser.close()


async def scrape_all() -> None:
    """Discover all leaflets on the gazetki index page and scrape each one."""
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

        leaflets = await scrape_gazetki_index(browser)
        if not leaflets:
            print("No leaflets found on the index page.")
            await browser.close()
            return

        for i, (slug, leaflet_url) in enumerate(leaflets, 1):
            print(f"\n{'='*60}")
            print(f"Leaflet {i}/{len(leaflets)}: {slug}")
            await _scrape_leaflet(context, leaflet_url, slug)

        await browser.close()
        print(f"\n{'='*60}")
        print(f"All {len(leaflets)} leaflet(s) processed.")


def main():
    parser = argparse.ArgumentParser(
        description="Download images from Biedronka press/leaflet pages."
    )
    parser.add_argument(
        "url",
        nargs="?",
        help=(
            "Biedronka press page URL. "
            "If omitted, scrapes all leaflets from the gazetki index."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help=f"Discover and scrape all leaflets from {GAZETKI_URL}",
    )
    args = parser.parse_args()

    if args.all or args.url is None:
        asyncio.run(scrape_all())
    else:
        asyncio.run(scrape(args.url))


if __name__ == "__main__":
    main()
