# KGB

A small, general-purpose knowledge base. Give it your documents, then ask questions in everyday language.

- **Reads what you have.** Scans, photos, PDFs, spreadsheets and text documents all go in the same way.
- **Organizes it.** Useful facts become individual entries, each linked back to its source page.
- **Answers plainly.** Ask in your own words and get matching entries with their source.
- **Stays tidy.** Re-adding a document changes nothing, and the index can be rebuilt at any time.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 and the dependencies) and an OpenAI API key.

```bash
uv sync
cp .env.example .env        # then set OPENAI_API_KEY in .env
uv run python -m kgb.config # prints the active settings; the key shows only as set/MISSING
```

All settings are `KGB_*` environment variables (or lines in `.env`): models, data folders, parallel workers, page render size, image detail for extraction. Defaults are listed in `.env.example`.

## How it works

1. **Load:** every file becomes a list of pages: scans and PDFs as images, office files as text.
2. **Triage:** one cheap low-resolution pass labels each page, so only pages with items are read closely.
3. **Extract:** item pages are read in parallel at full resolution into structured entries (name, code, spec, price, VAT, page). Section headers, merged cells and VAT notes are carried across page breaks, and garbled readings are retried.
4. **Contract facts:** one cheap call over the first key-terms pages gives parties, validity dates and a short note (rough on purpose; items matter most).
5. **Store:** each entry's description is embedded, and entries go into a single-file index with keyword (substring) and meaning-based lookup. Everything per document is kept in `data/work/<id>/`, so repeating a document costs nothing and the index can be rebuilt from it at any time.
6. **Search:** keyword and meaning matches combined into one ranked answer (or either side alone), each hit with its price, unit, VAT, supplier, validity and source page.
7. **API:** the same search, lookups and one-PDF upload over HTTP, with interactive docs.

## Usage

Each module can be run on its own as a probe:

```bash
uv run python -m kgb.load "data/contracts/doc.pdf" [out_dir]    # pages, sizes; optionally dump them as images/markdown
uv run python -m kgb.llm ask "data/contracts/doc.pdf" 6         # label one page with the model (API call)
uv run python -m kgb.llm embed "first text" "second text"       # embedding shape and similarity (API call)
uv run python -m kgb.triage "data/contracts/doc.pdf"            # label every page, list the ones to extract (API call)
uv run python -m kgb.extract "data/contracts/doc.pdf" [pages]   # items as a table + JSON in data/probe/extract (API calls)
uv run python -m kgb.meta "data/contracts/doc.pdf" [pages]      # parties and validity dates from the key-terms pages (API calls)
uv run python -m kgb.ingest "data/contracts" [--jobs 3]         # whole pipeline into the index (API calls the first time only)
uv run python -m kgb.ingest --pdf "data/contracts/doc.pdf"      # one PDF, exactly what the upload endpoint does
uv run python -m kgb.ingest --reindex                           # rebuild the index from data/work (no API calls)
uv run python -m kgb.ingest --export kgb-snapshot.sqlite        # consistent single-file copy of the index
uv run python -m kgb.store fts "хамгаалалтын гутал"             # counts and a keyword lookup in the index
uv run python -m kgb.search "хамгаалалтын гутал" "муфт 20"       # best items for each query (one embedding call)
uv run python -m kgb.search --mode keyword "муфт 20"            # keyword only (no API call); also: semantic, hybrid
uv run python -m kgb.api                                        # HTTP API, interactive docs at http://127.0.0.1:8000/docs
```

Put source documents in `data/contracts/` (git-ignored). Supported: `pdf`, `png`, `jpg`, `tif`, `webp`, `docx`, `xlsx`, `csv`.

## HTTP API

```bash
uv run python -m kgb.api              # http://127.0.0.1:8000/docs  (Swagger UI: try every route in the browser)
uv run python -m kgb.api --selftest   # the same requests in-process against your index, printed
```

| Route | What it does |
|---|---|
| `GET /health` | Index counts, embedding model, whether keys are set |
| `GET /search?q=…&mode=hybrid\|keyword\|semantic&k=10&active_only=false` | Best items for one query |
| `POST /search` `{"queries": [...], "mode": …, "k": …, "active_only": …}` | A whole purchase list with one embedding request |
| `GET /items/{item_id}` | One item with its contract |
| `GET /contracts`, `GET /contracts/{doc_id}` | Indexed documents; one document with all its items |
| `POST /contracts` (multipart `file`) | Add exactly one PDF: full pipeline (about a minute); a PDF seen before returns at once from cache (status 200, `cached: true`, no model call) |

`keyword` mode needs no OpenAI call; `hybrid` (the default) is the most reliable. Settings: `KGB_API_HOST` (use `0.0.0.0` for the office LAN), `KGB_API_PORT`, `KGB_API_KEY` (when set, send it as the `X-API-Key` header; the Swagger UI has an Authorize button), `KGB_MAX_UPLOAD_MB`. The machine-readable description is at `/openapi.json`, for AI tools that read OpenAPI.

**Loading many contracts once:** put them in `data/contracts/`, run `uv run python -m kgb.ingest data/contracts --jobs 3` (re-run until it reports 0 failed; finished work is never redone), check with `uv run python -m kgb.search`, then either serve that index directly or copy it with `--export` to the server (set `KGB_DB` there). Back up `data/work/`: it holds every reading and embedding, and `--reindex` rebuilds an index from it for free. The server must use the same `KGB_EMBED_MODEL` and `KGB_EMBED_DIM` as the ingestion, and needs `OPENAI_API_KEY` for `semantic`/`hybrid` queries and uploads.

## Tests

```bash
uv run pytest -q    # offline: no API key or network needed
```

## Layout

| Path | Role |
|---|---|
| `kgb/config.py` | Settings from environment / `.env` |
| `kgb/schema.py` | Shared data shapes and model output schemas |
| `kgb/load.py` | Any supported file → list of pages (images or text) |
| `kgb/llm.py` | The only OpenAI-facing module: structured answers and embeddings |
| `kgb/triage.py` | Cheap first pass: labels each page as items / key terms / other |
| `kgb/extract.py` | Item pages → structured items; carries categories, merged cells and VAT across pages |
| `kgb/meta.py` | Key-terms pages → contract facts (parties, validity dates, short note) |
| `kgb/store.py` | The index (SQLite: items, vectors, keyword index); safe for concurrent readers |
| `kgb/search.py` | Keyword (FTS5 trigram) and meaning (embeddings) fused with RRF; returns items with price and contract |
| `kgb/ingest.py` | Document → work folder → index; every step cached, `--reindex` rebuilds from disk; `ingest_pdf` for one upload |
| `kgb/api.py` | HTTP API (FastAPI): search, lookups, one-PDF upload; Swagger UI at `/docs` |

## Status

Early development, built in small steps that can each be checked on their own. Done: configuration, document loading, model access, page triage, item extraction, contract facts, storage, ingestion, search and the HTTP API. Later: an MCP server for AI assistants, the coverage check and the command line.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
