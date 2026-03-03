"""
Biedronka press page image scraper.

Can scrape a single leaflet URL or automatically discover and scrape all
leaflets listed on the Biedronka gazetki index page.

Single leaflet usage:
    python scrape_biedronka.py [URL]

Scrape all leaflets:
    python scrape_biedronka.py --all

Images are saved under leaflets/<leaflet_id>/page_NNN.ext
Already-downloaded pages are skipped automatically.
"""

import asyncio
import json
import re
import argparse
from pathlib import Path
from playwright.async_api import async_playwright

GAZETKI_URL = "https://www.biedronka.pl/pl/gazetki"


def build_page_url(base_url: str, page_num: int) -> str:
    """Return the URL with #page=N fragment set."""
    base = base_url.split("#")[0]
    return f"{base}#page={page_num}"


async def intercept_and_load(page, url) -> dict | None:
    """
    Navigate to `url`, intercept the leaflet API response body, and return
    the parsed JSON.  The listener must be registered BEFORE navigation.
    """
    future: asyncio.Future = asyncio.get_event_loop().create_future()

    async def on_response(response):
        if (
            "leaflet-api" in response.url
            and "/api/leaflets/" in response.url
            and not future.done()
        ):
            try:
                body = await response.body()
                future.set_result(json.loads(body))
            except Exception as exc:
                future.set_exception(exc)

    page.on("response", on_response)
    await page.goto(url, wait_until="networkidle", timeout=60000)

    try:
        data = await asyncio.wait_for(future, timeout=20)
        print(f"  Leaflet API intercepted ({len(data.get('images_desktop', []))} pages)")
        return data
    except asyncio.TimeoutError:
        print("  WARNING: leaflet API response not intercepted within timeout.")
        return None
    finally:
        page.remove_listener("response", on_response)


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
        # Wait for the widget to inject pagination spans
        await page.wait_for_selector("#gallery-leaflet span", timeout=10000)
        spans = await page.evaluate(
            "() => Array.from(document.querySelectorAll('#gallery-leaflet span')).map(s => s.innerText)"
        )
        return get_total_pages_from_dom(spans)
    except Exception:
        pass
    return None


async def get_biggest_leaflet_image_for_page(
    page_entry: dict, request_context
) -> tuple[str, bytes] | None:
    """
    Given a single page entry from the API (with 'images' list),
    fetch every image and return (url, content) of the largest one.
    """
    best_url = None
    best_data = None

    for img_url in page_entry.get("images", []):
        if not img_url:
            continue
        # Strip CDN transformation params to get the original full-res file
        clean_url = img_url.split("?")[0]
        try:
            response = await request_context.get(clean_url)
            if response.ok:
                data = await response.body()
                if best_data is None or len(data) > len(best_data):
                    best_data = data
                    best_url = clean_url
        except Exception as e:
            print(f"    Warning: could not fetch {clean_url}: {e}")

    return (best_url, best_data) if best_url else None


def extract_leaflet_id(url: str) -> str | None:
    """Extract the leaflet ID from a Biedronka press URL."""
    m = re.search(r"press,id,([^,/#]+)", url)
    return m.group(1) if m else None


def get_existing_pages(output_dir: Path) -> set[int]:
    """Return the set of page numbers already downloaded in output_dir."""
    existing = set()
    if not output_dir.exists():
        return existing
    for f in output_dir.iterdir():
        m = re.match(r"page_(\d+)\.", f.name)
        if m:
            existing.add(int(m.group(1)))
    return existing


async def close_popup_if_present(page) -> None:
    """Dismiss the store-selection popup if it appears."""
    try:
        # The popup has a close button containing the close icon
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
    (leaflet_id, leaflet_url) for every leaflet found on the page.
    """
    page = await browser.new_page()
    print(f"Loading gazetki index: {GAZETKI_URL}")
    await page.goto(GAZETKI_URL, wait_until="networkidle", timeout=60000)
    await page.wait_for_timeout(2000)

    await close_popup_if_present(page)

    # Collect all <a> hrefs that point to a press/leaflet page
    hrefs = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href*="press,id,"]'))
                   .map(a => a.href)
    """)

    seen = set()
    leaflets = []
    for href in hrefs:
        leaflet_id = extract_leaflet_id(href)
        if leaflet_id and leaflet_id not in seen:
            seen.add(leaflet_id)
            # Normalise to a clean base URL (drop fragment)
            clean = href.split("#")[0]
            leaflets.append((leaflet_id, clean))

    await page.close()
    print(f"Found {len(leaflets)} leaflet(s) on the index page.")
    return leaflets


async def _scrape_leaflet(context, base_url: str, output_dir: Path) -> None:
    """
    Core per-leaflet scraping logic.  Reuses an existing Playwright context
    so multiple leaflets can share one browser session.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {output_dir}")

    browser_page = await context.new_page()

    # ── Step 1: load page 1 and intercept the leaflet API ─────────────────
    url_p1 = build_page_url(base_url, 1)
    print(f"Loading page 1: {url_p1}")

    leaflet_data = await intercept_and_load(browser_page, url_p1)
    if not leaflet_data:
        print("ERROR: Could not retrieve leaflet API data. Skipping.")
        await browser_page.close()
        return

    pages = leaflet_data.get("images_desktop", [])
    if not pages:
        print("ERROR: No pages found in leaflet API data. Skipping.")
        await browser_page.close()
        return

    # ── Step 2: determine total pages ──────────────────────────────────────
    await browser_page.wait_for_timeout(3000)
    total_pages = await get_dom_total_pages(browser_page)
    if total_pages:
        print(f"Total pages (from widget spans): {total_pages}")
    else:
        total_pages = len(pages)
        print(f"Total pages (from API): {total_pages}")

    # ── Step 3: check which pages are already downloaded ───────────────────
    existing = get_existing_pages(output_dir)
    missing = [p for p in range(1, total_pages + 1) if p not in existing]

    if not missing:
        print(f"  All {total_pages} pages already downloaded. Skipping leaflet.")
        await browser_page.close()
        return

    if existing:
        print(f"  {len(existing)} page(s) already present; downloading {len(missing)} missing page(s).")

    # ── Step 4: iterate pages, navigate, download biggest image ────────────
    for page_num in missing:
        page_idx = page_num - 1
        if page_idx >= len(pages):
            break

        url = build_page_url(base_url, page_num)
        print(f"\nPage {page_num}/{total_pages}: {url}")

        if page_num > 1:
            await browser_page.goto(url, wait_until="networkidle", timeout=60000)
            await browser_page.wait_for_timeout(2000)

        page_entry = pages[page_idx]
        result = await get_biggest_leaflet_image_for_page(page_entry, context.request)
        if result is None:
            print(f"  No downloadable image found for page {page_num}, skipping.")
            continue

        img_url, img_data = result
        ext = Path(img_url.split("?")[0]).suffix or ".png"
        filename = f"page_{page_num:03d}{ext}"
        dest = output_dir / filename
        dest.write_bytes(img_data)
        print(f"  Saved: {filename} ({len(img_data) // 1024} KB) from {img_url}")

    await browser_page.close()
    print(f"\nDone. Images saved to: {output_dir}")


async def scrape(base_url: str, output_dir: Path) -> None:
    """Scrape a single leaflet URL into output_dir."""
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
        await _scrape_leaflet(context, base_url, output_dir)
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

        for i, (leaflet_id, leaflet_url) in enumerate(leaflets, 1):
            print(f"\n{'='*60}")
            print(f"Leaflet {i}/{len(leaflets)}: {leaflet_id}")
            output_dir = Path("leaflets") / leaflet_id
            await _scrape_leaflet(context, leaflet_url, output_dir)

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
            "If omitted and --all is not set, uses the gazetki index to scrape all."
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
        leaflet_id = extract_leaflet_id(args.url)
        if leaflet_id:
            output_dir = Path("leaflets") / leaflet_id
        else:
            output_dir = Path("downloaded_images")
        asyncio.run(scrape(args.url, output_dir))


if __name__ == "__main__":
    main()
