"""
pdf_extractor.py — Download a PDF from a URL and extract text with page numbers.

Extraction priority:
  1. pdfplumber  — native text layer (fast, exact)
  2. Document AI — Google Cloud Document AI (OCR + table parsing for scanned PDFs)
  3. pytesseract  — local Tesseract fallback if Document AI is unavailable/unconfigured

Usage:
    python pdf_extractor.py --url https://example.com/permit.pdf
    python pdf_extractor.py --url https://example.com/permit.pdf --output extracted.json
    python pdf_extractor.py --file /path/to/local.pdf --output extracted.json
    python pdf_extractor.py --file /path/to/local.pdf --force-docai
"""

import argparse
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

load_dotenv(override=True)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False
    print("[PDF] WARNING: pdfplumber not installed — install with: pip install pdfplumber")

try:
    from google.cloud import documentai
    from google.oauth2 import service_account
    HAS_DOCAI = True
except ImportError:
    HAS_DOCAI = False

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    HAS_TESSERACT = HAS_PIL  # Tesseract needs PIL to handle images
except ImportError:
    HAS_TESSERACT = False

try:
    import fitz  # PyMuPDF — used only for Tesseract rendering fallback
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def download_pdf(url: str, timeout: int = 60) -> bytes:
    """Download a PDF from a URL and return raw bytes."""
    print(f"[PDF] Downloading: {url}")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NymphSeedAgent/1.0; +https://nyad.io)"}
    resp = requests.get(url, timeout=timeout, headers=headers)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "")
    if "pdf" not in content_type.lower() and not url.lower().endswith(".pdf"):
        print(f"[PDF] WARNING: Content-Type is '{content_type}' — proceeding anyway")
    print(f"[PDF] Downloaded {len(resp.content):,} bytes")
    return resp.content


def load_local_pdf(file_path: str) -> bytes:
    with open(file_path, "rb") as f:
        return f.read()


def is_scanned(pages: list[dict], threshold: int = 10) -> bool:
    """Return True if average words per page is below threshold (scanned PDF)."""
    if not pages:
        return False
    avg_words = sum(p["word_count"] for p in pages) / len(pages)
    return avg_words < threshold


# ---------------------------------------------------------------------------
# Extraction method 1: pdfplumber (native text layer)
# ---------------------------------------------------------------------------

def extract_with_pdfplumber(pdf_bytes: bytes) -> list[dict]:
    if not HAS_PDFPLUMBER:
        raise RuntimeError("pdfplumber not installed")
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        total = len(pdf.pages)
        print(f"[PDF] pdfplumber: {total} pages detected")
        for i, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append({
                "page": i,
                "text": text.strip(),
                "word_count": len(text.split()),
                "method": "pdfplumber",
            })
    return pages


# ---------------------------------------------------------------------------
# Extraction method 2: Google Cloud Document AI
# ---------------------------------------------------------------------------

def _docai_client():
    """
    Build a Document AI client.
    Uses a service account JSON key file when GOOGLE_APPLICATION_CREDENTIALS is set
    and the file exists; falls back to Application Default Credentials otherwise.
    Resolves a relative path against the project root (one level above scripts/).
    """
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    # Resolve relative path against the project root
    if creds_path and not os.path.isabs(creds_path):
        from pathlib import Path
        resolved = Path(__file__).parent.parent / creds_path
        if resolved.exists():
            creds_path = str(resolved)

    if creds_path and os.path.exists(creds_path):
        credentials = service_account.Credentials.from_service_account_file(
            creds_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return documentai.DocumentProcessorServiceClient(
            credentials=credentials,
            client_options={"api_endpoint": "us-documentai.googleapis.com"},
        )

    # Fall back to Application Default Credentials
    return documentai.DocumentProcessorServiceClient(
        client_options={"api_endpoint": "us-documentai.googleapis.com"}
    )


def _get_text(layout, full_text: str) -> str:
    """Extract text for a Document AI layout element using text anchor offsets."""
    text_parts = []
    for segment in layout.text_anchor.text_segments:
        start = int(segment.start_index)
        end = int(segment.end_index)
        text_parts.append(full_text[start:end])
    return "".join(text_parts).strip()


def _parse_docai_tables(document) -> dict:
    """
    Parse structured tables from a Document AI response.

    Returns a dict keyed by page number (1-based):
        {
          1: [
            {
              "headers": ["Parameter", "Sample Freq", "Sample Type", ...],
              "rows": [
                ["BOD", "2X Weekly", "24-Hr Composite", ...],
                ...
              ]
            },
            ...
          ],
          ...
        }

    Also attempts to identify the discharge limits table specifically and
    returns a flat list of row dicts under the key "discharge_limits_rows":
        [
          {
            "parameter": "BOD",
            "frequency": "2X Weekly",
            "sampleType": "24-Hr Composite",
            "season": None,
            ...raw cells...
          },
          ...
        ]
    """
    full_text = document.text
    tables_by_page: dict[int, list[dict]] = {}

    for page in document.pages:
        page_num = page.page_number  # 1-based
        page_tables = []
        for table in page.tables:
            # Extract header row
            headers: list[str] = []
            if table.header_rows:
                for cell in table.header_rows[0].cells:
                    headers.append(_get_text(cell.layout, full_text))

            # Extract body rows
            body_rows: list[list[str]] = []
            for row in table.body_rows:
                cells = [_get_text(cell.layout, full_text) for cell in row.cells]
                body_rows.append(cells)

            if headers or body_rows:
                page_tables.append({"headers": headers, "rows": body_rows})

        if page_tables:
            tables_by_page[page_num] = page_tables

    # --- Identify discharge limits table and map to structured rows ---
    discharge_rows: list[dict] = []
    for _page_num, page_tables in tables_by_page.items():
        for tbl in page_tables:
            headers_lower = [h.lower() for h in tbl["headers"]]
            # A discharge limits table has a "parameter" column and a freq/sample column
            has_param = any("param" in h for h in headers_lower)
            has_freq = any("freq" in h or "frequency" in h for h in headers_lower)
            if not (has_param or has_freq):
                continue

            # Map column indices
            col = {}
            for i, h in enumerate(headers_lower):
                if "param" in h and "col" not in col:
                    col["parameter"] = i
                elif "freq" in h or "frequency" in h:
                    col["frequency"] = i
                elif "sample type" in h or "type" in h and "sample" in " ".join(headers_lower):
                    col["sampleType"] = i
                elif "season" in h:
                    col["season"] = i
                elif "unit" in h:
                    col["unit"] = i
                elif "quantity" in h or "loading" in h:
                    col["quantity_unit"] = i
                elif "concent" in h or "quality" in h:
                    col["conc_unit"] = i

            for cells in tbl["rows"]:
                if not cells:
                    continue
                row_dict: dict = {"_raw_cells": cells, "_headers": tbl["headers"]}
                for field, idx in col.items():
                    row_dict[field] = cells[idx] if idx < len(cells) else None
                # Default parameter to first cell if column not identified
                if "parameter" not in row_dict:
                    row_dict["parameter"] = cells[0] if cells else None
                discharge_rows.append(row_dict)

    return {"by_page": tables_by_page, "discharge_limits_rows": discharge_rows}


_DOCAI_CHUNK_SIZE = 10  # Conservative chunk size; pages are compressed to JPEG before upload


def _split_pdf_bytes(pdf_bytes: bytes, chunk_size: int = _DOCAI_CHUNK_SIZE) -> list[tuple[str, int]]:
    """
    Split pdf_bytes into temp files of at most chunk_size pages each.

    Returns a list of (temp_file_path, page_offset) tuples where page_offset is
    the 0-based index of the first page in that chunk within the original PDF.
    Caller is responsible for deleting the temp files.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(pdf_bytes))
    total = len(reader.pages)
    chunks: list[tuple[str, int]] = []

    for start in range(0, total, chunk_size):
        writer = PdfWriter()
        for page_num in range(start, min(start + chunk_size, total)):
            writer.add_page(reader.pages[page_num])
        tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        writer.write(tmp)
        tmp.close()
        chunks.append((tmp.name, start))

    return chunks


def compress_pdf_chunk(pdf_path: str, dpi: int = 150, quality: int = 75) -> list[bytes]:
    """
    Render every page in a PDF chunk to a compressed JPEG and return the bytes.

    Uses pdf2image (poppler) when available; falls back to PyMuPDF.
    Returns a list of JPEG byte strings, one per page.
    """
    if not HAS_PIL:
        raise RuntimeError("Pillow not installed — pip install Pillow")

    images: list = []
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(pdf_path, dpi=dpi)
    except Exception as exc:
        if not HAS_PYMUPDF:
            raise RuntimeError(
                f"pdf2image failed ({exc}) and PyMuPDF is not installed — "
                "pip install pymupdf"
            ) from exc
        # PyMuPDF fallback
        doc = fitz.open(pdf_path)
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        for page in doc:
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            images.append(img)
        doc.close()

    jpeg_pages: list[bytes] = []
    for img in images:
        if img.mode != "RGB":
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        jpeg_pages.append(buf.getvalue())

    return jpeg_pages


def _process_docai_chunk(
    chunk_path: str,
    page_offset: int,
    chunk_idx: int,
    total_chunks: int,
    client,
    processor_name: str,
) -> tuple[list[dict], dict]:
    """
    Process one PDF chunk through Document AI by compressing each page to JPEG
    and sending them individually as image/jpeg.  This keeps individual request
    sizes well below Document AI's file-size limit.

    Page numbers in the output are globally correct (page_offset + local 1-based index).
    """
    jpeg_pages = compress_pdf_chunk(chunk_path)
    n_pages = len(jpeg_pages)
    total_bytes = sum(len(j) for j in jpeg_pages)
    print(
        f"[PDF] Document AI: chunk {chunk_idx}/{total_chunks} — "
        f"{n_pages} page(s), {total_bytes:,} bytes compressed "
        f"(pages {page_offset + 1}–{page_offset + n_pages})"
    )

    pages: list[dict] = []
    tables_by_page: dict = {}
    discharge_rows: list[dict] = []

    for local_idx, jpeg_bytes in enumerate(jpeg_pages):
        global_page = page_offset + local_idx + 1
        print(
            f"[PDF] Document AI: sending page {global_page} "
            f"({len(jpeg_bytes):,} bytes JPEG)"
        )
        raw_document = documentai.RawDocument(
            content=jpeg_bytes, mime_type="image/jpeg"
        )
        request = documentai.ProcessRequest(
            name=processor_name, raw_document=raw_document
        )
        result = client.process_document(request=request)
        document = result.document

        full_text = document.text

        # Extract page text (single-page response — document.pages[0])
        page_text_parts: list[str] = []
        if document.pages:
            for block in document.pages[0].blocks:
                t = _get_text(block.layout, full_text)
                if t:
                    page_text_parts.append(t)
        page_text = "\n".join(page_text_parts).strip()
        word_count = len(page_text.split())
        pages.append({
            "page": global_page,
            "text": page_text,
            "word_count": word_count,
            "method": "document_ai",
        })
        print(f"[PDF] Document AI: page {global_page} — {word_count} words")

        # Parse tables — remap page 1 (chunk-local) to global page number
        raw_tables = _parse_docai_tables(document)
        for _local_pg, tbl_list in raw_tables["by_page"].items():
            tables_by_page[global_page] = tbl_list
        discharge_rows.extend(raw_tables["discharge_limits_rows"])

    return pages, {"by_page": tables_by_page, "discharge_limits_rows": discharge_rows}


def _merge_docai_results(chunk_results: list[tuple[list[dict], dict]]) -> tuple[list[dict], dict]:
    """Merge pages and tables from multiple Document AI chunk results."""
    all_pages: list[dict] = []
    merged_by_page: dict = {}
    merged_discharge_rows: list[dict] = []

    for pages, tables in chunk_results:
        all_pages.extend(pages)
        merged_by_page.update(tables.get("by_page", {}))
        merged_discharge_rows.extend(tables.get("discharge_limits_rows", []))

    # Sort pages by global page number
    all_pages.sort(key=lambda p: p["page"])

    return all_pages, {
        "by_page": merged_by_page,
        "discharge_limits_rows": merged_discharge_rows,
    }


def extract_with_documentai(pdf_bytes: bytes, source_path: str = "") -> tuple[list[dict], dict]:
    """
    Extract text and structured tables from a PDF using Google Document AI.

    Automatically splits PDFs that exceed Document AI's 30-page limit into
    chunks of 25 pages, processes each chunk, then merges all results.
    Page numbers in the output are globally correct (1-based across the full PDF).

    Returns:
        (pages, tables)
        pages: list of {page, text, word_count, method}  — same shape as pdfplumber
        tables: dict with keys "by_page" and "discharge_limits_rows"
    """
    if not HAS_DOCAI:
        raise RuntimeError("google-cloud-documentai not installed — pip install google-cloud-documentai")

    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "").strip()
    location = os.environ.get("DOCUMENT_AI_LOCATION", "us").strip()
    processor_id = os.environ.get("DOCUMENT_AI_PROCESSOR_ID", "").strip()

    if not project_id or not processor_id:
        raise RuntimeError(
            "Document AI not configured — set GOOGLE_CLOUD_PROJECT_ID and "
            "DOCUMENT_AI_PROCESSOR_ID in .env"
        )

    client = _docai_client()
    processor_name = client.processor_path(project_id, location, processor_id)

    # Split into chunks to stay within Document AI's 30-page limit
    chunk_specs = _split_pdf_bytes(pdf_bytes, chunk_size=_DOCAI_CHUNK_SIZE)
    total_chunks = len(chunk_specs)
    print(
        f"[PDF] Document AI: {len(pdf_bytes):,} bytes → {total_chunks} chunk(s) "
        f"of ≤{_DOCAI_CHUNK_SIZE} pages (processor {processor_id})"
    )

    chunk_results: list[tuple[list[dict], dict]] = []
    tmp_files: list[str] = [path for path, _ in chunk_specs]

    try:
        for idx, (chunk_path, page_offset) in enumerate(chunk_specs, start=1):
            pages, tables = _process_docai_chunk(
                chunk_path, page_offset, idx, total_chunks, client, processor_name
            )
            chunk_results.append((pages, tables))
    finally:
        # Always clean up temp chunk files
        for tmp_path in tmp_files:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    all_pages, merged_tables = _merge_docai_results(chunk_results)
    total_words = sum(p["word_count"] for p in all_pages)
    print(
        f"[PDF] Document AI: {len(all_pages)} pages total, {total_words:,} words, "
        f"tables on {len(merged_tables['by_page'])} page(s), "
        f"{len(merged_tables['discharge_limits_rows'])} discharge-limit row(s)"
    )

    return all_pages, merged_tables


# ---------------------------------------------------------------------------
# Extraction method 3: Tesseract fallback
# ---------------------------------------------------------------------------

def _extract_with_pymupdf(pdf_bytes: bytes) -> list[dict]:
    if not HAS_PYMUPDF:
        raise RuntimeError("PyMuPDF (fitz) not installed")
    pages = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    print(f"[PDF] PyMuPDF: {total} pages detected")
    for i in range(total):
        page = doc[i]
        text = page.get_text()
        pages.append({
            "page": i + 1,
            "text": text.strip(),
            "word_count": len(text.split()),
            "method": "pymupdf",
        })
    doc.close()
    return pages


def extract_with_tesseract(pdf_bytes: bytes, dpi: int = 200) -> list[dict]:
    """Local Tesseract OCR. Used only when Document AI is unavailable."""
    if not HAS_TESSERACT:
        raise RuntimeError("pytesseract / Pillow not installed")
    if not HAS_PYMUPDF:
        raise RuntimeError("PyMuPDF required for Tesseract rendering — pip install pymupdf")

    pages = []
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    print(f"[PDF] Tesseract: {total} pages total — processing all pages")

    for i in range(total):
        page = doc[i]
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        text = pytesseract.image_to_string(img, lang="eng")
        word_count = len(text.split())
        pages.append({
            "page": i + 1,
            "text": text.strip(),
            "word_count": word_count,
            "method": "ocr_tesseract",
        })
        print(f"[PDF] Tesseract: page {i + 1}/{total} — {word_count} words")

    doc.close()
    return pages


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

def extract_text(
    pdf_bytes: bytes,
    force_docai: bool = False,
    source_path: str = "",
) -> dict:
    """
    Main extraction pipeline.

    1. pdfplumber — native text layer (always tried first unless force_docai).
    2. Document AI — Google Cloud OCR + table parsing (when pdfplumber returns
       sparse/empty text, or when force_docai=True).
    3. Tesseract  — local fallback when Document AI is not configured or fails.

    Returns a dict with keys:
        extractedAt, totalPages, totalWords, methodUsed, warnings, pages, tables

    pages: list of {page, text, word_count, method}
    tables: {by_page, discharge_limits_rows}  — populated by Document AI only;
            empty dict when using pdfplumber or Tesseract.
    """
    extracted_at = datetime.now(timezone.utc).isoformat()
    pages: list[dict] = []
    tables: dict = {}
    method_used = "unknown"
    warnings: list[str] = []

    # --- Step 1: native text layer ---
    if not force_docai:
        if HAS_PDFPLUMBER:
            try:
                pages = extract_with_pdfplumber(pdf_bytes)
                method_used = "pdfplumber"
            except Exception as exc:
                warnings.append(f"pdfplumber failed: {exc}")
                print(f"[PDF] pdfplumber error: {exc}")

        if not pages and HAS_PYMUPDF:
            try:
                pages = _extract_with_pymupdf(pdf_bytes)
                method_used = "pymupdf"
            except Exception as exc:
                warnings.append(f"pymupdf failed: {exc}")
                print(f"[PDF] pymupdf error: {exc}")

    # --- Step 2: OCR needed? ---
    need_ocr = force_docai or (pages and is_scanned(pages)) or (not pages)

    if need_ocr:
        reason = "force_docai=True" if force_docai else (
            "native extraction produced mostly empty pages" if pages else "no pages extracted"
        )
        print(f"[PDF] OCR needed ({reason}) — trying Document AI first")
        if pages:
            warnings.append(f"Native extraction returned sparse text; OCR fallback used ({reason}).")

        docai_ok = (
            HAS_DOCAI
            and os.environ.get("DOCUMENT_AI_PROCESSOR_ID", "").strip()
            and os.environ.get("GOOGLE_CLOUD_PROJECT_ID", "").strip()
        )

        if docai_ok:
            try:
                pages, tables = extract_with_documentai(pdf_bytes, source_path=source_path)
                method_used = "document_ai"
            except Exception as exc:
                warnings.append(f"Document AI failed: {exc}")
                print(f"[PDF] Document AI error: {exc}")
                print("[PDF] Falling back to Tesseract OCR")
                pages = []

        if not pages:
            # Tesseract fallback
            if not docai_ok:
                print("[PDF] WARNING: Document AI not configured — falling back to Tesseract OCR")
                warnings.append("Document AI not configured; Tesseract fallback used.")
            try:
                pages = extract_with_tesseract(pdf_bytes)
                method_used = "ocr_tesseract"
            except Exception as exc:
                warnings.append(f"Tesseract OCR failed: {exc}")
                print(f"[PDF] Tesseract error: {exc}")

    total_words = sum(p["word_count"] for p in pages)
    print(f"[PDF] Extracted {len(pages)} pages, {total_words:,} total words using {method_used}")

    return {
        "extractedAt": extracted_at,
        "totalPages": len(pages),
        "totalWords": total_words,
        "methodUsed": method_used,
        "warnings": warnings,
        "pages": pages,
        "tables": tables,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Extract text from a permit PDF")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url",  help="URL to download and extract")
    group.add_argument("--file", help="Local PDF file path")
    parser.add_argument("--output",      help="Optional JSON output file path")
    parser.add_argument("--force-docai", action="store_true",
                        help="Skip native extraction, use Document AI directly")
    args = parser.parse_args()

    if args.url:
        pdf_bytes = download_pdf(args.url)
        source = args.url
    else:
        pdf_bytes = load_local_pdf(args.file)
        source = args.file

    result = extract_text(pdf_bytes, force_docai=args.force_docai, source_path=source)
    result["source"] = source

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"[PDF] Extraction saved to {args.output}")
    else:
        if result["pages"]:
            preview = result["pages"][0]["text"][:500]
            print(f"\n--- Page 1 preview ---\n{preview}\n...")
        print(f"\nTotal pages: {result['totalPages']}, Total words: {result['totalWords']}")
        if result["tables"].get("discharge_limits_rows"):
            print(f"Discharge limit rows found: {len(result['tables']['discharge_limits_rows'])}")

    return result


if __name__ == "__main__":
    main()
