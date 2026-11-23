"""Offline tests for kgb.ingest: a real csv file, a real work directory and index, every model call faked."""

import hashlib

import numpy as np
import pytest

from kgb import config, extract, ingest, llm, load, meta, store, triage
from kgb.schema import ContractMeta, Item, PageItems, PageLabel

DIM = 4
CSV = "name,price\nХамгаалалтын гутал,85000\nОффисын сандал,250000\nМуфт 20мм,1500\n"


class Fakes:
    """Counts the faked model calls so tests can assert what a run did and did not do."""

    def __init__(self):
        self.triage = self.extract = self.meta = self.embed = self.load = 0


@pytest.fixture
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setattr(config, "EMBED_DIM", DIM)

    def fake_triage(pages, **_):
        f.triage += 1
        return [PageLabel(page=p.no, label="items" if "price" in p.text else "key_terms", reason="") for p in pages]

    def fake_extract_page(page, **_):
        f.extract += 1
        rows = [line for line in page.text.splitlines() if line.startswith("|")][2:]  # skip header and separator
        names = [row.strip("| ").split("|")[0].strip() for row in rows]
        return PageItems(items=[Item(name=n, search_text=n.lower(), unit_price=1.0) for n in names])

    def fake_meta(pages, key_term_nos=None, **_):
        f.meta += 1
        return ContractMeta(doc_type="framework", supplier="Нийлүүлэгч", valid_to="2099-01-01")

    def fake_embed(texts):
        f.embed += 1
        rows = [np.frombuffer(hashlib.sha256(t.encode()).digest()[: DIM * 4], dtype=np.uint32).astype(np.float32) for t in texts]
        return np.array(rows, dtype=np.float32).reshape(len(texts), DIM)

    real_load = load.load

    def counting_load(path):
        f.load += 1
        return real_load(path)

    monkeypatch.setattr(triage, "triage", fake_triage)
    monkeypatch.setattr(extract, "extract_page", fake_extract_page)
    monkeypatch.setattr(meta, "extract_meta", fake_meta)
    monkeypatch.setattr(llm, "embed", fake_embed)
    monkeypatch.setattr(load, "load", counting_load)
    return f


@pytest.fixture
def env(tmp_path, fakes):
    src = tmp_path / "contract.csv"
    src.write_text(CSV, encoding="utf-8")
    return src, tmp_path / "work", store.Store(tmp_path / "kgb.sqlite")


def test_first_run_does_every_stage_and_writes_the_artifacts(env, fakes):
    src, work, st = env
    report = ingest.ingest_file(src, st, work)
    wd = work / report.doc_id
    assert report.ran == ["triage", "extract:1", "meta", "embed"]
    assert (report.filename, report.n_pages, report.n_items) == ("contract.csv", 1, 3)
    for name in ("source.csv", "doc.json", "triage.json", "items/001.json", "items.json", "meta.json", "embeddings.npy", "embeddings.json"):
        assert (wd / name).exists(), name
    assert st.counts()["items"] == 3 and st.counts()["vectors"] == 3
    assert [d["supplier"] for d in st.documents()] == ["Нийлүүлэгч"]


def test_rerun_makes_no_model_call_and_does_not_even_load_the_file(env, fakes):
    src, work, st = env
    ingest.ingest_file(src, st, work)
    before = (fakes.triage, fakes.extract, fakes.meta, fakes.embed, fakes.load)
    again = ingest.ingest_file(src, st, work)
    assert (fakes.triage, fakes.extract, fakes.meta, fakes.embed, fakes.load) == before
    assert again.ran == [] and again.n_items == 3
    assert st.counts()["items"] == 3  # upsert replaced, not duplicated


def test_same_content_under_another_name_is_the_same_document(env, fakes, tmp_path):
    src, work, st = env
    copy = tmp_path / "renamed.csv"
    copy.write_text(CSV, encoding="utf-8")
    a, b = ingest.ingest_file(src, st, work), ingest.ingest_file(copy, st, work)
    assert a.doc_id == b.doc_id and b.ran == []
    assert st.counts()["documents"] == 1


def test_deleting_one_artifact_redoes_only_that_stage(env, fakes):
    src, work, st = env
    report = ingest.ingest_file(src, st, work)
    (work / report.doc_id / "meta.json").unlink()
    assert ingest.ingest_file(src, st, work).ran == ["meta"]
    (work / report.doc_id / "items" / "001.json").unlink()
    assert ingest.ingest_file(src, st, work).ran == ["extract:1"]  # same texts, so the embeddings are still valid


def test_changed_item_text_invalidates_the_embeddings(env, fakes):
    src, work, st = env
    report = ingest.ingest_file(src, st, work)
    items = work / report.doc_id / "items" / "001.json"
    items.write_text(items.read_text(encoding="utf-8").replace("гутал", "гутал2"), encoding="utf-8")
    assert ingest.ingest_file(src, st, work).ran == ["embed"]


def test_a_failed_page_keeps_the_finished_ones_and_a_retry_redoes_only_it(env, fakes, monkeypatch, tmp_path):
    src, work, st = env
    big = tmp_path / "big.csv"
    big.write_text("name,price\n" + "".join(f"Бараа {i},{i}\n" for i in range(200)), encoding="utf-8")  # several text pages
    real = extract.extract_page

    def flaky(page, **kw):
        if page.no == 2:
            raise RuntimeError("model hiccup")
        return real(page, **kw)

    monkeypatch.setattr(extract, "extract_page", flaky)
    reports, failures = ingest.ingest([big], st, work)
    assert reports == [] and "model hiccup" in failures[0][1]
    wd = work / ingest.sha12(big)
    assert (wd / "items" / "001.json").exists() and not (wd / "items" / "002.json").exists()
    monkeypatch.setattr(extract, "extract_page", real)
    before = fakes.extract
    report = ingest.ingest_file(big, st, work)
    assert fakes.extract - before == 1 and report.ran[0].startswith("extract:1")


def test_reindex_rebuilds_identical_counts_with_no_model_call(env, fakes, tmp_path):
    src, work, st = env
    ingest.ingest_file(src, st, work)
    other = tmp_path / "second.csv"
    other.write_text(CSV + "Принтерийн хор,9000\n", encoding="utf-8")
    ingest.ingest_file(other, st, work)
    expected = st.counts()
    calls = (fakes.triage, fakes.extract, fakes.meta, fakes.embed)

    fresh = store.Store(tmp_path / "fresh.sqlite")
    n, skipped = ingest.reindex(fresh, work)
    assert (n, skipped) == (2, [])
    assert fresh.counts() == {**expected, "generation": fresh.counts()["generation"]}
    assert [(d["id"], d["n_items"]) for d in fresh.documents()] == [(d["id"], d["n_items"]) for d in st.documents()]
    assert (fakes.triage, fakes.extract, fakes.meta, fakes.embed) == calls
    assert len(fresh.vector_search(np.ones(DIM), k=3)) == 3


def test_reindex_skips_incomplete_and_stale_directories_and_says_why(env, fakes, tmp_path):
    src, work, st = env
    good = ingest.ingest_file(src, st, work)
    (work / "deadbeef0000").mkdir()  # an interrupted ingest
    (work / "deadbeef0000" / "doc.json").write_text("{}", encoding="utf-8")
    n, skipped = ingest.reindex(st, work)
    assert n == 1 and len(skipped) == 1 and skipped[0].startswith("deadbeef0000: missing")
    (work / good.doc_id / "embeddings.json").write_text('{"key": "old"}', encoding="utf-8")
    n, skipped = ingest.reindex(st, work)
    assert n == 0 and "stale" in skipped[0]


def test_folder_ingest_skips_unsupported_files(env, fakes, tmp_path):
    src, work, st = env
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "a.csv").write_text(CSV, encoding="utf-8")
    (folder / "notes.txt").write_text("not a contract", encoding="utf-8")
    reports, failures = ingest.ingest([folder], st, work)
    assert [r.filename for r in reports] == ["a.csv"] and failures == []


def test_embedded_text_is_the_search_text_with_the_name_as_fallback():
    assert ingest.embed_text(Item(name="Гутал", search_text="  хамгаалалтын гутал ")) == "хамгаалалтын гутал"
    assert ingest.embed_text(Item(name="Гутал", search_text="")) == "Гутал"
