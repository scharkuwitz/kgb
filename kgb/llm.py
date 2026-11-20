"""The only module that talks to OpenAI: structured answers and embeddings.

    ask(prompt, schema, images=[...], text=..., model=...)  -> instance of `schema`
    embed(["text", ...])                                    -> float32 array, shape (n, config.EMBED_DIM)

Probes (these call the API and cost a fraction of a cent):
    uv run python -m kgb.llm ask  "<file>" <page_no>   # triage-style label for one page
    uv run python -m kgb.llm embed "хамгаалалтын гутал" "оффисын сандал"
"""

import base64
import functools
from typing import TypeVar

import numpy as np
from pydantic import BaseModel

from kgb import config
from kgb.schema import Page

T = TypeVar("T", bound=BaseModel)

EMBED_BATCH = 256  # inputs per request (API limit is 2048 inputs / 300k tokens)


@functools.cache
def client():
    """One shared client (it is thread-safe). Retries 429/5xx with backoff on its own."""
    import os

    from openai import OpenAI

    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set. Put it in .env (see .env.example).")
    return OpenAI(max_retries=5, timeout=120)


def _data_url(image: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(image).decode()}"


def ask(
    prompt: str,
    schema: type[T],
    images: list[bytes] | None = None,
    text: str | None = None,
    model: str = config.EXTRACT_MODEL,
    mime: str = "image/jpeg",
) -> T:
    """One call, strict structured output. `text` (e.g. a docx page) is appended after the prompt."""
    content: list[dict] = [{"type": "input_text", "text": prompt if text is None else f"{prompt}\n\n{text}"}]
    for image in images or []:
        content.append({"type": "input_image", "image_url": _data_url(image, mime), "detail": "high"})
    response = client().responses.parse(
        model=model,
        input=[{"role": "user", "content": content}],
        text_format=schema,
        store=False,
    )
    if response.output_parsed is None:
        raise RuntimeError(f"{model} returned no parsed {schema.__name__} (status={response.status}, refused or cut off)")
    return response.output_parsed


def ask_page(prompt: str, schema: type[T], page: Page, model: str = config.EXTRACT_MODEL) -> T:
    """`ask` for one Page, whichever kind it is (image or text)."""
    if page.image is not None:
        return ask(prompt, schema, images=[page.image], model=model, mime=page.mime)
    return ask(prompt, schema, text=page.text, model=model)


def embed(texts: list[str]) -> np.ndarray:
    """Embed texts in order. Returns float32 (len(texts), EMBED_DIM)."""
    if not texts:
        return np.zeros((0, config.EMBED_DIM), dtype=np.float32)
    rows = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = [t.strip() or " " for t in texts[start : start + EMBED_BATCH]]  # empty input is rejected
        response = client().embeddings.create(model=config.EMBED_MODEL, input=batch, dimensions=config.EMBED_DIM)
        rows += [d.embedding for d in sorted(response.data, key=lambda d: d.index)]
    return np.asarray(rows, dtype=np.float32)


if __name__ == "__main__":
    import sys

    from kgb.load import load
    from kgb.schema import PageLabel

    if len(sys.argv) >= 4 and sys.argv[1] == "ask":
        pages = load(sys.argv[2])
        page = pages[int(sys.argv[3]) - 1]
        prompt = (
            "Label this page of a document: 'items' if it lists purchasable goods or services with prices, "
            "'key_terms' if it holds the parties, dates or terms, otherwise 'other'. "
            f"The page number is {page.no}."
        )
        print(ask_page(prompt, PageLabel, page, model=config.TRIAGE_MODEL).model_dump_json(indent=1))
    elif len(sys.argv) >= 3 and sys.argv[1] == "embed":
        vectors = embed(sys.argv[2:])
        print("shape", vectors.shape, "dtype", vectors.dtype)
        print("norms", np.linalg.norm(vectors, axis=1).round(3))
        if len(vectors) > 1:
            print("cosine(0,1)", float(vectors[0] @ vectors[1] / np.linalg.norm(vectors[0]) / np.linalg.norm(vectors[1])))
    else:
        sys.exit(__doc__)
