"""
adem_playwright.py — Playwright automation to download ADEM eFile permit PDFs.

Downloads the most recent Final Permit (FPER) for an Alabama NPDES number from
https://app.adem.alabama.gov/eFile/. Falls back to Draft Permit (DPER) if no
final permit is found.

The ADEM eFile portal uses ASP.NET WebForms + UpdatePanels and typically takes
~30 seconds per AJAX response, so all timeouts are set accordingly.

The actual PDF is served directly by the Laserfiche document management system
(lf.adem.alabama.gov) via a simple HTTP GET once we have the document ID.

Usage:
    python adem_playwright.py --npdes AL0061671
    python adem_playwright.py --npdes AL0061671 --output /tmp/permits/

Requirements:
    pip install playwright requests
    python -m playwright install chromium
"""

import argparse
import asyncio
import re
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

EFILE_URL = "https://app.adem.alabama.gov/eFile/"
LASERFICHE_BASE = "http://lf.adem.alabama.gov/weblink"

# Preferred document type codes, in priority order
PREFERRED_TYPES = ["FPER", "DPER"]

# The server is very slow — each AJAX call takes ~29 seconds.
AJAX_TIMEOUT_S = 65
PAGE_LOAD_TIMEOUT_MS = 45_000
MAX_PAGES = 12


# ---------------------------------------------------------------------------
# HTML parsing helpers (pure regex — no extra deps)
# ---------------------------------------------------------------------------

def _extract_doc_id(weblink_url: str) -> Optional[str]:
    """Extract Laserfiche docid from a DocView.aspx URL."""
    m = re.search(r"[?&]id=(\d+)", weblink_url, re.IGNORECASE)
    return m.group(1) if m else None


def _parse_date(date_str: str) -> datetime:
    """Parse MM/DD/YYYY to datetime for sorting; returns datetime.min on failure."""
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y")
    except ValueError:
        return datetime.min


def _parse_grid_rows(html: str) -> list[dict]:
    """
    Parse data rows from the DocsGridView HTML fragment returned in UpdatePanel2.

    Each row has 8 cells: Download link | Master ID | Name | Permit Number |
    County | Date | Type | File Name.
    """
    pattern = re.compile(
        r'<a\s+href="(http://lf\.adem\.alabama\.gov/weblink/DocView\.aspx\?[^"]+)"[^>]*>Download</a>'
        r"</td><td>([^<]*)</td>"   # Master ID
        r"<td>([^<]*)</td>"        # Name
        r"<td>([^<]*)</td>"        # Permit Number
        r"<td>([^<]*)</td>"        # County
        r"<td>([^<]*)</td>"        # Date
        r"<td>([^<]*)</td>"        # Document Type
        r"<td>([^<]*)</td>",       # File Name
        re.DOTALL,
    )
    rows = []
    for m in pattern.finditer(html):
        rows.append({
            "download_url": m.group(1).replace("&amp;", "&"),
            "master_id":    m.group(2).strip(),
            "name":         m.group(3).strip(),
            "permit_number":m.group(4).strip(),
            "county":       m.group(5).strip(),
            "date_str":     m.group(6).strip(),
            "doc_type":     m.group(7).strip(),
            "file_name":    m.group(8).strip(),
        })
    return rows


def _extract_page_count(html: str) -> int:
    """
    Infer total page count from the pagination nav inside DocsGridView.
    Returns 1 if no pagination is visible.
    """
    nums = [int(m) for m in re.findall(r"'Page\$(\d+)'", html)]
    if not nums:
        return 1
    # The pager shows up to ~10 page links; the "..." link points to the next group.
    # The highest number visible is the upper bound of the current group, not total pages.
    # We use it as a conservative upper bound — the loop will stop naturally when the
    # next-page link no longer appears.
    return max(nums)


# ---------------------------------------------------------------------------
# Playwright async helpers
# ---------------------------------------------------------------------------

async def _wait_for_initial_results(page, timeout_s: int = AJAX_TIMEOUT_S) -> bool:
    """
    Poll MessageLabel until it shows 'N Documents Found'.
    Returns True if results loaded before timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            msg = await page.inner_text("#ctl00_ContentPlaceHolder1_MessageLabel")
            if msg.strip():
                print(f"[ADEM] {msg.strip()}")
                return True
        except Exception:
            pass
        await page.wait_for_timeout(1500)
    return False


async def _navigate_grid_page(page, page_num: int) -> bool:
    """
    Trigger DocsGridView pagination via __doPostBack and wait for the grid to update.
    Returns True when the grid has new rows.
    """
    # Record the current first-row URL so we can detect when the grid refreshes.
    try:
        current_html = await page.inner_html("#ctl00_ContentPlaceHolder1_UpdatePanel2")
        current_first = _parse_grid_rows(current_html)
        current_first_url = current_first[0]["download_url"] if current_first else None
    except Exception:
        current_first_url = None

    print(f"[ADEM] Loading page {page_num} (server is slow — up to {AJAX_TIMEOUT_S}s)...")
    try:
        await page.evaluate(
            f"__doPostBack('ctl00$ContentPlaceHolder1$DocsGridView','Page${page_num}')"
        )
    except Exception as exc:
        print(f"[ADEM] doPostBack failed: {exc}")
        return False

    deadline = time.monotonic() + AJAX_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            new_html = await page.inner_html("#ctl00_ContentPlaceHolder1_UpdatePanel2")
            new_rows = _parse_grid_rows(new_html)
            if new_rows:
                new_first_url = new_rows[0]["download_url"]
                if current_first_url is None or new_first_url != current_first_url:
                    return True
        except Exception:
            pass
        await page.wait_for_timeout(1500)

    print(f"[ADEM] Timed out waiting for page {page_num}")
    return False


# ---------------------------------------------------------------------------
# Direct PDF download via Laserfiche (no browser navigation needed)
# ---------------------------------------------------------------------------

def _download_pdf_direct(doc_id: str, npdes: str, output_dir: str) -> Optional[str]:
    """
    Download the PDF directly via Laserfiche ElectronicFile.aspx.
    Returns the saved file path, or None if the server does not return a PDF.
    """
    url = f"{LASERFICHE_BASE}/ElectronicFile.aspx?docid={doc_id}&dbid=0"
    print(f"[ADEM] Downloading PDF: {url}")
    try:
        resp = requests.get(
            url, timeout=120, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; NymphSeedAgent/1.0)"},
        )
        content_type = resp.headers.get("content-type", "")
        if resp.status_code == 200 and ("pdf" in content_type.lower() or resp.content[:4] == b"%PDF"):
            out_path = Path(output_dir) / f"{npdes}_final_permit.pdf"
            out_path.write_bytes(resp.content)
            size_mb = len(resp.content) / 1_048_576
            print(f"[ADEM] Saved {size_mb:.1f} MB to {out_path}")
            return str(out_path)
        print(f"[ADEM] Unexpected response: HTTP {resp.status_code} content-type={content_type!r}")
    except Exception as exc:
        print(f"[ADEM] PDF download failed: {exc}")
    return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def download_permit_pdf(
    npdes: str,
    output_dir: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """
    Download the most recent FPER (Final Permit) PDF for an Alabama NPDES number
    from the ADEM eFile portal.

    Returns (file_path, error_message).
    On success: (path_to_pdf, None).
    On failure: (None, human-readable error message).
    """
    try:
        from playwright.async_api import async_playwright  # local import keeps startup fast
    except ImportError:
        return None, (
            "playwright is not installed. Run: "
            "pip install playwright && python -m playwright install chromium"
        )

    output_dir = output_dir or tempfile.mkdtemp(prefix="adem_permits_")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()

        try:
            # ------------------------------------------------------------------
            # 1. Load the portal
            # ------------------------------------------------------------------
            print(f"[ADEM] Loading eFile portal...")
            try:
                await page.goto(EFILE_URL, timeout=PAGE_LOAD_TIMEOUT_MS)
                await page.wait_for_load_state("networkidle")
            except Exception as exc:
                return None, f"ADEM eFile portal is unreachable: {exc}"

            # Check for login wall
            page_text = await page.inner_text("body")
            if any(kw in page_text.lower() for kw in ("log in", "sign in", "username", "password")):
                return None, "ADEM eFile portal requires login — automated access is blocked"

            # ------------------------------------------------------------------
            # 2. Fill and submit the search form
            # ------------------------------------------------------------------
            await page.check("#ctl00_ContentPlaceHolder1_LibraryCheckBoxList_2")  # Water
            await page.fill("#ctl00_ContentPlaceHolder1_PermitNumberTextBox", npdes)
            await page.select_option("#ctl00_ContentPlaceHolder1_DocCatDropDownList", "Permitting")
            print(f"[ADEM] Searching for {npdes} — Permitting category (server is slow, up to {AJAX_TIMEOUT_S}s)...")
            await page.click("#ctl00_ContentPlaceHolder1_SearchButton", force=True)

            arrived = await _wait_for_initial_results(page)
            if not arrived:
                return None, f"ADEM eFile search timed out for {npdes} — server may be down"

            # ------------------------------------------------------------------
            # 3. Page through results, collecting FPER / DPER rows
            # ------------------------------------------------------------------
            all_target_rows: list[dict] = []
            page_count = None

            for grid_page in range(1, MAX_PAGES + 1):
                up2_html = await page.inner_html("#ctl00_ContentPlaceHolder1_UpdatePanel2")
                rows = _parse_grid_rows(up2_html)
                print(f"[ADEM] Page {grid_page}: {len(rows)} documents")

                for row in rows:
                    if row["doc_type"] in PREFERRED_TYPES:
                        all_target_rows.append(row)
                        print(
                            f"[ADEM]   >> {row['doc_type']}  {row['date_str']}  {row['file_name']}"
                        )

                if page_count is None:
                    page_count = _extract_page_count(up2_html)
                    print(f"[ADEM] Estimated pages: {page_count}")

                if grid_page >= page_count:
                    break

                ok = await _navigate_grid_page(page, grid_page + 1)
                if not ok:
                    print(f"[ADEM] Could not load page {grid_page + 1} — stopping pagination")
                    break

            # ------------------------------------------------------------------
            # 4. Pick best document: most recent FPER, then most recent DPER
            # ------------------------------------------------------------------
            if not all_target_rows:
                return None, (
                    f"No FPER (Final Permit) or DPER (Draft Permit) documents found for "
                    f"{npdes} in ADEM eFile Permitting category"
                )

            best_row: Optional[dict] = None
            for preferred_type in PREFERRED_TYPES:
                typed = [r for r in all_target_rows if r["doc_type"] == preferred_type]
                if typed:
                    typed.sort(key=lambda r: _parse_date(r["date_str"]), reverse=True)
                    best_row = typed[0]
                    print(
                        f"[ADEM] Selected: {best_row['doc_type']}  {best_row['date_str']}  "
                        f"{best_row['file_name']}"
                    )
                    break

            # ------------------------------------------------------------------
            # 5. Download the PDF via Laserfiche ElectronicFile.aspx
            # ------------------------------------------------------------------
            doc_id = _extract_doc_id(best_row["download_url"])
            if not doc_id:
                return None, f"Could not parse Laserfiche doc ID from: {best_row['download_url']}"

            file_path = _download_pdf_direct(doc_id, npdes, output_dir)
            if file_path:
                return file_path, None

            return None, (
                f"Laserfiche direct download failed for doc ID {doc_id}. "
                f"View manually: {best_row['download_url']}"
            )

        except Exception as exc:
            return None, f"ADEM eFile automation error: {exc}"

        finally:
            await browser.close()


def download_permit_pdf_sync(
    npdes: str,
    output_dir: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Synchronous wrapper around download_permit_pdf() for use from seed_runner.py."""
    return asyncio.run(download_permit_pdf(npdes, output_dir))


def main():
    parser = argparse.ArgumentParser(
        description="Download ADEM eFile final permit PDF via Playwright"
    )
    parser.add_argument("--npdes", required=True, help="Alabama NPDES number (e.g. AL0061671)")
    parser.add_argument("--output", default=None, help="Directory to save the PDF")
    args = parser.parse_args()

    npdes = args.npdes.strip().upper()
    print(f"\nADEM eFile Permit Downloader — {npdes}\n")

    file_path, error = download_permit_pdf_sync(npdes, args.output)

    if file_path:
        print(f"\nSUCCESS: {file_path}")
    else:
        print(f"\nFAILED: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
