"""Offline tests for kgb.store (a real SQLite file in a temp folder, tiny vectors)."""

import threading
from datetime import date

import numpy as np
import pytest

from kgb import config
from kgb.schema import ContractMeta, Item
from kgb.store import DocRecord, Store, fts_text

DIM = 4


@pytest.fixture(autouse=True)
def small_vectors(monkeypatch):
    monkeypatch.setattr(config, "EMBED_DIM", DIM)


def item(name, code=None, text=None, price=1000.0, page=1, **kw):
    return Item(name=name, code=code, unit_price=price, currency="MNT", search_text=text or name, page=page, **kw)


def vec(*values):
    return np.array(values, dtype=np.float32)


def record(doc_id="aaa", items=None, vectors=None, valid_to="2099-01-01", supplier="Нийлүүлэгч ХХК"):
    items = items if items is not None else [
        item("Хамгаалалтын гутал", "G1", price=85000.0),
        item("Оффисын сандал", "S2", price=250000.0, spec="хар"),
        item("Муфт 20мм", "619", text="Полипропилен муфт 20мм"),
    ]
    if vectors is None:
        vectors = np.eye(len(items), DIM, dtype=np.float32)
    meta = ContractMeta(doc_type="framework", contract_no="M-1", supplier=supplier, valid_from="2026-01-01", valid_to=valid_to)
    return DocRecord(doc_id, f"{doc_id}.pdf", 5, meta, items, vectors)


@pytest.fixture
def st(tmp_path):
    return Store(tmp_path / "kgb.sqlite")


def test_roundtrip_counts_and_hit_fields(st):
    st.upsert(record())
    assert st.counts() | {"generation": 0} == {"documents": 1, "items": 3, "vectors": 3, "fts_rows": 3, "generation": 0}
    (pair,) = st.fts_search('"сандал"')
    (hit,) = st.hits([pair], today=date(2026, 6, 1))
    assert (hit.name, hit.code, hit.spec, hit.unit_price, hit.currency, hit.page) == ("Оффисын сандал", "S2", "хар", 250000.0, "MNT", 1)
    assert (hit.supplier, hit.contract_no, hit.valid_to, hit.expired) == ("Нийлүүлэгч ХХК", "M-1", "2099-01-01", False)
    (doc,) = st.documents()
    assert (doc["id"], doc["n_items"], doc["supplier"]) == ("aaa", 3, "Нийлүүлэгч ХХК")


def test_expired_flag_follows_valid_to(st):
    st.upsert(record("old", valid_to="2020-01-01"))
    st.upsert(record("open", valid_to=None, items=[item("Бусад")], vectors=vec([1, 0, 0, 0])))
    by_name = {h.name: h for h in st.hits([(i, 0.0) for i in range(1, 5)], today=date(2026, 6, 1))}
    assert by_name["Хамгаалалтын гутал"].expired is True
    assert by_name["Бусад"].expired is None


def test_upsert_replaces_the_document_and_leaves_no_orphans(st):
    st.upsert(record())
    st.upsert(record(items=[item("Нэг ширхэг")]))
    counts = st.counts()
    assert (counts["documents"], counts["items"], counts["vectors"], counts["fts_rows"]) == (1, 1, 1, 1)
    assert st.fts_search('"гутал"') == []
    assert [h.name for h in st.hits(st.fts_search('"ширхэг"'))] == ["Нэг ширхэг"]


def test_delete_doc(st):
    st.upsert(record("a"))
    st.upsert(record("b"))
    assert st.delete_doc("a") is True
    assert st.delete_doc("a") is False
    counts = st.counts()
    assert (counts["documents"], counts["items"], counts["vectors"], counts["fts_rows"]) == (1, 3, 3, 3)


def test_keyword_search_is_substring_and_case_insensitive_on_cyrillic(st):
    st.upsert(record())
    names = lambda q: sorted(h.name for h in st.hits(st.fts_search(q)))  # noqa: E731
    assert names('"ГУТАЛ"') == ["Хамгаалалтын гутал"]
    assert names('"амгаалалт"') == ["Хамгаалалтын гутал"]  # inside a word
    assert names('"619"') == ["Муфт 20мм"]  # code
    assert names('"полипропилен"') == ["Муфт 20мм"]  # only in search_text
    assert names('"хар"') == ["Оффисын сандал"]  # only in spec


def test_fts_text_adds_raw_fields_only_when_missing():
    i = item("Муфт 20мм", "619", text="Полипропилен муфт 20мм", brand="PPR", spec="20мм")
    assert fts_text(i) == "Полипропилен муфт 20мм 619 PPR"


def test_vector_search_ranks_by_cosine_and_handles_small_k(st):
    st.upsert(record(vectors=np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0.9, 0.1, 0, 0]], dtype=np.float32)))
    got = st.vector_search(vec(2, 0, 0, 0), k=10)  # query length does not matter
    assert [i for i, _ in got] == [1, 3, 2]
    assert got[0][1] == pytest.approx(1.0)
    assert len(st.vector_search(vec(1, 0, 0, 0), k=1)) == 1
    assert st.vector_search(vec(1, 0, 0, 0), k=0) == []


def test_empty_index_and_wrong_dimension(st):
    assert st.vector_search(vec(1, 0, 0, 0)) == []
    assert st.fts_search('"гутал"') == []
    st.upsert(record())
    with pytest.raises(ValueError, match="dimensions"):
        st.vector_search(vec(1, 0, 0))
    with pytest.raises(ValueError, match="vectors"):
        st.upsert(record("bad", vectors=np.zeros((3, DIM + 1), dtype=np.float32)))
    with pytest.raises(ValueError, match="3 items but 2 vectors"):
        st.upsert(record("bad", vectors=np.zeros((2, DIM), dtype=np.float32)))
    assert st.counts()["documents"] == 1  # the bad records left nothing behind


def test_a_second_store_object_sees_writes_and_refreshes_its_vector_cache(st, tmp_path):
    other = Store(tmp_path / "kgb.sqlite")  # as another process or server worker would
    assert other.vector_search(vec(1, 0, 0, 0)) == []  # caches the empty snapshot
    st.upsert(record())
    assert [i for i, _ in other.vector_search(vec(1, 0, 0, 0), k=1)] == [1]
    st.upsert(record(vectors=np.array([[0, 0, 1, 0]] * 3, dtype=np.float32)))
    assert other.vector_search(vec(1, 0, 0, 0), k=1)[0][1] == pytest.approx(0.0)


def test_replace_all_is_atomic(st):
    st.upsert(record("keep"))

    def broken():
        yield record("new1")
        raise RuntimeError("disk error")

    with pytest.raises(RuntimeError):
        st.replace_all(broken())
    assert [d["id"] for d in st.documents()] == ["keep"]  # old contents survive
    assert st.replace_all([record("x"), record("y")]) == 2
    assert [d["id"] for d in st.documents()] == ["x", "y"]
    assert st.counts()["items"] == 6


def test_reads_keep_working_while_a_writer_replaces_documents(st):
    st.upsert(record("a"))
    errors, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                found = st.hits(st.fts_search('"гутал"') + st.vector_search(vec(1, 0, 0, 0), k=5))
                assert all(h.name for h in found)
                assert st.counts()["items"] in (3, 6)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for n in range(15):  # alternate between one and two documents
            st.upsert(record("b")) if n % 2 == 0 else st.delete_doc("b")
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert errors == []
