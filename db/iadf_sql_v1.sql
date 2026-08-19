-- IADF SQL v1 Schema — Relational projection for workflow execution state
-- ADD §19.2, §19.3: Idempotent DDL for PostgreSQL 18

CREATE SCHEMA IF NOT EXISTS iadf_sql_v1;

-- ADD §19.2: WorkflowExecution row
CREATE TABLE IF NOT EXISTS iadf_sql_v1.workflow_executions (
    id uuid PRIMARY KEY,
    manifest_id uuid NOT NULL UNIQUE,
    project_id uuid NOT NULL,
    state text NOT NULL CHECK (state IN (
        'INTAKE', 'PLANNED', 'CONTRACTED', 'TEST_RED', 'IMPLEMENTING',
        'VERIFY_FAST', 'VERIFY_DEEP', 'ADVERSARIAL_REVIEW', 'MERGE_READY',
        'AUTO_MERGED', 'TRUSTED_BUILD', 'SANDBOX', 'CANARY',
        'PROGRESSIVE_RELEASE', 'OBSERVING', 'COMPLETE',
        'REPAIR', 'FRONTIER_DIAGNOSIS', 'AUTO_ROLLBACK',
        'ROLLED_BACK', 'ABORTED', 'SUPERSEDED', 'QUARANTINED'
    )),
    version integer NOT NULL DEFAULT 0,
    current_changeset_id uuid,
    current_repair_attempt_id uuid,
    current_deployment_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ADD §19.3: Primary index for workflow executions
CREATE INDEX IF NOT EXISTS idx_workflow_executions_project_state
    ON iadf_sql_v1.workflow_executions (project_id, state, updated_at);

-- ADD §19.2: WorkflowState row (append-only state history)
CREATE TABLE IF NOT EXISTS iadf_sql_v1.workflow_states (
    id uuid PRIMARY KEY,
    execution_id uuid NOT NULL REFERENCES iadf_sql_v1.workflow_executions(id),
    sequence bigint NOT NULL,
    state text NOT NULL CHECK (state IN (
        'INTAKE', 'PLANNED', 'CONTRACTED', 'TEST_RED', 'IMPLEMENTING',
        'VERIFY_FAST', 'VERIFY_DEEP', 'ADVERSARIAL_REVIEW', 'MERGE_READY',
        'AUTO_MERGED', 'TRUSTED_BUILD', 'SANDBOX', 'CANARY',
        'PROGRESSIVE_RELEASE', 'OBSERVING', 'COMPLETE',
        'REPAIR', 'FRONTIER_DIAGNOSIS', 'AUTO_ROLLBACK',
        'ROLLED_BACK', 'ABORTED', 'SUPERSEDED', 'QUARANTINED'
    )),
    prior_state text,
    command_type text NOT NULL,
    idempotency_key text NOT NULL,
    policy_digest text,
    entered_at timestamptz NOT NULL DEFAULT now(),
    exited_at timestamptz,
    UNIQUE (execution_id, sequence),
    UNIQUE (command_type, idempotency_key)
);

-- ADD §19.2: ChangeSet row + §13.1 QueuePort substrate
CREATE TABLE IF NOT EXISTS iadf_sql_v1.changesets (
    id uuid PRIMARY KEY,
    execution_id uuid NOT NULL REFERENCES iadf_sql_v1.workflow_executions(id),
    version integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN (
        'PENDING', 'LEASED', 'RUNNING', 'COMPLETED', 'FAILED', 'QUARANTINED'
    )),
    scope_digest text NOT NULL CHECK (scope_digest ~ '^[a-f0-9]{64}$'),
    depends_on uuid[] NOT NULL DEFAULT '{}',
    lease_owner text,
    lease_expires_at timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ADD §19.3: Indexes for changeset leasing and execution lookup
CREATE INDEX IF NOT EXISTS idx_changesets_lease
    ON iadf_sql_v1.changesets (status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_changesets_execution
    ON iadf_sql_v1.changesets (execution_id, status);

-- ADD §19.2: RepairAttempt row (immutable ordinal/lineage)
CREATE TABLE IF NOT EXISTS iadf_sql_v1.repair_attempts (
    id uuid PRIMARY KEY,
    changeset_id uuid NOT NULL REFERENCES iadf_sql_v1.changesets(id),
    ordinal integer NOT NULL CHECK (ordinal >= 1),
    status text NOT NULL,
    input_sha text NOT NULL,
    fingerprint_before text NOT NULL,
    fingerprint_after text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (changeset_id, ordinal)
);

-- ADD §19.2: EvidenceReceipt row with §20.2 result algebra
CREATE TABLE IF NOT EXISTS iadf_sql_v1.evidence_receipts (
    id uuid PRIMARY KEY,
    subject_digest text NOT NULL CHECK (subject_digest ~ '^[a-f0-9]{64}$'),
    gate_id text NOT NULL,
    issuer_id text NOT NULL,
    result text NOT NULL CHECK (result IN (
        'PASS', 'FAIL', 'NOT_RUN', 'SKIPPED', 'UNKNOWN', 'ERROR',
        'INCONCLUSIVE', 'TIMEOUT', 'STALE', 'EXPIRED', 'SUPERSEDED'
    )),
    schema_id text NOT NULL,
    policy_digest text,
    binding_digest text,
    issued_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    signature text NOT NULL
);

-- ADD §19.3: Index for evidence receipt lookups
CREATE INDEX IF NOT EXISTS idx_evidence_receipts_subject
    ON iadf_sql_v1.evidence_receipts (subject_digest, gate_id, issuer_id);

-- ADD §19.2: TokenLedger row (append-only metering)
CREATE TABLE IF NOT EXISTS iadf_sql_v1.token_ledgers (
    id uuid PRIMARY KEY,
    sequence bigint GENERATED ALWAYS AS IDENTITY,
    manifest_id uuid NOT NULL,
    binding_id uuid NOT NULL,
    agent_run_id uuid,
    invocation_id uuid,
    uncached_input_tokens bigint NOT NULL DEFAULT 0 CHECK (uncached_input_tokens >= 0),
    cached_input_tokens bigint NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    cache_write_tokens bigint NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    output_tokens bigint NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    recorded_at timestamptz NOT NULL DEFAULT now()
);

-- ADD §19.2: Index for token ledger queries by manifest+binding+time
CREATE INDEX IF NOT EXISTS idx_token_ledgers_manifest
    ON iadf_sql_v1.token_ledgers (manifest_id, binding_id, recorded_at);

-- ADD §19.2: OutboxEvent row (IADF-ADR-004 transactional outbox)
CREATE TABLE IF NOT EXISTS iadf_sql_v1.outbox_events (
    sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE,
    aggregate_id uuid NOT NULL,
    aggregate_version integer NOT NULL,
    topic text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'DISPATCHED')),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ADD §19.3: Index for outbox event processing
CREATE INDEX IF NOT EXISTS idx_outbox_events_status
    ON iadf_sql_v1.outbox_events (status, sequence);
