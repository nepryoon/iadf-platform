"""
QueuePort: PostgreSQL-based task queue with atomic leasing.

Uses FOR UPDATE SKIP LOCKED for lock-free concurrent task acquisition.
All operations are idempotent and safe against expired-lease races through
worker_id guards.
"""
from dataclasses import dataclass
from typing import Any, Callable, Optional


# SQL constants using named-parameter style %(param_name)s
SQL_LEASE_NEXT = """
UPDATE iadf_sql_v1.changesets
SET status = 'LEASED',
    lease_owner = %(worker_id)s,
    lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
    attempt = attempt + 1,
    updated_at = now()
WHERE id = (
    SELECT id
    FROM iadf_sql_v1.changesets
    WHERE status = 'PENDING'
       OR (status = 'LEASED' AND lease_expires_at < now())
    ORDER BY created_at
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING id, execution_id, attempt;
"""

SQL_HEARTBEAT = """
UPDATE iadf_sql_v1.changesets
SET lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
    updated_at = now()
WHERE id = %(changeset_id)s
  AND lease_owner = %(worker_id)s
  AND status = 'LEASED';
"""

SQL_COMPLETE = """
UPDATE iadf_sql_v1.changesets
SET status = 'COMPLETED',
    lease_owner = NULL,
    lease_expires_at = NULL,
    updated_at = now()
WHERE id = %(changeset_id)s
  AND lease_owner = %(worker_id)s
  AND status = 'LEASED';
"""

SQL_FAIL = """
UPDATE iadf_sql_v1.changesets
SET status = 'FAILED',
    lease_owner = NULL,
    lease_expires_at = NULL,
    updated_at = now()
WHERE id = %(changeset_id)s
  AND lease_owner = %(worker_id)s
  AND status = 'LEASED';
"""


@dataclass(frozen=True)
class LeasedTask:
    """
    Represents a task that has been successfully leased by a worker.
    
    Attributes:
        id: Changeset identifier
        execution_id: Execution identifier for this task
        attempt: Number of times this task has been attempted (1-based)
    """
    id: str
    execution_id: str
    attempt: int


class QueuePort:
    """
    PostgreSQL-based task queue port with atomic leasing.
    
    All operations use explicit transactions and are idempotent through
    worker_id guards. Connection management follows the pattern:
    - Open connection from factory
    - Use cursor as context manager
    - Commit on success, rollback on any exception
    - Always close connection in finally block
    """
    
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        """
        Initialize QueuePort with a DB-API connection factory.
        
        Args:
            connection_factory: Callable that returns a DB-API 2.0 connection
        """
        self._connection_factory = connection_factory
    
    def lease_next(self, worker_id: str, lease_seconds: int) -> Optional[LeasedTask]:
        """
        Atomically lease the next available task.
        
        Uses FOR UPDATE SKIP LOCKED to avoid blocking on concurrent workers.
        Leases tasks that are PENDING or have expired leases.
        
        Args:
            worker_id: Identifier of the worker requesting the lease
            lease_seconds: Duration of the lease in seconds (must be > 0)
            
        Returns:
            LeasedTask if a task was successfully leased, None otherwise
            
        Raises:
            ValueError: If lease_seconds <= 0
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than 0")
        
        conn = self._connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(SQL_LEASE_NEXT, {
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds
                })
                row = cur.fetchone()
                conn.commit()
                
                if row is None:
                    return None
                
                return LeasedTask(
                    id=row[0],
                    execution_id=row[1],
                    attempt=row[2]
                )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def heartbeat(self, changeset_id: str, worker_id: str, lease_seconds: int) -> bool:
        """
        Extend the lease expiration time for a task.
        
        Only succeeds if the worker currently owns the lease and the task
        is in LEASED status. This makes the operation idempotent and safe
        against expired-lease races.
        
        Args:
            changeset_id: ID of the changeset to extend
            worker_id: ID of the worker that owns the lease
            lease_seconds: New lease duration in seconds (must be > 0)
            
        Returns:
            True if the lease was extended, False if no matching lease found
            
        Raises:
            ValueError: If lease_seconds <= 0
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than 0")
        
        conn = self._connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(SQL_HEARTBEAT, {
                    "changeset_id": changeset_id,
                    "worker_id": worker_id,
                    "lease_seconds": lease_seconds
                })
                conn.commit()
                return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def complete(self, changeset_id: str, worker_id: str) -> bool:
        """
        Mark a task as completed and release the lease.
        
        Only succeeds if the worker currently owns the lease and the task
        is in LEASED status. This makes the operation idempotent.
        
        Args:
            changeset_id: ID of the changeset to complete
            worker_id: ID of the worker that owns the lease
            
        Returns:
            True if the task was marked complete, False if no matching lease found
        """
        conn = self._connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(SQL_COMPLETE, {
                    "changeset_id": changeset_id,
                    "worker_id": worker_id
                })
                conn.commit()
                return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def fail(self, changeset_id: str, worker_id: str) -> bool:
        """
        Mark a task as failed and release the lease.
        
        Only succeeds if the worker currently owns the lease and the task
        is in LEASED status. This makes the operation idempotent.
        
        Args:
            changeset_id: ID of the changeset to fail
            worker_id: ID of the worker that owns the lease
            
        Returns:
            True if the task was marked failed, False if no matching lease found
        """
        conn = self._connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(SQL_FAIL, {
                    "changeset_id": changeset_id,
                    "worker_id": worker_id
                })
                conn.commit()
                return cur.rowcount > 0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
