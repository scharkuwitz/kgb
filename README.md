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
6. **Search** *(next)*: keyword and meaning matches combined into one ranked answer.

## Usage

Each module can be run on its own as a probe:

```bash
uv run python -m kgb.load "data/contracts/doc.pdf" [out_dir]    # pages, sizes; optionally dump them as images/markdown
uv run python -m kgb.llm ask "data/contracts/doc.pdf" 6         # label one page with the model (API call)
uv run python -m kgb.llm embed "first text" "second text"       # embedding shape and similarity (API call)
uv run python -m kgb.triage "data/contracts/doc.pdf"            # label every page, list the ones to extract (API call)
uv run python -m kgb.extract "data/contracts/doc.pdf" [pages]   # items as a table + JSON in data/probe/extract (API calls)
uv run python -m kgb.meta "data/contracts/doc.pdf" [pages]      # parties and validity dates from the key-terms pages (API calls)
uv run python -m kgb.ingest "data/contracts"                    # whole pipeline into the index (API calls the first time only)
uv run python -m kgb.ingest --reindex                           # rebuild the index from data/work (no API calls)
uv run python -m kgb.store fts "хамгаалалтын гутал"             # counts and a keyword lookup in the index
```

Put source documents in `data/contracts/` (git-ignored). Supported: `pdf`, `png`, `jpg`, `tif`, `webp`, `docx`, `xlsx`, `csv`.

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
| `kgb/ingest.py` | Document → work folder → index; every step cached, `--reindex` rebuilds from disk |

## Status

Early development, built in small steps that can each be checked on their own. Done: configuration, document loading, model access, page triage, item extraction, contract facts, storage and ingestion. Next: search.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
