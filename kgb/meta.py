"""Contract-level facts (parties, dates, one note) from the first few key-terms pages.

Deliberately cheap and low precision: one call, the cheap model, small page images, no retries.
Items are what the base is for; this only feeds the expiry check and a human-readable note,
and both tolerate an occasional miss.

    meta = extract_meta(pages, key_term_nos)     # ContractMeta; dates are valid ISO strings or None

Probe (calls the API; without page numbers it runs triage first; checks the dates of samples A-D):
    uv run python -m kgb.meta "data/contracts/[Sample] Contract #D.pdf"
    uv run python -m kgb.meta "data/contracts/[Sample] Contract #D.pdf" 1 2
"""

from datetime import date

from kgb import config, llm
from kgb.load import shrink
from kgb.schema import ContractMeta, Page

MAX_PAGES = 3  # key-terms pages sent; they sit at the front of a contract
FALLBACK_PAGES = 3  # when triage found no key-terms page: the first N pages

PROMPT = """These are the first pages of a supplier contract (scans or text; usually Mongolian).
Fill in the contract-level facts. Use null for anything you cannot see; do not guess.

- doc_type: "framework" for a master / long-term / framework agreement, "one_time" for a single purchase.
- contract_no, supplier, supplier_reg_no, signed_date.
- supplier is the seller / contractor ("Нийлүүлэгч", "Гүйцэтгэгч"), with its own registry number.
  The buyer ("Худалдан авагч", "Захиалагч") is always Макс Импэкс ХХК (Max Impex; also spelled
  Макс Импекс). Never return it as supplier; use the other party.
- valid_from / valid_to: the period the contract (and its prices) is valid, as YYYY-MM-DD.
  If only a duration is given ("2 жил", "6 сар"), add it to the start date to get valid_to.
  "2026 оны 09 дүгээр сарын 21" is 2026-09-21.
- terms_note: at most 3 short sentences on payment, delivery, warranty, discount."""


def _iso(value: str | None) -> str | None:
    """The value if it is a real YYYY-MM-DD date, else None."""
    try:
        return date.fromisoformat(value.strip()).isoformat() if value else None
    except ValueError:
        return None


def extract_meta(
    pages: list[Page],
    key_term_nos: list[int] | None = None,
    model: str = config.META_MODEL,
    px: int = config.META_PX,
) -> ContractMeta:
    """One call over the first MAX_PAGES of `key_term_nos` (default and fallback: the first pages)."""
    by_no = {p.no: p for p in pages}
    chosen = [by_no[n] for n in (key_term_nos or []) if n in by_no][:MAX_PAGES] or pages[:FALLBACK_PAGES]
    meta = llm.ask_pages(PROMPT, ContractMeta, [shrink(p, px) for p in chosen], model=model)
    return meta.model_copy(update={f: _iso(getattr(meta, f)) for f in ("signed_date", "valid_from", "valid_to")})


# Expected validity periods of the four samples, used only by the probe.
EXPECTED = {
    "A": ("2026-09-21", "2027-03-21"),
    "B": ("2026-04-01", "2028-04-01"),
    "C": ("2026-09-10", "2028-09-10"),
    "D": ("2026-06-23", "2028-06-23"),
}


if __name__ == "__main__":
    import sys
    import time
    from pathlib import Path

    from kgb.load import load

    if len(sys.argv) < 2:
        sys.exit(__doc__)
    path = Path(sys.argv[1])
    pages = load(path)
    nos = [int(a) for a in sys.argv[2:]]
    if not nos:
        from kgb.triage import key_term_pages, triage

        nos = key_term_pages(triage(pages))
        print("triage key_terms pages:", nos)
    started = time.perf_counter()
    meta = extract_meta(pages, nos)
    print(f"meta in {time.perf_counter() - started:.1f}s from pages {nos or 'first ' + str(FALLBACK_PAGES)}")
    print(meta.model_dump_json(indent=1))
    expected = EXPECTED.get(path.stem[-1].upper())
    if expected:
        got = (meta.valid_from, meta.valid_to)
        print(f"expected {expected[0]} -> {expected[1]}: " + ("OK" if got == expected else f"MISMATCH, got {got[0]} -> {got[1]}"))
