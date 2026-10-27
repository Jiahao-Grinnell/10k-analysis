"""Settings from config.toml; relative paths resolve against the file's folder."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"
CLASSIFY_DEFAULTS = {
    "backend": "openai",        # any OpenAI-compatible local server; "fake" is a test stand-in
    "base_url": "http://127.0.0.1:8080",
    "model_tag": "",            # set once a local model is chosen
    "model": "",                # model name sent in requests (llama-server ignores it)
    "parallel_requests": 4,     # concurrent requests for chunks of the SAME filing
    "timeout_seconds": 300,
    "max_output_tokens": 256,
    "context_tokens": 300,      # tail of the previous chunk given as context
    "prompt_version": "classify_v1",
    "chat_template_kwargs": {"enable_thinking": False},
}
KEEP_DEFAULTS = {
    "reference_kinds": ["note", "item", "exhibit"],  # cross-reference targets that are kept (rules.py)
}
COMPACT_DEFAULTS = {
    "rules": [],  # compaction rules to apply (pipeline/compact/rules.py); chosen with `compact --profile`
}


@dataclass(frozen=True)
class Config:
    path: Path
    raw_dir: Path
    markdown_dir: Path
    outputs_dir: Path
    runs_dir: Path
    encoding: str
    tiktoken_cache: Path
    vram_budget_mib: int
    chunk_max_tokens: int
    min_free_gb: float
    classify: dict
    keep: dict
    compact: dict
    data: dict

    @property
    def db_path(self) -> Path:
        return self.runs_dir / "pipeline.sqlite"


def load(path: Path | str | None = None) -> Config:
    path = Path(path or DEFAULT_CONFIG).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    paths, tokens = data.get("paths", {}), data.get("tokens", {})
    resolve = lambda key, default: (base / paths.get(key, default)).resolve()
    return Config(
        path=path,
        raw_dir=resolve("raw", "10K_Filings"),
        markdown_dir=resolve("markdown", "Markdown_10K"),
        outputs_dir=resolve("outputs", "outputs"),
        runs_dir=resolve("runs", "runs"),
        encoding=tokens.get("encoding", "o200k_base"),
        tiktoken_cache=(base / tokens.get("cache_dir", ".cache/tiktoken")).resolve(),
        vram_budget_mib=int(data.get("gpu", {}).get("vram_budget_mib", 14336)),
        chunk_max_tokens=int(data.get("segment", {}).get("chunk_max_tokens", 2000)),
        min_free_gb=float(data.get("disk", {}).get("min_free_gb", 50)),
        classify={**CLASSIFY_DEFAULTS, **data.get("classify", {}),
                  "prompts_dir": str((base / data.get("classify", {}).get("prompts_dir", "prompts")).resolve())},
        keep={**KEEP_DEFAULTS, **data.get("keep", {})},
        compact={**COMPACT_DEFAULTS, **data.get("compact", {})},
        data=data,
    )
