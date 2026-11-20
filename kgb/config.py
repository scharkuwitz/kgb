"""Runtime settings: environment variables (optionally from ./.env) -> plain constants.

Every other module reads its knobs from here, so this is the one place to look
when asking "which model / folder / size is being used?".

Probe:
    uv run python -m kgb.config
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Read KEY=VALUE lines into os.environ. Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


load_dotenv()


def _env(name: str, default) -> str:
    return os.environ.get(name, str(default))


# Where things live
DATA = Path(_env("KGB_DATA", ROOT / "data"))
DB = Path(_env("KGB_DB", DATA / "kgb.sqlite"))  # disposable index, rebuilt by `reindex`
WORK = Path(_env("KGB_WORK", DATA / "work"))  # per-document artifacts = source of truth

# Models (any vision-capable chat model with Structured Outputs works for the first three).
# gpt-6-luna: $0.10 in / $0.50 out per 1M tokens (checked on OpenAI pricing page 2026-11-20);
# replaces gpt-5-mini / gpt-5-nano, which shut down 2026-12-11.
TRIAGE_MODEL = _env("KGB_TRIAGE_MODEL", "gpt-6-luna")
EXTRACT_MODEL = _env("KGB_EXTRACT_MODEL", "gpt-6-luna")
JUDGE_MODEL = _env("KGB_JUDGE_MODEL", "gpt-6-luna")
EMBED_MODEL = _env("KGB_EMBED_MODEL", "text-embedding-3-small")
EMBED_DIM = int(_env("KGB_EMBED_DIM", 512))

# Throughput
WORKERS = int(_env("KGB_WORKERS", 8))  # parallel LLM calls

# Page images
RENDER_PX = int(_env("KGB_RENDER_PX", 2000))  # long side of rendered pages (extraction)
TRIAGE_PX = int(_env("KGB_TRIAGE_PX", 768))  # long side of thumbnails (triage)


def summary() -> dict[str, str]:
    """All settings as strings; the API key is only reported as set/missing."""
    out = {k: str(v) for k, v in globals().items() if k.isupper()}
    out["OPENAI_API_KEY"] = "set" if os.environ.get("OPENAI_API_KEY") else "MISSING"
    return out


if __name__ == "__main__":
    for key, value in summary().items():
        print(f"{key:<15} {value}")
