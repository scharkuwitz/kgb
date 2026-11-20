"""Offline tests for kgb.llm (no API key, no network)."""

import numpy as np
import pytest
from openai.lib._parsing._responses import type_to_text_format_param

from kgb import llm
from kgb.schema import ContractMeta, PageItems, TriageResult, Verdict


@pytest.mark.parametrize("model", [TriageResult, PageItems, ContractMeta, Verdict])
def test_schemas_convert_to_strict_json_schema(model):
    fmt = type_to_text_format_param(model)  # raises if the model cannot be a strict schema
    assert fmt["strict"] is True and fmt["type"] == "json_schema"


def test_embed_empty_needs_no_network():
    assert llm.embed([]).shape[0] == 0


def test_missing_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    llm.client.cache_clear()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        llm.client()
    llm.client.cache_clear()


def test_embed_batches_and_keeps_order(monkeypatch):
    calls = []

    class Fake:
        class embeddings:
            @staticmethod
            def create(model, input, dimensions):
                calls.append(len(input))
                data = [type("D", (), {"index": i, "embedding": [float(len(t))] * dimensions})() for i, t in enumerate(input)]
                return type("R", (), {"data": data[::-1]})()  # out of order on purpose

    monkeypatch.setattr(llm, "client", lambda: Fake)
    texts = ["a" * (i % 7 + 1) for i in range(llm.EMBED_BATCH + 5)]
    vectors = llm.embed(texts)
    assert calls == [llm.EMBED_BATCH, 5]
    assert vectors.dtype == np.float32
    assert [int(v[0]) for v in vectors] == [len(t) for t in texts]
