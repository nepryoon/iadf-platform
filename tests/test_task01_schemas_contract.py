"""Red-proof tests for TASK-01-SCHEMAS (ADD §4, §19.2, §20.2, §25.5).

Hermetic: pure stdlib + jsonschema + production modules under test.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import iadf
from iadf.schemas import (
    RESULT_ALGEBRA,
    SCHEMA_DIR,
    SchemaValidationError,
    load_schema,
    validate_document,
)


# ---------------------------------------------------------------------------
# Fixtures: valid baseline documents
# ---------------------------------------------------------------------------

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"
UUID_D = "44444444-4444-4444-8444-444444444444"
SHA256_A = "a" * 64
SHA256_B = "b" * 64
SHA256_C = "c" * 64


def valid_signature() -> dict:
    return {"alg": "ed25519", "key_id": "key-1", "value": "deadbeef"}


def valid_evidence_receipt() -> dict:
    return {
        "receipt_id": UUID_A,
        "schema_id": "iadf://schema/evidence-receipt/v1",
        "subject_digest": SHA256_A,
        "gate_id": "gate.unit-tests",
        "issuer_id": "issuer.ci",
        "result": "PASS",
        "issued_at": "2025-01-01T00:00:00Z",
        "expires_at": None,
        "policy_digest": SHA256_B,
        "binding_digest": None,
        "signature": valid_signature(),
    }


def valid_aoe() -> dict:
    return {
        "envelope_id": UUID_A,
        "project_id": UUID_B,
        "version": 1,
        "status": "ACTIVE",
        "allowed_change_classes": ["code", "config"],
        "excluded_paths": ["/secrets/**"],
        "risk_ceiling": "R1",
        "budgets": {
            "max_cost_eur": 10.5,
            "max_tokens_per_call": 4000,
            "max_wall_seconds": 300,
        },
        "data_residency": "eu-only",
        "retention_days": 365,
        "effective_from": "2025-01-01T00:00:00Z",
        "effective_to": "2026-01-01T00:00:00Z",
        "signature": valid_signature(),
    }


def valid_acm() -> dict:
    return {
        "manifest_id": UUID_A,
        "backlog_item_id": UUID_B,
        "envelope_id": UUID_C,
        "version": 1,
        "status": "DRAFT",
        "scope_digest": SHA256_A,
        "policy_digest": SHA256_B,
        "budget_digest": SHA256_C,
        "data_class": "INT",
        "risk_class": "R1",
        "change_set_plan": [
            {
                "changeset_id": UUID_D,
                "path_globs": ["src/**/*.py"],
                "depends_on": [],
            }
        ],
        "rollback_contract": {
            "type": "redeploy_previous_digest",
            "target": "digest:abc123",
        },
        "environments": ["DEV-EU"],
        "created_at": "2025-01-01T00:00:00Z",
        "activated_at": None,
    }


# ---------------------------------------------------------------------------
# Package marker
# ---------------------------------------------------------------------------

def test_iadf_version():
    assert iadf.__version__ == "0.1.0"


# ---------------------------------------------------------------------------
# RESULT_ALGEBRA — exact order, ADD §20.2
# ---------------------------------------------------------------------------

def test_result_algebra_exact_order():
    assert RESULT_ALGEBRA == (
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


def test_schema_dir_points_to_schemas_package():
    assert isinstance(SCHEMA_DIR, Path)
    assert (SCHEMA_DIR / "evidence_receipt.schema.json").is_file()
    assert (SCHEMA_DIR / "aoe.schema.json").is_file()
    assert (SCHEMA_DIR / "acm.schema.json").is_file()


# ---------------------------------------------------------------------------
# load_schema
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["evidence_receipt", "aoe", "acm"])
def test_load_schema_returns_dict_with_draft_2020_12(name):
    schema = load_schema(name)
    assert isinstance(schema, dict)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


def test_load_schema_unknown_name_raises_keyerror():
    with pytest.raises(KeyError):
        load_schema("does_not_exist")


def test_load_schema_is_cached_same_object_identity():
    a = load_schema("evidence_receipt")
    b = load_schema("evidence_receipt")
    assert a is b


def test_evidence_receipt_schema_id_and_result_enum():
    schema = load_schema("evidence_receipt")
    assert schema["$id"] == "iadf://schema/evidence-receipt/v1"
    assert schema["properties"]["result"]["enum"] == list(RESULT_ALGEBRA)


def test_aoe_schema_id_and_data_residency_const():
    schema = load_schema("aoe")
    assert schema["$id"] == "iadf://schema/autonomous-operating-envelope/v1"
    assert schema["properties"]["data_residency"]["const"] == "eu-only"
    assert schema["properties"]["risk_ceiling"]["enum"] == ["R0", "R1", "R2", "R3"]
    assert schema["properties"]["status"]["enum"] == [
        "DRAFT", "ACTIVE", "EXPIRED", "SUPERSEDED",
    ]


def test_acm_schema_id_and_status_enum():
    schema = load_schema("acm")
    assert schema["$id"] == "iadf://schema/autonomous-change-manifest/v1"
    assert schema["properties"]["status"]["enum"] == ["DRAFT", "ACTIVATED", "REJECTED"]
    assert schema["properties"]["data_class"]["enum"] == ["PUB", "INT", "CONF", "SRC", "SEC"]


# ---------------------------------------------------------------------------
# validate_document — happy paths
# ---------------------------------------------------------------------------

def test_validate_document_valid_evidence_receipt_passes():
    validate_document("evidence_receipt", valid_evidence_receipt())


def test_validate_document_valid_aoe_passes():
    validate_document("aoe", valid_aoe())


def test_validate_document_valid_acm_passes():
    validate_document("acm", valid_acm())


# ---------------------------------------------------------------------------
# validate_document — EvidenceReceipt violations
# ---------------------------------------------------------------------------

def test_evidence_receipt_missing_required_field_raises():
    doc = valid_evidence_receipt()
    del doc["gate_id"]
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_document("evidence_receipt", doc)
    assert any("gate_id" in e for e in exc_info.value.errors)


def test_evidence_receipt_invalid_result_enum_raises():
    doc = valid_evidence_receipt()
    doc["result"] = "MAYBE"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_document("evidence_receipt", doc)
    assert any("result" in e for e in exc_info.value.errors)


def test_evidence_receipt_bad_digest_pattern_raises():
    doc = valid_evidence_receipt()
    doc["subject_digest"] = "not-a-hex-digest"
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_evidence_receipt_uppercase_hex_digest_rejected():
    doc = valid_evidence_receipt()
    doc["subject_digest"] = "A" * 64
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_evidence_receipt_wrong_schema_id_const_rejected():
    doc = valid_evidence_receipt()
    doc["schema_id"] = "iadf://schema/other/v1"
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_evidence_receipt_additional_property_rejected():
    doc = valid_evidence_receipt()
    doc["unexpected_field"] = "nope"
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_evidence_receipt_signature_missing_key_raises():
    doc = valid_evidence_receipt()
    doc["signature"] = {"alg": "ed25519", "key_id": "k1"}
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_evidence_receipt_multiple_violations_all_reported():
    doc = valid_evidence_receipt()
    del doc["gate_id"]
    doc["result"] = "NOPE"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_document("evidence_receipt", doc)
    assert len(exc_info.value.errors) >= 2


# ---------------------------------------------------------------------------
# validate_document — AOE violations
# ---------------------------------------------------------------------------

def test_aoe_invalid_data_residency_rejected():
    doc = valid_aoe()
    doc["data_residency"] = "us-only"
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_aoe_zero_budget_rejected():
    doc = valid_aoe()
    doc["budgets"]["max_cost_eur"] = 0
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_aoe_negative_budget_rejected():
    doc = valid_aoe()
    doc["budgets"]["max_wall_seconds"] = -5
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_aoe_retention_days_over_max_rejected():
    doc = valid_aoe()
    doc["retention_days"] = 2556
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_aoe_empty_allowed_change_classes_rejected():
    doc = valid_aoe()
    doc["allowed_change_classes"] = []
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_aoe_duplicate_allowed_change_classes_rejected():
    doc = valid_aoe()
    doc["allowed_change_classes"] = ["code", "code"]
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_aoe_invalid_risk_ceiling_rejected():
    doc = valid_aoe()
    doc["risk_ceiling"] = "R9"
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_aoe_unknown_status_rejected():
    doc = valid_aoe()
    doc["status"] = "UNKNOWN_STATUS"
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_aoe_budgets_additional_property_rejected():
    doc = valid_aoe()
    doc["budgets"]["extra"] = 1
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


# ---------------------------------------------------------------------------
# validate_document — ACM violations
# ---------------------------------------------------------------------------

def test_acm_empty_change_set_plan_rejected():
    doc = valid_acm()
    doc["change_set_plan"] = []
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_acm_change_set_plan_item_additional_property_rejected():
    doc = valid_acm()
    doc["change_set_plan"][0]["extra_field"] = "nope"
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_acm_invalid_data_class_rejected():
    doc = valid_acm()
    doc["data_class"] = "TOPSECRET"
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_acm_invalid_rollback_contract_type_rejected():
    doc = valid_acm()
    doc["rollback_contract"]["type"] = "manual_fix"
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_acm_empty_environments_rejected():
    doc = valid_acm()
    doc["environments"] = []
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_acm_invalid_environment_value_rejected():
    doc = valid_acm()
    doc["environments"] = ["PROD-US"]
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_acm_bad_scope_digest_rejected():
    doc = valid_acm()
    doc["scope_digest"] = "xyz"
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_acm_duplicate_environments_rejected():
    doc = valid_acm()
    doc["environments"] = ["DEV-EU", "DEV-EU"]
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_acm_missing_rollback_contract_raises():
    doc = valid_acm()
    del doc["rollback_contract"]
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_document("acm", doc)
    assert any("rollback_contract" in e for e in exc_info.value.errors)


def test_acm_activated_status_allows_activated_at():
    doc = valid_acm()
    doc["status"] = "ACTIVATED"
    doc["activated_at"] = "2025-02-01T00:00:00Z"
    validate_document("acm", doc)


# ---------------------------------------------------------------------------
# Raw JSON files loadable and match $schema literal exactly
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "filename",
    [
        "evidence_receipt.schema.json",
        "aoe.schema.json",
        "acm.schema.json",
    ],
)
def test_raw_schema_file_is_valid_json_with_draft_literal(filename):
    path = SCHEMA_DIR / filename
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["$schema"] == "https://json-schema.org/draft/2020-12/schema"
