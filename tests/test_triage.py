"""Offline tests for kgb.triage (the model call is faked)."""

import io

from PIL import Image

from kgb import llm, triage
from kgb.load import is_blank, shrink
from kgb.schema import Page, PageLabel, TriageResult


def _img(color, size=(300, 400), noise=False):
    img = Image.new("RGB", size, color)
    if noise:
        for x in range(0, size[0], 4):
            for y in range(0, size[1], 40):
                img.putpixel((x, y), (0, 0, 0))
        for x in range(0, size[0], 2):
            for y in range(1, size[1], 2):
                img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_shrink_and_is_blank():
    blank, busy = Page(no=1, image=_img("white")), Page(no=2, image=_img("white", noise=True))
    assert is_blank(blank) and not is_blank(busy)
    with Image.open(io.BytesIO(shrink(busy, 100).image)) as img:
        assert max(img.size) == 100
    assert is_blank(Page(no=3, text="  ")) and not is_blank(Page(no=4, text="x"))


def test_triage_batches_and_fills_missing_pages_as_items(monkeypatch):
    pages = [Page(no=i, text=f"page {i}") for i in range(1, triage.BATCH + 6)]
    seen = []

    def fake(prompt, schema, batch, model):
        seen.append([p.no for p in batch])
        # the model skips page 2 and invents page 999
        labels = [PageLabel(page=p.no, label="other", reason="r") for p in batch if p.no != 2]
        return TriageResult(pages=labels + [PageLabel(page=999, label="items", reason="r")])

    monkeypatch.setattr(llm, "ask_pages", fake)
    labels = triage.triage(pages)
    assert [len(b) for b in seen] == [triage.BATCH, 5]
    assert [x.page for x in labels] == [p.no for p in pages]
    assert labels[1].label == "items" and labels[0].label == "other"


def test_item_pages_fallback_uses_last_non_blank_pages():
    pages = [Page(no=i, text="" if i == 9 else "text") for i in range(1, 11)]
    none = [PageLabel(page=p.no, label="other", reason="") for p in pages]
    assert triage.item_pages(none, pages) == [5, 6, 7, 8, 10]
    some = [*none[:2], PageLabel(page=3, label="items", reason=""), *none[3:]]
    assert triage.item_pages(some, pages) == [3]


def test_key_term_pages_limit():
    labels = [PageLabel(page=i, label="key_terms", reason="") for i in range(1, 6)]
    assert triage.key_term_pages(labels) == [1, 2, 3]
