"""Offline tests for kgb.load (no API key, no network)."""

import io
from pathlib import Path

import pytest
from PIL import Image

from kgb import config
from kgb.load import load

SAMPLE_D = Path(__file__).resolve().parent.parent / "data" / "contracts" / "[Sample] Contract #D.pdf"


@pytest.mark.skipif(not SAMPLE_D.exists(), reason="sample contract D not present")
def test_pdf_renders_every_page_as_jpeg():
    pages = load(SAMPLE_D)
    assert [p.no for p in pages] == list(range(1, 11))  # D has 10 pages
    for page in pages:
        assert page.text is None and page.image[:3] == b"\xff\xd8\xff"  # JPEG magic
        with Image.open(io.BytesIO(page.image)) as img:
            assert max(img.size) == config.RENDER_PX


def test_image_file(tmp_path):
    path = tmp_path / "scan.png"
    Image.new("RGB", (400, 800), "white").save(path)
    (page,) = load(path)
    with Image.open(io.BytesIO(page.image)) as img:
        assert img.size == (config.RENDER_PX // 2, config.RENDER_PX)


def test_docx_keeps_paragraphs_and_tables_in_order(tmp_path):
    import docx

    document = docx.Document()
    document.add_paragraph("Хавсралт №2")
    table = document.add_table(rows=2, cols=3)
    for cell, text in zip(table.rows[0].cells, ["Код", "Нэр", "Үнэ"]):
        cell.text = text
    for cell, text in zip(table.rows[1].cells, ["101", "хоишоо хаусл", "231000"]):
        cell.text = text
    document.add_paragraph("Төгсгөл")
    path = tmp_path / "contract.docx"
    document.save(path)

    (page,) = load(path)
    text = page.text
    assert text.index("Хавсралт №2") < text.index("| Код | Нэр | Үнэ |") < text.index("Төгсгөл")
    assert "| 101 | хоишоо хаусл | 231000 |" in text


def test_xlsx_chunks_rows_and_repeats_header(tmp_path):
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Үнэ"
    sheet.append(["Код", "Нэр", "Үнэ"])
    for i in range(200):
        sheet.append([1000 + i, f"бараа {i}", 1500.0])
    path = tmp_path / "prices.xlsx"
    workbook.save(path)

    pages = load(path)
    assert len(pages) == 3  # 200 rows / 80 per page
    assert all(p.text.count("| Код | Нэр | Үнэ |") == 1 for p in pages)
    assert "(rows 2-81)" in pages[0].text and "(rows 162-201)" in pages[2].text
    assert "| 1000 | бараа 0 | 1500 |" in pages[0].text  # float 1500.0 printed as 1500


def test_csv(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text("Нэр,Тоо\nсандал,2\n", encoding="utf-8")
    (page,) = load(path)
    assert "| сандал | 2 |" in page.text


def test_unsupported_type(tmp_path):
    with pytest.raises(ValueError, match="Unsupported file type"):
        load(tmp_path / "old.doc")


def test_pdfium_is_never_entered_by_two_threads_at_once(tmp_path, monkeypatch):
    """PDFium is not thread-safe even across documents: concurrent load_pdf calls must take turns."""
    import threading
    import time

    import pypdfium2
    from PIL import Image

    from kgb import load

    inside, peak, guard = 0, 0, threading.Lock()

    def enter():
        nonlocal inside, peak
        with guard:
            inside += 1
            peak = max(peak, inside)

    def leave():
        nonlocal inside
        with guard:
            inside -= 1

    class FakeBitmap:
        def to_pil(self):
            return Image.new("RGB", (20, 30), "white")

    class FakePage:
        def get_size(self):
            return (200.0, 300.0)

        def render(self, scale):
            time.sleep(0.01)  # wide enough for an unguarded second thread to arrive
            return FakeBitmap()

        def close(self):
            pass

    class FakeDoc:
        def __init__(self, path):
            enter()

        def __len__(self):
            return 3

        def __getitem__(self, i):
            return FakePage()

        def close(self):
            leave()

    monkeypatch.setattr(pypdfium2, "PdfDocument", FakeDoc)
    results = []
    threads = [threading.Thread(target=lambda n=n: results.append(load.load_pdf(tmp_path / f"{n}.pdf", px=50))) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 6 and all(len(pages) == 3 for pages in results)
    assert peak == 1
