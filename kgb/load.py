"""Any supported file -> list[Page]. Pure: no files written, no network.

Scans, PDFs and photos become image pages (JPEG, long side = config.RENDER_PX).
docx, xlsx and csv become text pages (tables as markdown), chunked so that one
page fits one LLM call comfortably.

Probe:
    uv run python -m kgb.load "data/contracts/[Sample] Contract #D.pdf"            # summary only
    uv run python -m kgb.load "data/contracts/[Sample] Contract #D.pdf" out_dir    # also dump pages to out_dir
"""

import csv
import io
import threading
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageSequence

from kgb import config
from kgb.schema import Page

JPEG_QUALITY = 90
TEXT_PAGE_CHARS = 6000  # docx: max characters per text page
SHEET_PAGE_ROWS = 80  # xlsx/csv: max data rows per text page


def load(path: str | Path) -> list[Page]:
    """Dispatch on file extension. Raises ValueError for unsupported types."""
    path = Path(path)
    loader = LOADERS.get(path.suffix.lower())
    if loader is None:
        raise ValueError(f"Unsupported file type '{path.suffix}'. Supported: {', '.join(sorted(LOADERS))}")
    return loader(path)


# ---- images ----------------------------------------------------------------


def _encode(img: Image.Image, px: int) -> bytes:
    """Scale so the long side is px, then encode as JPEG."""
    img = img.convert("RGB")
    scale = px / max(img.size)
    if abs(scale - 1) > 0.01:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# PDFium is not thread-safe, not even across different documents
# (https://pypdfium2.readthedocs.io/en/stable/python_api.html#incompatibility-with-threading), so every PDFium
# call in the process goes through this one lock. Concurrent uploads and `ingest --jobs` render one PDF at a
# time; the slow parts (model calls) still run in parallel.
PDFIUM_LOCK = threading.Lock()


def load_pdf(path: Path, px: int = config.RENDER_PX) -> list[Page]:
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            pages = []
            for i in range(len(pdf)):
                page = pdf[i]
                scale = px / max(page.get_size())  # page size is in points (1/72 inch)
                img = page.render(scale=scale).to_pil()  # may share the bitmap's buffer, so encode under the lock too
                pages.append(Page(no=i + 1, image=_encode(img, px)))
                page.close()
            return pages
        finally:
            pdf.close()


def load_image(path: Path, px: int = config.RENDER_PX) -> list[Page]:
    """Single images and multi-page TIFFs."""
    with Image.open(path) as img:
        return [Page(no=i + 1, image=_encode(frame, px)) for i, frame in enumerate(ImageSequence.Iterator(img))]


# ---- text documents ----------------------------------------------------------


def _md_table(rows: list[list[str]]) -> str:
    """Rows of cell strings -> markdown table (first row is the header)."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    clean = [[str(c).replace("|", "/").replace("\n", " ").strip() for c in r] + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(clean[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in clean[1:]]
    return "\n".join(lines)


def _chunk_blocks(blocks: list[str], limit: int = TEXT_PAGE_CHARS) -> list[Page]:
    """Greedy packing of text blocks into pages of at most ~limit characters."""
    pages, current, size = [], [], 0
    for block in blocks:
        if current and size + len(block) > limit:
            pages.append("\n\n".join(current))
            current, size = [], 0
        current.append(block)
        size += len(block)
    if current:
        pages.append("\n\n".join(current))
    return [Page(no=i + 1, text=t) for i, t in enumerate(pages)]


def load_docx(path: Path) -> list[Page]:
    """Paragraphs and tables in document order. A single table is never split across pages."""
    import docx
    from docx.table import Table

    document = docx.Document(str(path))
    blocks = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            blocks.append(_md_table([[cell.text for cell in row.cells] for row in block.rows]))
        elif block.text.strip():
            blocks.append(block.text.strip())
    return _chunk_blocks(blocks)


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _sheet_pages(title: str, rows: list[list[str]], rows_per_page: int) -> list[str]:
    """One sheet -> markdown chunks. The first non-empty row is repeated as the header of every chunk."""
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(max((i + 1 for i, c in enumerate(r) if c), default=0) for r in rows)  # drop empty right columns
    rows = [r[:width] for r in rows]
    header, body = rows[0], rows[1:]
    if not body:
        return [f"## Sheet: {title}\n\n" + _md_table([header])]
    chunks = []
    for start in range(0, len(body), rows_per_page):
        part = body[start : start + rows_per_page]
        span = f"rows {start + 2}-{start + 1 + len(part)}"
        chunks.append(f"## Sheet: {title} ({span})\n\n" + _md_table([header, *part]))
    return chunks


def load_xlsx(path: Path, rows_per_page: int = SHEET_PAGE_ROWS) -> list[Page]:
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        texts = []
        for sheet in workbook.worksheets:
            rows = [[_cell(v) for v in row] for row in sheet.iter_rows(values_only=True)]
            texts += _sheet_pages(sheet.title, rows, rows_per_page)
    finally:
        workbook.close()
    return [Page(no=i + 1, text=t) for i, t in enumerate(texts)]


def load_csv(path: Path, rows_per_page: int = SHEET_PAGE_ROWS) -> list[Page]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [[c.strip() for c in row] for row in csv.reader(f)]
    return [Page(no=i + 1, text=t) for i, t in enumerate(_sheet_pages(path.stem, rows, rows_per_page))]


# ---- page helpers -----------------------------------------------------------


def shrink(page: Page, px: int) -> Page:
    """A smaller copy of an image page (long side = px), e.g. for triage. Text pages are returned as is."""
    if page.image is None:
        return page
    with Image.open(io.BytesIO(page.image)) as img:
        return Page(no=page.no, image=_encode(img, px), mime="image/jpeg")


def strips(page: Page, n: int, overlap: float = 0.06) -> list[bytes]:
    """Cut an image page into n horizontal strips, top to bottom, each overlapping the next by
    `overlap` of the page height. Used to give dense tables more pixels per row."""
    with Image.open(io.BytesIO(page.image)) as img:
        img = img.convert("RGB")
        width, height = img.size
        step, extra = height / n, round(height * overlap)
        out = []
        for i in range(n):
            top, bottom = max(0, round(i * step) - extra), min(height, round((i + 1) * step) + extra)
            buf = io.BytesIO()
            img.crop((0, top, width, bottom)).save(buf, format="JPEG", quality=JPEG_QUALITY)
            out.append(buf.getvalue())
        return out


def is_blank(page: Page, threshold: float = 6.0) -> bool:
    """True for an empty page: no text, or an image with almost no tonal variation (scan noise included)."""
    if page.image is None:
        return not (page.text or "").strip()
    with Image.open(io.BytesIO(page.image)) as img:
        small = img.convert("L").resize((200, 280))
    pixels = list(small.getdata())
    mean = sum(pixels) / len(pixels)
    return (sum((p - mean) ** 2 for p in pixels) / len(pixels)) ** 0.5 < threshold


LOADERS: dict[str, Callable[[Path], list[Page]]] = {
    ".pdf": load_pdf,
    ".png": load_image,
    ".jpg": load_image,
    ".jpeg": load_image,
    ".tif": load_image,
    ".tiff": load_image,
    ".webp": load_image,
    ".docx": load_docx,
    ".xlsx": load_xlsx,
    ".xlsm": load_xlsx,
    ".csv": load_csv,
}


def dump(pages: list[Page], out_dir: str | Path) -> list[Path]:
    """Write pages to out_dir as 001.jpg / 001.md for viewing. Returns the written paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for page in pages:
        if page.image is not None:
            target = out_dir / f"{page.no:03d}.jpg"
            target.write_bytes(page.image)
        else:
            target = out_dir / f"{page.no:03d}.md"
            target.write_text(page.text or "", encoding="utf-8")
        written.append(target)
    return written


if __name__ == "__main__":
    import sys
    import time

    if len(sys.argv) < 2:
        sys.exit(__doc__)
    started = time.perf_counter()
    pages = load(sys.argv[1])
    print(f"{len(pages)} pages in {time.perf_counter() - started:.1f}s")
    for page in pages:
        if page.image is not None:
            with Image.open(io.BytesIO(page.image)) as img:
                print(f"  p{page.no:03d}  image {img.width}x{img.height}  {len(page.image) // 1024} KB")
        else:
            first = (page.text or "").splitlines()[0][:70] if page.text else ""
            print(f"  p{page.no:03d}  text  {len(page.text or '')} chars  | {first}")
    if len(sys.argv) > 2:
        written = dump(pages, sys.argv[2])
        print(f"wrote {len(written)} files to {Path(sys.argv[2]).resolve()}")
