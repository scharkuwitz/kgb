"""Offline tests for kgb.extract (no API calls)."""

import io

from PIL import Image

from kgb import extract, llm
from kgb.extract import fill_forward
from kgb.load import strips
from kgb.schema import Item, Page, PageItems


def _item(name, **kw):
    return Item(name=name, search_text=kw.pop("search_text", name), **kw)


def test_category_carries_across_pages_until_a_new_header():
    per_page = {
        17: PageItems(items=[_item("Уян ф15", category="Уян холбоос")], open_category="Уян холбоос"),
        18: PageItems(
            items=[_item("Double lin уян"), _item("Угаалтуурын сифон", category="дагалдах")],
            open_category="дагалдах",
        ),
        19: PageItems(items=[_item("төмөр сифон")]),
    }
    items = fill_forward(per_page)
    assert [(i.page, i.category) for i in items] == [
        (17, "Уян холбоос"), (18, "Уян холбоос"), (18, "дагалдах"), (19, "дагалдах"),
    ]


def test_continued_rows_inherit_name_and_brand_from_the_row_above():
    per_page = {
        17: PageItems(items=[_item("Уян ф15", brand="Kc", code="804")]),
        18: PageItems(items=[
            _item("", continued=True, code="805", search_text="уян холбоос 120см"),
            _item("", continued=True, code="806", search_text="Уян ф15 150см"),
            _item("Double lin", code="813"),
        ]),
    }
    items = fill_forward(per_page)
    assert [i.name for i in items] == ["Уян ф15", "Уян ф15", "Уян ф15", "Double lin"]
    assert [i.brand for i in items] == ["Kc", "Kc", "Kc", None]
    assert items[1].search_text == "Уян ф15 уян холбоос 120см"  # inherited name added for search
    assert items[2].search_text == "Уян ф15 150см"  # already there, not duplicated


def test_empty_name_without_continuation_falls_back_to_category():
    per_page = {5: PageItems(items=[_item("", category="Уян холбоос", spec="60см")])}
    (item,) = fill_forward(per_page)
    assert item.name == "Уян холбоос"


def test_vat_and_currency_carry_forward_but_explicit_values_win():
    per_page = {
        10: PageItems(items=[_item("a", vat_included=True, currency="MNT")]),
        11: PageItems(items=[_item("b"), _item("c", vat_included=False)]),
    }
    items = fill_forward(per_page)
    assert [(i.vat_included, i.currency) for i in items] == [(True, "MNT"), (True, "MNT"), (False, "MNT")]


def test_vat_stated_only_at_the_end_is_backfilled():
    per_page = {
        11: PageItems(items=[_item("a"), _item("b")]),
        13: PageItems(items=[_item("c", vat_included=True), _item("d")]),
    }
    assert [i.vat_included for i in fill_forward(per_page)] == [True, True, True, True]


def test_fill_forward_does_not_mutate_input():
    original = _item("", continued=True)
    per_page = {1: PageItems(items=[_item("x")]), 2: PageItems(items=[original])}
    fill_forward(per_page)
    assert original.name == "" and original.page is None


def test_mixed_script_words_flags_only_intra_word_switches():
    ok = PageItems(items=[_item("Double lin уян ф15", spec="ф50 2м", brand="Ledeme"), _item("kv22-300 труба")])
    bad = PageItems(items=[_item("сифон", category="дагaldax")], open_category="дагaldax")
    assert extract.mixed_script_words(ok) == 0
    assert extract.mixed_script_words(bad) == 2


def test_fix_lookalikes_only_touches_mostly_cyrillic_words():
    assert extract.fix_lookalikes("Цоргoтой халаагч") == "Цорготой халаагч"  # Latin o inside Cyrillic word
    assert extract.fix_lookalikes("Double lin уян ф15") == "Double lin уян ф15"
    assert extract.fix_lookalikes("KCG-D3021 BL") == "KCG-D3021 BL"
    assert extract.fix_lookalikes(None) is None
    (item,) = fill_forward({1: PageItems(items=[_item("Цоргoтой", category="Бойлуурa")])})
    assert (item.name, item.category, item.search_text) == ("Цорготой", "Бойлуура", "Цорготой")


def test_glitched_page_is_retried_once_and_cleaner_result_kept(monkeypatch):
    results = iter([
        PageItems(items=[_item("сифон", category="дагaldax")]),
        PageItems(items=[_item("сифон", category="дагалдах")]),
    ])
    monkeypatch.setattr(llm, "ask", lambda *a, **k: next(results))
    assert extract.extract_page(Page(no=1, text="x")).items[0].category == "дагалдах"


def test_persistent_glitch_falls_back_to_other_detail_level(monkeypatch):
    glitched = PageItems(items=[_item("сифон", category="дагaldah")])
    clean = PageItems(items=[_item("сифон", category="дагалдах")])
    details = []

    def fake(*a, detail=None, **k):
        details.append(detail)
        return clean if detail == "high" else glitched

    monkeypatch.setattr(llm, "ask", fake)
    page = Page(no=1, image=_jpeg())
    assert extract.extract_page(page, detail="original").items[0].category == "дагалдах"
    assert details == ["original", "original", "high"]


def _jpeg():
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buf, format="JPEG")
    return buf.getvalue()


def test_strips_overlap_and_cover_the_page():
    img = Image.new("RGB", (100, 1000), "white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    parts = strips(Page(no=1, image=buf.getvalue()), 2, overlap=0.1)
    heights = [Image.open(io.BytesIO(p)).size[1] for p in parts]
    assert heights == [600, 600]  # 500 + 100 overlap each


def test_extract_pages_keeps_page_order_and_passes_options(monkeypatch):
    calls = []

    def fake(prompt, schema, images=None, text=None, model=None, mime=None, detail=None):
        calls.append((text, detail))
        return PageItems(items=[_item(text)])

    monkeypatch.setattr(llm, "ask", fake)
    pages = [Page(no=i, text=f"t{i}") for i in range(1, 6)]
    result = extract.extract_pages(pages, [4, 2, 2], workers=3)
    assert list(result) == [2, 4]  # duplicates dropped, page order kept
    assert [r.items[0].name for r in result.values()] == ["t2", "t4"]
    assert len(calls) == 2
