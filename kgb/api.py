"""HTTP API over the knowledge base: search items, look up items and contracts, add one PDF contract.

    uv run python -m kgb.api                 # serve on http://127.0.0.1:8000 (KGB_API_HOST / KGB_API_PORT)
                                             # interactive docs (Swagger UI): http://127.0.0.1:8000/docs
    uv run python -m kgb.api --selftest      # in-process requests against the real index, printed; no server

Routes (all JSON; shapes are the Pydantic models in `kgb.schema`):
    GET  /health                         index counts, embedding model, whether keys are set (never needs a key)
    GET  /search?q=..&mode=..&k=..       one query                      -> SearchResult
    POST /search  {"queries": [..]}      a whole purchase list, one embedding request -> list[SearchResult]
    GET  /items/{item_id}                one item with its contract     -> ItemDetail
    GET  /contracts                      every indexed document         -> list[ContractSummary]
    GET  /contracts/{doc_id}             one document with all items    -> ContractDetail
    POST /contracts  (multipart, file)   exactly one PDF through the whole pipeline -> IngestResult

Modes: hybrid (default; keyword and meaning fused), keyword (no OpenAI call) and semantic (meaning only).
Expired contracts are returned with `expired: true` unless `active_only` is set.

Adding a contract runs `kgb.ingest.ingest_pdf`, the same function as `python -m kgb.ingest --pdf <file>`: a PDF
seen before (same bytes, any name) is answered from its work folder with no model call (`cached: true`); a new one
takes about a minute (triage, OCR of the item pages, contract facts, embeddings) and touches no other document.

Concurrency: one process, one shared `Store` (thread-safe; the vector matrix is loaded once). Plain `def` routes run
on the server's thread pool; the upload route hands the slow pipeline to that pool too, so searches keep flowing.
"""

import os
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, Security, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from kgb import config, ingest, search
from kgb.schema import ContractDetail, ContractSummary, ItemDetail, Mode, SearchResult
from kgb.store import Store

MAX_QUERIES = 200  # per POST /search; a purchase list rarely has more lines


# ---- dependencies (tests override these) ---------------------------------------------------


def get_store() -> Store:
    """The process-wide Store for config.DB (shared with `kgb.search`, so the vector matrix is loaded once)."""
    return search._store(config.DB)


def get_embedder() -> search.Embedder | None:
    """None = the real embedding model (`llm.embed`)."""
    return None


_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Needed only when KGB_API_KEY is set")


def require_key(key: Annotated[str | None, Security(_key_header)]) -> None:
    if config.API_KEY and key != config.API_KEY:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong X-API-Key")


StoreDep = Annotated[Store, Depends(get_store)]
EmbedDep = Annotated[search.Embedder | None, Depends(get_embedder)]


class EmbeddingUnavailable(RuntimeError):
    """The query could not be embedded (no OpenAI key, network, quota)."""


def _guarded(embed: search.Embedder | None) -> search.Embedder:
    real = embed or search._embedder(None)

    def run(texts):
        try:
            return real(texts)
        except Exception as e:  # turned into a 503 that points at keyword mode, which needs no embedding
            raise EmbeddingUnavailable(f"{type(e).__name__}: {e}") from e

    return run


# ---- request / response shapes that exist only at the HTTP edge ------------------------------


class SearchRequest(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=MAX_QUERIES,
                               description="Item descriptions in Mongolian Cyrillic, e.g. one line per purchase request item",
                               examples=[["хамгаалалтын гутал", "муфт 20", "ус халаагч"]])
    mode: Mode = "hybrid"
    k: int = Field(10, ge=1, le=search.POOL, description="Hits per query")
    active_only: bool = Field(False, description="Leave out items whose contract has expired")


class IngestResult(BaseModel):
    doc_id: str = Field(description="Document id: first 12 hex digits of the file's SHA-256")
    filename: str = Field(description="The name the document was first ingested under")
    n_pages: int
    n_items: int
    cached: bool = Field(description="True when the document was known: no model call, nothing re-read")
    indexed: bool = Field(description="True when the index changed (the document was added or its content changed)")
    ran: list[str] = Field(description="Pipeline stages that did work, e.g. triage, extract:7, meta, embed")
    llm_calls: int = Field(description="Model calls made meanwhile (includes other uploads running at the same time)")
    seconds: float


class Health(BaseModel):
    status: str = Field(description="ok, or empty when the index holds no items yet")
    index: str
    counts: dict[str, int]
    embed_model: str
    embed_dim: int
    openai_key: bool = Field(description="Whether OPENAI_API_KEY is set (needed by semantic/hybrid search and uploads)")
    api_key_required: bool


# ---- the app ------------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = app.dependency_overrides.get(get_store, get_store)()
    started = time.perf_counter()
    n = await run_in_threadpool(store.warm)  # load the vector matrix before the first query
    print(f"kgb.api: index {store.path.resolve()} ({n} vectors loaded in {time.perf_counter() - started:.2f}s)", flush=True)
    yield


DESCRIPTION = """
Knowledge base of supplier contracts: **is this item already covered by a contract? With whom, at what price,
and is the contract still valid?**

* Queries are item descriptions in **Mongolian Cyrillic** (e.g. `хамгаалалтын гутал`, `муфт 20`). Codes are matched exactly.
* `mode`: `hybrid` (default, best), `keyword` (substring match; words under 3 characters are ignored; no OpenAI call),
  `semantic` (meaning only).
* Each hit carries the item (name, code, spec, unit, unit/list price, currency, VAT), the contract (supplier,
  contract number, validity, `expired`) and the source page.
* For a purchase list, send all lines at once to `POST /search`.
* `POST /contracts` adds exactly one PDF. A PDF already ingested returns at once from cache.
"""

app = FastAPI(title="KGB contract item search", version="0.1.0", description=DESCRIPTION, lifespan=lifespan)
protected = [Depends(require_key)]


@app.exception_handler(EmbeddingUnavailable)
def _embedding_unavailable(request: Request, exc: EmbeddingUnavailable):
    return JSONResponse(status_code=503, content={
        "detail": f"the query could not be embedded ({exc}); mode=keyword works without the embedding model"})


@app.get("/health", response_model=Health, tags=["status"])
def health(store: StoreDep) -> Health:
    counts = store.counts()
    return Health(status="ok" if counts["items"] else "empty", index=str(store.path.resolve()), counts=counts,
                  embed_model=config.EMBED_MODEL, embed_dim=config.EMBED_DIM,
                  openai_key=bool(os.environ.get("OPENAI_API_KEY")), api_key_required=bool(config.API_KEY))


@app.get("/search", response_model=SearchResult, dependencies=protected, tags=["search"])
def search_one(
    store: StoreDep,
    embed: EmbedDep,
    q: Annotated[str, Query(description="Item description, Mongolian Cyrillic", examples=["хамгаалалтын гутал"])],
    mode: Mode = "hybrid",
    k: Annotated[int, Query(ge=1, le=search.POOL)] = 10,
    active_only: bool = False,
) -> SearchResult:
    """The best matching items for one query."""
    hits = search.search(q, k, store, _guarded(embed), mode=mode, active_only=active_only)
    return SearchResult(query=q, mode=mode, hits=hits)


@app.post("/search", response_model=list[SearchResult], dependencies=protected, tags=["search"])
def search_batch(body: SearchRequest, store: StoreDep, embed: EmbedDep) -> list[SearchResult]:
    """Several queries (a purchase list) with one embedding request. Result i belongs to queries[i]."""
    batch = search.search_many(body.queries, body.k, store, _guarded(embed), mode=body.mode, active_only=body.active_only)
    return [SearchResult(query=q, mode=body.mode, hits=hits) for q, hits in zip(body.queries, batch)]


@app.get("/items/{item_id}", response_model=ItemDetail, dependencies=protected, tags=["lookup"])
def get_item(item_id: int, store: StoreDep) -> ItemDetail:
    """One item by the `item_id` of a search hit, with its contract."""
    found = store.item(item_id)
    if found is None:
        raise HTTPException(404, f"no item {item_id} (it may have been removed by a re-ingest)")
    return found


@app.get("/contracts", response_model=list[ContractSummary], dependencies=protected, tags=["lookup"])
def list_contracts(store: StoreDep) -> list[ContractSummary]:
    """Every indexed document with its contract facts and item count."""
    return [ContractSummary(**row) for row in store.documents()]


@app.get("/contracts/{doc_id}", response_model=ContractDetail, dependencies=protected, tags=["lookup"])
def get_contract(doc_id: str, store: StoreDep) -> ContractDetail:
    """One document with its contract facts and all of its items in document order."""
    found = store.document(doc_id)
    if found is None:
        raise HTTPException(404, f"no contract {doc_id!r}")
    return found


_REJECTED_STATUS = {"type": 415, "size": 413, "empty": 400, "unreadable": 422}


@app.post("/contracts", response_model=IngestResult, dependencies=protected, tags=["ingest"],
          status_code=status.HTTP_201_CREATED,
          responses={200: {"model": IngestResult, "description": "Already ingested: answered from the cache"},
                     400: {"description": "Empty file or not exactly one file"}, 413: {"description": "Too large"},
                     415: {"description": "Not a PDF"}, 422: {"description": "The PDF cannot be read"},
                     503: {"description": "A model call failed (e.g. OPENAI_API_KEY missing); finished stages are kept"}})
async def add_contract(
    request: Request,
    response: Response,
    store: StoreDep,
    file: Annotated[UploadFile, File(description="One contract as a PDF")],
) -> IngestResult:
    """Add exactly one PDF contract: triage, OCR of the item pages, contract facts, embeddings, index.

    Takes about a minute for a new document. The same PDF again (under any name) is answered from the cache with
    no model call (status 200, `cached: true`). Other documents are not touched."""
    form = await request.form()  # already parsed by FastAPI; inspect it to refuse more than one file
    uploads = [value for _, value in form.multi_items() if not isinstance(value, str)]
    if len(uploads) != 1:
        raise HTTPException(400, f"send exactly one file, not {len(uploads)}")
    data = await file.read()
    try:
        report = await run_in_threadpool(ingest.ingest_pdf, data, file.filename or "upload.pdf", store)
    except ingest.RejectedFile as e:
        raise HTTPException(_REJECTED_STATUS.get(e.reason, 400), str(e)) from e
    except ingest.IncompleteError as e:
        raise HTTPException(500, str(e)) from e
    except Exception as e:  # a model call failed: finished stages are cached, a retry resumes
        raise HTTPException(503, f"ingest failed, a retry resumes where it stopped: {type(e).__name__}: {e}") from e
    if report.cached:
        response.status_code = status.HTTP_200_OK
    return IngestResult(doc_id=report.doc_id, filename=report.filename, n_pages=report.n_pages, n_items=report.n_items,
                        cached=report.cached, indexed=report.indexed, ran=report.ran, llm_calls=report.llm_calls,
                        seconds=round(report.seconds, 2))


# ---- probe ---------------------------------------------------------------------------------


def _selftest() -> int:
    """Real index, real embedding model, in-process HTTP: what a client would see. Returns the number of failures."""
    from fastapi.testclient import TestClient

    failures = 0

    def show(method: str, url: str, r, note: str = "") -> None:
        nonlocal failures
        ok = r.status_code < 400
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {method:<4} {url:<44} -> {r.status_code}  {note}")

    headers = {"X-API-Key": config.API_KEY} if config.API_KEY else {}
    with TestClient(app, headers=headers) as client:
        r = client.get("/health")
        show("GET", "/health", r, str(r.json().get("counts")))
        if not r.json()["counts"]["items"]:
            print("the index is empty: run `uv run python -m kgb.ingest data/contracts` first")
            return failures + 1

        queries = [q for q, _, _ in search.EXPECTED]
        first_hit = None
        for mode in search.MODES:
            started = time.perf_counter()
            r = client.post("/search", json={"queries": queries, "mode": mode, "k": 10})
            ms = (time.perf_counter() - started) * 1000
            if r.status_code >= 400:
                show("POST", f"/search mode={mode}", r, r.text[:120])
                continue
            results = r.json()
            ranks = []
            for (query, marker, code), result in zip(search.EXPECTED, results):
                rank = next((n for n, h in enumerate(result["hits"], 1)
                             if marker in (h["filename"] or "") and (code is None or h["code"] == code)), None)
                ranks.append(rank)
            first_hit = first_hit or (results[0]["hits"] or [None])[0]
            show("POST", f"/search mode={mode} ({len(queries)} queries)", r,
                 f"{ms:6.0f} ms  expected contract found {sum(x is not None for x in ranks)}/{len(ranks)}, ranks {ranks}")

        r = client.get("/search", params={"q": "муфт 20", "mode": "keyword", "k": 3})
        show("GET", "/search?q=муфт 20&mode=keyword&k=3", r, ", ".join(f"{h['code']} {h['name']}" for h in r.json()["hits"]))
        if first_hit:
            r = client.get(f"/items/{first_hit['item_id']}")
            j = r.json()
            show("GET", f"/items/{first_hit['item_id']}", r, f"{j['name']} | {j['unit_price']} {j['currency']} | {j['supplier']}")
        r = client.get("/contracts")
        show("GET", "/contracts", r, f"{len(r.json())} documents")
        doc_id = r.json()[0]["id"]
        r = client.get(f"/contracts/{doc_id}")
        show("GET", f"/contracts/{doc_id}", r, f"{r.json()['filename']}: {len(r.json()['items'])} items")
        r = client.get("/items/999999999")
        print(f"{'ok  ' if r.status_code == 404 else 'FAIL'} GET  /items/999999999 (missing)                 -> {r.status_code}")
        failures += r.status_code != 404

        r = client.post("/contracts", files={"file": ("notes.txt", b"not a pdf", "text/plain")})
        print(f"{'ok  ' if r.status_code == 415 else 'FAIL'} POST /contracts with a .txt (must be refused)   -> {r.status_code}  {r.json()['detail']}")
        failures += r.status_code != 415
        # a PDF that is already in the index must come back from the cache, with no model call
        known = next((p for p in sorted((config.DATA / "contracts").glob("*.pdf"))), None)
        if known:
            started = time.perf_counter()
            r = client.post("/contracts", files={"file": (known.name, known.read_bytes(), "application/pdf")})
            j = r.json()
            ok = r.status_code == 200 and j.get("cached") and j.get("llm_calls") == 0
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} POST /contracts {known.name[:28]:<28} -> {r.status_code}  "
                  f"cached={j.get('cached')} llm_calls={j.get('llm_calls')} {(time.perf_counter() - started) * 1000:.0f} ms")
    print(f"\n{'all requests behaved' if not failures else f'{failures} FAILED'}")
    return failures


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(1 if _selftest() else 0)

    import uvicorn

    base = f"http://{'127.0.0.1' if config.API_HOST in ('0.0.0.0', '::') else config.API_HOST}:{config.API_PORT}"
    print(f"kgb.api: Swagger UI {base}/docs   OpenAPI {base}/openapi.json   "
          f"API key {'required (X-API-Key)' if config.API_KEY else 'not required'}", flush=True)
    print(f'  curl "{base}/search?q=муфт%2020&mode=keyword"', flush=True)
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)
