"""Documents -> work directory -> index. Every stage writes an artifact and skips itself when it exists.

    report = ingest_file("contract.pdf", store)     # Report: stages that ran, LLM calls made
    n, skipped = reindex(store)                     # rebuild the index from the work directories, no model calls

Work directory `config.WORK/<sha12>/` (sha12 = first 12 hex digits of the file's SHA-256; the same file
under another name is the same document). It is the source of truth and the LLM cache:

    source.<ext>        copy of the original
    doc.json            filename, page count
    triage.json         page labels, the item pages and key-terms pages chosen
    items/NNN.json      raw extraction of one item page (written as each page finishes, so a crash resumes)
    items.json          all items after fill_forward (rewritten on every run; it is free)
    meta.json           contract facts
    embeddings.npy      one vector per item, plus embeddings.json with a key over model, dimension and texts

So re-running a file makes no model call, deleting the index loses nothing, and changing a stage's
artifact (delete one file) redoes only that stage and what depends on it.

What gets embedded: `Item.search_text` only (the normalized description; falls back to the name).
Prices, units and contract fields are columns, not meaning.

Probe (the first run calls the API; a repeat run does not):
    uv run python -m kgb.ingest "data/contracts"                    # every supported file in the folder
    uv run python -m kgb.ingest "data/contracts/[Sample] Contract #D.pdf"
    uv run python -m kgb.ingest --reindex                           # drop and rebuild the index from data/work
"""

import hashlib
import io
import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from kgb import config, extract, llm, load, meta, store, triage
from kgb.schema import ContractMeta, Item, PageItems


@dataclass
class Report:
    doc_id: str
    filename: str
    n_pages: int
    n_items: int
    ran: list[str] = field(default_factory=list)  # stages that did work, e.g. ["triage", "extract:7", "meta", "embed"]
    llm_calls: int = 0
    seconds: float = 0.0


class IncompleteError(ValueError):
    """A work directory lacks an artifact (or has a stale one), so it cannot be indexed from disk alone."""


# ---- small file helpers -------------------------------------------------------


def sha12(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:12]


def _write_bytes(path: Path, data: bytes) -> None:
    """Write via a temporary file so a crash never leaves a half-written artifact behind."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _write_json(path: Path, obj) -> None:
    _write_bytes(path, json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"))


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def embed_text(item: Item) -> str:
    """The text that is embedded for an item."""
    return item.search_text.strip() or item.name


def _embed_key(items: list[Item]) -> str:
    """Identifies the embeddings: they are stale when the model, the dimension or any text changes."""
    parts = [config.EMBED_MODEL, str(config.EMBED_DIM), *(embed_text(i) for i in items)]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


# ---- one document -----------------------------------------------------------------


def ingest_file(path: str | Path, st: store.Store | None = None, work: str | Path | None = None) -> Report:
    """Run every stage that has no artifact yet, then (when `st` is given) upsert the document into the index."""
    path, work = Path(path), Path(work or config.WORK)
    started, calls_before = time.perf_counter(), llm.call_count()
    doc_id = sha12(path)
    wd = work / doc_id
    (wd / "items").mkdir(parents=True, exist_ok=True)
    ran: list[str] = []

    source = wd / f"source{path.suffix.lower()}"
    if not source.exists():
        _write_bytes(source, path.read_bytes())

    loaded: list = []

    def pages():  # pages are only rendered when a stage needs the model
        if not loaded:
            loaded.append(load.load(source))
        return loaded[0]

    if not (wd / "doc.json").exists():
        _write_json(wd / "doc.json", {"id": doc_id, "filename": path.name, "n_pages": len(pages())})
    doc = _read_json(wd / "doc.json")

    if not (wd / "triage.json").exists():
        labels = triage.triage(pages())
        _write_json(wd / "triage.json", {
            "labels": [x.model_dump() for x in labels],
            "item_pages": triage.item_pages(labels, pages()),
            "key_term_pages": triage.key_term_pages(labels),
        })
        ran.append("triage")
    plan = _read_json(wd / "triage.json")

    todo = [n for n in plan["item_pages"] if not (wd / "items" / f"{n:03d}.json").exists()]
    if todo:
        by_no = {p.no: p for p in pages()}
        failure = None
        with ThreadPoolExecutor(max_workers=max(1, min(config.WORKERS, len(todo)))) as pool:
            futures = {pool.submit(extract.extract_page, by_no[n]): n for n in todo}
            for future in as_completed(futures):
                try:
                    _write_json(wd / "items" / f"{futures[future]:03d}.json", future.result().model_dump())
                except Exception as e:  # keep the pages that did succeed, so a retry only redoes the failed ones
                    failure = failure or e
        if failure:
            raise failure
        ran.append(f"extract:{len(todo)}")
    per_page = {n: PageItems.model_validate(_read_json(wd / "items" / f"{n:03d}.json")) for n in sorted(plan["item_pages"])}
    items = extract.fill_forward(per_page)
    _write_json(wd / "items.json", [i.model_dump() for i in items])

    if not (wd / "meta.json").exists():
        _write_json(wd / "meta.json", meta.extract_meta(pages(), plan["key_term_pages"]).model_dump())
        ran.append("meta")
    contract = ContractMeta.model_validate(_read_json(wd / "meta.json"))

    key = _embed_key(items)
    info = wd / "embeddings.json"
    if info.exists() and (wd / "embeddings.npy").exists() and _read_json(info).get("key") == key:
        vectors = np.load(wd / "embeddings.npy")
    else:
        vectors = llm.embed([embed_text(i) for i in items])
        buf = io.BytesIO()
        np.save(buf, vectors)
        _write_bytes(wd / "embeddings.npy", buf.getvalue())
        _write_json(info, {"key": key, "model": config.EMBED_MODEL, "dim": config.EMBED_DIM, "n": len(items)})
        ran.append("embed")

    if st is not None:
        st.upsert(store.DocRecord(doc_id, doc["filename"], doc["n_pages"], contract, items, vectors))
    return Report(doc_id, doc["filename"], doc["n_pages"], len(items), ran, llm.call_count() - calls_before,
                  time.perf_counter() - started)


def ingest(paths: list[str | Path], st: store.Store | None = None, work: str | Path | None = None):
    """Ingest files and folders (one level; unsupported file types are skipped).
    Returns (reports, failures) where failures is a list of (path, error text)."""
    files: list[Path] = []
    for p in map(Path, paths):
        files += sorted(f for f in p.iterdir() if f.is_file() and f.suffix.lower() in load.LOADERS) if p.is_dir() else [p]
    reports, failures = [], []
    for f in files:
        try:
            reports.append(ingest_file(f, st, work))
        except Exception as e:  # one bad file must not stop a batch; its finished stages are kept for the retry
            failures.append((f, f"{type(e).__name__}: {e}"))
    return reports, failures


# ---- rebuild the index from disk --------------------------------------------------------


def load_record(wd: Path) -> store.DocRecord:
    """A work directory -> DocRecord, with no model call. Raises IncompleteError if something is missing or stale."""
    needed = ["doc.json", "items.json", "meta.json", "embeddings.npy", "embeddings.json"]
    missing = [n for n in needed if not (wd / n).exists()]
    if missing:
        raise IncompleteError(f"missing {', '.join(missing)}; run ingest on the source file")
    doc = _read_json(wd / "doc.json")
    items = [Item.model_validate(d) for d in _read_json(wd / "items.json")]
    if _read_json(wd / "embeddings.json").get("key") != _embed_key(items):
        raise IncompleteError("embeddings are stale (model, dimension or texts changed); run ingest on the source file")
    return store.DocRecord(
        doc["id"], doc["filename"], doc["n_pages"], ContractMeta.model_validate(_read_json(wd / "meta.json")),
        items, np.load(wd / "embeddings.npy"),
    )


def reindex(st: store.Store, work: str | Path | None = None) -> tuple[int, list[str]]:
    """Replace the index contents with what the work directories hold. Atomic: searches keep the old
    contents until the new ones are complete. Returns (documents indexed, skipped directories with reason)."""
    work = Path(work or config.WORK)
    skipped: list[str] = []

    def records():
        for wd in sorted(p for p in work.iterdir() if p.is_dir()) if work.is_dir() else []:
            try:
                yield load_record(wd)
            except Exception as e:
                skipped.append(f"{wd.name}: {e}")

    return st.replace_all(records()), skipped


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    st = store.Store()
    if args == ["--reindex"]:
        started = time.perf_counter()
        n, skipped = reindex(st)
        print(f"reindexed {n} documents in {time.perf_counter() - started:.1f}s, 0 model calls")
        for line in skipped:
            print("  skipped", line)
        print("counts", st.counts())
        sys.exit(1 if skipped else 0)
    started, calls_before = time.perf_counter(), llm.call_count()
    reports, failures = ingest(args, st)
    for r in reports:
        print(f"  {r.doc_id}  {r.n_items:>4} items  {r.n_pages:>3} pages  {r.seconds:5.1f}s  {r.llm_calls:>3} calls  "
              f"{', '.join(r.ran) or 'cached'}  | {r.filename}")
    for f, error in failures:
        print(f"  FAILED {f}: {error}")
    print(f"{len(reports)} ingested, {len(failures)} failed, {llm.call_count() - calls_before} model calls "
          f"in {time.perf_counter() - started:.1f}s")
    print("counts", st.counts())
    sys.exit(1 if failures else 0)
