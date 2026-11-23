"""Offline tests for kgb.search (a real SQLite store in a temp folder, a fake embedder, no API)."""

import threading
import time
from datetime import date

import numpy as np
import pytest

from kgb import config, search
from kgb.schema import ContractMeta, Item
from kgb.search import keyword_search, match_expression, rrf, search_many, terms
from kgb.store import DocRecord, Store

DIM = 4


@pytest.fixture(autouse=True)
def small_vectors(monkeypatch):
    monkeypatch.setattr(config, "EMBED_DIM", DIM)


def item(name, code=None, text=None, **kw):
    return Item(name=name, code=code, unit_price=1000.0, currency="MNT", search_text=text or name, page=1, **kw)


def doc(doc_id, items, axes, valid_to="2099-01-01"):
    """One document whose item i points along axis axes[i] of the toy embedding space."""
    vectors = np.zeros((len(items), DIM), dtype=np.float32)
    for row, axis in enumerate(axes):
        vectors[row, axis] = 1
    meta = ContractMeta(doc_type="framework", supplier=f"Supplier {doc_id}", valid_from="2026-01-01", valid_to=valid_to)
    return DocRecord(doc_id, f"{doc_id}.pdf", 3, meta, items, vectors)


def embedder(mapping):
    """Fake embedding model: a query is the unit vector of the first key it contains (else axis 3)."""
    calls = []

    def embed(texts):
        calls.append(list(texts))
        rows = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            axis = next((a for key, a in mapping.items() if key in text), 3)
            rows[row, axis] = 1
        return rows

    embed.calls = calls
    return embed


@pytest.fixture
def st(tmp_path):
    store = Store(tmp_path / "kgb.sqlite")
    store.upsert(doc("shoes", [item("Хамгаалалтын гутал"), item("Өвлийн гутал")], [0, 0]))
    store.upsert(doc("chairs", [item("Оффисын сандал", "DHT200"), item("Ажилтан сандал")], [1, 1]))
    store.upsert(doc("pipes", [item("муфт", "603", text="муфт ф15"), item("муфт", "619", text="муфт ф20")], [2, 2], valid_to="2020-01-01"))
    return store


# ---- query building ---------------------------------------------------------


def test_terms_drop_short_words_punctuation_and_duplicates():
    assert terms("Муфт 20, «Гутал» (гутал)") == ["муфт", "гутал"]
    assert terms("М-02 6005 ф20") == ["м-02", "6005", "ф20"]  # hyphens and digits inside a word stay
    assert terms("  20 мм  ") == []  # nothing the trigram index could match
    assert terms("") == []


def test_match_expression_quotes_and_ors_the_terms():
    assert match_expression("хамгаалалтын гутал") == '"хамгаалалтын" OR "гутал"'
    assert match_expression('а"б"в') == '"а""б""в"'  # a quote cannot break out of the phrase
    assert match_expression("20") == ""


def test_odd_queries_never_break_the_fts_syntax(st):
    for query in ['"', "AND OR NOT", "гутал*", "(гутал", "гутал:", "NEAR(гутал", "-гутал", "^гутал", "a\"b"]:
        keyword_search(query, 5, st)  # must not raise


# ---- fusion -----------------------------------------------------------------


def test_rrf_rewards_agreement_and_keeps_order_among_ties():
    fused = rrf([[1, 2, 3], [3, 1]])
    assert [i for i, _ in fused] == [1, 3, 2]  # 1: ranks 1+2, 3: ranks 3+1, 2: only rank 2
    assert dict(fused)[1] == pytest.approx(1 / 61 + 1 / 62)
    assert [i for i, _ in rrf([[5], [6]])] == [5, 6]  # equal scores: first appearance wins
    assert rrf([]) == [] and rrf([[], []]) == []


# ---- search -----------------------------------------------------------------


def test_keyword_and_meaning_both_contribute(st):
    embed = embedder({"гутал": 0, "сандал": 1, "муфт": 2})
    hits = search.search("гутал", k=3, store=st, embed=embed)
    assert [h.filename for h in hits[:2]] == ["shoes.pdf", "shoes.pdf"]
    # a query the keyword side cannot read at all ("20" is under 3 characters) is carried by the vectors
    assert search.search("20", k=2, store=st, embed=embedder({"20": 2}))[0].filename == "pipes.pdf"
    # a query the vectors point nowhere for is carried by the keywords
    only_keywords = search.search("сандал", k=1, store=st, embed=embedder({}))
    assert only_keywords[0].filename == "chairs.pdf"


def test_a_code_is_found_exactly(st):
    (hit, *_) = search.search("DHT200", k=3, store=st, embed=embedder({}))
    assert (hit.code, hit.filename) == ("DHT200", "chairs.pdf")


def test_hits_carry_contract_facts_and_expiry(st):
    hits = search.search("муфт", k=2, store=st, embed=embedder({"муфт": 2}), today=date(2026, 6, 1))
    assert {h.code for h in hits} == {"603", "619"}
    assert all(h.expired is True and h.valid_to == "2020-01-01" and h.supplier == "Supplier pipes" for h in hits)
    shoe = search.search("гутал", k=1, store=st, embed=embedder({"гутал": 0}), today=date(2026, 6, 1))[0]
    assert shoe.expired is False and shoe.doc_id == "shoes"


def test_k_limits_results_and_a_small_index_is_fine(st):
    embed = embedder({"гутал": 0})
    assert len(search.search("гутал", k=1, store=st, embed=embed)) == 1
    assert len(search.search("гутал", k=100, store=st, embed=embed)) == 6  # whole index, each item once
    ids = [h.item_id for h in search.search("гутал", k=100, store=st, embed=embed)]
    assert len(ids) == len(set(ids))


def test_search_many_embeds_once_and_keeps_the_order(st):
    embed = embedder({"гутал": 0, "сандал": 1})
    results = search_many(["гутал", "сандал", "   "], k=2, store=st, embed=embed)
    assert len(embed.calls) == 1 and embed.calls[0] == ["гутал", "сандал"]  # the blank one is not sent
    assert [r[0].filename for r in results[:2]] == ["shoes.pdf", "chairs.pdf"]
    assert len(results) == 3 and results[2] == []  # a blank query still gets its slot
    assert search_many([], store=st, embed=embed) == []


def test_blank_query_and_empty_index_return_nothing_without_an_api_call(tmp_path):
    embed = embedder({})
    assert search.search("   ", store=Store(tmp_path / "a.sqlite"), embed=embed) == []
    assert embed.calls == []
    assert search.search("гутал", store=Store(tmp_path / "b.sqlite"), embed=embed) == []  # empty index


def test_concurrent_searches_while_the_index_changes(st):
    embed = embedder({"гутал": 0, "сандал": 1})
    errors, stop = [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                hits = search.search("гутал", k=5, store=st, embed=embed)
                assert hits and all(h.name for h in hits)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                return

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for n in range(12):
            if n % 2:
                st.delete_doc("extra")
            else:
                st.upsert(doc("extra", [item("Нэмэлт гутал")], [0]))
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert errors == []


def test_first_concurrent_searches_build_one_store(tmp_path, monkeypatch):
    built = []

    class SlowStore:
        def __init__(self, path):
            built.append(path)
            time.sleep(0.05)  # wide enough that every thread arrives while the first is still building

    monkeypatch.setattr(search, "Store", SlowStore)
    monkeypatch.setattr(search, "_stores", {})
    path = tmp_path / "kgb.sqlite"
    got, barrier = [], threading.Barrier(8)

    def grab():
        barrier.wait()
        got.append(search._store(path))

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1 and len({id(s) for s in got}) == 1 and len(got) == 8
    assert search._store(tmp_path / "other.sqlite") is not got[0]  # a different file gets its own Store


# ---- modes and filters --------------------------------------------------------


def test_keyword_mode_makes_no_embedding_call(st):
    def boom(texts):
        raise AssertionError("keyword mode must not embed")

    hits = search.search("сандал", k=5, store=st, embed=boom, mode="keyword")
    assert {h.filename for h in hits} == {"chairs.pdf"}
    assert search_many(["гутал", "сандал"], store=st, embed=boom, mode="keyword")[1][0].filename == "chairs.pdf"


def test_semantic_mode_ignores_the_keywords(st):
    # the words say chairs, the embedder says shoes: semantic follows the vectors, keyword the words
    embed = embedder({"сандал": 0})
    assert {h.filename for h in search.search("сандал", k=2, store=st, embed=embed, mode="semantic")} == {"shoes.pdf"}
    assert {h.filename for h in search.search("сандал", k=2, store=st, embed=embed, mode="keyword")} == {"chairs.pdf"}
    top = search.search("сандал", k=1, store=st, embed=embed, mode="semantic")[0]
    assert top.score == pytest.approx(1.0)  # cosine


def test_active_only_drops_expired_but_still_fills_k(st):
    embed = embedder({"муфт": 2})
    every = search.search("муфт", k=6, store=st, embed=embed, today=date(2026, 6, 1))
    assert every[0].expired is True  # the two pipes (expired) rank first
    active = search.search("муфт", k=3, store=st, embed=embed, today=date(2026, 6, 1), active_only=True)
    assert len(active) == 3 and all(h.expired is False for h in active)


def test_unknown_mode_and_k_cap(st):
    with pytest.raises(ValueError):
        search.search("гутал", store=st, embed=embedder({}), mode="fuzzy")
    assert len(search.search("гутал", k=10_000, store=st, embed=embedder({}))) == 6  # capped at POOL, index has 6
    assert search.search("гутал", k=0, store=st, embed=embedder({})) == []
