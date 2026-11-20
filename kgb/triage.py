"""Cheap first pass: which pages hold items, which hold key terms, which are noise?

Pages are shrunk to thumbnails (config.TRIAGE_PX) and sent in one call per batch.
The bias is toward recall: a false 'items' only costs one extra extraction call,
a missed item page loses data silently.

    labels = triage(pages)                 # one PageLabel per page, in page order
    nos = item_pages(labels, pages)        # page numbers to extract, with a fallback

Probe (calls the API; expected item pages are checked for the four samples when the file name ends in A-D):
    uv run python -m kgb.triage "data/contracts/[Sample] Contract #B.pdf"
"""

from kgb import config, llm
from kgb.load import is_blank, shrink
from kgb.schema import Page, PageLabel, TriageResult

BATCH = 30  # pages per call
FALLBACK_PAGES = 5  # when no item page is found: the last N non-blank pages

PROMPT = """You are sorting the pages of a supplier contract document (scans, any language, often Mongolian).
Give every page below exactly one label:

- items: the page lists goods or services that can be purchased: price lists, tables of products or
  services with prices, product specification sheets, price quotes, or an annex that enumerates items.
  It does not matter whether the annex is called Appendix 1, 2 or anything else, or where it sits in the document.
- key_terms: the page states the contract number, the parties, dates or validity, or the key commercial terms
  (payment, delivery, warranty, discount) in a table or summary list.
- other: everything else: standard boilerplate clauses, signatures, certificates, lists of affiliated
  companies, brochures, blank pages.

When unsure whether a page lists items, choose items. Return one entry per page, using the page numbers shown."""


def triage(pages: list[Page], model: str = config.TRIAGE_MODEL, px: int = config.TRIAGE_PX) -> list[PageLabel]:
    """One PageLabel per page, in order. Pages the model skipped are labelled 'items' (recall bias)."""
    got: dict[int, PageLabel] = {}
    for start in range(0, len(pages), BATCH):
        batch = [shrink(p, px) for p in pages[start : start + BATCH]]
        result = llm.ask_pages(PROMPT, TriageResult, batch, model=model)
        valid = {p.no for p in batch}
        for label in result.pages:
            if label.page in valid:
                got.setdefault(label.page, label)
    return [got.get(p.no) or PageLabel(page=p.no, label="items", reason="not labelled by the model") for p in pages]


def item_pages(labels: list[PageLabel], pages: list[Page]) -> list[int]:
    """Page numbers to extract. If triage found none, fall back to the last non-blank pages."""
    nos = [x.page for x in labels if x.label == "items"]
    if nos:
        return nos
    tail = [p.no for p in pages if not is_blank(p)]
    return tail[-FALLBACK_PAGES:]


def key_term_pages(labels: list[PageLabel], limit: int = 3) -> list[int]:
    """The first few key_terms pages (the metadata call only needs those)."""
    return [x.page for x in labels if x.label == "key_terms"][:limit]


# Expected item pages in the four samples, used only by the probe.
EXPECTED = {"A": {11, 12}, "B": set(range(10, 23)), "C": set(range(11, 19)), "D": {6, 7}}


if __name__ == "__main__":
    import sys
    import time
    from pathlib import Path

    from kgb.load import load

    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    pages = load(path)
    started = time.perf_counter()
    labels = triage(pages)
    print(f"{len(pages)} pages triaged in {time.perf_counter() - started:.1f}s")
    for x in labels:
        print(f"  p{x.page:02d}  {x.label:<9} {x.reason}")
    found = set(item_pages(labels, pages))
    print("item pages:", sorted(found), "| key_terms:", key_term_pages(labels))
    expected = EXPECTED.get(path.stem[-1].upper())
    if expected:
        missed, extra = sorted(expected - found), sorted(found - expected)
        print(f"expected {sorted(expected)}: " + ("OK, nothing missed" if not missed else f"MISSED {missed}") + (f", extra {extra}" if extra else ""))
