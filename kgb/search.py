"""Find items: keyword and meaning together.

    search("хамгаалалтын гутал", k=10)              -> list[Hit], best first (an item, its price, its contract)
    search_many(["гутал", "сандал"], k=10)          -> list[list[Hit]], one embedding request for all queries

Two rankings are fused with reciprocal rank fusion (RRF, k=60):
  - keyword: FTS5 trigram over the item text. The query is cut into words, words under 3 characters are
    dropped (the trigram index cannot match them) and the rest are OR-ed, so an item with more of the
    words ranks higher (bm25) but one missing word does not exclude it. Matching is by substring, so a
    word inside a longer one is found, and a code such as "619" matches exactly.
  - meaning: cosine between the embedded query and the item vectors. It carries what the keyword side
    cannot: short terms ("20" in "муфт 20"), synonyms and spelling differences.
RRF uses ranks only, so the two scores need no common scale. `Hit.score` is the fused score. Expired
contracts are not hidden: `Hit.expired` says so and the caller decides.

Search is read-only and thread-safe (see `kgb.store`): one `Store` can serve many threads while
`ingest` writes. The only network call is the query embedding.

Probe:
    uv run python -m kgb.search                              # API: the acceptance queries from the milestone list
    uv run python -m kgb.search "муфт 20" "ус халаагч" [-k 10]   # API: your own queries
    uv run python -m kgb.search --keyword "муфт 20"          # no API: keyword side only
"""

import threading
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path

import numpy as np

from kgb import config
from kgb.schema import Hit
from kgb.store import Store

RRF_K = 60  # the usual constant: damps the lead of the top ranks
POOL = 50  # candidates taken from each side before fusing
MIN_TERM = 3  # FTS5 trigram cannot match anything shorter
_EDGE_PUNCTUATION = ".,;:!?()[]{}<>\"'`«»“”„"

Embedder = Callable[[list[str]], np.ndarray]


def terms(query: str) -> list[str]:
    """The words of a query that the keyword index can match: split on spaces, edge punctuation removed,
    lower-cased, at least MIN_TERM characters, duplicates dropped. Hyphens and digits inside a word stay
    (codes like "М-02" or "6005")."""
    seen: dict[str, None] = {}
    for word in query.lower().split():
        word = word.strip(_EDGE_PUNCTUATION)
        if len(word) >= MIN_TERM:
            seen.setdefault(word)
    return list(seen)


def match_expression(query: str) -> str:
    """FTS5 expression for a query: each term as a quoted phrase, OR-ed. Empty when nothing can be matched."""
    return " OR ".join('"' + term.replace('"', '""') + '"' for term in terms(query))


def rrf(rankings: Iterable[Iterable[int]], k: int = RRF_K) -> list[tuple[int, float]]:
    """Fuse ranked id lists (best first). Each id scores sum(1 / (k + rank)) over the lists that have it
    (rank starts at 1); returns (id, score) best first, ties in order of first appearance."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: -pair[1])  # sorted() is stable


_stores: dict[Path, Store] = {}
_stores_lock = threading.Lock()


def _store(path: Path) -> Store:
    """One Store per index file for the process, so its vector matrix is loaded once and shared.
    Lookup and construction happen under one lock: `functools.cache` would let several threads that miss
    at the same moment each build (and keep) a Store of their own, each loading its own copy of the matrix."""
    key = Path(path).resolve()
    with _stores_lock:
        store = _stores.get(key)
        if store is None:
            store = _stores[key] = Store(key)
        return store


def _embedder(embed: Embedder | None) -> Embedder:
    if embed is not None:
        return embed
    from kgb import llm  # imported late: keyword-only use and the tests need no API client

    return llm.embed


def search_many(
    queries: list[str],
    k: int = 10,
    store: Store | None = None,
    embed: Embedder | None = None,
    today: date | None = None,
) -> list[list[Hit]]:
    """`search` for several queries with one embedding request. Result i belongs to queries[i]."""
    store = store or _store(config.DB)
    asked = [n for n, query in enumerate(queries) if query.strip()]  # a blank query has no results and costs nothing
    results: list[list[Hit]] = [[] for _ in queries]
    if not asked:
        return results
    vectors = _embedder(embed)([queries[n] for n in asked])
    for n, vector in zip(asked, vectors):
        match = match_expression(queries[n])
        keyword = [i for i, _ in store.fts_search(match, POOL)] if match else []
        meaning = [i for i, _ in store.vector_search(vector, POOL)]
        results[n] = store.hits(rrf([keyword, meaning])[:k], today)
    return results


def search(
    query: str,
    k: int = 10,
    store: Store | None = None,
    embed: Embedder | None = None,
    today: date | None = None,
) -> list[Hit]:
    """The k best items for one query, with their contract facts. Blank query: no results."""
    if not query.strip():
        return []
    return search_many([query], k, store, embed, today)[0]


def keyword_search(query: str, k: int = 10, store: Store | None = None, today: date | None = None) -> list[Hit]:
    """Keyword side alone (no API call): what the trigram index finds for the query's words."""
    store = store or _store(config.DB)
    match = match_expression(query)
    return store.hits(store.fts_search(match, k), today) if match else []


# What the milestone checklist expects: (query, marker in the source filename, item code or None).
EXPECTED = [
    ("хамгаалалтын гутал", "#A", None),
    ("оффисын сандал", "#C", None),
    ("ус халаагч", "#B", None),
    ("муфт 20", "#B", "619"),
    ("өндөрт ажиллах сургалт", "#D", None),
    ("кран оператор", "#D", None),
]


def _print_hits(hits: list[Hit]) -> None:
    for rank, h in enumerate(hits, start=1):
        price = f"{h.unit_price:,.0f} {h.currency or ''}".strip() if h.unit_price is not None else "-"
        flag = {True: "  EXPIRED", False: "", None: ""}[h.expired]
        print(f"  {rank:>2}. {h.score:.4f}  {(h.code or ''):<10} {h.name[:44]:<44} {price:>16}  "
              f"{(h.filename or '')[-12:]} p{h.page}{flag}")


def _rank_of(hits: list[Hit], marker: str, code: str | None) -> int | None:
    for rank, h in enumerate(hits, start=1):
        if marker in (h.filename or "") and (code is None or h.code == code):
            return rank
    return None


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    keyword_only = "--keyword" in args
    k = 10
    if "-k" in args:
        at = args.index("-k")
        k = int(args[at + 1])
        del args[at : at + 2]
    args = [a for a in args if a != "--keyword"]

    store = _store(config.DB)
    print("index", store.path.resolve(), store.counts())
    if not store.counts()["items"]:
        sys.exit("the index is empty: run `uv run python -m kgb.ingest data/contracts` first")

    if args:
        batch = search_many(args, k, store) if not keyword_only else [keyword_search(q, k, store) for q in args]
        for query, hits in zip(args, batch):
            print(f"\n{query!r}  keyword terms: {terms(query)}")
            _print_hits(hits)
    else:
        batch = search_many([q for q, _, _ in EXPECTED], 10, store)
        failed = 0
        for (query, marker, code), hits in zip(EXPECTED, batch):
            rank = _rank_of(hits, marker, code)
            failed += rank is None
            want = f"{marker}{' code ' + code if code else ''}"
            print(f"\n{'PASS' if rank else 'FAIL'}  {query!r} -> expect {want}: "
                  f"{'first at rank ' + str(rank) if rank else 'not in the top 10'}   keyword terms: {terms(query)}")
            _print_hits(hits[:5])
        print(f"\n{len(EXPECTED) - failed}/{len(EXPECTED)} queries reach the right contract")
        sys.exit(1 if failed else 0)
