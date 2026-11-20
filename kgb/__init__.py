"""KGB: a lightweight contract item base.

Pipeline (one module per step, each runnable on its own with `uv run python -m kgb.<module>`):
    load -> triage -> extract -> store -> search -> check
"""

import sys

# Windows pipes and redirects default to cp1252, which cannot print Cyrillic.
# Make every probe's output UTF-8 regardless of where it is sent.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
