"""The index: documents, contracts, items, vectors and a full-text index in one SQLite file.

The only module that knows the storage engine. Everything else sees plain data:

    st = Store()                                    # config.DB; created on first use
    st.upsert(DocRecord(doc_id, filename, n_pages, meta, items, vectors))   # replaces that document
    st.replace_all(records)                         # atomic rebuild (what `reindex` uses)
    st.delete_doc(doc_id)
    st.fts_search('"муфт"', k)                      # [(item_id, score)], keyword (FTS5 trigram, substring match)
    st.vector_search(query_vec, k)                  # [(item_id, cosine)], brute force over a cached matrix
    st.hits(pairs)                                  # [(item_id, score)] -> list[Hit], with contract facts
    st.item(item_id), st.document(doc_id)           # ItemDetail / ContractDetail (with its items), or None
    st.counts(), st.documents()
    st.export("snapshot.sqlite")                    # consistent single-file copy (VACUUM INTO), safe while in use
    st.warm()                                       # load the vector matrix now (server start)

The index is disposable: `data/work/<sha12>/` holds the source of truth, and `kgb.ingest --reindex`
rebuilds this file from it without any model call. To move to another engine (Qdrant, Weaviate),
reimplement this class; callers keep the same calls.

Concurrency (`kgb.api` serves searches from a thread pool while uploads or a CLI ingest write):
- WAL journal: readers never block the writer and never see a half-written document.
- Every call opens its own short-lived connection, so threads (or an async server's thread pool) never
  share one. Reads are `query_only`.
- Writes are serialised in-process by a lock and across processes by SQLite (`BEGIN IMMEDIATE` plus a
  busy timeout), so a CLI `ingest` can run while a server is answering queries.
- The vector matrix is cached as an immutable snapshot tagged with a `generation` counter that every
  write bumps in its own transaction. A reader compares generations and reloads only when they differ
  (one thread reloads, the rest wait for it); arrays are never mutated after publication, so queries in
  flight keep using the snapshot they started with.

Probe (no API calls):
    uv run python -m kgb.store                      # SQLite/FTS5 check, counts, documents, a sample row
    uv run python -m kgb.store fts "муфт 20"        # keyword lookup, printed as hits
"""

import hashlib
import sqlite3
import threading
import warnings
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np

from kgb import config
from kgb.schema import ContractDetail, ContractMeta, Hit, Item, ItemDetail, ItemRecord

SCHEMA_VERSION = 2  # bump when the tables change; an index with another version is dropped (it is disposable)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    n_pages INTEGER NOT NULL,
    fingerprint TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS contracts (
    doc_id TEXT PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    doc_type TEXT, contract_no TEXT, supplier TEXT, supplier_reg_no TEXT,
    signed_date TEXT, valid_from TEXT, valid_to TEXT, terms_note TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- never reused, so an id from an earlier search cannot name a different item
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL, code TEXT, brand TEXT, category TEXT, spec TEXT, unit TEXT,
    unit_price REAL, list_price REAL, currency TEXT, vat_included INTEGER,
    search_text TEXT NOT NULL,
    page INTEGER
);
CREATE INDEX IF NOT EXISTS items_doc ON items(doc_id);
CREATE TABLE IF NOT EXISTS vectors (
    item_id INTEGER PRIMARY KEY REFERENCES items(id) ON DELETE CASCADE,
    vec BLOB NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
INSERT OR IGNORE INTO state VALUES ('generation', 0);
INSERT OR IGNORE INTO state VALUES ('schema', %d);
""" % SCHEMA_VERSION

_TABLES = ("items_fts", "vectors", "items", "contracts", "documents")

BUSY_MS = 30_000
_IN_CHUNK = 500  # ids per "IN (...)" query


@dataclass
class DocRecord:
    """Everything the index holds about one document. `vectors[i]` belongs to `items[i]`."""

    doc_id: str
    filename: str
    n_pages: int
    meta: ContractMeta
    items: list[Item]
    vectors: np.ndarray  # float32, (len(items), config.EMBED_DIM)


@dataclass(frozen=True)
class _Snapshot:
    generation: int
    ids: np.ndarray  # int64, (n,)
    matrix: np.ndarray  # float32, (n, dim), rows unit length
    valid_to: np.ndarray  # str, (n,): the contract's end date per row, "" when unknown (for active-only searches)


def fts_text(item: Item) -> str:
    """What the keyword index sees: the normalized description plus the raw fields, so a code, brand or
    spec fragment typed by a user matches even when the description paraphrased it."""
    text = item.search_text.strip()
    for part in (item.name, item.code, item.brand, item.category, item.spec):
        part = (part or "").strip()
        if part and part.lower() not in text.lower():  # trigram search is substring search: no need to repeat
            text = f"{text} {part}".strip()
    return text


def fingerprint(record: DocRecord) -> str:
    """Hash of everything the index stores for a document: equal fingerprints mean nothing would change."""
    digest = hashlib.sha256()
    for part in (record.filename, str(record.n_pages), record.meta.model_dump_json(), *(i.model_dump_json() for i in record.items)):
        digest.update(part.encode("utf-8"))
        digest.update(b"")
    digest.update(np.ascontiguousarray(record.vectors, dtype="<f4").tobytes())
    return digest.hexdigest()


# One item with its contract and document, the shape every item read starts from.
_HIT_SELECT = (
    "SELECT i.id AS item_id, i.seq, i.name, i.code, i.brand, i.category, i.spec, i.unit, i.unit_price, i.list_price,"
    " i.currency, i.vat_included, i.page, i.search_text, c.supplier, c.contract_no, c.valid_from, c.valid_to,"
    " c.terms_note, d.id AS doc_id, d.filename"
    " FROM items i JOIN documents d ON d.id = i.doc_id LEFT JOIN contracts c ON c.doc_id = i.doc_id"
)

# An item whose contract has not ended before a given day (the ? parameter, ISO date) or has no end date.
# Must agree with `_expired` below and with the vector snapshot's filter in `vector_search`.
_ACTIVE = "(c.valid_to IS NULL OR c.valid_to = '' OR c.valid_to >= ?)"

# One document with its contract facts and item count.
_DOC_SELECT = (
    "SELECT d.id, d.filename, d.n_pages, c.doc_type, c.contract_no, c.supplier, c.supplier_reg_no, c.signed_date,"
    " c.valid_from, c.valid_to, c.terms_note, (SELECT COUNT(*) FROM items i WHERE i.doc_id = d.id) AS n_items"
    " FROM documents d LEFT JOIN contracts c ON c.doc_id = d.id"
)


def _record(row: sqlite3.Row, today_iso: str) -> dict:
    """The ItemRecord fields of a `_HIT_SELECT` row."""
    valid_to, vat = row["valid_to"], row["vat_included"]
    return dict(
        item_id=row["item_id"], name=row["name"], code=row["code"], brand=row["brand"], category=row["category"],
        spec=row["spec"], unit=row["unit"], unit_price=row["unit_price"], list_price=row["list_price"],
        currency=row["currency"], vat_included=None if vat is None else bool(vat), doc_id=row["doc_id"],
        filename=row["filename"], supplier=row["supplier"], contract_no=row["contract_no"],
        valid_from=row["valid_from"], valid_to=valid_to, expired=_expired(valid_to, today_iso), page=row["page"],
    )


def _expired(valid_to: str | None, today_iso: str) -> bool | None:
    return None if not valid_to else valid_to < today_iso


def _unit(rows: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(rows, axis=-1, keepdims=True)
    return rows / np.where(norms == 0, 1, norms)


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or config.DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._snap: _Snapshot | None = None
        with closing(self._connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            if self._create_schema(con):
                warnings.warn("the index used an older layout and was emptied; run `python -m kgb.ingest --reindex`",
                              RuntimeWarning, stacklevel=2)

    @staticmethod
    def _create_schema(con: sqlite3.Connection) -> bool:
        """Create the tables, in one transaction. An index written by an older layout is dropped and recreated in
        that same transaction (readers in other processes see the old or the new layout, never a gap): it is
        disposable, and `ingest --reindex` refills it from the work folders without model calls. The generation is
        bumped so running readers reload. Returns True when an old layout was emptied."""
        con.execute("BEGIN IMMEDIATE")
        try:
            has_state = con.execute("SELECT 1 FROM sqlite_master WHERE name = 'state'").fetchone()
            row = has_state and con.execute("SELECT value FROM state WHERE key = 'schema'").fetchone()
            emptied = bool(has_state) and (row is None or row[0] != SCHEMA_VERSION)
            if emptied:
                for table in _TABLES:
                    con.execute(f"DROP TABLE IF EXISTS {table}")
                con.execute("DELETE FROM state WHERE key = 'schema'")
                con.execute("UPDATE state SET value = value + 1 WHERE key = 'generation'")
            for statement in SCHEMA.split(";"):  # not executescript(): that commits on its own
                if statement.strip():
                    con.execute(statement)
            con.execute("COMMIT")
            return emptied
        except BaseException:
            con.execute("ROLLBACK")
            raise

    # ---- connections -----------------------------------------------------------

    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=BUSY_MS / 1000, isolation_level=None)  # we manage transactions
        con.execute(f"PRAGMA busy_timeout={BUSY_MS}")
        con.execute("PRAGMA foreign_keys=ON")
        if readonly:
            con.execute("PRAGMA query_only=ON")
        return con

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        with closing(self._connect(readonly=True)) as con:
            yield con

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        """One write transaction: all or nothing, and it bumps the generation so readers reload."""
        with self._write_lock, closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
                con.execute("UPDATE state SET value = value + 1 WHERE key = 'generation'")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    # ---- writes ----------------------------------------------------------------

    def upsert(self, record: DocRecord) -> bool:
        """Insert or replace one document with its items and vectors, atomically. Returns False, touching
        nothing (no write lock, no generation bump, so searchers keep their cached vectors), when the index
        already holds exactly this content."""
        self._check(record)
        stamp = fingerprint(record)
        with self._reader() as con:
            row = con.execute("SELECT fingerprint FROM documents WHERE id = ?", (record.doc_id,)).fetchone()
        if row and row[0] == stamp:
            return False
        with self._writer() as con:
            self._delete(con, record.doc_id)
            self._insert(con, record, stamp)
        return True

    def replace_all(self, records: Iterable[DocRecord]) -> int:
        """Rebuild the whole index in one transaction. Readers keep the old contents until the commit;
        if anything fails (including the iterable) the old contents stay. Returns the number of documents."""
        count = 0
        with self._writer() as con:
            for table in _TABLES:
                con.execute(f"DELETE FROM {table}")
            for record in records:
                self._check(record)
                self._insert(con, record, fingerprint(record))
                count += 1
        return count

    def delete_doc(self, doc_id: str) -> bool:
        with self._writer() as con:
            return self._delete(con, doc_id)

    @staticmethod
    def _check(record: DocRecord) -> None:
        vectors = record.vectors
        if len(vectors) != len(record.items):
            raise ValueError(f"{record.doc_id}: {len(record.items)} items but {len(vectors)} vectors")
        if len(vectors) and (vectors.ndim != 2 or vectors.shape[1] != config.EMBED_DIM):
            raise ValueError(f"{record.doc_id}: vectors have shape {vectors.shape}, expected (n, {config.EMBED_DIM})")

    @staticmethod
    def _delete(con: sqlite3.Connection, doc_id: str) -> bool:
        con.execute("DELETE FROM items_fts WHERE rowid IN (SELECT id FROM items WHERE doc_id = ?)", (doc_id,))
        # foreign keys cascade from documents to contracts, items and vectors
        return con.execute("DELETE FROM documents WHERE id = ?", (doc_id,)).rowcount > 0

    @staticmethod
    def _insert(con: sqlite3.Connection, record: DocRecord, stamp: str) -> None:
        con.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", (record.doc_id, record.filename, record.n_pages, stamp))
        m = record.meta
        con.execute(
            "INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (record.doc_id, m.doc_type, m.contract_no, m.supplier, m.supplier_reg_no,
             m.signed_date, m.valid_from, m.valid_to, m.terms_note),
        )
        for seq, (item, vector) in enumerate(zip(record.items, record.vectors)):
            cur = con.execute(
                "INSERT INTO items (doc_id, seq, name, code, brand, category, spec, unit, unit_price, list_price,"
                " currency, vat_included, search_text, page) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.doc_id, seq, item.name, item.code, item.brand, item.category, item.spec, item.unit,
                 item.unit_price, item.list_price, item.currency,
                 None if item.vat_included is None else int(item.vat_included), item.search_text, item.page),
            )
            item_id = cur.lastrowid
            con.execute("INSERT INTO vectors VALUES (?, ?)", (item_id, np.asarray(vector, dtype="<f4").tobytes()))
            con.execute("INSERT INTO items_fts (rowid, text) VALUES (?, ?)", (item_id, fts_text(item)))

    # ---- reads -----------------------------------------------------------------

    def fts_search(self, match: str, k: int = 50, active_on: date | None = None) -> list[tuple[int, float]]:
        """Keyword search. `match` is an FTS5 expression (trigram tokenizer: substring match, case-insensitive,
        terms shorter than 3 characters match nothing). Score: higher is better. With `active_on`, only items whose
        contract has not ended before that day (or has no end date) are candidates, so the k returned are all active."""
        sql = "SELECT items_fts.rowid, -bm25(items_fts) FROM items_fts"
        params: list = [match]
        if active_on is not None:
            sql += f" JOIN items i ON i.id = items_fts.rowid LEFT JOIN contracts c ON c.doc_id = i.doc_id WHERE items_fts MATCH ? AND {_ACTIVE}"
            params.append(active_on.isoformat())
        else:
            sql += " WHERE items_fts MATCH ?"
        with self._reader() as con:
            rows = con.execute(f"{sql} ORDER BY bm25(items_fts) LIMIT ?", (*params, k)).fetchall()
        return [(int(i), float(s)) for i, s in rows]

    def vector_search(self, vector: np.ndarray, k: int = 50, active_on: date | None = None) -> list[tuple[int, float]]:
        """Nearest items by cosine similarity, best first. With `active_on`, only items whose contract has not
        ended before that day (or has no end date) are candidates."""
        snap = self._snapshot()
        if k <= 0 or not len(snap.ids):
            return []
        query = np.asarray(vector, dtype=np.float32).reshape(-1)
        if query.shape[0] != snap.matrix.shape[1]:
            raise ValueError(f"query has {query.shape[0]} dimensions, the index has {snap.matrix.shape[1]}")
        scores = snap.matrix @ _unit(query)
        rows = np.arange(len(scores))
        if active_on is not None:
            rows = rows[(snap.valid_to == "") | (snap.valid_to >= active_on.isoformat())]
            if not len(rows):
                return []
        k = min(k, len(rows))
        top = rows[np.argpartition(-scores[rows], k - 1)[:k]]
        top = top[np.argsort(-scores[top])]
        return [(int(snap.ids[i]), float(scores[i])) for i in top]

    def hits(self, pairs: Iterable[tuple[int, float]], today: date | None = None) -> list[Hit]:
        """(item_id, score) pairs -> Hit objects in the same order, with their contract's supplier and validity.
        `expired` is None when the contract has no validity end date."""
        pairs = list(pairs)
        today_iso = (today or date.today()).isoformat()
        found: dict[int, tuple] = {}
        with self._reader() as con:
            con.row_factory = sqlite3.Row
            for start in range(0, len(pairs), _IN_CHUNK):
                ids = [i for i, _ in pairs[start : start + _IN_CHUNK]]
                marks = ",".join("?" * len(ids))
                for row in con.execute(f"{_HIT_SELECT} WHERE i.id IN ({marks})", ids):
                    found[row["item_id"]] = row
        out = []
        for item_id, score in pairs:
            row = found.get(item_id)
            if row is None:  # removed by a concurrent write between search and lookup
                continue
            out.append(Hit(**_record(row, today_iso), score=score))
        return out

    def item(self, item_id: int, today: date | None = None) -> ItemDetail | None:
        """Everything about one item, or None when no such item exists (any more)."""
        with self._reader() as con:
            con.row_factory = sqlite3.Row
            row = con.execute(f"{_HIT_SELECT} WHERE i.id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        return ItemDetail(**_record(row, (today or date.today()).isoformat()), seq=row["seq"],
                          search_text=row["search_text"], terms_note=row["terms_note"])

    def document(self, doc_id: str, today: date | None = None) -> ContractDetail | None:
        """One document: its contract facts and its items in document order. None when it is not indexed."""
        with self._reader() as con:
            con.row_factory = sqlite3.Row
            con.execute("BEGIN")  # the document and its items from one consistent read
            try:
                head = con.execute(f"{_DOC_SELECT} WHERE d.id = ?", (doc_id,)).fetchone()
                rows = con.execute(f"{_HIT_SELECT} WHERE i.doc_id = ? ORDER BY i.seq", (doc_id,)).fetchall() if head else []
            finally:
                con.execute("COMMIT")
        if head is None:
            return None
        today_iso = (today or date.today()).isoformat()
        return ContractDetail(**dict(head), expired=_expired(head["valid_to"], today_iso),
                              items=[ItemRecord(**_record(r, today_iso)) for r in rows])

    def export(self, dest: str | Path) -> Path:
        """Write a consistent single-file copy of the index (`VACUUM INTO`): includes what still sits in the
        WAL file, which a plain file copy can miss. Safe while others read or write. Refuses to overwrite."""
        dest = Path(dest)
        if dest.exists():
            raise FileExistsError(f"{dest} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con:
            con.execute("VACUUM INTO ?", (str(dest),))
        return dest

    def warm(self) -> int:
        """Load the vector matrix now (a server calls this at start, so the first query is not slow).
        Returns the number of vectors."""
        return len(self._snapshot().ids)

    def counts(self) -> dict[str, int]:
        with self._reader() as con:
            return {
                "documents": con.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
                "items": con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
                "vectors": con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0],
                "fts_rows": con.execute("SELECT COUNT(*) FROM items_fts").fetchone()[0],
                "generation": self._generation(con),
            }

    def documents(self, today: date | None = None) -> list[dict]:
        """One row per document, ordered by id, with the contract facts, its item count and whether the contract
        has expired (the fields of `ContractSummary`)."""
        today_iso = (today or date.today()).isoformat()
        with self._reader() as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(f"{_DOC_SELECT} ORDER BY d.id").fetchall()
        return [dict(r) | {"expired": _expired(r["valid_to"], today_iso)} for r in rows]

    # ---- vector snapshot -------------------------------------------------------

    @staticmethod
    def _generation(con: sqlite3.Connection) -> int:
        return con.execute("SELECT value FROM state WHERE key = 'generation'").fetchone()[0]

    def _snapshot(self) -> _Snapshot:
        with self._reader() as con:
            generation = self._generation(con)
        snap = self._snap
        if snap is not None and snap.generation == generation:
            return snap
        with self._load_lock:  # one thread reloads; the others wait and reuse its result
            snap = self._snap
            if snap is None or snap.generation != self._current_generation():
                snap = self._load_snapshot()
                self._snap = snap
            return snap

    def _current_generation(self) -> int:
        with self._reader() as con:
            return self._generation(con)

    def _load_snapshot(self) -> _Snapshot:
        dim = config.EMBED_DIM
        with self._reader() as con:
            con.execute("BEGIN")  # one read transaction: the generation and the vectors belong together
            try:
                generation = self._generation(con)
                n = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
                ids = np.empty(n, dtype=np.int64)
                matrix = np.empty((n, dim), dtype=np.float32)
                valid_to = []
                for row, (item_id, blob, end) in enumerate(con.execute(
                    "SELECT v.item_id, v.vec, c.valid_to FROM vectors v JOIN items i ON i.id = v.item_id"
                    " LEFT JOIN contracts c ON c.doc_id = i.doc_id ORDER BY v.item_id"
                )):
                    if len(blob) != dim * 4:
                        raise RuntimeError(
                            f"item {item_id} has a {len(blob) // 4}-dimensional vector but KGB_EMBED_DIM is {dim}: "
                            "re-ingest to re-embed, then run `python -m kgb.ingest --reindex`"
                        )
                    ids[row] = item_id
                    matrix[row] = np.frombuffer(blob, dtype="<f4")
                    valid_to.append(end or "")
            finally:
                con.execute("COMMIT")
        return _Snapshot(generation, ids, _unit(matrix), np.array(valid_to, dtype=str).reshape(n))


if __name__ == "__main__":
    import sys

    with closing(sqlite3.connect(":memory:")) as probe:
        try:
            probe.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
            probe.execute("INSERT INTO t VALUES ('Хамгаалалтын ГУТАЛ')")
            ok = probe.execute("SELECT count(*) FROM t WHERE t MATCH '\"гутал\"'").fetchone()[0] == 1
            print(f"SQLite {sqlite3.sqlite_version}: FTS5 trigram {'OK, case-insensitive on Cyrillic' if ok else 'BROKEN for Cyrillic'}")
        except sqlite3.OperationalError as e:
            sys.exit(f"SQLite {sqlite3.sqlite_version} has no FTS5 trigram tokenizer: {e}")

    store = Store()
    print("index", store.path.resolve())
    print("counts", store.counts())
    if len(sys.argv) < 3 or sys.argv[1] != "fts":
        for doc in store.documents():
            print(f"  {doc['id']}  {doc['n_items']:>4} items  {doc['n_pages']:>3} pages  "
                  f"{doc['contract_no'] or '-':<16} {doc['supplier'] or '-':<28} "
                  f"{doc['valid_from'] or '?'} -> {doc['valid_to'] or '?'}  {doc['filename']}")
    if len(sys.argv) >= 3 and sys.argv[1] == "fts":
        # every word of 3+ characters must occur (the trigram index cannot match shorter ones)
        words = [w for w in " ".join(sys.argv[2:]).split() if len(w) >= 3]
        match = " AND ".join('"' + w.replace('"', '""') + '"' for w in words)
        found = store.hits(store.fts_search(match, 15)) if match else []
        print(f"keyword {match or '(no word of 3+ characters)'}: {len(found)} hits")
        for h in found:
            price = f"{h.unit_price:,.0f}" if h.unit_price is not None else "-"
            flag = {True: " EXPIRED", False: "", None: ""}[h.expired]
            print(f"  #{h.item_id:<6} {h.score:6.2f}  {h.code or '':<10} {h.name[:40]:<40} {price:>12}  "
                  f"{h.supplier or '-'} {h.contract_no or ''} p{h.page}{flag}")
    elif store.counts()["items"]:
        with store._reader() as con:
            row = con.execute("SELECT id, doc_id, name, code, spec, unit_price, search_text FROM items ORDER BY id LIMIT 1").fetchone()
        print("sample item", row)
