
"""
Red-Proof tests for TASK-02-FSM-CANONICAL.

These tests are fully hermetic:
- No network, no database connections.
- The SQL deliverable is validated by reading it as text.
- Only stdlib + pytest are imported.
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

HAPPY_PATH_EXPECTED = (
    "INTAKE", "PLANNED", "CONTRACTED", "TEST_RED", "IMPLEMENTING",
    "VERIFY_FAST", "VERIFY_DEEP", "ADVERSARIAL_REVIEW", "MERGE_READY",
    "AUTO_MERGED", "TRUSTED_BUILD", "SANDBOX", "CANARY",
    "PROGRESSIVE_RELEASE", "OBSERVING", "COMPLETE",
)

FAILURE_ROUTING_EXPECTED = ("REPAIR", "FRONTIER_DIAGNOSIS", "AUTO_ROLLBACK")

TERMINAL_STATES_EXPECTED = frozenset({
    "COMPLETE", "ROLLED_BACK", "ABORTED", "SUPERSEDED", "QUARANTINED",
})

TECHNICAL_SUBSTATES_EXPECTED = frozenset({
    "TECHNICAL_PAUSE", "CONFLICT_RESOLUTION", "REBASING",
})

STATES_EXPECTED = (
    "INTAKE", "PLANNED", "CONTRACTED", "TEST_RED", "IMPLEMENTING",
    "VERIFY_FAST", "VERIFY_DEEP", "ADVERSARIAL_REVIEW", "MERGE_READY",
    "AUTO_MERGED", "TRUSTED_BUILD", "SANDBOX", "CANARY",
    "PROGRESSIVE_RELEASE", "OBSERVING", "COMPLETE",
    "REPAIR", "FRONTIER_DIAGNOSIS", "AUTO_ROLLBACK",
    "ROLLED_BACK", "ABORTED", "SUPERSEDED", "QUARANTINED",
)

CANONICAL_TRANSITIONS_EXPECTED = frozenset({
    ("INTAKE", "PLANNED"),
    ("PLANNED", "CONTRACTED"),
    ("CONTRACTED", "TEST_RED"),
    ("TEST_RED", "IMPLEMENTING"),
    ("IMPLEMENTING", "VERIFY_FAST"),
    ("VERIFY_FAST", "VERIFY_DEEP"),
    ("VERIFY_DEEP", "ADVERSARIAL_REVIEW"),
    ("ADVERSARIAL_REVIEW", "MERGE_READY"),
    ("MERGE_READY", "AUTO_MERGED"),
    ("AUTO_MERGED", "TRUSTED_BUILD"),
    ("TRUSTED_BUILD", "SANDBOX"),
    ("SANDBOX", "CANARY"),
    ("CANARY", "PROGRESSIVE_RELEASE"),
    ("PROGRESSIVE_RELEASE", "OBSERVING"),
    ("OBSERVING", "COMPLETE"),
    ("VERIFY_FAST", "REPAIR"),
    ("VERIFY_DEEP", "REPAIR"),
    ("ADVERSARIAL_REVIEW", "REPAIR"),
    ("REPAIR", "VERIFY_FAST"),
    ("REPAIR", "FRONTIER_DIAGNOSIS"),
    ("FRONTIER_DIAGNOSIS", "VERIFY_FAST"),
    ("FRONTIER_DIAGNOSIS", "QUARANTINED"),
    ("SANDBOX", "AUTO_ROLLBACK"),
    ("CANARY", "AUTO_ROLLBACK"),
    ("PROGRESSIVE_RELEASE", "AUTO_ROLLBACK"),
    ("OBSERVING", "AUTO_ROLLBACK"),
    ("AUTO_ROLLBACK", "ROLLED_BACK"),
    ("INTAKE", "ABORTED"),
    ("MERGE_READY", "SUPERSEDED"),
})

RESULT_ALGEBRA_EXPECTED = (
    "PASS", "FAIL", "NOT_RUN", "SKIPPED", "UNKNOWN", "ERROR",
    "INCONCLUSIVE", "TIMEOUT", "STALE", "EXPIRED", "SUPERSEDED",
)


# --------------------------------------------------------------------------
# iadf/core/__init__.py
# --------------------------------------------------------------------------

def test_core_package_importable():
    import iadf.core  # noqa: F401


# --------------------------------------------------------------------------
# iadf/core/fsm.py — constants
# --------------------------------------------------------------------------

def test_fsm_happy_path_exact():
    from iadf.core import fsm
    assert isinstance(fsm.HAPPY_PATH, tuple)
    assert len(fsm.HAPPY_PATH) == 16
    assert fsm.HAPPY_PATH == HAPPY_PATH_EXPECTED


def test_fsm_failure_routing_exact():
    from iadf.core import fsm
    assert tuple(fsm.FAILURE_ROUTING) == FAILURE_ROUTING_EXPECTED


def test_fsm_terminal_states_exact():
    from iadf.core import fsm
    assert isinstance(fsm.TERMINAL_STATES, frozenset)
    assert fsm.TERMINAL_STATES == TERMINAL_STATES_EXPECTED


def test_fsm_technical_substates_exact():
    from iadf.core import fsm
    assert isinstance(fsm.TECHNICAL_SUBSTATES, frozenset)
    assert fsm.TECHNICAL_SUBSTATES == TECHNICAL_SUBSTATES_EXPECTED
    # substates must never be terminal states
    assert fsm.TECHNICAL_SUBSTATES.isdisjoint(fsm.TERMINAL_STATES)


def test_fsm_states_exact():
    from iadf.core import fsm
    assert len(fsm.STATES) == 23
    assert tuple(fsm.STATES) == STATES_EXPECTED
    assert len(set(fsm.STATES)) == 23  # no duplicates


def test_fsm_canonical_transitions_exact():
    from iadf.core import fsm
    assert isinstance(fsm.CANONICAL_TRANSITIONS, frozenset)
    assert len(fsm.CANONICAL_TRANSITIONS) == 29
    assert fsm.CANONICAL_TRANSITIONS == CANONICAL_TRANSITIONS_EXPECTED
    # every state referenced in transitions must be a known state
    for src, dst in fsm.CANONICAL_TRANSITIONS:
        assert src in fsm.STATES
        assert dst in fsm.STATES


def test_fsm_result_algebra_present_and_correct():
    from iadf.core import fsm
    assert tuple(fsm.RESULT_ALGEBRA) == RESULT_ALGEBRA_EXPECTED
    assert len(fsm.RESULT_ALGEBRA) == 11


# --------------------------------------------------------------------------
# iadf/core/fsm.py — InvalidTransitionError & IadfStateMachine
# --------------------------------------------------------------------------

def test_invalid_transition_error_is_exception():
    from iadf.core.fsm import InvalidTransitionError
    assert issubclass(InvalidTransitionError, Exception)


def test_state_machine_can_transition_true_for_happy_path_edges():
    from iadf.core.fsm import IadfStateMachine
    sm = IadfStateMachine()
    assert sm.can_transition("INTAKE", "PLANNED") is True
    assert sm.can_transition("OBSERVING", "COMPLETE") is True
    assert sm.can_transition("REPAIR", "FRONTIER_DIAGNOSIS") is True


def test_state_machine_can_transition_false_for_unknown_edge():
    from iadf.core.fsm import IadfStateMachine
    sm = IadfStateMachine()
    assert sm.can_transition("INTAKE", "COMPLETE") is False
    assert sm.can_transition("COMPLETE", "INTAKE") is False


def test_state_machine_can_transition_false_for_unknown_state():
    from iadf.core.fsm import IadfStateMachine
    sm = IadfStateMachine()
    assert sm.can_transition("NOT_A_STATE", "PLANNED") is False
    assert sm.can_transition("INTAKE", "NOT_A_STATE") is False


def test_state_machine_transition_returns_target_on_valid_edge():
    from iadf.core.fsm import IadfStateMachine
    sm = IadfStateMachine()
    result = sm.transition("INTAKE", "PLANNED")
    assert result == "PLANNED"


def test_state_machine_transition_raises_on_invalid_edge():
    from iadf.core.fsm import IadfStateMachine, InvalidTransitionError
    sm = IadfStateMachine()
    with pytest.raises(InvalidTransitionError):
        sm.transition("INTAKE", "COMPLETE")


def test_state_machine_transition_raises_on_unknown_state():
    from iadf.core.fsm import IadfStateMachine, InvalidTransitionError
    sm = IadfStateMachine()
    with pytest.raises(InvalidTransitionError):
        sm.transition("BOGUS", "PLANNED")
    with pytest.raises(InvalidTransitionError):
        sm.transition("INTAKE", "BOGUS")


def test_state_machine_transition_raises_when_current_is_terminal():
    from iadf.core.fsm import IadfStateMachine, InvalidTransitionError
    sm = IadfStateMachine()
    for terminal in ("COMPLETE", "ROLLED_BACK", "ABORTED", "SUPERSEDED", "QUARANTINED"):
        with pytest.raises(InvalidTransitionError):
            sm.transition(terminal, "INTAKE")


def test_state_machine_is_terminal():
    from iadf.core.fsm import IadfStateMachine
    sm = IadfStateMachine()
    for terminal in ("COMPLETE", "ROLLED_BACK", "ABORTED", "SUPERSEDED", "QUARANTINED"):
        assert sm.is_terminal(terminal) is True
    for non_terminal in ("INTAKE", "PLANNED", "REPAIR", "AUTO_ROLLBACK"):
        assert sm.is_terminal(non_terminal) is False
    # unknown state must not raise, must be treated as non-terminal
    assert sm.is_terminal("BOGUS") is False


def test_state_machine_is_stateless_across_instances():
    from iadf.core.fsm import IadfStateMachine
    sm1 = IadfStateMachine()
    sm2 = IadfStateMachine()
    assert sm1.transition("INTAKE", "PLANNED") == sm2.transition("INTAKE", "PLANNED")


# --------------------------------------------------------------------------
# db/iadf_sql_v1.sql — text-based hermetic validation (no DB connections)
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def sql_text():
    sql_path = REPO_ROOT / "db" / "iadf_sql_v1.sql"
    assert sql_path.exists(), f"Missing DDL file: {sql_path}"
    return sql_path.read_text(encoding="utf-8")


def test_sql_creates_schema_idempotently(sql_text):
    assert "CREATE SCHEMA IF NOT EXISTS iadf_sql_v1;" in sql_text


def test_sql_all_create_table_are_if_not_exists(sql_text):
    # No CREATE TABLE without IF NOT EXISTS
    bare = re.findall(r"CREATE TABLE(?!\s+IF NOT EXISTS)\s+", sql_text, re.IGNORECASE)
    assert bare == [], f"Found non-idempotent CREATE TABLE statements: {bare}"


def test_sql_all_create_index_are_if_not_exists(sql_text):
    bare = re.findall(r"CREATE INDEX(?!\s+IF NOT EXISTS)\s+", sql_text, re.IGNORECASE)
    assert bare == [], f"Found non-idempotent CREATE INDEX statements: {bare}"


@pytest.mark.parametrize("table", [
    "workflow_executions",
    "workflow_states",
    "changesets",
    "repair_attempts",
    "evidence_receipts",
    "token_ledgers",
    "outbox_events",
])
def test_sql_defines_all_seven_tables(sql_text, table):
    pattern = rf"CREATE TABLE IF NOT EXISTS iadf_sql_v1\.{table}\s*\("
    assert re.search(pattern, sql_text, re.IGNORECASE), f"Table {table} not defined idempotently"


@pytest.mark.parametrize("index_name", [
    "idx_workflow_executions_project_state",
    "idx_changesets_lease",
    "idx_changesets_execution",
    "idx_evidence_receipts_subject",
    "idx_token_ledgers_manifest",
    "idx_outbox_events_status",
])
def test_sql_defines_required_indexes(sql_text, index_name):
    pattern = rf"CREATE INDEX IF NOT EXISTS {index_name}\s+ON\s+iadf_sql_v1\."
    assert re.search(pattern, sql_text, re.IGNORECASE), f"Index {index_name} missing"


def test_sql_workflow_states_unique_constraints(sql_text):
    assert "UNIQUE (execution_id, sequence)" in sql_text
    assert "UNIQUE (command_type, idempotency_key)" in sql_text


def test_sql_repair_attempts_unique_constraint(sql_text):
    assert "UNIQUE (changeset_id, ordinal)" in sql_text


def test_sql_scope_digest_check_regex(sql_text):
    assert "scope_digest ~ '^[a-f0-9]{64}$'" in sql_text


def test_sql_subject_digest_check_regex(sql_text):
    assert "subject_digest ~ '^[a-f0-9]{64}$'" in sql_text


def test_sql_changesets_status_values(sql_text):
    for status in ("PENDING", "LEASED", "RUNNING", "COMPLETED", "FAILED", "QUARANTINED"):
        assert f"'{status}'" in sql_text


def test_sql_outbox_status_values(sql_text):
    assert "'PENDING'" in sql_text
    assert "'DISPATCHED'" in sql_text


def test_sql_outbox_events_generated_identity(sql_text):
    assert "GENERATED ALWAYS AS IDENTITY" in sql_text


def test_sql_token_ledgers_nonnegative_checks(sql_text):
    for col in ("uncached_input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens"):
        assert col in sql_text


@pytest.mark.parametrize("state", STATES_EXPECTED)
def test_sql_contains_every_state_literal(sql_text, state):
    assert f"'{state}'" in sql_text, f"State literal '{state}' missing from DDL"


@pytest.mark.parametrize("result", RESULT_ALGEBRA_EXPECTED)
def test_sql_contains_every_result_literal(sql_text, result):
    assert f"'{result}'" in sql_text, f"Result literal '{result}' missing from DDL"


def test_sql_foreign_keys_reference_iadf_sql_v1_schema(sql_text):
    assert "REFERENCES iadf_sql_v1.workflow_executions(id)" in sql_text
    assert "REFERENCES iadf_sql_v1.changesets(id)" in sql_text
