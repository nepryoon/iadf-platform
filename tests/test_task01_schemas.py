"""Red-proof tests for TASK-01-SCHEMAS."""
import json
import uuid
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

EXPECTED_RESULT_ALGEBRA = (
    "PASS", "FAIL", "NOT_RUN", "SKIPPED", "UNKNOWN", "ERROR",
    "INCONCLUSIVE", "TIMEOUT", "STALE", "EXPIRED", "SUPERSEDED",
)

SHA256_HEX = "a" * 64


def make_valid_evidence_receipt():
    return {
        "receipt_id": str(uuid.uuid4()),
        "schema_id": "iadf://schema/evidence-receipt/v1",
        "subject_digest": SHA256_HEX,
        "gate_id": "gate-1",
        "issuer_id": "issuer-1",
        "result": "PASS",
        "issued_at": "2024-01-01T00:00:00Z",
        "expires_at": None,
        "policy_digest": SHA256_HEX,
        "binding_digest": None,
        "signature": {"alg": "ed25519", "key_id": "k1", "value": "sig"},
    }


def make_valid_aoe():
    return {
        "envelope_id": str(uuid.uuid4()),
        "project_id": str(uuid.uuid4()),
        "version": 1,
        "status": "ACTIVE",
        "allowed_change_classes": ["refactor"],
        "excluded_paths": [],
        "risk_ceiling": "R1",
        "budgets": {
            "max_cost_eur": 10.5,
            "max_tokens_per_call": 1000,
            "max_wall_seconds": 60,
        },
        "data_residency": "eu-only",
        "retention_days": 365,
        "effective_from": "2024-01-01T00:00:00Z",
        "effective_to": "2025-01-01T00:00:00Z",
        "signature": {"alg": "ed25519", "key_id": "k1", "value": "sig"},
    }


def make_valid_acm():
    changeset_id = str(uuid.uuid4())
    return {
        "manifest_id": str(uuid.uuid4()),
        "backlog_item_id": str(uuid.uuid4()),
        "envelope_id": str(uuid.uuid4()),
        "version": 1,
        "status": "DRAFT",
        "scope_digest": SHA256_HEX,
        "policy_digest": SHA256_HEX,
        "budget_digest": SHA256_HEX,
        "data_class": "INT",
        "risk_class": "R1",
        "change_set_plan": [
            {
                "changeset_id": changeset_id,
                "path_globs": ["src/**"],
                "depends_on": [],
            }
        ],
        "rollback_contract": {
            "type": "redeploy_previous_digest",
            "target": "prod-eu",
        },
        "environments": ["DEV-EU"],
        "created_at": "2024-01-01T00:00:00Z",
        "activated_at": None,
    }


# ---------------------------------------------------------------------------
# Package marker
# ---------------------------------------------------------------------------

def test_package_version():
    assert iadf.__version__ == "0.1.0"


# ---------------------------------------------------------------------------
# RESULT_ALGEBRA
# ---------------------------------------------------------------------------

def test_result_algebra_exact_order():
    assert RESULT_ALGEBRA == EXPECTED_RESULT_ALGEBRA


def test_result_algebra_length():
    assert len(RESULT_ALGEBRA) == 11


# ---------------------------------------------------------------------------
# SCHEMA_DIR & schema files existence / validity
# ---------------------------------------------------------------------------

def test_schema_dir_is_path():
    assert isinstance(SCHEMA_DIR, Path)
    assert SCHEMA_DIR.is_dir()


@pytest.mark.parametrize("filename", [
    "evidence_receipt.schema.json",
    "aoe.schema.json",
    "acm.schema.json",
])
def test_schema_file_is_valid_json_draft202012(filename):
    path = SCHEMA_DIR / filename
    assert path.is_file(), f"missing schema file {path}"
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert data["type"] == "object"
    assert data["additionalProperties"] is False


def test_evidence_receipt_schema_id_and_ids():
    schema = load_schema("evidence_receipt")
    assert schema["$id"] == "iadf://schema/evidence-receipt/v1"


def test_aoe_schema_id():
    schema = load_schema("aoe")
    assert schema["$id"] == "iadf://schema/autonomous-operating-envelope/v1"


def test_acm_schema_id():
    schema = load_schema("acm")
    assert schema["$id"] == "iadf://schema/autonomous-change-manifest/v1"


def test_evidence_receipt_result_enum_exact():
    schema = load_schema("evidence_receipt")
    result_enum = schema["properties"]["result"]["enum"]
    assert tuple(result_enum) == EXPECTED_RESULT_ALGEBRA


def test_load_schema_unknown_raises_keyerror():
    with pytest.raises(KeyError):
        load_schema("does_not_exist")


def test_load_schema_is_cached_same_object_or_equal():
    s1 = load_schema("aoe")
    s2 = load_schema("aoe")
    assert s1 == s2


# ---------------------------------------------------------------------------
# validate_document — EvidenceReceipt
# ---------------------------------------------------------------------------

def test_validate_evidence_receipt_valid_passes():
    validate_document("evidence_receipt", make_valid_evidence_receipt())


def test_validate_evidence_receipt_bad_result_enum_raises():
    doc = make_valid_evidence_receipt()
    doc["result"] = "MAYBE"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_document("evidence_receipt", doc)
    assert len(exc_info.value.errors) >= 1
    assert any(isinstance(e, str) for e in exc_info.value.errors)


def test_validate_evidence_receipt_bad_digest_pattern_raises():
    doc = make_valid_evidence_receipt()
    doc["subject_digest"] = "not-a-valid-digest"
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_validate_evidence_receipt_missing_required_raises():
    doc = make_valid_evidence_receipt()
    del doc["issuer_id"]
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_validate_evidence_receipt_additional_property_raises():
    doc = make_valid_evidence_receipt()
    doc["unexpected_field"] = "nope"
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_validate_evidence_receipt_bad_signature_raises():
    doc = make_valid_evidence_receipt()
    doc["signature"] = {"alg": "ed25519", "key_id": "k1"}  # missing value
    with pytest.raises(SchemaValidationError):
        validate_document("evidence_receipt", doc)


def test_validate_evidence_receipt_multiple_errors_collected():
    doc = make_valid_evidence_receipt()
    doc["result"] = "BOGUS"
    doc["subject_digest"] = "bad"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate_document("evidence_receipt", doc)
    assert len(exc_info.value.errors) >= 2


# ---------------------------------------------------------------------------
# validate_document — AOE
# ---------------------------------------------------------------------------

def test_validate_aoe_valid_passes():
    validate_document("aoe", make_valid_aoe())


def test_validate_aoe_bad_status_raises():
    doc = make_valid_aoe()
    doc["status"] = "UNKNOWN_STATE"
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_validate_aoe_bad_data_residency_raises():
    doc = make_valid_aoe()
    doc["data_residency"] = "us-only"
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_validate_aoe_budget_non_positive_raises():
    doc = make_valid_aoe()
    doc["budgets"]["max_cost_eur"] = 0
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_validate_aoe_retention_days_out_of_range_raises():
    doc = make_valid_aoe()
    doc["retention_days"] = 3000
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_validate_aoe_empty_allowed_change_classes_raises():
    doc = make_valid_aoe()
    doc["allowed_change_classes"] = []
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


def test_validate_aoe_additional_property_raises():
    doc = make_valid_aoe()
    doc["extra"] = 1
    with pytest.raises(SchemaValidationError):
        validate_document("aoe", doc)


# ---------------------------------------------------------------------------
# validate_document — ACM
# ---------------------------------------------------------------------------

def test_validate_acm_valid_passes():
    validate_document("acm", make_valid_acm())


def test_validate_acm_bad_status_raises():
    doc = make_valid_acm()
    doc["status"] = "PENDING"
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_validate_acm_bad_data_class_raises():
    doc = make_valid_acm()
    doc["data_class"] = "TOPSECRET"
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_validate_acm_empty_change_set_plan_raises():
    doc = make_valid_acm()
    doc["change_set_plan"] = []
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_validate_acm_bad_rollback_type_raises():
    doc = make_valid_acm()
    doc["rollback_contract"]["type"] = "manual_intervention"
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_validate_acm_bad_environment_raises():
    doc = make_valid_acm()
    doc["environments"] = ["MOON-BASE"]
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_validate_acm_change_set_plan_item_additional_property_raises():
    doc = make_valid_acm()
    doc["change_set_plan"][0]["extra_field"] = "nope"
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


def test_validate_acm_additional_property_raises():
    doc = make_valid_acm()
    doc["extra_top_level"] = True
    with pytest.raises(SchemaValidationError):
        validate_document("acm", doc)


# ---------------------------------------------------------------------------
# SchemaValidationError shape
# ---------------------------------------------------------------------------

def test_schema_validation_error_has_errors_attribute():
    err = SchemaValidationError(["problem 1", "problem 2"])
    assert err.errors == ["problem 1", "problem 2"]
