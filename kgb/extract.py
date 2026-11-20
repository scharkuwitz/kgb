"""Item pages -> items. One vision call per page, pages in parallel, then a pure fill-forward pass.

    per_page = extract_pages(pages, [10, 11, 12])     # {page_no: PageItems}
    items = fill_forward(per_page)                    # list[Item], page set, carried-over cells filled

Dense tables can be sent as overlapping strips (split=2) or at full resolution (detail="original").

Probe (calls the API; without page numbers it runs triage first):
    uv run python -m kgb.extract "data/contracts/[Sample] Contract #D.pdf"
    uv run python -m kgb.extract "data/contracts/[Sample] Contract #B.pdf" 11 17 18 --split 2 --detail high
"""

import re
from concurrent.futures import ThreadPoolExecutor

from kgb import config, llm
from kgb.load import strips
from kgb.schema import Item, Page, PageItems

PROMPT = """This is page {no} of a supplier contract (a scan or text; usually in Mongolian).
Extract every purchasable item on it: each priced table row, each product sheet, each listed service.

Rules:
- One entry per row or item, in reading order. Do not skip, merge or invent rows.
- Copy names, codes and specs verbatim (keep Cyrillic and Latin as written; never switch alphabet
  inside a word).
- name: the item's own name. On a product sheet, the product name without headings such as
  "ҮНИЙН САНАЛ" (price offer) or "мэдээлэл".
- A merged cell spanning several rows applies to each of them: repeat its text on every row it covers.
- At the top of the page, a name or brand cell can be empty because a merged cell that started on the
  previous page continues here (no top border at the page edge). Then leave it empty (name "") and set
  continued=true. Any other empty name cell: name "" and continued=null.
- category: the section header (a full-width title row inside the table, or a heading over the list) the
  row falls under. null when no header on this page applies; it is carried over from the previous page.
  It names a product group (e.g. "Уян холбоос", "Суултуур"). Never use a company name, logo banner,
  document title, brand, country or any other column value; a list without product-group headers has
  category null throughout.
- brand: the brand / manufacturer column or label, when there is one.
- open_category: the section header still in effect at the bottom of the page, or null.
- code: the supplier code / article column, or the model number on a product sheet.
- spec: size, dimensions, color, variant, standard, taken from a size/spec column or from labelled spec
  lines. Do not copy parts of the name into spec; null when that column is empty.
- unit_price: the per-unit price actually payable, after any discount the page shows. list_price: the
  price before that discount, only when both are shown. Plain numbers: "1,906,300.00" -> 1906300.
- vat_included: true if the price text on this page says VAT is included, false if it says excluded,
  otherwise null (it is carried over between pages). The note can be in a column heading, next to the
  price, or in a total row (e.g. "НИЙЛБЭР ДҮН /НӨАТ-той/"); it applies to every row of that table.
  In Mongolian "НӨАТ-тай / -тэй / -той / -төй / -ай / -эй / -ой", "НӨАТ орсон" mean WITH VAT (true);
  "НӨАТ-гүй", "НӨАТ ороогүй" mean WITHOUT VAT (false).
- currency: ISO code ("төг", "₮", "төгрөг" -> MNT). If none is stated in a Mongolian document, MNT.
- Ignore page headers and footers, logos, stamps, signatures and total rows.
{continued_hint}"""

STRIPS_NOTE = """The page is given as {n} overlapping horizontal strips, top to bottom. Rows in the overlap
appear twice: list each row only once."""


_MIXED_WORD = re.compile(r"[а-яёөү][a-z]|[a-z][а-яёөү]", re.IGNORECASE)


def mixed_script_words(result: PageItems) -> int:
    """Count words that switch between Cyrillic and Latin letters mid-word ("дагaldax").
    This is a decoding glitch, not something scans contain."""
    fields = [x for i in result.items for x in (i.name, i.category, i.brand, i.spec)] + [result.open_category]
    return sum(len(_MIXED_WORD.findall(f)) for f in fields if f)


_LOOKALIKE = str.maketrans("aceopxyACEHKMOPTXBY", "асеорхуАСЕНКМОРТХВУ")  # Latin -> same-looking Cyrillic
_WORD = re.compile(r"\w+")


def fix_lookalikes(text: str | None) -> str | None:
    """In words that are mostly Cyrillic, replace Latin letters that look Cyrillic ("Цоргoтой" -> "Цорготой").
    Mostly-Latin words and codes are left alone."""
    if not text:
        return text

    def fix(m: re.Match) -> str:
        word = m.group()
        cyr = sum("а" <= c.lower() <= "я" or c.lower() in "ёөү" for c in word)
        lat = sum("a" <= c.lower() <= "z" for c in word)
        return word.translate(_LOOKALIKE) if cyr and lat and cyr > lat else word

    return _WORD.sub(fix, text)


def extract_page(page: Page, model: str = config.EXTRACT_MODEL, split: int = 1, detail: str = config.EXTRACT_DETAIL) -> PageItems:
    """Items of one page. `page` field of each item is left for fill_forward to set.
    A result with mixed-script words is retried, first with the same settings, then at the other
    image detail level (the glitch depends on the rendering); the cleanest result is kept."""
    result = _extract_once(page, model, split, detail)
    for retry_detail in (detail, "high" if detail != "high" else "original"):
        if not mixed_script_words(result):
            break
        retry = _extract_once(page, model, split, retry_detail)
        if mixed_script_words(retry) < mixed_script_words(result):
            result = retry
    return result


def _extract_once(page: Page, model: str, split: int, detail: str) -> PageItems:
    if page.image is not None and split > 1:
        prompt = PROMPT.format(no=page.no, continued_hint=STRIPS_NOTE.format(n=split))
        return llm.ask(prompt, PageItems, images=strips(page, split), model=model, mime="image/jpeg", detail=detail)
    prompt = PROMPT.format(no=page.no, continued_hint="")
    if page.image is not None:
        return llm.ask(prompt, PageItems, images=[page.image], model=model, mime=page.mime, detail=detail)
    return llm.ask(prompt, PageItems, text=page.text, model=model)


def extract_pages(
    pages: list[Page],
    nos: list[int],
    model: str = config.EXTRACT_MODEL,
    split: int = 1,
    detail: str = config.EXTRACT_DETAIL,
    workers: int = config.WORKERS,
) -> dict[int, PageItems]:
    """Extract the pages numbered `nos` in parallel. Returns {page_no: PageItems} in page order."""
    by_no = {p.no: p for p in pages}
    todo = sorted(set(nos))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(todo)))) as pool:
        results = pool.map(lambda no: extract_page(by_no[no], model=model, split=split, detail=detail), todo)
        return dict(zip(todo, results))


def fill_forward(per_page: dict[int, PageItems]) -> list[Item]:
    """Pure. Walk pages in order and
    - set `page` on every item,
    - give items without a category the section header in effect (carried across pages),
    - give items without vat_included / currency the last value seen (price headers sit on the first
      page of a table only); items before the first known value get that first value (a VAT note in a
      total row at the end of a table),
    - fill empty name/brand of `continued` rows from the row above (merged cells across page breaks),
    - name any still-nameless row after its category (or spec / code) so nothing is left blank."""
    out: list[Item] = []
    category: str | None = None
    carried: dict = {"vat_included": None, "currency": None}
    for no in sorted(per_page):
        page_items = per_page[no]
        for item in page_items.items:
            update: dict = {"page": no}
            for field in ("name", "category", "brand", "spec", "search_text"):
                value = getattr(item, field)
                if fix_lookalikes(value) != value:
                    update[field] = fix_lookalikes(value)
            item = item.model_copy(update=update)
            for field in carried:
                value = getattr(item, field)
                if value is not None:
                    carried[field] = value
                elif carried[field] is not None:
                    update[field] = carried[field]
            name = item.name.strip()
            if item.continued and out:
                above = out[-1]
                if not name:
                    name = above.name
                    update["name"] = name
                    if name.lower() not in item.search_text.lower():
                        update["search_text"] = f"{name} {item.search_text}".strip()
                if not item.brand and above.brand:
                    update["brand"] = above.brand
            if item.category:
                category = item.category
            elif category:
                update["category"] = category
            if not name:
                update["name"] = update.get("category") or item.category or item.spec or item.code or ""
            out.append(item.model_copy(update=update))
        if page_items.open_category:
            category = fix_lookalikes(page_items.open_category)
    for field in carried:  # backfill leading gaps with the first known value
        first = next((getattr(i, field) for i in out if getattr(i, field) is not None), None)
        if first is not None:
            for k, item in enumerate(out):
                if getattr(item, field) is not None:
                    break
                out[k] = item.model_copy(update={field: first})
    return out


if __name__ == "__main__":
    import argparse
    import json
    import time
    from pathlib import Path

    from kgb.load import load

    parser = argparse.ArgumentParser(description="Extract items from a document (probe).")
    parser.add_argument("file")
    parser.add_argument("pages", nargs="*", type=int, help="page numbers; default: triage decides")
    parser.add_argument("--split", type=int, default=1)
    parser.add_argument("--detail", default=config.EXTRACT_DETAIL, choices=["low", "high", "auto", "original"])
    args = parser.parse_args()

    path = Path(args.file)
    pages = load(path)
    nos = args.pages
    if not nos:
        from kgb.triage import item_pages, triage

        nos = item_pages(triage(pages), pages)
        print("triage item pages:", nos)
    started = time.perf_counter()
    per_page = extract_pages(pages, nos, split=args.split, detail=args.detail)
    items = fill_forward(per_page)
    print(f"{len(items)} items from {len(nos)} pages in {time.perf_counter() - started:.1f}s "
          f"(split={args.split}, detail={args.detail})")
    for no, page_items in per_page.items():
        print(f"  p{no:02d}: {len(page_items.items)} items, open_category={page_items.open_category!r}")
    for item in items:
        price = f"{item.unit_price:,.0f}" if item.unit_price is not None else "-"
        flag = " [cont]" if item.continued else ""
        print(f"  p{item.page:02d} {item.code or '':<10} {item.name[:38]:<38} {(item.spec or '')[:20]:<20} "
              f"{price:>12} {item.currency or ''} | {item.category or ''}{flag}")
    out = config.DATA / "probe" / "extract" / f"{path.stem}.split{args.split}.{args.detail}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([i.model_dump() for i in items], ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", out)
