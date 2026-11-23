# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

KGB is a lightweight knowledge base of supplier contracts (mostly scanned Mongolian PDFs, plus docx/xlsx/images). The question it answers: "Is this item already covered by a contract? With whom, at what price, and is the contract still valid?" It is built in small milestones (M0–M8), each checkable on its own. Done so far: config, load, llm, triage, extract, meta, store, ingest, search (M0–M7) and the HTTP API `api` (M8a, FastAPI). Not yet written (sidelined): `check`, `cli`, an MCP server. Do not assume those modules exist.

## Commands

Uses [uv](https://docs.astral.sh/uv/) (Python 3.13 pinned in `.python-version`, `requires-python >=3.12`).

```bash
uv sync                                  # install deps
cp .env.example .env                     # then set OPENAI_API_KEY
uv run python -m kgb.config              # print active settings (key shown as set/MISSING)
uv run pytest -q                         # all tests; offline, no API key or network
uv run pytest tests/test_extract.py -q   # one file
uv run pytest tests/test_extract.py -k fill_forward -q   # one test by name
```

There is no linter or formatter configured. Every module is also a CLI probe (the ones marked "API" call OpenAI and cost money):

```bash
uv run python -m kgb.load "data/contracts/doc.pdf" [out_dir]     # pages + sizes; optionally dump them
uv run python -m kgb.llm ask "<file>" <page_no>                  # API: label one page
uv run python -m kgb.llm embed "text one" "text two"             # API: embedding shape/similarity
uv run python -m kgb.triage "<file>"                             # API: label all pages
uv run python -m kgb.extract "<file>" [pages] [--split N] [--detail high|original]   # API: items table + JSON in data/probe/extract
uv run python -m kgb.meta "<file>" [pages]                       # API: parties + validity dates; checks samples A-D
uv run python -m kgb.ingest "<file|folder>"... [--jobs N]        # API on first run, none on a repeat: work dir + index
uv run python -m kgb.ingest --pdf "<file.pdf>"                   # API unless cached: one PDF, exactly what POST /contracts runs
uv run python -m kgb.ingest --reindex                            # rebuild data/kgb.sqlite from data/work, no API
uv run python -m kgb.ingest --export <dest.sqlite>               # consistent single-file copy of the index (VACUUM INTO)
uv run python -m kgb.store [fts "<words>"]                       # SQLite/FTS5 check, counts, documents; keyword lookup
uv run python -m kgb.search ["<query>"...] [--mode hybrid|keyword|semantic] [--active] [-k N]   # API: six acceptance queries x 3 modes, or yours; keyword needs no API
uv run python -m kgb.api                                         # serve on 127.0.0.1:8000, Swagger UI at /docs (KGB_API_HOST/PORT/KEY)
uv run python -m kgb.api --selftest                              # API (embeddings): in-process requests against the real index
```

Source documents go in `data/contracts/` (git-ignored; real sample files named `[Sample] Contract #A..D.pdf`). The triage probe checks results against the expected item pages of those four samples (`triage.EXPECTED`).

## Architecture

Pipeline: `load → triage → extract → fill_forward → extract_meta → embed → store → search → (check)`, all but `search` and the parenthesised part driven by `ingest`. The parenthesised step is planned, not built; the approved design is in the auto-memory notes (`kgb-design-doc`, `kgb-milestones`).

Dependency graph (acyclic): `config`, `schema` ← everything; `load` ← schema; `llm` ← config, schema; `triage`, `extract` ← llm, load, schema; `meta` ← llm, load.shrink, schema; `store` ← config, schema; `search` ← config, schema, store (`llm.embed` imported late); `ingest` ← all of the above except `search`; `api` ← config, schema, store, search, ingest.

Seams that must be preserved:
- **Only `kgb/llm.py` imports `openai`.** It uses the Responses API (`responses.parse(text_format=...)`) with strict structured outputs, `store=False`, and a cached thread-safe client. Pydantic models in `schema.py` double as the LLM output schemas.
- **Only `kgb/load.py` knows file formats.** `load(path) -> list[Page]` dispatches through the `LOADERS` registry. A `Page` (frozen dataclass in `schema.py`) is either an image (scans/PDFs/photos, JPEG) or text (docx/xlsx/csv); `Page.no` is 1-based, as a human cites it. PDFium is not thread-safe even across documents, so all PDFium use goes through `load.PDFIUM_LOCK` (concurrent uploads and `ingest --jobs` render one PDF at a time).
- **Settings come only from `kgb/config.py`** (`KGB_*` env vars, optionally `.env`; real env vars win). Add new knobs there and in `.env.example`.
- **Only `kgb/api.py` imports FastAPI/uvicorn.** It is a thin HTTP edge: every route calls `search`, `Store` or `ingest.ingest_pdf` and returns the Pydantic models of `schema.py` (`SearchResult`, `Hit`, `ItemDetail`, `ContractSummary`, `ContractDetail`). The store and embedder come from the dependencies `get_store`/`get_embedder` (tests override them). Embedding failures become 503 pointing at `mode=keyword`; the ASGI guard `UploadSizeLimit` refuses a `POST /contracts` body over `KGB_MAX_UPLOAD_MB` (+64 KB multipart slack) with 413 before it is parsed (declared Content-Length, or by counting a chunked body and stopping); `KGB_API_KEY`, when set, is checked via the `X-API-Key` header on everything but `/health` and the docs.
- **Storage engine knowledge lives only in `kgb/store.py`** (`Store`): one SQLite file (documents, contracts, items, vectors BLOB, FTS5 trigram table). It exposes raw lookups `fts_search` and `vector_search` (brute-force cosine over a cached numpy matrix) plus `hits()`; fusing them is `search.py`'s job. It is meant to be swappable later for Qdrant/Weaviate. The index is disposable; `data/work/<sha12>/` is the source of truth and LLM cache, written by `ingest` (each stage skips itself when its artifact exists).

Behaviour that spans files and is easy to break:
- **Triage is recall-biased.** `triage()` sends ≤30 low-res thumbnails (`TRIAGE_PX`) per call; pages the model skips are labelled `items`; `item_pages()` falls back to the last 5 non-blank pages if none are found. Item pages are not always "Appendix 2" or at the end of the document.
- **Extraction is per page, in parallel** (`ThreadPoolExecutor`, `WORKERS`), then `fill_forward()` (pure function) repairs what a single page cannot know: carries category, `vat_included` and `currency` across page breaks (and backfills leading gaps), fills merged-cell names/brands of `continued` rows from the row above, sets `Item.page`, and fixes Latin lookalike letters in Cyrillic words.
- **Glitch guard in `extract_page`:** a result containing mixed-script words (e.g. "дагaldah") is retried with the same settings, then at the other image `detail` level; the cleanest result is kept.
- **`EXTRACT_DETAIL` defaults to `original`** (reads VAT notes like "Нөат-эй" correctly, ~10% more tokens than `high`). `split=2` (overlapping strips) exists but was found unnecessary.
- `Item.unit_price` is the price payable after discount; `list_price` is pre-discount. `Item.search_text` is a normalized Mongolian Cyrillic description meant for embedding/FTS; queries are typed in Mongolian Cyrillic.
- **Contract metadata is deliberately low-effort** (`meta.extract_meta`): one cheap call over ≤3 key-terms pages, no retries or verification. Only the validity dates matter downstream (expiry check); invalid dates are dropped to `None`. Don't spend more tokens or code on it unless asked.
- **Ingest is idempotent and resumable.** `sha12` of the file bytes names the work dir, so a renamed copy is the same document. Item pages are written one by one (`items/NNN.json`), so a crash redoes only the failed pages; `items.json` (after `fill_forward`) is rewritten each run; embeddings (`embeddings.npz`: vectors and a key over model, dimension and texts in one file) are redone only when the key changes (the earlier `embeddings.npy`+`.json` pair is converted on first use); artifacts are written through a unique temp file plus `os.replace` (retried on Windows sharing violations, as is the read of `embeddings.npz` while another thread replaces it). A repeat run does not even render the PDF. `reindex` rebuilds the DB from disk with zero model calls and skips (and reports) incomplete or stale directories. Only `Item.search_text` (name as fallback) is embedded; prices, units and contract fields are columns.
- **Store is safe for concurrent readers** (the API shares one `Store` across its thread pool): WAL, a short-lived connection per call, reads `query_only`, writes serialised by a lock plus `BEGIN IMMEDIATE`/busy timeout, and the vector matrix is an immutable snapshot tagged with a `generation` counter that every write bumps (readers reload only on change). Item ids are `AUTOINCREMENT` (never reused, so a stale search result is dropped by `hits()` instead of naming another item), `upsert` is a no-op returning False when the document's fingerprint is unchanged (a cached `ingest` re-run does not touch the index), and a changed table layout (`SCHEMA_VERSION`) is dropped and recreated in one transaction with a warning; `--reindex` refills it. `replace_all` is one transaction, so searches see the old index until the new one is complete.
- **Search modes** (`search_many(..., mode=, active_only=)`): `hybrid` (RRF of both sides, default), `keyword` (FTS only, never embeds; score = bm25), `semantic` (vectors only; score = cosine). `k` is capped at `POOL` (50). `active_only` excludes expired contracts when candidates are chosen (an SQL condition in `fts_search`, a `valid_to` column in the vector snapshot for `vector_search`), never after the 50-candidate pool, so a long run of expired matches cannot hide active ones. Live: hybrid and keyword 6/6 on the acceptance queries, semantic 5/6 (misses "хамгаалалтын гутал"), so hybrid stays the default.
- **One PDF upload** (`ingest.ingest_pdf(bytes, filename, store)`, used by both `POST /contracts` and `kgb.ingest --pdf`): `check_pdf` refuses non-PDF, empty or oversized input (`RejectedFile.reason`: type/size/empty/unreadable) before anything is written; a PDF that cannot be rendered leaves no half work folder. The sha12 of the bytes is the id, so the same PDF again (any name) is fully cached: 0 model calls, no rendering, no index write. A per-document lock (`_doc_lock`) makes simultaneous uploads of one file run the pipeline once. Only that document is touched (no folder scan, no reindex). `Report.llm_calls` is a global counter delta, so it also counts calls of other documents in flight (`--jobs`, concurrent uploads); `Report.ran` is exact.
- **FTS5 trigram is substring search, case-insensitive on Cyrillic, but a term shorter than 3 characters matches nothing** (e.g. the "20" in "муфт 20"); `search.terms()` drops such terms from the keyword side and the vector side carries them. Known limit: a size typed as a bare number ("муфт 20" vs the stored "ф20") ranks all sizes of муфт about equally (code 619 comes 2nd of 4); fixing it needs a short-term boost, not done.
- Strict structured outputs require every field; in LLM-facing models only use `X | None = None` defaults.

## Conventions

- Minimal dependencies (openai, pydantic, pypdfium2, pillow, numpy, openpyxl, python-docx, fastapi, uvicorn, python-multipart; dev: pytest, httpx). Ask before adding a framework; no LangChain/LlamaIndex/vector DB/Tesseract.
- Every module has a `__main__` probe that prints inputs, outputs and the path of any file it wrote. The user cannot review code line by line and relies on these to check work; add one to any new module. Probe output goes under `data/probe/` (git-ignored).
- Functions take and return plain data; keep each module callable on its own.
- Tests are offline: stub the LLM layer, never call the API.
- Windows environment: `kgb/__init__.py` forces UTF-8 stdout/stderr; for ad-hoc `python -c` scripts that print Cyrillic set `PYTHONIOENCODING=utf-8`. The Bash tool is the easiest way to run `uv run ...`.
- Commit or push only when the user asks.

## Housekeeping notes

- `.claude/skills/tlds/` is a tracked skill (repository study documents); its scripts are stdlib-only and unrelated to the `kgb` package. `.gitignore` ignores the whole `.claude/` directory (and has the `settings.local.json` rule commented out), so new files under `.claude/` need `git add -f`; the skill files already tracked are unaffected.
- Model defaults (`gpt-6-luna`) were chosen because `gpt-5-mini`/`gpt-5-nano` shut down 2026-12-11; if OCR quality on Mongolian regresses, `KGB_EXTRACT_MODEL=gpt-6.1-terra` is the noted fallback.
