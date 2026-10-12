"""Conservative, local SEC 10-K -> Markdown conversion and token accounting.

Inspired by Jiahao-Grinnell/10-k-analysis's preparation/audit/report workflow.
Unlike a direct sec2md wrapper, retain merged/nested tables as minimal HTML
inside Markdown; preserve legacy fixed-width tables in fenced text blocks.
No LLM, keyword-based section selection, or network file retrieval is used.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from dataclasses import dataclass, field
import hashlib
import html as entities
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import unquote, urlsplit

from lxml import etree, html
import mistune
import tiktoken

ROOT = Path(__file__).resolve().parent
VERSION = "1.1.0"
CODE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
# Version 1.0's outputs can be reused only when all preservation checks passed.
# The 1.1 fixes affect missing primary documents and already-failed HTML output.
# Keep the actual conversion hash; separately record verification by this version.
COMPATIBLE_CHECKED_HASHES = {"6f5ee1eec9b012f1215dfd777260fcaa7cb4176943fcdb223fd0b67451ee3c12"}
SUPPORTED = {".htm", ".html", ".txt", ".sgml", ".sgm"}
FORMS = {"10-K", "10-K405", "10-K/A", "10-K405/A"}
BLOCKS = {"p", "div", "section", "article", "header", "footer", "blockquote",
          "li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}
TECHNICAL = {"head", "script", "style", "ix:header", "ix:hidden", "xbrli:xbrl",
             "xbrl", "link:schemaref", "link:linkbaseref"}
WS = re.compile(r"\s+")
NUMBER = re.compile(r"(?<![\w])\d+(?:,\d{3})*(?:\.\d+)?%?")
HTML_HINT = re.compile(r"<(?:html|body|div|p|font|span|h[1-6]|td|tr)\b", re.I)
DOC_START = re.compile(r"<DOCUMENT\s*>", re.I)
HIDDEN = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!important\s*)?(?:;|$)", re.I)
RENDER = mistune.create_markdown(escape=False, plugins=["table"])


class IncompleteSource(ValueError):
    """The downloaded SEC file has metadata but no report body."""


def compact(value: str) -> str:
    return WS.sub("", value)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def clean_space(value: str) -> str:
    return WS.sub(" ", value.replace("\u00a0", " ")).strip()


def escape_attr(value: str) -> str:
    # Literal blank lines in an image alt/href can terminate Markdown HTML blocks.
    return entities.escape(value, quote=True).replace("\n", "&#10;").replace("\r", "&#13;").replace("\t", "&#9;")


def tag_name(node: etree._Element) -> str:
    return node.tag.lower() if isinstance(node.tag, str) else ""


def decode_source(raw: bytes) -> tuple[str, str, list[str]]:
    """Never decode with errors=ignore/replace; report fallback encodings."""
    if raw.startswith((b"%PDF", b"PK\x03\x04")) or b"\x00" in raw[:4096] and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("Binary/PDF/ZIP input is not a supported textual filing")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16", []
    try:
        return raw.decode("utf-8-sig"), "utf-8-sig", []
    except UnicodeDecodeError:
        pass
    declaration = re.search(br"(?:charset\s*=\s*[\"']?|encoding\s*=\s*[\"'])([\w.-]+)", raw[:8192], re.I)
    encodings = [declaration.group(1).decode("ascii")] if declaration else []
    encodings.extend(["cp1252", "latin-1"])
    for encoding in dict.fromkeys(encodings):
        try:
            return raw.decode(encoding), encoding, (["Latin-1 fallback: verify source encoding"] if encoding == "latin-1" else [])
        except (UnicodeDecodeError, LookupError):
            continue
    raise ValueError("Unable to decode source without losing bytes")


@dataclass
class Document:
    kind: str
    filename: str
    description: str
    body: str


def split_submission(text: str) -> tuple[list[Document], list[str]]:
    """SEC DOCUMENT/TEXT records are SGML, not an HTML document tree."""
    starts = list(DOC_START.finditer(text))
    if not starts:
        header_end = re.search(r"</SEC-HEADER\s*>", text, re.I)
        if header_end:
            candidate = text[header_end.end():]
            candidate = re.sub(r"</SEC-DOCUMENT\s*>.*", "", candidate, flags=re.I | re.S).strip()
            if not candidate:
                raise IncompleteSource("SEC file contains only the filing header; no 10-K document body")
            return [Document("10-K", "", "", candidate)], ["SEC header found without DOCUMENT wrapper; retained trailing report text"]
        return [Document("10-K", "", "", text)], []
    docs, warnings = [], []
    for index, start in enumerate(starts):
        record = text[start.end():starts[index + 1].start() if index + 1 < len(starts) else len(text)]
        ending = re.search(r"</DOCUMENT\s*>", record, re.I)
        if ending:
            record = record[:ending.start()]
        else:
            warnings.append(f"Unclosed SEC DOCUMENT record {index + 1}")
        body_start = re.search(r"<TEXT\s*>", record, re.I)
        header = record[:body_start.start()] if body_start else record[:8192]
        def metadata(name: str) -> str:
            match = re.search(r"<" + name + r">\s*([^\r\n<]*)", header, re.I)
            return match.group(1).strip() if match else ""
        if body_start:
            body = record[body_start.end():]
            endings = list(re.finditer(r"</TEXT\s*>", body, re.I))
            if endings:
                body = body[:endings[-1].start()]
            else:
                warnings.append(f"Unclosed SEC TEXT record {index + 1}")
        else:
            body = record
            warnings.append(f"Missing SEC TEXT wrapper in record {index + 1}")
        docs.append(Document(metadata("TYPE").upper(), metadata("FILENAME"), metadata("DESCRIPTION"), body))
    return docs, warnings


def select_documents(docs: list[Document]) -> tuple[list[Document], list[dict], list[str]]:
    """Retain the 10-K and all readable textual exhibits, not just selected Items."""
    if not any(doc.kind in FORMS for doc in docs):
        available = ", ".join(f"{doc.kind or 'unknown'}:{doc.filename}" for doc in docs[:8])
        raise IncompleteSource("Source has no 10-K/10-K405 primary document; available records: " + available)
    included, excluded, warnings = [], [], []
    for doc in docs:
        suffix = Path(doc.filename).suffix.lower()
        technical = (doc.kind.startswith("EX-101") or doc.kind.startswith("EX-27")
                     or doc.kind in {"XML", "GRAPHIC", "ZIP", "JSON", "PDF"}
                     or suffix in {".xml", ".xsd", ".json", ".jpg", ".jpeg", ".gif", ".png", ".zip", ".pdf", ".xlsx"})
        encoded = bool(re.match(r"\s*begin\s+\d{3}\s+", doc.body))
        is_report = doc.kind in FORMS
        is_exhibit = doc.kind.startswith("EX-")
        if is_report and (technical or encoded):
            raise ValueError("The primary 10-K is not a readable text/HTML document")
        if not technical and not encoded and (is_report or is_exhibit):
            included.append(doc)
        else:
            reason = "technical_or_nontext_attachment" if technical or encoded else "not_report_or_textual_exhibit"
            excluded.append({"type": doc.kind, "filename": doc.filename, "reason": reason, "characters": len(doc.body)})
            if suffix == ".pdf" or doc.kind == "PDF":
                warnings.append(f"Unconverted PDF attachment: {doc.filename}")
    return included, excluded, warnings


def visible_text(node: etree._Element) -> str:
    """Do not insert spaces between adjacent inline pieces of a signed number."""
    pieces = [node.text or ""]
    for child in node:
        name = tag_name(child)
        if name == "img":
            pieces.append(child.get("alt", ""))
        else:
            text = visible_text(child)
            pieces.append(" " + text + " " if name in BLOCKS | {"table", "tr", "td", "th", "pre", "br"} else text)
        pieces.append(child.tail or "")
    return "".join(pieces)


def protect_block_start(value: str) -> str:
    # A literal numbered paragraph must not turn into a Markdown list and lose
    # its printed number. Only escape punctuation at a Markdown block start.
    def protect(match):
        prefix = match.group(2)
        return match.group(1) + prefix[:-1] + "\\" + prefix[-1]
    escaped = re.sub(r"(?m)^(\s*)([-+#]|\d+[.)])(?=\s)", protect, value)
    return re.sub(r"(?m)^(\s*)([-=]{3,})\s*$", protect, escaped)


def escape_md(value: str) -> str:
    # Numeric punctuation is escaped only where Markdown would consume it.
    return protect_block_start(re.sub(r"([\\`*_\[\]<>|])", r"\\\1", value))


def fenced(value: str) -> str:
    runs = [len(m.group()) for m in re.finditer(r"`+", value)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return f"{fence}text\n{value.rstrip()}\n{fence}"


def native_table_check(source, output) -> dict:
    cells = lambda t: [compact(visible_text(c)) for c in t.iter() if tag_name(c) in {'td', 'th'}]
    shape = lambda t: [(tag_name(c), c.get('rowspan', '1'), c.get('colspan', '1')) for c in t.iter() if tag_name(c) in {'table', 'tr', 'td', 'th'}]
    return {'mode': 'html_geometry', 'rows': len(list(source.iter('tr'))), 'cells': len(cells(source)),
            'cell_text_order_preserved': cells(source) == cells(output), 'geometry_preserved': shape(source) == shape(output)}


@dataclass
class Conversion:
    markdown: str
    source_visible: str
    raw_format: str
    details: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class HtmlConverter:
    def __init__(self, source_dir: Path):
        self.source_dir = source_dir
        self.tables: list[dict] = []
        self.images: list[dict] = []
        self.removed: Counter = Counter()
        self.warnings: list[str] = []

    def prepare(self, body: str) -> etree._Element:
        body = re.sub(r"<\?xml\b.*?\?>", "", body, flags=re.I | re.S)
        parser = html.HTMLParser(recover=True, remove_comments=True, huge_tree=True)
        tree = html.document_fromstring(body, parser=parser)
        # Remove only non-visible technical nodes; visible ix facts are unwrapped
        # by the renderer, and their displayed values/signs are never recomputed.
        for node in list(tree.iter()):
            if node.getparent() is None:
                continue
            name = tag_name(node)
            if name in TECHNICAL or "hidden" in node.attrib or HIDDEN.search(node.get("style", "")):
                self.removed[name or "hidden"] += 1
                node.drop_tree()
        if parser.error_log:
            serious = [str(e) for e in parser.error_log if e.level_name in {"ERROR", "FATAL"} and e.type_name not in {"HTML_UNKNOWN_TAG", "ERR_TAG_NAME_MISMATCH"}]
            if serious:
                self.warnings.append("HTML recovery diagnostics: " + "; ".join(serious[:3]))
        if any("position:absolute" in re.sub(r"\s", "", n.get("style", "").lower()) for n in tree.iter()):
            self.warnings.append("Absolutely positioned content: DOM text retained; visual reading order needs review")
        return tree

    def image(self, node: etree._Element) -> str:
        src, alt = node.get("src", ""), node.get("alt", "")
        parsed = urlsplit(src)
        if src.startswith("data:"):
            status, display = "embedded_not_ocr", "embedded-image"
        elif parsed.scheme or parsed.netloc:
            status, display = "external_not_fetched", src
        else:
            status = "available_not_ocr" if (self.source_dir / unquote(parsed.path)).is_file() else "missing"
            display = src or "unnamed-image"
        self.images.append({"src": src[:500], "alt": alt, "status": status})
        # Alt text is useful, but image contents cannot be asserted to be read.
        safe_src = escape_attr(display)
        return f'<img src="{safe_src}" alt="{escape_attr(alt)}">'

    def children(self, node: etree._Element) -> str:
        output = [escape_md(WS.sub(" ", node.text or ""))]
        for child in node:
            output.extend([self.render(child), escape_md(WS.sub(" ", child.tail or ""))])
        return "".join(output)

    def minimal_html(self, node: etree._Element) -> str:
        """Preserve table geometry, strip styling/XBRL/UUID attributes."""
        name = tag_name(node)
        if name == "img":
            return self.image(node)
        if name == "pre":
            return "<pre>" + entities.escape("".join(node.itertext())) + "</pre>"
        if name == "br":
            return "<br>"
        pieces = [entities.escape(WS.sub(" ", node.text or ""))]
        for child in node:
            pieces.extend([self.minimal_html(child), entities.escape(WS.sub(" ", child.tail or ""))])
        value = "".join(pieces)
        if name in BLOCKS | {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption", "sup", "sub", "a"}:
            attrs = ""
            if name in {"td", "th"}:
                for key in ("rowspan", "colspan"):
                    if node.get(key) and node.get(key) != "1":
                        attrs += f' {key}="{escape_attr(node.get(key))}"'
            for key in ({"ol": ("start",), "li": ("value",), "a": ("href",)}.get(name, ())):
                if node.get(key):
                    attrs += f' {key}="{escape_attr(node.get(key))}"'
            return f"<{name}{attrs}>{value}</{name}>"
        return value

    def table(self, node: etree._Element) -> str:
        rows = [r for r in node.iter("tr") if next(r.iterancestors("table"), None) is node]
        cells = [[c for c in row if tag_name(c) in {"td", "th"}] for row in rows]
        has_nested = any(tag_name(c) == "table" for c in node.iterdescendants())
        has_spans = any(c.get("rowspan", "1") != "1" or c.get("colspan", "1") != "1" for row in cells for c in row)
        rectangular = bool(cells) and bool(cells[0]) and all(len(row) == len(cells[0]) for row in cells)
        contains_special = any(tag_name(c) in {"pre", "img", "sup", "sub"} for c in node.iterdescendants())
        # Do not guess column alignment or fuse year headers. Simple rectangular
        # tables use pipe Markdown; other tables retain minimal native geometry.
        use_pipe = rectangular and not (has_nested or has_spans or contains_special)
        mode = "markdown" if use_pipe else "html_geometry"
        if use_pipe:
            rendered = [[escape_md(clean_space(visible_text(c))) for c in row] for row in cells]
            lines = ["|" + "|".join(row) + "|" for row in rendered]
            lines.insert(1, "|" + "|".join("---" for _ in cells[0]) + "|")
            caption = " ".join(clean_space(visible_text(c)) for c in node if tag_name(c) == "caption")
            result = (escape_md(caption) + "\n\n" if caption else "") + "\n".join(lines)
        else:
            result = self.minimal_html(node)
        # Verify the actual serialized table, including nested tables, row/col
        # spans and signs, rather than searching cells document-wide.
        rendered_tree = html.fragment_fromstring(RENDER(result), create_parent="div")
        reconstructed = next(rendered_tree.iter("table"), None)
        source_sequence = [compact(visible_text(c)) for c in node.iter() if tag_name(c) in {"td", "th"}]
        out_sequence = [compact(visible_text(c)) for c in reconstructed.iter() if tag_name(c) in {"td", "th"}] if reconstructed is not None else []
        same_cells = source_sequence == out_sequence
        if use_pipe:
            shape_ok = reconstructed is not None and len(list(reconstructed.iter("tr"))) == len(rows)
        else:
            source_shape = [(tag_name(c), c.get("rowspan", "1"), c.get("colspan", "1")) for c in node.iter() if tag_name(c) in {"table", "tr", "td", "th"}]
            out_shape = [(tag_name(c), c.get("rowspan", "1"), c.get("colspan", "1")) for c in reconstructed.iter() if tag_name(c) in {"table", "tr", "td", "th"}] if reconstructed is not None else []
            shape_ok = source_shape == out_shape
        self.tables.append({"mode": mode, "rows": len(rows), "cells": sum(map(len, cells)), "cell_text_order_preserved": same_cells, "geometry_preserved": shape_ok})
        return "\n\n" + result + "\n\n"

    def render(self, node: etree._Element) -> str:
        name = tag_name(node)
        if name == "table":
            return self.table(node)
        if name == "pre":
            return "\n\n" + fenced("".join(node.itertext())) + "\n\n"
        if name == "img":
            return self.image(node)
        if name in {"br", "hr"}:
            return "\n\n" if name == "hr" else "\n"
        if name in {"ul", "ol"}:
            # Lists containing blocks/tables do not render consistently across
            # Markdown engines. Native minimal HTML preserves list ordinals and
            # embedded financial tables without flattening their row structure.
            result = self.minimal_html(node)
            reconstructed = html.fragment_fromstring(result, create_parent="div")
            source_tables = [t for t in node.iter("table") if next(t.iterancestors("table"), None) is None]
            output_tables = [t for t in reconstructed.iter("table") if next(t.iterancestors("table"), None) is None]
            for source_table, output_table in zip(source_tables, output_tables):
                source_cells = [compact(visible_text(c)) for c in source_table.iter() if tag_name(c) in {"td", "th"}]
                output_cells = [compact(visible_text(c)) for c in output_table.iter() if tag_name(c) in {"td", "th"}]
                shape = lambda t: [(tag_name(c), c.get("rowspan", "1"), c.get("colspan", "1")) for c in t.iter() if tag_name(c) in {"table", "tr", "td", "th"}]
                self.tables.append({"mode": "html_geometry", "rows": len(list(source_table.iter('tr'))), "cells": len(source_cells),
                                    "cell_text_order_preserved": source_cells == output_cells,
                                    "geometry_preserved": shape(source_table) == shape(output_table)})
            return "\n\n" + result + "\n\n"
        content = self.children(node)
        if re.fullmatch(r"h[1-6]", name):
            return "\n\n" + "#" * int(name[1]) + " " + content.strip() + "\n\n"
        if name == "li":
            parent = node.getparent()
            prefix = "- "
            if parent is not None and tag_name(parent) == "ol":
                try:
                    number = int(parent.get("start", "1"))
                    for sibling in parent:
                        if tag_name(sibling) == "li":
                            number = int(sibling.get("value", str(number)))
                            if sibling is node:
                                break
                            number += 1
                    prefix = f"{number}. "
                except ValueError:
                    self.warnings.append("Invalid ordered-list numbering")
            return "\n\n" + prefix + content.strip().replace("\n", "\n    ") + "\n\n"
        if name == "a":
            href = node.get("href", "")
            if href and not href.startswith("#") and not href.lower().startswith(("javascript:", "mailto:")):
                return f'<a href="{escape_attr(href)}">{entities.escape(visible_text(node))}</a>'
            return content
        if name in {"sup", "sub"}:
            return f"<{name}>" + entities.escape(visible_text(node)) + f"</{name}>"
        if name in BLOCKS:
            value = content.strip()
            if not any(tag_name(c) in BLOCKS | {"table", "pre"} for c in node.iterdescendants()):
                value = protect_block_start(value)
            return "\n\n" + value + "\n\n"
        return content

    def fallback(self, body: str) -> Conversion:
        """When Markdown syntax alters content, preserve native body geometry."""
        tree = self.prepare(body)
        source_visible = visible_text(tree)
        markdown = '<div>\n' + self.minimal_html(tree) + '\n</div>\n'
        output = html.fragment_fromstring(RENDER(markdown), create_parent='div')
        source_tables = [t for t in tree.iter('table') if next(t.iterancestors('table'), None) is None]
        output_tables = [t for t in output.iter('table') if next(t.iterancestors('table'), None) is None]
        self.tables = [native_table_check(a, b) for a, b in zip(source_tables, output_tables)]
        if len(source_tables) != len(output_tables):
            self.warnings.append('Fallback table-count mismatch')
        self.warnings.append('Used minimal HTML body after Markdown preservation check failed')
        return Conversion(markdown, source_visible, 'ixbrl_html' if re.search(r'<ix:', body, re.I) else 'html',
                          {'tables': self.tables, 'images': self.images, 'removed_nodes': dict(self.removed), 'native_html_fallback': True}, self.warnings)

    def convert(self, body: str) -> Conversion:
        tree = self.prepare(body)
        source_visible = visible_text(tree)
        result = self.render(tree)
        # Collapse excess layout whitespace without reflowing fenced text.
        result = re.sub(r"\n[ \t]+\n", "\n\n", result)
        result = re.sub(r"\n{3,}", "\n\n", result).strip() + "\n"
        return Conversion(result, source_visible, "ixbrl_html" if re.search(r"<ix:", body, re.I) else "html",
                          {"tables": self.tables, "images": self.images, "removed_nodes": dict(self.removed)}, self.warnings)


def convert_legacy(body: str) -> Conversion:
    """Preserve column spacing in ASCII/SGML tables; do not infer numbers."""
    body = entities.unescape(body).replace("\r\n", "\n").replace("\r", "\n")
    body = re.sub(r"</?(?:PAGE|TEXT|DOCUMENT)\b[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"<(F\d+)>", r"[\1]", body, flags=re.I)
    body = re.sub(r"</?(?:S|C|FN|CAPTION|P|BR)\s*>", "", body, flags=re.I)
    body = re.sub(r"<\?[^>]*\?>", "", body)
    tables = list(re.finditer(r"<TABLE\b[^>]*>(.*?)</TABLE\s*>", body, re.I | re.S))
    parts, source_parts, cursor = [], [], 0
    for table in tables:
        prose = body[cursor:table.start()]
        if prose.strip():
            # Plain legacy prose may contain fixed-width statements outside
            # TABLE wrappers. Fence it too so no alignment is silently lost.
            parts.append(fenced(prose.strip("\n")))
            source_parts.append(prose)
        content = table.group(1).strip("\n")
        parts.append(fenced(content))
        source_parts.append(content)
        cursor = table.end()
    last = body[cursor:]
    if last.strip():
        parts.append(fenced(last.strip("\n")))
        source_parts.append(last)
    warnings = []
    if re.search(r"</?TABLE\b", last, re.I):
        warnings.append("Unclosed legacy TABLE: raw text retained for review")
    return Conversion("\n\n".join(parts) + "\n", "\n".join(source_parts), "legacy_fixed_width",
                      {"tables": [{"mode": "fixed_width", "cell_text_order_preserved": True, "geometry_preserved": True} for _ in tables], "images": [], "removed_nodes": {}}, warnings)


def audit_conversion(conversion: Conversion) -> dict:
    rendered = html.fragment_fromstring(RENDER(conversion.markdown), create_parent="div")
    output_text = visible_text(rendered)
    source_text = conversion.source_visible
    missing = Counter(NUMBER.findall(source_text)) - Counter(NUMBER.findall(output_text))
    exact = compact(source_text) == compact(output_text)
    tables = conversion.details["tables"]
    bad_tables = [t for t in tables if not (t["cell_text_order_preserved"] and t["geometry_preserved"])]
    images = conversion.details["images"]
    return {
        "visible_text_equal_ignoring_whitespace": exact,
        "source_visible_sha256": digest(compact(source_text)),
        "output_visible_sha256": digest(compact(output_text)),
        "source_numeric_occurrences": sum(Counter(NUMBER.findall(source_text)).values()),
        "missing_numeric_occurrences": sum(missing.values()),
        "missing_numeric_examples": dict(missing.most_common(12)),
        "top_level_tables": len(tables),
        "table_modes": dict(Counter(t["mode"] for t in tables)),
        "tables_failing_preservation": len(bad_tables),
        "table_failure_examples": bad_tables[:5],
        "images": images,
        "removed_nodes": conversion.details["removed_nodes"],
        "quality_status": "CHECKED_TEXT" if exact and not missing and not bad_tables and not images and not conversion.warnings else "REVIEW_REQUIRED",
        "warnings": conversion.warnings,
    }


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(value, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def conversion_job(source: Path, destination: Path, tokenizers: dict, metadata: dict, resume: bool = False) -> dict:
    start = time.perf_counter()
    raw = source.read_bytes()
    raw_hash = digest(raw)
    audit_path = destination.with_suffix(".audit.json")
    if resume and destination.is_file() and audit_path.is_file():
        previous = json.loads(audit_path.read_text(encoding="utf-8"))
        compatible = (previous.get("converter_sha256") in COMPATIBLE_CHECKED_HASHES
                      and previous.get("quality_status") in {"CHECKED_TEXT", "REVIEW_REQUIRED"}
                      and bool(previous.get("documents"))
                      and all(d["visible_text_equal_ignoring_whitespace"] and not d["missing_numeric_occurrences"]
                              and not d["tables_failing_preservation"] for d in previous["documents"]))
        same_code = previous.get("converter_version") == VERSION and previous.get("converter_sha256") == CODE_HASH
        if (previous.get("source_sha256") == raw_hash and (same_code or compatible)
                and set(previous.get("tokens", {})) == set(tokenizers)
                and previous.get("markdown_sha256") == digest(destination.read_bytes())):
            if compatible:
                previous["compatible_converter_reuse"] = True
                previous["verified_by_converter_sha256"] = CODE_HASH
                atomic_text(audit_path, json.dumps(previous, ensure_ascii=False, indent=2))
            previous["resumed"] = True
            return previous
    text, encoding, decoding_warnings = decode_source(raw)
    try:
        docs, wrapper_warnings = split_submission(text)
        included, excluded, selection_warnings = select_documents(docs)
    except IncompleteSource as error:
        markdown = ("# Incomplete 10-K source\n\n" + escape_md(str(error)) + ".\n\n"
                    "Do not use this placeholder to infer undisclosed or zero intangible investment.\n")
        counts = {name: {"raw_source": len(tokenizer.encode_ordinary(text)), "included_documents_raw": 0,
                         "markdown": len(tokenizer.encode_ordinary(markdown)), "saved_vs_raw": None,
                         "reduction_vs_raw_pct": None, "saved_vs_included": None, "reduction_vs_included_pct": None}
                  for name, tokenizer in tokenizers.items()}
        report = {"source": str(source), "markdown": str(destination), "source_sha256": raw_hash,
                  "markdown_sha256": digest(markdown), "converter_version": VERSION, "converter_sha256": CODE_HASH, "source_encoding": encoding,
                  "raw_bytes": len(raw), "markdown_bytes": len(markdown.encode('utf-8')), "metadata": metadata,
                  "tokens": counts, "documents": [], "excluded_documents": [], "warnings": [str(error)],
                  "quality_status": "SOURCE_INCOMPLETE", "elapsed_seconds": round(time.perf_counter()-start, 3)}
        atomic_text(destination, markdown)
        atomic_text(audit_path, json.dumps(report, ensure_ascii=False, indent=2))
        return report
    content_parts, document_reports, selected_raw = [], [], []
    for index, doc in enumerate(included):
        conversion = HtmlConverter(source.parent).convert(doc.body) if HTML_HINT.search(doc.body) else convert_legacy(doc.body)
        if not conversion.markdown.strip():
            raise ValueError(f"Empty converted document: {doc.kind} {doc.filename}")
        audit = audit_conversion(conversion)
        if HTML_HINT.search(doc.body) and (not audit['visible_text_equal_ignoring_whitespace'] or audit['missing_numeric_occurrences'] or audit['tables_failing_preservation']):
            conversion = HtmlConverter(source.parent).fallback(doc.body)
            audit = audit_conversion(conversion)
        if index:
            content_parts.append(f"\n\n# Attached document: {escape_md(doc.kind)} — {escape_md(doc.filename)}\n\n")
        content_parts.append(conversion.markdown)
        document_reports.append({"type": doc.kind, "filename": doc.filename, "description": doc.description,
                                 "format": conversion.raw_format, 'native_html_fallback': conversion.details.get('native_html_fallback', False), **audit})
        selected_raw.append(doc.body)
    markdown = "".join(content_parts).strip() + "\n"
    selected_text = "\n".join(selected_raw)
    counts = {}
    for name, tokenizer in tokenizers.items():
        count = lambda s: len(tokenizer.encode_ordinary(s))
        raw_tokens, selected_tokens, md_tokens = count(text), count(selected_text), count(markdown)
        counts[name] = {"raw_source": raw_tokens, "included_documents_raw": selected_tokens, "markdown": md_tokens,
                        "saved_vs_raw": raw_tokens - md_tokens, "reduction_vs_raw_pct": round(100 * (1 - md_tokens / raw_tokens), 4),
                        "saved_vs_included": selected_tokens - md_tokens,
                        "reduction_vs_included_pct": round(100 * (1 - md_tokens / selected_tokens), 4) if selected_tokens else None}
    warnings = decoding_warnings + wrapper_warnings + selection_warnings
    report = {"source": str(source), "markdown": str(destination), "source_sha256": raw_hash,
              "markdown_sha256": digest(markdown), "converter_version": VERSION, "converter_sha256": CODE_HASH, "source_encoding": encoding,
              "raw_bytes": len(raw), "markdown_bytes": len(markdown.encode("utf-8")), "metadata": metadata,
              'container_format': 'sec_sgml' if DOC_START.search(text) else 'standalone_html' if HTML_HINT.search(text) else 'standalone_text',
              "tokens": counts, "documents": document_reports, "excluded_documents": excluded, "warnings": warnings,
              "quality_status": "CHECKED_TEXT" if not warnings and all(d["quality_status"] == "CHECKED_TEXT" for d in document_reports) else "REVIEW_REQUIRED",
              "elapsed_seconds": round(time.perf_counter() - start, 3)}
    atomic_text(destination, markdown)
    atomic_text(audit_path, json.dumps(report, ensure_ascii=False, indent=2))
    return report


def discover(input_path: Path) -> tuple[list[tuple[Path, Path, dict]], Path]:
    if input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED:
            raise ValueError(f"Unsupported extension: {input_path.suffix}")
        return [(input_path, Path(input_path.name).with_suffix(".md"), {})], input_path.parent
    manifests = sorted(input_path.rglob("filings.csv"))
    sources = []
    if manifests:
        for manifest in manifests:
            with manifest.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    if row.get("status") not in {"downloaded", "already_present"}:
                        continue
                    source = (manifest.parent / row["file"]).resolve()
                    if not source.is_relative_to(input_path):
                        raise ValueError(f"Manifest path escapes input directory: {source}")
                    if not source.is_file():
                        raise ValueError(f"Manifest filing is missing: {source}")
                    sources.append((source, source.relative_to(input_path).with_suffix(".md"), row))
    else:
        sources = [(p, p.relative_to(input_path).with_suffix(".md"), {}) for p in sorted(input_path.rglob("*")) if p.suffix.lower() in SUPPORTED and p.is_file()]
    destinations = [str(p[1]).casefold() for p in sources]
    if len(set(destinations)) != len(destinations):
        raise ValueError("Input filenames collide after changing extensions to .md")
    if not sources:
        raise ValueError("No supported filing files found")
    return sorted(sources, key=lambda s: str(s[0])), input_path


def write_summary(output: Path, reports: list[dict], names: list[str]) -> dict:
    successful = [r for r in reports if r.get("tokens") and r["quality_status"] != "SOURCE_INCOMPLETE"]
    summary = {"converter_version": VERSION, "source_files": len(reports), "converted": len(successful),
               "errors": sum(r["quality_status"] == "ERROR" for r in reports),
               "incomplete_sources": sum(r["quality_status"] == "SOURCE_INCOMPLETE" for r in reports),
               "markdown_files": sum(r["quality_status"] != "ERROR" for r in reports),
               "quality_status_counts": dict(Counter(r["quality_status"] for r in reports)),
               "raw_format_counts": dict(Counter(d["format"] for r in successful for d in r["documents"])),
               'container_format_counts': dict(Counter(r.get('container_format', 'unknown') for r in successful)),
               'native_html_fallback_documents': sum(d.get('native_html_fallback', False) for r in successful for d in r['documents']),
               "documents_with_text_mismatch": sum(not d["visible_text_equal_ignoring_whitespace"] for r in successful for d in r["documents"]),
               "missing_numeric_occurrences": sum(d["missing_numeric_occurrences"] for r in successful for d in r["documents"]),
               "tables_failing_preservation": sum(d["tables_failing_preservation"] for r in successful for d in r["documents"]),
               "tokens": {}}
    for name in names:
        raw = sum(r["tokens"][name]["raw_source"] for r in successful)
        included = sum(r["tokens"][name]["included_documents_raw"] for r in successful)
        md = sum(r["tokens"][name]["markdown"] for r in successful)
        summary["tokens"][name] = {"raw_source": raw, "included_documents_raw": included, "markdown": md,
                                  "saved_vs_raw": raw - md, "reduction_vs_raw_pct": 100 * (1 - md / raw) if raw else 0,
                                  "saved_vs_included": included - md, "reduction_vs_included_pct": 100 * (1 - md / included) if included else 0}
    fields = ["source", "markdown", "quality_status", "encoding", "raw_tokens", "included_raw_tokens", "markdown_tokens", "saved_tokens", "reduction_pct", "visible_text_mismatches", "missing_numeric_occurrences", "bad_tables", "error"]
    csv_path = output / "conversion_report.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for r in reports:
            for name in names:
                t = r.get("tokens", {}).get(name, {})
                writer.writerow({"source": r["source"], "markdown": r.get("markdown", ""), "quality_status": r["quality_status"], "encoding": name,
                                 "raw_tokens": t.get("raw_source", ""), "included_raw_tokens": t.get("included_documents_raw", ""), "markdown_tokens": t.get("markdown", ""),
                                 "saved_tokens": t.get("saved_vs_raw", ""), "reduction_pct": t.get("reduction_vs_raw_pct", ""),
                                 "visible_text_mismatches": sum(not d["visible_text_equal_ignoring_whitespace"] for d in r.get("documents", [])),
                                 "missing_numeric_occurrences": sum(d["missing_numeric_occurrences"] for d in r.get("documents", [])),
                                 "bad_tables": sum(d["tables_failing_preservation"] for d in r.get("documents", [])), "error": r.get("error", "")})
    atomic_text(output / "conversion_summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
    lines = ["# 10-K Markdown conversion report", "", f"Converted {summary['converted']:,} / {summary['source_files']:,} report bodies; incomplete sources: {summary['incomplete_sources']}; errors: {summary['errors']}.", "",
             "Local deterministic conversion. No LLM calls, no keyword-based section selection, no filings uploaded.", "",
             "| Tokenizer | Original raw tokens | Included-document raw tokens | Markdown tokens | Saved vs raw | Reduction vs raw | Reduction vs included |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    for name, t in summary["tokens"].items():
        lines.append(f"| {name} | {t['raw_source']:,} | {t['included_documents_raw']:,} | {t['markdown']:,} | {t['saved_vs_raw']:,} | {t['reduction_vs_raw_pct']:.2f}% | {t['reduction_vs_included_pct']:.2f}% |")
    lines += ["", "## What the comparison means", "",
              "- Raw-source counts include the literal HTML/SGML, metadata, hidden XBRL and encoded attachments. They are not a claim about how ChatGPT would internally process an uploaded file.",
              "- Included-document raw counts cover exactly the main report and retained readable textual exhibits, before HTML cleanup. This separates format savings from excluded technical attachments.",
              "- Counts use local tiktoken encodings, not an API billing measurement. Prompt, output, reasoning and image tokens are not included.",
              "- Total reduction is computed from summed tokens, not the average of per-file percentages. Legacy TXT can grow slightly due to Markdown fences.", "",
              "- SOURCE_INCOMPLETE placeholders are excluded from savings totals because there is no report body to compare.", "",
              "## Preservation checks", "",
              f"- Included documents with visible-text mismatch: {summary['documents_with_text_mismatch']:,}.",
              f"- Numeric occurrences absent from rendered output: {summary['missing_numeric_occurrences']:,}.",
              f"- Tables failing ordered-cell/geometry checks: {summary['tables_failing_preservation']:,}.",
              "- CHECKED_TEXT verifies normalized text and implemented table checks; it is not proof of financial interpretation or complete visual recovery.",
              "- REVIEW_REQUIRED covers images, parser/encoding diagnostics, absolute positioning and actual preservation mismatches. See each filing's .audit.json.",
              "- Merged/nested/irregular tables retain minimal HTML within valid Markdown. Legacy fixed-width tables remain fenced text. No OCR or external exhibit downloads are performed.",
              "- Financial-data schedules (EX-27), XBRL/XML and binary attachments are excluded and listed in audit records; readable EX-13 annual reports and other textual exhibits are retained.", "",
              "## Formats", ""]
    lines += [f"- {name}: {count:,} included documents" for name, count in summary["raw_format_counts"].items()]
    atomic_text(output / "conversion_report.md", "\n".join(lines) + "\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "10K_Filings")
    parser.add_argument("--output", type=Path, default=ROOT / "Markdown_10K")
    parser.add_argument("--tokenizer", action="append", help="tiktoken encoding; repeat to measure more than one. Default: o200k_base")
    parser.add_argument("--limit", type=int, help="Convert the first N files (for smoke tests)")
    parser.add_argument("--workers", type=int, default=1, help="Local format conversion workers, not LLM requests (default: sequential)")
    parser.add_argument("--resume", action="store_true", help="Reuse outputs whose source, converter, tokenizers and output hashes match")
    args = parser.parse_args(argv)
    input_path, output = args.input.resolve(), args.output.resolve()
    if output == input_path or output.is_relative_to(input_path if input_path.is_dir() else input_path.parent):
        parser.error("Output must be outside the raw input directory")
    if args.workers < 1 or args.limit is not None and args.limit < 1:
        parser.error("workers/limit must be positive")
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(ROOT / ".cache" / "tiktoken"))
    names = list(dict.fromkeys(args.tokenizer or ["o200k_base"]))
    try:
        jobs, _ = discover(input_path)
        if args.limit:
            jobs = jobs[:args.limit]
        tokenizers = {name: tiktoken.get_encoding(name) for name in names}
    except Exception as error:
        parser.error(str(error))
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    total = len(jobs)
    log = output / "conversion_progress.jsonl"
    def run(job):
        source, relpath, metadata = job
        try:
            report = conversion_job(source, output / relpath, tokenizers, metadata, args.resume)
        except Exception as error:
            report = {"source": str(source), "markdown": str(output / relpath), "quality_status": "ERROR", "error": f"{type(error).__name__}: {error}"}
        report["source"] = str(source.relative_to(input_path)) if input_path.is_dir() else source.name
        report["markdown"] = str(relpath)
        return report
    def record(report):
        reports.append(report)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False) + "\n")
        t = report.get("tokens", {}).get(names[0], {})
        if t and t.get('reduction_vs_raw_pct') is not None:
            status = f"{t['raw_source']:,} -> {t['markdown']:,} ({t['reduction_vs_raw_pct']:.1f}%)"
        else:
            status = report.get("error", "; ".join(report.get('warnings', [])))
        print(f"[{len(reports)}/{total}] {report['source']}: {status} {report['quality_status']}", flush=True)
    print(f"Converting {total} filings; tokenizer={','.join(names)}; local workers={args.workers}", flush=True)
    if args.workers == 1:
        for job in jobs:
            record(run(job))
    else:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run, job) for job in jobs]
            for future in as_completed(futures):
                record(future.result())
    reports.sort(key=lambda r: r["source"])
    summary = write_summary(output, reports, names)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 1 if summary["errors"] else 2 if summary["incomplete_sources"] else 0


if __name__ == "__main__":
    sys.exit(main())
