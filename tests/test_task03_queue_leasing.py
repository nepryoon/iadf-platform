"""
Red-proof tests for TASK-03-QUEUE-LEASING.

These tests import iadf.ports.queue_port and exercise QueuePort using a
fake DB-API connection (no real database, no psycopg import needed).
"""
import sys
import types
import pytest


# ---------------------------------------------------------------------------
# Guard: ensure psycopg/psycopg2 are NOT required to import the module.
# We simulate their absence by making sure they are not already imported,
# and by asserting the module doesn't need them (import success is enough).
# ---------------------------------------------------------------------------

def test_ports_package_marker_exists():
    import iadf.ports  # noqa: F401


def test_queue_port_module_importable_without_psycopg(monkeypatch):
    # Ensure psycopg / psycopg2 are not importable, to prove queue_port.py
    # does not import them at module level.
    for modname in ("psycopg", "psycopg2"):
        monkeypatch.setitem(sys.modules, modname, None)  # forces ImportError if imported

    # Force a fresh import
    for mod in list(sys.modules):
        if mod == "iadf.ports.queue_port":
            del sys.modules[mod]

    import iadf.ports.queue_port as qp  # should succeed without touching psycopg
    assert qp is not None


import iadf.ports.queue_port as qp
from iadf.ports.queue_port import QueuePort, LeasedTask


# ---------------------------------------------------------------------------
# SQL constant literal assertions (ADD contract)
# ---------------------------------------------------------------------------

def test_sql_lease_next_contains_required_literals():
    sql = qp.SQL_LEASE_NEXT
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "'PENDING'" in sql
    assert "'LEASED'" in sql
    assert "lease_expires_at < now()" in sql
    assert "ORDER BY created_at" in sql
    assert "LIMIT 1" in sql
    assert "RETURNING id, execution_id, attempt" in sql
    assert "%(worker_id)s" in sql
    assert "%(lease_seconds)s" in sql
    assert "iadf_sql_v1.changesets" in sql


def test_sql_heartbeat_contains_required_literals():
    sql = qp.SQL_HEARTBEAT
    assert "SET lease_expires_at" in sql
    assert "%(worker_id)s" in sql
    assert "%(lease_seconds)s" in sql
    assert "%(changeset_id)s" in sql
    assert "status = 'LEASED'" in sql
    assert "lease_owner = %(worker_id)s" in sql
    assert "WHERE id = %(changeset_id)s" in sql


def test_sql_complete_contains_required_literals():
    sql = qp.SQL_COMPLETE
    assert "'COMPLETED'" in sql
    assert "lease_owner = NULL" in sql
    assert "lease_expires_at = NULL" in sql
    assert "status = 'LEASED'" in sql
    assert "lease_owner = %(worker_id)s" in sql
    assert "%(changeset_id)s" in sql
    assert "WHERE id = %(changeset_id)s" in sql


def test_sql_fail_contains_required_literals():
    sql = qp.SQL_FAIL
    assert "'FAILED'" in sql
    assert "lease_owner = NULL" in sql
    assert "lease_expires_at = NULL" in sql
    assert "status = 'LEASED'" in sql
    assert "lease_owner = %(worker_id)s" in sql
    assert "%(changeset_id)s" in sql
    assert "WHERE id = %(changeset_id)s" in sql


def test_no_string_interpolation_of_values_in_sql():
    # Heuristic: the constants should not contain f-string leftovers or
    # direct value interpolation like "worker_id}" without the %()s wrapper,
    # nor should they contain "%s" positional style mixed in a way that
    # bypasses named params. We just check the named-param markers are used
    # and raw "{" curly braces (format-string artifacts) are absent.
    for sql in (qp.SQL_LEASE_NEXT, qp.SQL_HEARTBEAT, qp.SQL_COMPLETE, qp.SQL_FAIL):
        assert "{" not in sql
        assert "}" not in sql


# ---------------------------------------------------------------------------
# LeasedTask dataclass
# ---------------------------------------------------------------------------

def test_leased_task_is_frozen_dataclass():
    lt = LeasedTask(id="cs-1", execution_id="exec-1", attempt=2)
    assert lt.id == "cs-1"
    assert lt.execution_id == "exec-1"
    assert lt.attempt == 2
    with pytest.raises(Exception):
        lt.id = "other"  # frozen -> should raise (dataclasses.FrozenInstanceError)


# ---------------------------------------------------------------------------
# Fake DB-API plumbing
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, fetchone_result=None, rowcount=0, raise_on_execute=None):
        self.executed = []  # list of (sql, params)
        self._fetchone_result = fetchone_result
        self.rowcount = rowcount
        self._raise_on_execute = raise_on_execute
        self.closed = False

    def execute(self, sql, params=None):
        if self._raise_on_execute is not None:
            raise self._raise_on_execute
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class FakeConnection:
    def __init__(self, cursor_obj):
        self._cursor_obj = cursor_obj
        self.committed = False
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self._cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# lease_next
# ---------------------------------------------------------------------------

def test_lease_next_success_returns_leased_task():
    cursor = FakeCursor(fetchone_result=("cs-1", "exec-1", 3), rowcount=1)
    conn = FakeConnection(cursor)
    factory_calls = []

    def factory():
        factory_calls.append(1)
        return conn

    port = QueuePort(connection_factory=factory)
    result = port.lease_next(worker_id="worker-a", lease_seconds=30)

    assert result == LeasedTask(id="cs-1", execution_id="exec-1", attempt=3)
    assert len(factory_calls) == 1
    assert conn.committed is True
    assert conn.rolled_back is False
    assert conn.closed is True

    sql, params = cursor.executed[0]
    assert sql == qp.SQL_LEASE_NEXT
    assert params == {"worker_id": "worker-a", "lease_seconds": 30}


def test_lease_next_no_available_task_returns_none():
    cursor = FakeCursor(fetchone_result=None, rowcount=0)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    result = port.lease_next(worker_id="worker-a", lease_seconds=30)

    assert result is None
    assert conn.committed is True
    assert conn.closed is True


def test_lease_next_invalid_lease_seconds_raises_without_connection():
    factory_calls = []

    def factory():
        factory_calls.append(1)
        raise AssertionError("connection_factory should not be called")

    port = QueuePort(connection_factory=factory)

    with pytest.raises(ValueError):
        port.lease_next(worker_id="worker-a", lease_seconds=0)

    with pytest.raises(ValueError):
        port.lease_next(worker_id="worker-a", lease_seconds=-5)

    assert factory_calls == []


def test_lease_next_execute_error_rolls_back_and_closes_and_reraises():
    boom = RuntimeError("db exploded")
    cursor = FakeCursor(raise_on_execute=boom)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    with pytest.raises(RuntimeError):
        port.lease_next(worker_id="worker-a", lease_seconds=30)

    assert conn.rolled_back is True
    assert conn.committed is False
    assert conn.closed is True


# ---------------------------------------------------------------------------
# heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_success_true():
    cursor = FakeCursor(rowcount=1)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    ok = port.heartbeat(changeset_id="cs-1", worker_id="worker-a", lease_seconds=60)

    assert ok is True
    sql, params = cursor.executed[0]
    assert sql == qp.SQL_HEARTBEAT
    assert params == {
        "changeset_id": "cs-1",
        "worker_id": "worker-a",
        "lease_seconds": 60,
    }
    assert conn.committed is True
    assert conn.closed is True


def test_heartbeat_no_match_returns_false():
    cursor = FakeCursor(rowcount=0)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    ok = port.heartbeat(changeset_id="cs-1", worker_id="worker-a", lease_seconds=60)

    assert ok is False
    assert conn.committed is True


def test_heartbeat_invalid_lease_seconds_raises_without_connection():
    def factory():
        raise AssertionError("must not be called")

    port = QueuePort(connection_factory=factory)
    with pytest.raises(ValueError):
        port.heartbeat(changeset_id="cs-1", worker_id="worker-a", lease_seconds=0)


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------

def test_complete_success_true():
    cursor = FakeCursor(rowcount=1)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    ok = port.complete(changeset_id="cs-1", worker_id="worker-a")

    assert ok is True
    sql, params = cursor.executed[0]
    assert sql == qp.SQL_COMPLETE
    assert params == {"changeset_id": "cs-1", "worker_id": "worker-a"}
    assert conn.committed is True
    assert conn.closed is True


def test_complete_no_match_returns_false_and_still_commits():
    cursor = FakeCursor(rowcount=0)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    ok = port.complete(changeset_id="cs-1", worker_id="worker-a")

    assert ok is False
    assert conn.committed is True


def test_complete_execute_error_rolls_back_and_reraises():
    boom = RuntimeError("db exploded")
    cursor = FakeCursor(raise_on_execute=boom)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    with pytest.raises(RuntimeError):
        port.complete(changeset_id="cs-1", worker_id="worker-a")

    assert conn.rolled_back is True
    assert conn.closed is True


# ---------------------------------------------------------------------------
# fail
# ---------------------------------------------------------------------------

def test_fail_success_true():
    cursor = FakeCursor(rowcount=1)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    ok = port.fail(changeset_id="cs-1", worker_id="worker-a")

    assert ok is True
    sql, params = cursor.executed[0]
    assert sql == qp.SQL_FAIL
    assert params == {"changeset_id": "cs-1", "worker_id": "worker-a"}
    assert conn.committed is True
    assert conn.closed is True


def test_fail_no_match_returns_false():
    cursor = FakeCursor(rowcount=0)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    ok = port.fail(changeset_id="cs-1", worker_id="worker-a")

    assert ok is False


def test_fail_execute_error_rolls_back_and_reraises():
    boom = RuntimeError("db exploded")
    cursor = FakeCursor(raise_on_execute=boom)
    conn = FakeConnection(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    with pytest.raises(RuntimeError):
        port.fail(changeset_id="cs-1", worker_id="worker-a")

    assert conn.rolled_back is True
    assert conn.closed is True


# ---------------------------------------------------------------------------
# Connection always closed even when commit itself raises
# ---------------------------------------------------------------------------

class FakeConnectionCommitRaises(FakeConnection):
    def commit(self):
        raise RuntimeError("commit failed")


def test_connection_closed_even_if_commit_raises():
    cursor = FakeCursor(rowcount=1)
    conn = FakeConnectionCommitRaises(cursor)
    port = QueuePort(connection_factory=lambda: conn)

    with pytest.raises(RuntimeError):
        port.complete(changeset_id="cs-1", worker_id="worker-a")

    assert conn.rolled_back is True
    assert conn.closed is True
