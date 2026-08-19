"""JSON Schema contracts for IADF (TASK-01-SCHEMAS).

Implements the normative contracts from ADD §4 (glossary), §19.2 (entity
catalogue), §20.2 (result algebra) and §25.5 (signature coverage).

Exposes cached schema loading and document validation with complete
error collection.
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

__all__ = [
    "RESULT_ALGEBRA",
    "SCHEMA_DIR",
    "SchemaValidationError",
    "load_schema",
    "validate_document",
]

SCHEMA_DIR: Path = Path(__file__).resolve().parent

RESULT_ALGEBRA: tuple[str, ...] = (
    "PASS",
    "FAIL",
    "NOT_RUN",
    "SKIPPED",
    "UNKNOWN",
    "ERROR",
    "INCONCLUSIVE",
    "TIMEOUT",
    "STALE",
    "EXPIRED",
    "SUPERSEDED",
)

_SCHEMA_FILES: dict[str, str] = {
    "evidence_receipt": "evidence_receipt.schema.json",
    "aoe": "aoe.schema.json",
    "acm": "acm.schema.json",
}

_FORMAT_CHECKER = FormatChecker()


class SchemaValidationError(Exception):
    """Validation error that aggregates all violations found."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Load and cache the schema identified by `name`.

    Args:
        name: One of "evidence_receipt", "aoe", or "acm".

    Returns:
        The parsed JSON schema as a dictionary.

    Raises:
        KeyError: If `name` is not a known schema name.
    """
    filename = _SCHEMA_FILES[name]
    path = SCHEMA_DIR / filename
    return json.loads(path.read_text(encoding="utf-8"))


def validate_document(name: str, document: dict[str, Any]) -> None:
    """Validate `document` against the schema identified by `name`.

    Collects all violations before raising, so the caller sees the
    complete set of defects in a single pass.

    Args:
        name: One of "evidence_receipt", "aoe", or "acm".
        document: The document to validate.

    Raises:
        KeyError: If `name` is not a known schema name.
        SchemaValidationError: If validation fails, containing all errors.
    """
    schema = load_schema(name)
    validator = Draft202012Validator(schema, format_checker=_FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        messages = [f"{e.json_path}: {e.message}" for e in errors]
        raise SchemaValidationError(messages)
