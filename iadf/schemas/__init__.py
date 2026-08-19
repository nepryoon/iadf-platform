"""Contratti JSON Schema IADF (TASK-01-SCHEMAS).

Espone il caricamento cache-ato degli schemi normativi e la validazione
dei documenti con raccolta completa degli errori.
"""
from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema

__all__ = [
    "RESULT_ALGEBRA",
    "SCHEMA_DIR",
    "SchemaValidationError",
    "load_schema",
    "validate_document",
]

SCHEMA_DIR = Path(__file__).parent

# ADD 20.2 - algebra dei risultati, ordine normativo vincolante.
RESULT_ALGEBRA = (
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

_SCHEMA_FILES = {
    "evidence_receipt": "evidence_receipt.schema.json",
    "aoe": "aoe.schema.json",
    "acm": "acm.schema.json",
}


class SchemaValidationError(Exception):
    """Errore di validazione che aggrega tutte le violazioni riscontrate."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


@cache
def load_schema(name: str) -> dict[str, Any]:
    """Carica e memorizza in cache lo schema `name`.

    Solleva KeyError se il nome non corrisponde a uno schema noto.
    """
    filename = _SCHEMA_FILES[name]
    path = SCHEMA_DIR / filename
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_document(name: str, document: Any) -> None:
    """Valida `document` contro lo schema `name`.

    Raccoglie tutte le violazioni prima di sollevare, in modo che il
    chiamante veda l'insieme completo dei difetti in un solo passaggio.
    """
    validator = jsonschema.Draft202012Validator(
        load_schema(name),
        format_checker=jsonschema.FormatChecker(),
    )
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    if errors:
        raise SchemaValidationError(
            [f"{e.json_path}: {e.message}" for e in errors]
        )
