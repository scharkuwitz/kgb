"""Offline tests for kgb.meta (the model call is faked)."""

import io

from PIL import Image

from kgb import llm, meta
from kgb.schema import ContractMeta, Page


def _page(no, size=(2000, 2800)):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="JPEG")
    return Page(no=no, image=buf.getvalue())


def _fake(captured, result):
    def fake(prompt, schema, pages, model):
        captured.append([p.no for p in pages])
        captured.append(pages)
        return result

    return fake


def test_iso_accepts_only_real_dates():
    assert meta._iso("2026-09-21") == "2026-09-21"
    assert meta._iso(" 2026-09-21 ") == "2026-09-21"
    assert meta._iso("2026-13-40") is None
    assert meta._iso("2026 оны 9 сар") is None
    assert meta._iso(None) is None


def test_extract_meta_uses_first_three_key_pages_shrunk_and_cleans_dates(monkeypatch):
    seen = []
    result = ContractMeta(doc_type="framework", valid_from="2026-09-21", valid_to="soon", signed_date="2026-02-30")
    monkeypatch.setattr(llm, "ask_pages", _fake(seen, result))
    pages = [_page(i) for i in range(1, 8)]
    got = meta.extract_meta(pages, [2, 3, 5, 6], px=500)
    assert seen[0] == [2, 3, 5]
    with Image.open(io.BytesIO(seen[1][0].image)) as img:
        assert max(img.size) == 500
    assert (got.valid_from, got.valid_to, got.signed_date) == ("2026-09-21", None, None)
    assert got.doc_type == "framework"


def test_extract_meta_falls_back_to_first_pages(monkeypatch):
    seen = []
    monkeypatch.setattr(llm, "ask_pages", _fake(seen, ContractMeta(doc_type="other")))
    pages = [Page(no=i, text="t") for i in range(1, 8)]
    meta.extract_meta(pages, [])
    assert seen[0] == [1, 2, 3]
    meta.extract_meta(pages, [99])  # unknown page numbers are ignored, then the fallback applies
    assert seen[2] == [1, 2, 3]
