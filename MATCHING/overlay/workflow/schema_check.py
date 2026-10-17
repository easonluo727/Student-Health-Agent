"""Validate overlay documents against overlay/schemas/*.schema.json (jsonschema, Draft 2020-12)."""
from __future__ import annotations

import functools
import json
from pathlib import Path

import jsonschema

SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


class SchemaError(ValueError):
    pass


@functools.lru_cache(maxsize=None)
def _validator(name: str) -> jsonschema.Draft202012Validator:
    with open(SCHEMAS / f"{name}.schema.json", encoding="utf-8") as f:
        return jsonschema.Draft202012Validator(json.load(f), format_checker=jsonschema.FormatChecker())


def validate(doc, name: str) -> None:
    errors = sorted(_validator(name).iter_errors(doc), key=lambda e: list(e.path))
    if errors:
        e = errors[0]
        raise SchemaError(f"{name}: {'/'.join(map(str, e.path)) or '<root>'}: {e.message}")
