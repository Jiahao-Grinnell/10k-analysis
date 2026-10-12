"""Settings from config.toml; relative paths resolve against the file's folder."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config.toml"


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
        data=data,
    )
