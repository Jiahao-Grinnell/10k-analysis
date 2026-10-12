"""Versioned prompt files: definitions + instructions + JSON schema."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


@dataclass(frozen=True)
class Prompt:
    version: str
    system: str
    schema: dict
    sha256: str


def load(prompts_dir: Path | str, version: str) -> Prompt:
    folder = Path(prompts_dir)
    files = [folder / "definitions.md", folder / f"{version}.md", folder / "schema.json"]
    raw = [p.read_bytes() for p in files]
    definitions, template = raw[0].decode("utf-8").strip(), raw[1].decode("utf-8").strip()
    digest = hashlib.sha256(b"\0".join(raw)).hexdigest()
    return Prompt(version, template.replace("{definitions}", definitions), json.loads(raw[2]), digest)


def messages(prompt: Prompt, filing, chunk: dict, text: str, context: str) -> list[dict]:
    """Every message contains text from exactly one filing."""
    location = " > ".join(chunk["heading_path"])
    parts = [f"Filing: {filing.filing_id} (form {filing.form or 'unknown'}, report year {filing.year or 'unknown'})",
             f"Location: {location} | chunk {chunk['chunk_id']}"]
    if context:
        parts.append("Preceding text (context only, do not label it):\n<<<\n" + context + "\n>>>")
    parts.append("Excerpt to label:\n<<<\n" + text + "\n>>>")
    return [{"role": "system", "content": prompt.system}, {"role": "user", "content": "\n\n".join(parts)}]
