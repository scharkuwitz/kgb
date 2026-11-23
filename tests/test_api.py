"""Offline tests for kgb.api: real SQLite index and work folder in a temp dir, a toy embedder, every model call faked."""

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from kgb import api, config, extract, ingest, llm, meta, triage
from kgb.schema import ContractMeta, Item, PageItems, PageLabel
from kgb.store import DocRecord, Store

DIM = 4


def toy_embed(texts):
    """A query or item lies along axis 0 (shoes), 1 (chairs), 2 (pipes) or 3 (anything else)."""
    rows = np.zeros((len(texts), DIM), dtype=np.float32)
    for row, text in enumerate(texts):
        rows[row, next((axis for key, axis in (("гутал", 0), ("сандал", 1), ("муфт", 2)) if key in text), 3)] = 1
    return rows


def record(doc_id, names, axis, valid_to="2099-01-01"):
    items = [Item(name=n, code=f"{doc_id[:2]}{i}", unit="ш", unit_price=1000.0 * (i + 1), currency="MNT",
                  vat_included=True, search_text=n.lower(), page=2) for i, n in enumerate(names)]
    vectors = np.zeros((len(items), DIM), dtype=np.float32)
    vectors[:, axis] = 1
    meta_ = ContractMeta(doc_type="framework", contract_no=f"N-{doc_id}", supplier=f"Supplier {doc_id}",
                         valid_from="2025-01-01", valid_to=valid_to, terms_note="30 days payment")
    return DocRecord(doc_id, f"{doc_id}.pdf", 4, meta_, items, vectors)


class Calls:
    def __init__(self):
        self.triage = self.extract = self.meta = self.embed = 0


@pytest.fixture
def calls(monkeypatch):
    """Fakes for every model call the ingest pipeline makes (so an upload costs nothing and needs no key)."""
    c = Calls()

    def fake_triage(pages, **_):
        c.triage += 1
        return [PageLabel(page=p.no, label="items", reason="") for p in pages]

    def fake_extract_page(page, **_):
        c.extract += 1
        return PageItems(items=[Item(name="Ажлын бээлий", unit_price=5000.0, search_text="ажлын бээлий")])

    def fake_meta(pages, key_term_nos=None, **_):
        c.meta += 1
        return ContractMeta(doc_type="one_time", supplier="Шинэ ХХК", valid_to="2099-12-31")

    def fake_embed(texts):
        c.embed += 1
        return toy_embed(texts)

    monkeypatch.setattr(triage, "triage", fake_triage)
    monkeypatch.setattr(extract, "extract_page", fake_extract_page)
    monkeypatch.setattr(meta, "extract_meta", fake_meta)
    monkeypatch.setattr(llm, "embed", fake_embed)
    return c


@pytest.fixture
def st(tmp_path, monkeypatch, calls):
    monkeypatch.setattr(config, "EMBED_DIM", DIM)
    monkeypatch.setattr(config, "WORK", tmp_path / "work")
    monkeypatch.setattr(config, "API_KEY", "")
    store = Store(tmp_path / "kgb.sqlite")
    store.upsert(record("shoes", ["Хамгаалалтын гутал", "Өвлийн гутал"], 0))
    store.upsert(record("chairs", ["Оффисын сандал"], 1))
    store.upsert(record("pipes", ["Муфт ф20", "Муфт ф15"], 2, valid_to="2020-01-01"))
    return store


@pytest.fixture
def client(st):
    api.app.dependency_overrides[api.get_store] = lambda: st
    api.app.dependency_overrides[api.get_embedder] = lambda: toy_embed
    with TestClient(api.app) as c:
        yield c
    api.app.dependency_overrides.clear()


def pdf_bytes(pages=2, colour="white") -> bytes:
    images = [Image.new("RGB", (300, 400), colour) for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


# ---- status and search -----------------------------------------------------------------------


def test_health_reports_the_index(client):
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["counts"]["items"] == 5 and body["embed_dim"] == DIM


@pytest.mark.parametrize("mode", ["hybrid", "keyword", "semantic"])
def test_each_mode_finds_the_right_contract(client, mode):
    r = client.get("/search", params={"q": "хамгаалалтын гутал", "mode": mode, "k": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == mode and body["hits"][0]["doc_id"] == "shoes"
    hit = body["hits"][0]
    assert (hit["unit"], hit["vat_included"], hit["supplier"], hit["contract_no"], hit["valid_from"]) == \
        ("ш", True, "Supplier shoes", "N-shoes", "2025-01-01")


def test_keyword_mode_never_embeds(client):
    def boom(texts):
        raise AssertionError("keyword mode must not embed")

    api.app.dependency_overrides[api.get_embedder] = lambda: boom
    assert client.get("/search", params={"q": "сандал", "mode": "keyword"}).json()["hits"][0]["doc_id"] == "chairs"


def test_semantic_mode_uses_meaning_only(client):
    # "гутал" is nowhere in "Оффисын сандал", but the embedder places this query on the chairs axis
    hits = client.get("/search", params={"q": "суудал сандал", "mode": "semantic", "k": 1}).json()["hits"]
    assert hits[0]["doc_id"] == "chairs" and 0.99 < hits[0]["score"] <= 1.0001


def test_active_only_drops_expired_contracts(client):
    every = client.get("/search", params={"q": "муфт", "mode": "keyword"}).json()["hits"]
    assert every and all(h["expired"] for h in every)
    assert client.get("/search", params={"q": "муфт", "mode": "keyword", "active_only": True}).json()["hits"] == []


def test_batch_search_keeps_order_and_embeds_once(client):
    seen = []

    def counting(texts):
        seen.append(list(texts))
        return toy_embed(texts)

    api.app.dependency_overrides[api.get_embedder] = lambda: counting
    r = client.post("/search", json={"queries": ["оффисын сандал", "  ", "гутал"], "k": 1})
    assert r.status_code == 200
    assert [x["query"] for x in r.json()] == ["оффисын сандал", "  ", "гутал"]
    assert [x["hits"][0]["doc_id"] if x["hits"] else None for x in r.json()] == ["chairs", None, "shoes"]
    assert seen == [["оффисын сандал", "гутал"]]


def test_bad_search_requests_are_422(client):
    assert client.get("/search", params={"q": "гутал", "mode": "fuzzy"}).status_code == 422
    assert client.get("/search", params={"q": "гутал", "k": 0}).status_code == 422
    assert client.get("/search", params={"q": "гутал", "k": 51}).status_code == 422
    assert client.post("/search", json={"queries": []}).status_code == 422


def test_embedding_failure_is_503_and_points_to_keyword_mode(client):
    def no_key(texts):
        raise RuntimeError("OPENAI_API_KEY is not set")

    api.app.dependency_overrides[api.get_embedder] = lambda: no_key
    r = client.get("/search", params={"q": "гутал"})
    assert r.status_code == 503 and "mode=keyword" in r.json()["detail"]


# ---- lookups -----------------------------------------------------------------------------------


def test_item_and_contract_lookups(client):
    hit = client.get("/search", params={"q": "сандал", "mode": "keyword"}).json()["hits"][0]
    item = client.get(f"/items/{hit['item_id']}").json()
    assert (item["name"], item["search_text"], item["terms_note"], item["seq"]) == ("Оффисын сандал", "оффисын сандал", "30 days payment", 0)
    assert client.get("/items/999999").status_code == 404

    contracts = client.get("/contracts").json()
    assert [c["id"] for c in contracts] == ["chairs", "pipes", "shoes"]
    assert {c["id"]: c["expired"] for c in contracts} == {"chairs": False, "pipes": True, "shoes": False}
    shoes = client.get("/contracts/shoes").json()
    assert shoes["n_items"] == 2 and [i["name"] for i in shoes["items"]] == ["Хамгаалалтын гутал", "Өвлийн гутал"]
    assert client.get("/contracts/nope").status_code == 404


# ---- adding one PDF ----------------------------------------------------------------------------


def test_upload_runs_the_pipeline_once_and_a_repeat_is_cached(client, st, calls):
    data = pdf_bytes()
    r = client.post("/contracts", files={"file": ("new.pdf", data, "application/pdf")})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["cached"] is False and body["indexed"] is True and body["n_items"] == 2  # one item per page
    assert body["ran"] == ["triage", "extract:2", "meta", "embed"]
    assert (calls.triage, calls.extract, calls.meta, calls.embed) == (1, 2, 1, 1)
    assert (config.WORK / body["doc_id"] / "source.pdf").read_bytes() == data
    found = client.get("/search", params={"q": "бээлий", "mode": "keyword"}).json()["hits"]
    assert found and found[0]["supplier"] == "Шинэ ХХК"

    generation = st.counts()["generation"]
    again = client.post("/contracts", files={"file": ("renamed copy.pdf", data, "application/pdf")})
    assert again.status_code == 200
    assert again.json() | {"seconds": 0} == body | {"cached": True, "indexed": False, "ran": [], "llm_calls": 0, "seconds": 0}
    assert (calls.triage, calls.extract, calls.meta, calls.embed) == (1, 2, 1, 1)  # no model call the second time
    assert st.counts()["generation"] == generation  # and the index was not written


def test_an_upload_touches_no_other_document(client, st, calls):
    before = {d["id"]: d for d in st.documents()}
    body = client.post("/contracts", files={"file": ("new.pdf", pdf_bytes(1), "application/pdf")}).json()
    after = {d["id"]: d for d in st.documents()}
    assert set(after) - set(before) == {body["doc_id"]}
    assert all(after[k] == v for k, v in before.items())
    assert [p.name for p in config.WORK.iterdir()] == [body["doc_id"]]  # no other work folder was created or read


@pytest.mark.parametrize("name, data, code", [
    ("notes.txt", b"%PDF-1.4 but named txt", 415),
    ("fake.pdf", b"hello, not a pdf", 415),
    ("empty.pdf", b"", 400),
    ("broken.pdf", b"%PDF-1.4\n garbage that no renderer can open", 422),
])
def test_uploads_that_are_not_a_readable_pdf_are_refused_without_cost(client, calls, name, data, code):
    r = client.post("/contracts", files={"file": (name, data, "application/pdf")})
    assert r.status_code == code, r.text
    assert (calls.triage, calls.extract, calls.meta, calls.embed) == (0, 0, 0, 0)
    assert not config.WORK.exists() or not any(config.WORK.iterdir())  # no half folder for reindex to trip on


def test_an_oversized_upload_is_413(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0.0001)
    assert client.post("/contracts", files={"file": ("big.pdf", pdf_bytes(), "application/pdf")}).status_code == 413


def test_only_one_file_per_upload(client, calls):
    pdf = pdf_bytes(1)
    r = client.post("/contracts", files=[("file", ("a.pdf", pdf, "application/pdf")), ("file", ("b.pdf", pdf, "application/pdf"))])
    assert r.status_code == 400 and "exactly one" in r.json()["detail"]
    assert client.post("/contracts").status_code == 422  # no file at all
    assert calls.triage == 0


def test_a_failed_model_call_is_503_and_a_retry_resumes(client, calls, monkeypatch):
    def down(pages, key_term_nos=None, **_):
        raise ConnectionError("model unreachable")

    monkeypatch.setattr(meta, "extract_meta", down)
    data = pdf_bytes(1)
    r = client.post("/contracts", files={"file": ("x.pdf", data, "application/pdf")})
    assert r.status_code == 503 and "retry" in r.json()["detail"]
    monkeypatch.setattr(meta, "extract_meta", lambda pages, key_term_nos=None, **_: ContractMeta(doc_type="other"))
    retry = client.post("/contracts", files={"file": ("x.pdf", data, "application/pdf")}).json()
    assert retry["ran"] == ["meta", "embed"] and calls.triage == 1 and calls.extract == 1  # triage and OCR were kept


# ---- the API key -------------------------------------------------------------------------------


def test_api_key_is_enforced_when_set(client, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "s3cret")
    assert client.get("/health").status_code == 200  # always open
    assert client.get("/search", params={"q": "гутал"}).status_code == 401
    assert client.get("/search", params={"q": "гутал"}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/search", params={"q": "гутал"}, headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.post("/contracts", files={"file": ("a.pdf", pdf_bytes(1), "application/pdf")}).status_code == 401


def test_the_openapi_document_describes_every_route(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/health", "/search", "/items/{item_id}", "/contracts", "/contracts/{doc_id}"} <= set(paths)
    assert {"get", "post"} <= set(paths["/search"]) and {"get", "post"} <= set(paths["/contracts"])


# ---- upload size is enforced before the body is parsed ---------------------------------------


def test_a_declared_oversized_body_is_refused_before_parsing(client, monkeypatch, calls):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0.5)  # limit + multipart slack is about 0.56 MB
    big = b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024)
    r = client.post("/contracts", files={"file": ("big.pdf", big, "application/pdf")})
    assert r.status_code == 413 and "0.5 MB" in r.json()["detail"]
    assert calls.triage == 0


def test_a_streamed_body_stops_being_read_at_the_limit(monkeypatch):
    """No Content-Length (chunked upload): the guard counts bytes and stops reading once past the limit."""
    import asyncio

    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    chunk, total_chunks, reads = b"x" * (256 * 1024), 400, 0  # 100 MB offered in 256 KB pieces
    sent = []

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": chunk, "more_body": reads < total_chunks}

    async def send(message):
        sent.append(message)

    async def greedy_app(scope, receive, send):  # stands in for multipart parsing: reads everything
        while True:
            message = await receive()
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    scope = {"type": "http", "method": "POST", "path": "/contracts", "headers": [(b"content-type", b"multipart/form-data; boundary=b")]}
    asyncio.run(api.UploadSizeLimit(greedy_app)(scope, receive, send))
    assert sent[0]["status"] == 413
    assert reads <= 6  # 1 MB + slack is about 4.25 chunks; the other ~99 MB were never read


def test_an_app_error_after_the_limit_still_becomes_413(monkeypatch):
    """If the parser swallows the aborted read and answers 400 itself, the guard still answers 413."""
    import asyncio

    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0.1)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"x" * (512 * 1024), "more_body": True}

    async def send(message):
        sent.append(message)

    async def swallowing_app(scope, receive, send):
        try:
            await receive()
        except Exception:  # noqa: BLE001 - what a body parser that reports "error parsing the body" does
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"bad"})

    scope = {"type": "http", "method": "POST", "path": "/contracts", "headers": []}
    asyncio.run(api.UploadSizeLimit(swallowing_app)(scope, receive, send))
    assert [m.get("status") for m in sent if m["type"] == "http.response.start"] == [413]


def test_the_size_guard_leaves_other_routes_alone(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0.0001)
    assert client.post("/search", json={"queries": ["гутал"] * 50, "k": 1}).status_code == 200
