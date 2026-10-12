"""Text-token counting with the locally cached tiktoken encoding."""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

import tiktoken


@lru_cache(maxsize=None)
def encoding(name: str = "o200k_base", cache_dir: str | None = None):
    if cache_dir:
        os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path(cache_dir)))
    return tiktoken.get_encoding(name)


def count(text: str, name: str = "o200k_base", cache_dir: str | None = None) -> int:
    return len(encoding(name, cache_dir).encode_ordinary(text))
