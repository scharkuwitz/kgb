"""Data shapes passed between modules. No logic, no I/O.

`Page` is internal (it carries raw bytes). Everything else is a Pydantic model, so it
can be used as an LLM structured-output schema and dumped to JSON for artifacts,
the CLI and AI agents.

LLM-facing models only use `X | None = None` defaults. OpenAI strict structured
outputs require every field, and None defaults are the only ones they accept.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Page:
    """One page of a source document: a rendered image (scans, PDFs, photos) or text (docx, xlsx, csv)."""

    no: int  # 1-based page number, as a human would cite it ("p11")
    image: bytes | None = None  # encoded image (see `mime`)
    text: str | None = None
    mime: str = "image/jpeg"

    def __repr__(self) -> str:
        body = f"image={len(self.image) // 1024}KB" if self.image else f"text={len(self.text or '')}chars"
        return f"Page(no={self.no}, {body})"


# ---- triage ----------------------------------------------------------------

Label = Literal["items", "key_terms", "other"]


class PageLabel(BaseModel):
    page: int = Field(description="1-based page number")
    label: Label
    reason: str = Field(description="A few words on why")


class TriageResult(BaseModel):
    pages: list[PageLabel]


# ---- extraction ------------------------------------------------------------


class Item(BaseModel):
    """One purchasable line: a table row, a product sheet, or a service."""

    name: str = Field(description="Item name exactly as written; empty string if the name cell is empty")
    continued: bool | None = Field(
        None,
        description="true only when this row's empty name or brand cell belongs to a merged cell "
        "that started on the previous page; otherwise null",
    )
    code: str | None = Field(None, description="Supplier item code or model number, verbatim")
    brand: str | None = None
    category: str | None = Field(None, description="Section header or product type the item belongs to")
    spec: str | None = Field(None, description="Size, dimensions, color, variant, standard")
    unit: str | None = Field(None, description="Unit of measure, e.g. ш, м, хүн")
    unit_price: float | None = Field(None, description="Price per unit actually payable (after any discount shown), as a plain number")
    list_price: float | None = Field(None, description="Price per unit before discount; only when the page shows both")
    currency: str | None = Field(None, description="ISO code, e.g. MNT")
    vat_included: bool | None = None
    search_text: str = Field(
        description="Normalized Mongolian Cyrillic description for search: generic product type, "
        "name, key spec and brand. Render Latin-transliterated Mongolian words in Cyrillic, "
        "expand abbreviations, keep model and part codes verbatim."
    )
    page: int | None = Field(None, description="Set by the program; return null")


class PageItems(BaseModel):
    items: list[Item]
    open_category: str | None = Field(
        None, description="Category or section header still in effect at the bottom of the page"
    )


DocType = Literal["framework", "one_time", "other"]


class ContractMeta(BaseModel):
    """Contract-level facts. Low precision on purpose; only the dates are used for filtering."""

    doc_type: DocType = Field(description="framework = master/MSA agreement; one_time = single purchase")
    contract_no: str | None = None
    supplier: str | None = Field(None, description="Supplier / contractor legal name")
    supplier_reg_no: str | None = Field(None, description="Supplier state registration number")
    signed_date: str | None = Field(None, description="YYYY-MM-DD")
    valid_from: str | None = Field(None, description="YYYY-MM-DD")
    valid_to: str | None = Field(None, description="YYYY-MM-DD")
    terms_note: str | None = Field(None, description="At most 3 sentences: payment, delivery, warranty, discount")


# ---- search and check ------------------------------------------------------


class Hit(BaseModel):
    """One search result: an item plus the contract it came from."""

    item_id: int
    score: float
    name: str
    code: str | None = None
    brand: str | None = None
    category: str | None = None
    spec: str | None = None
    unit_price: float | None = None
    currency: str | None = None
    doc_id: str | None = None
    filename: str | None = None
    supplier: str | None = None
    contract_no: str | None = None
    valid_to: str | None = None
    expired: bool | None = None
    page: int | None = None
    page_image: str | None = None  # path to the rendered source page, for checking by eye


Status = Literal["covered", "similar", "none"]


class Verdict(BaseModel):
    query: str
    status: Status
    match_ids: list[int] = Field(description="item_id of the matching candidates")
    reason: str = Field(description="Short reason, in Mongolian")


if __name__ == "__main__":
    import json

    for model in (TriageResult, PageItems, ContractMeta, Verdict):
        print(f"--- {model.__name__}")
        print(json.dumps(model.model_json_schema(), ensure_ascii=False, indent=1)[:1500])
