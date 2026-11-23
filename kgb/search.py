"""Find items: keyword and meaning together.

    search("хамгаалалтын гутал", k=10)              -> list[Hit], best first (an item, its price, its contract)
    search_many(["гутал", "сандал"], k=10)          -> list[list[Hit]], one embedding request for all queries
    search(q, mode="keyword" | "semantic" | "hybrid", active_only=True)   # one side only; skip expired contracts

Modes: "hybrid" (default) fuses both sides below; "keyword" uses the trigram index alone and makes no API
call; "semantic" uses the vectors alone. k is capped at POOL (50).

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
    uv run python -m kgb.search                              # API: the acceptance queries, ranks in all three modes
    uv run python -m kgb.search "муфт 20" "ус халаагч" [-k 10] [--mode semantic] [--active]   # API: your own queries
    uv run python -m kgb.search --keyword "муфт 20"          # no API: keyword side only (= --mode keyword)
"""

import threading
from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import get_args

import numpy as np

from kgb import config
from kgb.schema import Hit, Mode
from kgb.store import Store

MODES: tuple[str, ...] = get_args(Mode)  # ("hybrid", "keyword", "semantic")

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


def _ranked(store: Store, query: str, vector: np.ndarray | None, mode: Mode) -> list[tuple[int, float]]:
    """(item_id, score) best first, at most POOL per side. `vector` is None only in keyword mode."""
    match = match_expression(query)
    keyword = store.fts_search(match, POOL) if match and mode != "semantic" else []
    if mode == "keyword":
        return keyword
    meaning = store.vector_search(vector, POOL)
    if mode == "semantic":
        return meaning
    return rrf([[i for i, _ in keyword], [i for i, _ in meaning]])


def search_many(
    queries: list[str],
    k: int = 10,
    store: Store | None = None,
    embed: Embedder | None = None,
    today: date | None = None,
    mode: Mode = "hybrid",
    active_only: bool = False,
) -> list[list[Hit]]:
    """`search` for several queries with at most one embedding request (none in keyword mode).
    Result i belongs to queries[i]."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, not {mode!r}")
    store = store or _store(config.DB)
    k = max(0, min(k, POOL))
    asked = [n for n, query in enumerate(queries) if query.strip()]  # a blank query has no results and costs nothing
    results: list[list[Hit]] = [[] for _ in queries]
    if not asked or k == 0:
        return results
    if mode == "keyword":
        vectors = [None] * len(asked)
    else:
        vectors = _embedder(embed)([queries[n] for n in asked])
    for n, vector in zip(asked, vectors):
        ranked = _ranked(store, queries[n], vector, mode)
        if active_only:  # filter before cutting to k, so k active hits come back when that many exist
            results[n] = [h for h in store.hits(ranked, today) if h.expired is not True][:k]
        else:
            results[n] = store.hits(ranked[:k], today)
    return results


def search(
    query: str,
    k: int = 10,
    store: Store | None = None,
    embed: Embedder | None = None,
    today: date | None = None,
    mode: Mode = "hybrid",
    active_only: bool = False,
) -> list[Hit]:
    """The k best items for one query (k at most POOL), with their contract facts. Blank query: no results.
    mode: "hybrid" (keyword and meaning fused), "keyword" (no API call) or "semantic" (meaning only).
    active_only: leave out items whose contract has expired (unknown end dates are kept)."""
    if not query.strip():
        return []
    return search_many([query], k, store, embed, today, mode, active_only)[0]


def keyword_search(query: str, k: int = 10, store: Store | None = None, today: date | None = None) -> list[Hit]:
    """Keyword side alone (no API call): what the trigram index finds for the query's words."""
    return search(query, k, store, today=today, mode="keyword")


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
        print(f"  {rank:>2}. {h.score:8.4f}  {(h.code or ''):<10} {h.name[:40]:<40} {price:>16}  "
              f"{(h.supplier or '-')[:24]:<24} {(h.contract_no or '')[:12]:<12} {(h.filename or '')[-12:]} p{h.page}{flag}")


def _rank_of(hits: list[Hit], marker: str, code: str | None) -> int | None:
    for rank, h in enumerate(hits, start=1):
        if marker in (h.filename or "") and (code is None or h.code == code):
            return rank
    return None


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]

    def option(name: str, default: str) -> str:
        if name not in args:
            return default
        at = args.index(name)
        value = args[at + 1]
        del args[at : at + 2]
        return value

    mode = "keyword" if "--keyword" in args else option("--mode", "hybrid")
    k = int(option("-k", "10"))
    active_only = "--active" in args
    args = [a for a in args if a not in ("--keyword", "--active")]
    if mode not in MODES:
        sys.exit(f"--mode must be one of {', '.join(MODES)}")

    store = _store(config.DB)
    print("index", store.path.resolve(), store.counts())
    if not store.counts()["items"]:
        sys.exit("the index is empty: run `uv run python -m kgb.ingest data/contracts` first")

    if args:
        for query, hits in zip(args, search_many(args, k, store, mode=mode, active_only=active_only)):
            print(f"\n{query!r}  mode={mode}{'  active only' if active_only else ''}  keyword terms: {terms(query)}")
            _print_hits(hits)
    else:
        # the acceptance queries in every mode; hybrid and semantic embed the same texts, so embed once and reuse
        from kgb import llm

        queries = [q for q, _, _ in EXPECTED]
        vectors = dict(zip(queries, llm.embed(queries)))

        def cached(texts):
            return np.stack([vectors[t] for t in texts])

        table: dict[str, list[int | None]] = {}
        for m in MODES:
            batch = search_many(queries, 10, store, embed=cached, mode=m)
            table[m] = [_rank_of(hits, marker, code) for (_, marker, code), hits in zip(EXPECTED, batch)]
            if m == mode:
                shown = batch
        for (query, marker, code), hits, rank in zip(EXPECTED, shown, table[mode]):
            want = f"{marker}{' code ' + code if code else ''}"
            print(f"\n{'PASS' if rank else 'FAIL'}  [{mode}] {query!r} -> expect {want}: "
                  f"{'first at rank ' + str(rank) if rank else 'not in the top 10'}   keyword terms: {terms(query)}")
            _print_hits(hits[:5])
        print(f"\nrank of the expected contract in the top 10, per mode (- = missing)")
        print(f"  {'query':<26}" + "".join(f"{m:>10}" for m in MODES))
        for n, (query, marker, code) in enumerate(EXPECTED):
            print(f"  {query[:26]:<26}" + "".join(f"{table[m][n] or '-':>10}" for m in MODES))
        print("  " + " " * 26 + "".join(f"{sum(r is not None for r in table[m]):>8}/{len(EXPECTED)}" for m in MODES))
        sys.exit(0 if all(table[mode]) else 1)
