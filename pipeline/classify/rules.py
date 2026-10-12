"""Parse model answers and apply the keep rules. Failures are never dropped."""
from __future__ import annotations

import json

RULES_VERSION = "rules-1"
LABELS = {"KEEP", "DROP", "UNCERTAIN"}
CATEGORIES = {"rd", "advertising_marketing", "organizational_capital", "intellectual_property"}
KEPT = {"KEEP", "UNCERTAIN"}


class InvalidResponse(ValueError):
    code = "invalid_response"


def parse(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise InvalidResponse(f"not JSON: {str(raw)[:120]!r}") from exc
    if not isinstance(value, dict):
        raise InvalidResponse("not a JSON object")
    label, categories, reason = value.get("label"), value.get("categories", []), value.get("reason", "")
    if label not in LABELS:
        raise InvalidResponse(f"label {label!r} not in {sorted(LABELS)}")
    if not isinstance(categories, list) or not set(categories) <= CATEGORIES:
        raise InvalidResponse(f"bad categories {categories!r}")
    if not isinstance(reason, str):
        raise InvalidResponse("reason is not text")
    return {"label": label, "categories": sorted(set(categories)), "reason": reason.strip()}


def decide(parsed: dict | None) -> tuple[str, str]:
    """(final_label, kept_by). A failed or unreadable answer becomes UNCERTAIN, so it is kept."""
    if parsed is None:
        return "UNCERTAIN", "failure"
    return parsed["label"], "model"
