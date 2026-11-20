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

All settings are `KGB_*` environment variables (or lines in `.env`): models, data folders, parallel workers, page render size. Defaults are listed in `.env.example`.

## Usage

Each module can be run on its own as a probe:

```bash
uv run python -m kgb.load "data/contracts/doc.pdf" [out_dir]    # pages, sizes; optionally dump them as images/markdown
uv run python -m kgb.llm ask "data/contracts/doc.pdf" 6         # label one page with the model (API call)
uv run python -m kgb.llm embed "first text" "second text"       # embedding shape and similarity (API call)
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

## Status

Early development, built in small steps that can each be checked on their own. Done: configuration, document loading, model access. Next: page triage, item extraction, storage, search.

## License

GNU General Public License v3.0. See [LICENSE](LICENSE).
