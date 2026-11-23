"""Offline tests for kgb.ingest: a real csv file, a real work directory and index, every model call faked."""

import hashlib
import json
import os
import threading

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
    for name in ("source.csv", "doc.json", "triage.json", "items/001.json", "items.json", "meta.json", "embeddings.npz"):
        assert (wd / name).exists(), name
    assert st.counts()["items"] == 3 and st.counts()["vectors"] == 3
    assert [d["supplier"] for d in st.documents()] == ["Нийлүүлэгч"]
    assert report.indexed is True
    assert not list(wd.rglob("*.tmp"))  # no temporary files left behind


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
    vectors, _ = ingest._load_embeddings(work / good.doc_id)
    ingest._save_embeddings(work / good.doc_id, vectors, "old")
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


def test_a_cached_rerun_leaves_the_index_untouched_so_searchers_keep_their_cache(env, fakes, tmp_path):
    src, work, st = env
    assert ingest.ingest_file(src, st, work).indexed is True
    generation = st.counts()["generation"]
    again = ingest.ingest_file(src, st, work)
    assert again.indexed is False and st.counts()["generation"] == generation
    fresh = store.Store(tmp_path / "other.sqlite")  # an index that lost the document gets it back, still without model calls
    calls = (fakes.triage, fakes.extract, fakes.meta, fakes.embed)
    assert ingest.ingest_file(src, fresh, work).indexed is True
    assert (fakes.triage, fakes.extract, fakes.meta, fakes.embed) == calls


def test_an_interrupted_reembedding_never_pairs_new_vectors_with_an_old_key(env, fakes, monkeypatch):
    src, work, st = env
    report = ingest.ingest_file(src, st, work)
    wd = work / report.doc_id
    items = wd / "items" / "001.json"
    items.write_text(items.read_text(encoding="utf-8").replace("гутал", "гутал2"), encoding="utf-8")
    real_replace = os.replace

    def crash_on_embeddings(src_path, dst):
        if str(dst).endswith("embeddings.npz"):
            raise OSError("power cut")
        return real_replace(src_path, dst)

    monkeypatch.setattr(os, "replace", crash_on_embeddings)
    with pytest.raises(OSError):
        ingest.ingest_file(src, st, work)
    monkeypatch.undo()
    assert not list(wd.rglob("*.tmp"))
    n, skipped = ingest.reindex(st, work)
    assert n == 0 and "stale" in skipped[0]  # detected, not silently indexed


def test_concurrent_writers_of_one_artifact_do_not_collide(tmp_path):
    target = tmp_path / "items.json"
    errors = []

    def payload(n, i):
        return f"writer {n} round {i};".encode() * 1000

    def writer(n):
        try:
            for i in range(40):
                ingest._write_bytes(target, payload(n, i))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert target.read_bytes() in {payload(n, i) for n in range(8) for i in range(40)}  # one complete payload, never a mix
    assert [p.name for p in tmp_path.iterdir()] == ["items.json"]


def _make_legacy(wd):
    """Turn a work folder into the earlier layout: embeddings.npy + embeddings.json instead of embeddings.npz."""
    vectors, key = ingest._load_embeddings(wd)
    (wd / "embeddings.npz").unlink()
    np.save(wd / "embeddings.npy", vectors)
    (wd / "embeddings.json").write_text(json.dumps({"key": key}), encoding="utf-8")
    return vectors


def test_old_embedding_files_are_converted_so_reindex_and_ingest_need_no_model_call(env, fakes, tmp_path):
    src, work, st = env
    wd = work / ingest.ingest_file(src, st, work).doc_id
    vectors = _make_legacy(wd)
    calls = (fakes.triage, fakes.extract, fakes.meta, fakes.embed)
    fresh = store.Store(tmp_path / "fresh.sqlite")
    assert ingest.reindex(fresh, work) == (1, [])
    assert (fakes.triage, fakes.extract, fakes.meta, fakes.embed) == calls
    assert (wd / "embeddings.npz").exists() and not (wd / "embeddings.npy").exists() and not (wd / "embeddings.json").exists()
    assert np.array_equal(ingest._load_embeddings(wd)[0], vectors)
    _make_legacy(wd)
    assert ingest.ingest_file(src, st, work).ran == []  # ingest accepts the old files too
    assert fakes.embed == calls[3]


def test_old_embedding_files_with_a_stale_key_are_still_reported_stale(env, fakes):
    src, work, st = env
    wd = work / ingest.ingest_file(src, st, work).doc_id
    _make_legacy(wd)
    (wd / "embeddings.json").write_text('{"key": "old"}', encoding="utf-8")
    n, skipped = ingest.reindex(st, work)
    assert n == 0 and "stale" in skipped[0]


def test_converting_old_embedding_files_survives_another_process_doing_it_at_the_same_time(env, fakes, monkeypatch):
    src, work, st = env
    wd = work / ingest.ingest_file(src, st, work).doc_id
    expected = _make_legacy(wd)

    # (1) the other converter finishes and deletes the old files before we get to read them
    real_load, fired = np.load, []

    def load_after_other_converter(path, *a, **kw):
        if str(path).endswith("embeddings.npy") and not fired:
            fired.append(True)
            vectors, key = real_load(path), json.loads((wd / "embeddings.json").read_text(encoding="utf-8"))["key"]
            ingest._save_embeddings(wd, vectors, key)
            (wd / "embeddings.npy").unlink()
            (wd / "embeddings.json").unlink()
            raise FileNotFoundError(path)
        return real_load(path, *a, **kw)

    monkeypatch.setattr(np, "load", load_after_other_converter)
    vectors, _ = ingest._load_embeddings(wd)
    monkeypatch.undo()
    assert fired and np.array_equal(vectors, expected)

    # (2) the other converter removes the old files while we are between publishing and removing them
    _make_legacy(wd)
    real_save = ingest._save_embeddings

    def save_then_other_converter_cleans_up(folder, vecs, key):
        real_save(folder, vecs, key)
        (folder / "embeddings.npy").unlink()
        (folder / "embeddings.json").unlink()

    monkeypatch.setattr(ingest, "_save_embeddings", save_then_other_converter_cleans_up)
    vectors, _ = ingest._load_embeddings(wd)  # our own unlink finds nothing to remove and must not raise
    assert np.array_equal(vectors, expected)


def test_many_threads_converting_the_same_old_folder_all_succeed(env, fakes):
    src, work, st = env
    wd = work / ingest.ingest_file(src, st, work).doc_id
    expected = _make_legacy(wd)
    results, errors = [], []

    def convert():
        try:
            results.append(ingest._load_embeddings(wd)[0])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=convert) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and len(results) == 8
    assert all(np.array_equal(r, expected) for r in results)
    assert (wd / "embeddings.npz").exists()


# ---- one uploaded PDF, and folders in parallel -----------------------------------------


def _pdf(pages=1, colour="white"):
    import io

    from PIL import Image

    images = [Image.new("RGB", (200, 300), colour) for _ in range(pages)]
    buf = io.BytesIO()
    images[0].save(buf, "PDF", save_all=True, append_images=images[1:])
    return buf.getvalue()


@pytest.fixture
def pdf_fakes(fakes, monkeypatch):
    """PDF pages are images, so the csv-reading fake extractor is replaced by one item per page."""
    def fake_extract_page(page, **_):
        fakes.extract += 1
        return PageItems(items=[Item(name=f"Бараа {page.no}", search_text=f"бараа {page.no}", unit_price=1.0)])

    monkeypatch.setattr(triage, "triage", lambda pages, **_: [PageLabel(page=p.no, label="items", reason="") for p in pages])
    monkeypatch.setattr(extract, "extract_page", fake_extract_page)
    return fakes


def test_ingest_pdf_is_the_same_document_as_ingest_file(pdf_fakes, tmp_path):
    work, st = tmp_path / "work", store.Store(tmp_path / "kgb.sqlite")
    data = _pdf(2)
    first = ingest.ingest_pdf(data, "upload.pdf", st, work)
    assert (first.ran, first.n_items, first.cached, first.indexed) == (["triage", "extract:2", "meta", "embed"], 2, False, True)
    path = tmp_path / "same.pdf"
    path.write_bytes(data)
    again = ingest.ingest_file(path, st, work)  # same bytes from disk: same id, all cached
    assert (again.doc_id, again.cached, again.indexed, again.filename) == (first.doc_id, True, False, "upload.pdf")
    assert pdf_fakes.load == 1  # the cached run did not even render the PDF


def test_check_pdf_refuses_what_is_not_a_pdf(fakes, tmp_path):
    for name, data, reason in [("a.txt", _pdf(), "type"), ("a.pdf", b"PK\x03\x04", "type"), ("a.pdf", b"", "empty")]:
        with pytest.raises(ingest.RejectedFile) as e:
            ingest.ingest_pdf(data, name, None, tmp_path / "work")
        assert e.value.reason == reason
    assert not (tmp_path / "work").exists()


def test_concurrent_uploads_of_one_pdf_run_the_pipeline_once(pdf_fakes, tmp_path):
    work, st = tmp_path / "work", store.Store(tmp_path / "kgb.sqlite")
    data, reports = _pdf(1), []
    threads = [threading.Thread(target=lambda: reports.append(ingest.ingest_pdf(data, "x.pdf", st, work))) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(reports) == 4 and sum(not r.cached for r in reports) == 1
    assert (pdf_fakes.extract, pdf_fakes.meta, pdf_fakes.embed) == (1, 1, 1)


def test_folder_ingest_with_jobs_keeps_input_order(pdf_fakes, tmp_path):
    folder = tmp_path / "docs"
    folder.mkdir()
    for n, colour in enumerate(["white", "black", "red", "blue"]):
        (folder / f"{n}.pdf").write_bytes(_pdf(1, colour))
    (folder / "bad.pdf").write_bytes(b"%PDF-1.4 broken")
    st, done = store.Store(tmp_path / "kgb.sqlite"), []
    reports, failures = ingest.ingest([folder], st, tmp_path / "work", jobs=3, on_done=done.append)
    assert [r.filename for r in reports] == ["0.pdf", "1.pdf", "2.pdf", "3.pdf"]
    assert [f.name for f, _ in failures] == ["bad.pdf"] and "RejectedFile" in failures[0][1]
    assert len(done) == 4 and st.counts()["documents"] == 4
