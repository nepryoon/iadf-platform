"""
Canonical deterministic finite state machine for IADF workflow execution.

This module is pure and hermetic: no I/O, no randomness, no global mutation,
no imports beyond typing/dataclasses. All state transitions are validated
against the canonical transition graph defined in ADD §20.1.
"""

from dataclasses import dataclass
from typing import Tuple, FrozenSet

# ADD §20.1: The mandatory happy path (exactly 16 states in sequence)
HAPPY_PATH: Tuple[str, ...] = (
    "INTAKE",
    "PLANNED",
    "CONTRACTED",
    "TEST_RED",
    "IMPLEMENTING",
    "VERIFY_FAST",
    "VERIFY_DEEP",
    "ADVERSARIAL_REVIEW",
    "MERGE_READY",
    "AUTO_MERGED",
    "TRUSTED_BUILD",
    "SANDBOX",
    "CANARY",
    "PROGRESSIVE_RELEASE",
    "OBSERVING",
    "COMPLETE",
)

# Non-terminal failure states
FAILURE_ROUTING: Tuple[str, ...] = (
    "REPAIR",
    "FRONTIER_DIAGNOSIS",
    "AUTO_ROLLBACK",
)

# ADD §20.1, FR-IADF-040: The five canonical terminal states
TERMINAL_STATES: FrozenSet[str] = frozenset({
    "COMPLETE",
    "ROLLED_BACK",
    "ABORTED",
    "SUPERSEDED",
    "QUARANTINED",
})

# Controller-owned technical substates (never terminals, never wait for human)
TECHNICAL_SUBSTATES: FrozenSet[str] = frozenset({
    "TECHNICAL_PAUSE",
    "CONFLICT_RESOLUTION",
    "REBASING",
})

# All 23 top-level states
STATES: Tuple[str, ...] = (
    "INTAKE",
    "PLANNED",
    "CONTRACTED",
    "TEST_RED",
    "IMPLEMENTING",
    "VERIFY_FAST",
    "VERIFY_DEEP",
    "ADVERSARIAL_REVIEW",
    "MERGE_READY",
    "AUTO_MERGED",
    "TRUSTED_BUILD",
    "SANDBOX",
    "CANARY",
    "PROGRESSIVE_RELEASE",
    "OBSERVING",
    "COMPLETE",
    "REPAIR",
    "FRONTIER_DIAGNOSIS",
    "AUTO_ROLLBACK",
    "ROLLED_BACK",
    "ABORTED",
    "SUPERSEDED",
    "QUARANTINED",
)

# ADD §20.1: Exactly 29 canonical transitions (15 happy-path + 14 failure-routing)
CANONICAL_TRANSITIONS: FrozenSet[Tuple[str, str]] = frozenset({
    # Happy path (15 sequential edges)
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
    # Failure routing (14 edges)
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

# ADD §20.2: The 11-value result algebra for evidence receipts
RESULT_ALGEBRA: Tuple[str, ...] = (
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


class InvalidTransitionError(Exception):
    """Raised when attempting an invalid state transition."""
    pass


@dataclass
class IadfStateMachine:
    """
    Pure deterministic state machine for IADF workflow execution.
    
    Validates all transitions against CANONICAL_TRANSITIONS and enforces
    ADD §20.4 rule 7: terminal states are immutable.
    """

    def can_transition(self, current: str, target: str) -> bool:
        """
        Check if transition from current to target is valid.
        
        Returns False (never raises) for unknown states or invalid edges.
        """
        if current not in STATES or target not in STATES:
            return False
        if current in TERMINAL_STATES:
            return False
        return (current, target) in CANONICAL_TRANSITIONS

    def transition(self, current: str, target: str) -> str:
        """
        Execute a state transition, returning the target state.
        
        Raises InvalidTransitionError if:
        - Either state is unknown
        - The edge is not in CANONICAL_TRANSITIONS
        - Current state is terminal (ADD §20.4 rule 7)
        """
        if current not in STATES:
            raise InvalidTransitionError(f"Unknown current state: {current}")
        if target not in STATES:
            raise InvalidTransitionError(f"Unknown target state: {target}")
        if current in TERMINAL_STATES:
            raise InvalidTransitionError(
                f"Cannot transition from terminal state: {current}"
            )
        if (current, target) not in CANONICAL_TRANSITIONS:
            raise InvalidTransitionError(
                f"Invalid transition: {current} -> {target}"
            )
        return target

    def is_terminal(self, state: str) -> bool:
        """
        Check if a state is terminal.
        
        Returns False for unknown states (never raises).
        """
        return state in TERMINAL_STATES
