#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 IADF COST-OPTIMIZED ENGINE  v2.0.0  (iadf_cost_optimized_engine.py)
================================================================================
 Orchestratore autonomo Tiered Multi-Model per lo sviluppo end-to-end della
 piattaforma IADF, conforme alle sezioni normative dell'ADD
 `IADF_Architecture_Design_Document_v1.0.md`:

   §13.1/§15.1  6 deployable isolati + profilo LOCAL-SYNTH (Compose, PG, MinIO)
   §16.6        QueuePort su PostgreSQL con leasing `FOR UPDATE SKIP LOCKED`
   §19          Data model canonico: proiezione relazionale `iadf_sql_v1.*`
   §20          FSM canonica (happy path a 16 stati, 5 terminali) e
                algebra dei risultati a 11 stati
   §21/§22      Capability matrix, routing dei modelli, EU residency,
                `PriceBinding` e ledger atomico dei token
   §23          Contract-First e Red-Proof deterministico
   §24          Self-healing bounded: capsule, fingerprint, max 2 repair,
                1 sola diagnosi frontier, poi QUARANTINED
   §25.3        Sandbox rootless (gVisor), capability drop, egress negato

 ARCHITETTURA A TIER (Cost-Optimized)
   1. Architect ............. Claude Sonnet (Anthropic SDK, Tool Calling nativo)
   2. Implementer ........... Aider CLI headless (`--yes --no-auto-commits`,
                              modello economico via LiteLLM, repo map Tree-sitter)
   3. Adversarial Reviewer .. Claude Sonnet (quality gate su git diff + report)
   4. Frontier Diagnostician  Claude Opus, solo dopo il fallimento del primo
                              repair; massimo 1 diagnosi per task (§24.3 step 4)

 GARANZIE OPERATIVE
   - Nessuna "token explosion": l'ADD non viene mai inviato a runtime; le
     specifiche normative sono incapsulate come stringhe dense nella ROADMAP.
   - Stato persistente idempotente su `iadf_state.json` (ripresa post-crash
     senza duplicare commit; riconciliazione dalla storia git).
   - TECHNICAL_PAUSE automatica su HTTP 429 con lettura header `retry-after`.
   - Verify-Red / Verify-Green deterministici sugli exit code di pytest;
     VERIFY_DB deterministico: DDL applicata due volte su PostgreSQL 18 reale.
   - Red Test Preservation: il file di test viene sempre riscritto dopo ogni
     `git reset --hard && git clean -fd` e dopo ogni run di Aider.
   - Tutte le subprocess: timeout=600s, stdin=DEVNULL, env=os.environ.copy().

 La GUIDA COMPLETA ALLA CONFIGURAZIONE si trova in fondo a questo file.
================================================================================
"""
from __future__ import annotations

import argparse
import email.utils
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import anthropic
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERRORE FATALE: SDK 'anthropic' non installato.\n"
        "Esegui: pip install anthropic\n"
    )
    sys.exit(1)

# ==============================================================================
# COSTANTI E CONFIGURAZIONE
# ==============================================================================

ENGINE_VERSION = "2.0.0"

# Modelli (ID API correnti; sovrascrivibili via variabili d'ambiente).
DEFAULT_ARCHITECT_MODEL = os.environ.get("IADF_ARCHITECT_MODEL", "claude-sonnet-5")
DEFAULT_REVIEWER_MODEL = os.environ.get("IADF_REVIEWER_MODEL", DEFAULT_ARCHITECT_MODEL)
DEFAULT_DIAGNOSTICIAN_MODEL = os.environ.get("IADF_DIAGNOSTICIAN_MODEL", "claude-opus-5")
DEFAULT_AIDER_MODEL = os.environ.get("IADF_AIDER_MODEL", "deepseek/deepseek-chat")

SUBPROCESS_TIMEOUT = int(os.environ.get("IADF_SUBPROCESS_TIMEOUT", "600"))

MAX_REPAIR_ATTEMPTS = 2        # §24.3: 1 run iniziale + max 2 main repair.
MAX_PLAN_ATTEMPTS = 3          # Rigenerazioni del piano su Red-Proof violato.
MAX_RATE_LIMIT_PAUSES = 12     # Pause 429 consecutive prima di arrendersi.
MAX_TRANSIENT_RETRIES = 5      # Retry su errori di rete / HTTP 5xx / 529.

ARCHITECT_MAX_TOKENS = int(os.environ.get("IADF_ARCHITECT_MAX_TOKENS", "16000"))
REVIEWER_MAX_TOKENS = 4000
DIAGNOSTICIAN_MAX_TOKENS = 6000

MAX_DIFF_CHARS = 60_000
MAX_LOG_CHARS = 12_000
MAX_FILE_SNIPPET_LINES = 200

STATE_FILE_NAME = "iadf_state.json"
LOG_FILE_NAME = "iadf_engine.log"
COMPOSE_FILE_NAME = "docker-compose.synth.yml"

# Artefatti dell'engine da proteggere SEMPRE da `git clean -fd` (opzione -e).
PROTECTED_PATTERNS: List[str] = [
    STATE_FILE_NAME,
    STATE_FILE_NAME + ".tmp",
    "*.corrupt.json",
    LOG_FILE_NAME,
    "iadf_cost_optimized_engine.py",
    ".aider*",
    ".gitignore",
]

# Voci garantite in .gitignore (evitano che stato/log finiscano nei commit).
GITIGNORE_LINES: List[str] = [
    "# --- IADF engine artifacts ---",
    "iadf_state.json",
    "iadf_state.json.tmp",
    "*.corrupt.json",
    "iadf_engine.log",
    ".aider*",
    "__pycache__/",
    "*.pyc",
    ".pytest_cache/",
    ".ruff_cache/",
]

LOG = logging.getLogger("iadf")


@dataclass
class EngineConfig:
    """Configurazione runtime (mutabile: il preflight può disattivare push/DB)."""

    repo_root: Path
    architect_model: str = DEFAULT_ARCHITECT_MODEL
    reviewer_model: str = DEFAULT_REVIEWER_MODEL
    diagnostician_model: str = DEFAULT_DIAGNOSTICIAN_MODEL
    aider_model: str = DEFAULT_AIDER_MODEL
    enable_opus: bool = True
    git_push: bool = True
    strict_push: bool = False
    git_remote: str = "origin"
    map_tokens: int = 1024
    subprocess_timeout: int = SUBPROCESS_TIMEOUT
    compose_file: str = COMPOSE_FILE_NAME
    require_db: bool = False
    pg_user: str = "iadf"
    pg_db: str = "iadf"


# ==============================================================================
# COSTANTI NORMATIVE ESTRATTE DALL'ADD (fonte unica per ROADMAP e scaffolding)
# ==============================================================================

# §20.2 — Algebra dei risultati a 11 stati; solo PASS soddisfa un predicato.
RESULT_ALGEBRA: Tuple[str, ...] = (
    "PASS", "FAIL", "NOT_RUN", "SKIPPED", "UNKNOWN", "ERROR",
    "INCONCLUSIVE", "TIMEOUT", "STALE", "EXPIRED", "SUPERSEDED",
)

# §20.1 — Happy path obbligatorio: esattamente questi 16 stati in sequenza.
FSM_HAPPY_PATH: Tuple[str, ...] = (
    "INTAKE", "PLANNED", "CONTRACTED", "TEST_RED", "IMPLEMENTING",
    "VERIFY_FAST", "VERIFY_DEEP", "ADVERSARIAL_REVIEW", "MERGE_READY",
    "AUTO_MERGED", "TRUSTED_BUILD", "SANDBOX", "CANARY",
    "PROGRESSIVE_RELEASE", "OBSERVING", "COMPLETE",
)

# §20.1 — Routing di fallimento non terminale e 5 terminali canonici.
FSM_FAILURE_ROUTING: Tuple[str, ...] = ("REPAIR", "FRONTIER_DIAGNOSIS", "AUTO_ROLLBACK")
FSM_TERMINALS: Tuple[str, ...] = (
    "COMPLETE", "ROLLED_BACK", "ABORTED", "SUPERSEDED", "QUARANTINED",
)
FSM_TECH_SUBSTATES: Tuple[str, ...] = (
    "TECHNICAL_PAUSE", "CONFLICT_RESOLUTION", "REBASING",
)
FSM_STATES: Tuple[str, ...] = FSM_HAPPY_PATH + FSM_FAILURE_ROUTING + (
    "ROLLED_BACK", "ABORTED", "SUPERSEDED", "QUARANTINED",
)  # 23 stati top-level

# §20.1 — Archi canonici del diagramma di stato (15 happy + 14 failure routing).
FSM_EDGES: Tuple[Tuple[str, str], ...] = tuple(
    zip(FSM_HAPPY_PATH, FSM_HAPPY_PATH[1:])
) + (
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
)

_ALGEBRA_LIST = ", ".join(RESULT_ALGEBRA)
_ALGEBRA_SQL = ", ".join(f"'{r}'" for r in RESULT_ALGEBRA)
_STATES_LIST = ", ".join(FSM_STATES)
_STATES_SQL = ", ".join(f"'{s}'" for s in FSM_STATES)
_HAPPY_LIST = " -> ".join(FSM_HAPPY_PATH)
_EDGES_BLOCK = "\n".join(f"      {a} -> {b}" for a, b in FSM_EDGES)

# §13.1/§15.1 — I 6 deployable isolati del baseline (nome -> ruolo normativo).
DEPLOYABLES: Dict[str, str] = {
    "iadf-api": (
        "Intake, viste di lettura e comandi amministrativi pre-runtime "
        "(§13.1). Identità `svc-api`: tabelle/funzioni DB limitate; MAI "
        "merge, firma o deploy (§15.1). Idempotente, >=2 repliche quando la "
        "disponibilità lo richiede."
    ),
    "iadf-controller": (
        "Unica autorità di transizione di stato e policy (§13.1): rischio, "
        "budget, timer, outbox transazionale, emissione comandi side-effect. "
        "Singleton attivo con standby e leader lease; il crash riprende dallo "
        "stato canonico in PostgreSQL senza split brain (§15.1)."
    ),
    "iadf-worker": (
        "Pool effimeri non fidati (§13.1): context/index (`iadf-worker-context`), "
        "task agent in sandbox (`iadf-worker-agent`), verifica deterministica "
        "e firma receipt (`iadf-worker-verify`). Nessuna autorità operativa; "
        "kill immediato su violazione di policy (§15.1)."
    ),
    "iadf-release": (
        "Corsia privilegiata isolata (§13.1): merge, trusted build, firma, "
        "deploy e rollback con identità distinte a vita breve per operazione; "
        "ingresso dagli agent negato; fail closed e serializzato per target "
        "(§15.1, §17.3)."
    ),
    "iadf-console": (
        "UI operatore TypeScript read-oriented (§13.1): viste di lettura, "
        "drill-down dell'evidenza, authoring AOE pre-attivazione, comandi "
        "start/abort. OIDC utente solo verso iadf-api; nessun accesso "
        "diretto a dati o provider (§15.1)."
    ),
    "otel-collector": (
        "Collettore telemetrico non autoritativo (§13.1): redazione, batch "
        "ed export OTLP. Solo endpoint di segnale; non può chiamare l'API "
        "comandi del controller né alterare l'evidenza (§15.1)."
    ),
}

# §17.2 profilo LOCAL-SYNTH — Compose sintetico: PostgreSQL 18 + MinIO (S3).
DOCKER_COMPOSE_SYNTH = textwrap.dedent("""\
    # ============================================================================
    # IADF LOCAL-SYNTH runtime (ADD §15.1, §17.2)
    # Sviluppo deterministico e bootstrap: PostgreSQL canonico + object store
    # S3-compatibile (MinIO). Mai fidato per produzione; firmatario finto.
    # Generato da iadf_cost_optimized_engine.py — modifiche manuali consentite.
    # ============================================================================
    services:
      postgres:
        image: postgres:18-alpine
        container_name: iadf-synth-postgres
        environment:
          POSTGRES_USER: iadf
          POSTGRES_PASSWORD: iadf-synth-secret
          POSTGRES_DB: iadf
        ports:
          - "${IADF_PG_PORT:-5433}:5432"
        volumes:
          - iadf-pgdata:/var/lib/postgresql/data
          - ./db:/ddl:ro
        healthcheck:
          test: ["CMD-SHELL", "pg_isready -U iadf -d iadf"]
          interval: 2s
          timeout: 3s
          retries: 45
      minio:
        image: minio/minio:latest
        container_name: iadf-synth-minio
        command: server /data --console-address ":9001"
        environment:
          MINIO_ROOT_USER: iadf-minio
          MINIO_ROOT_PASSWORD: iadf-synth-minio-secret
        ports:
          - "${IADF_MINIO_PORT:-9000}:9000"
          - "${IADF_MINIO_CONSOLE_PORT:-9001}:9001"
        volumes:
          - iadf-miniodata:/data
        healthcheck:
          test: ["CMD-SHELL", "curl -sf http://localhost:9000/minio/health/live || exit 1"]
          interval: 5s
          timeout: 4s
          retries: 30
    volumes:
      iadf-pgdata:
      iadf-miniodata:
""")

# ==============================================================================
# ROADMAP DEI TASK — specifiche normative dense estratte dall'ADD
# (nessun caricamento dell'ADD a runtime: queste stringhe SONO il contratto)
# ==============================================================================

@dataclass(frozen=True)
class TaskSpec:
    """Definizione dichiarativa e autoritativa di un ChangeSet della roadmap."""

    id: str
    title: str
    objective: str
    target_files: Tuple[str, ...]
    depends_on: Tuple[str, ...] = ()
    requires_db: bool = False
    ddl_files: Tuple[str, ...] = ()


ROADMAP: List[TaskSpec] = [
    TaskSpec(
        id="TASK-01-SCHEMAS",
        title="Contract-first JSON Schemas: AOE, ACM, EvidenceReceipt (ADD §4, §19, §20.2, §25.5)",
        objective=textwrap.dedent(f"""\
            Produce the three foundational JSON Schema documents (Draft 2020-12)
            plus a Python loader/validator package. These encode the normative
            contracts of ADD §4 (glossary), §19.2 (entity catalogue), §20.2
            (result algebra) and §25.5 (signature coverage).

            Deliverables:
            1. iadf/__init__.py — package marker exposing __version__ = "0.1.0".

            2. iadf/schemas/evidence_receipt.schema.json
               - "$schema": "https://json-schema.org/draft/2020-12/schema";
                 "$id": "iadf://schema/evidence-receipt/v1"; type object;
                 "additionalProperties": false.
               - Required properties (ADD §19.2 EvidenceReceipt row + §25.5):
                 receipt_id (uuid format); schema_id (const
                 "iadf://schema/evidence-receipt/v1"); subject_digest (string,
                 pattern ^[a-f0-9]{{64}}$); gate_id (string, minLength 1);
                 issuer_id (string, minLength 1);
                 result — enum with EXACTLY these 11 values in this order
                 (ADD §20.2 result algebra): {_ALGEBRA_LIST};
                 issued_at (RFC 3339 date-time); expires_at (date-time or null);
                 policy_digest (pattern ^[a-f0-9]{{64}}$);
                 binding_digest (same sha256 pattern, or null);
                 signature (object, additionalProperties false, required
                 alg/key_id/value, each string minLength 1).

            3. iadf/schemas/aoe.schema.json — AutonomousOperatingEnvelope
               (ADD §4: "signed policy boundary within which IADF may act
               without runtime approval"; §19.2 AOE row).
               - "$id": "iadf://schema/autonomous-operating-envelope/v1";
                 object; additionalProperties false. Required:
                 envelope_id (uuid); project_id (uuid);
                 version (integer, minimum 1);
                 status enum [DRAFT, ACTIVE, EXPIRED, SUPERSEDED];
                 allowed_change_classes (array of string, minItems 1,
                 uniqueItems true); excluded_paths (array of string);
                 risk_ceiling enum [R0, R1, R2, R3] (ADD §13.4 risk classes);
                 budgets (object, additionalProperties false, required:
                 max_cost_eur number exclusiveMinimum 0;
                 max_tokens_per_call integer exclusiveMinimum 0;
                 max_wall_seconds integer exclusiveMinimum 0) — §21.2 tool-loop
                 guard and §31 budget hierarchy;
                 data_residency (const "eu-only") — ADD §17.1 EU topology and
                 DAT-IADF-003; retention_days (integer, minimum 1, maximum
                 2555) — §19.2 "7y max by policy";
                 effective_from and effective_to (date-time);
                 signature (same alg/key_id/value object as above).

            4. iadf/schemas/acm.schema.json — AutonomousChangeManifest
               (ADD §4: "authorized work, scope, budgets, policies, data class,
               environments and rollback contract"; §19.2 ACM row:
               DRAFT/ACTIVATED/REJECTED, immutable after activation).
               - "$id": "iadf://schema/autonomous-change-manifest/v1";
                 object; additionalProperties false. Required:
                 manifest_id (uuid); backlog_item_id (uuid); envelope_id (uuid);
                 version (integer, minimum 1);
                 status enum [DRAFT, ACTIVATED, REJECTED];
                 scope_digest, policy_digest, budget_digest (each pattern
                 ^[a-f0-9]{{64}}$);
                 data_class enum [PUB, INT, CONF, SRC, SEC] (ADD §19.2
                 classification abbreviations);
                 risk_class enum [R0, R1, R2, R3];
                 change_set_plan (array, minItems 1, of objects with
                 additionalProperties false and required: changeset_id uuid;
                 path_globs array of string minItems 1; depends_on array of
                 uuid) — acyclic ChangeSet plan per FR-IADF-009;
                 rollback_contract (object, additionalProperties false,
                 required: type enum [redeploy_previous_digest,
                 expand_contract_migration]; target string minLength 1) —
                 ADD §29.4 rollback contract;
                 environments (array minItems 1 uniqueItems of enum
                 [LOCAL-SYNTH, DEV-EU, STAGE-EU, PROD-EU]) — §17.2 profiles;
                 created_at (date-time); activated_at (date-time or null).

            5. iadf/schemas/__init__.py
               - RESULT_ALGEBRA: tuple of the 11 result strings in the exact
                 order above (single source of truth for later tasks).
               - SCHEMA_DIR: Path of the schema directory.
               - load_schema(name: str) -> dict, cached with
                 functools.lru_cache; accepts "evidence_receipt" | "aoe" |
                 "acm"; raises KeyError on unknown name.
               - class SchemaValidationError(Exception) carrying a list of
                 human-readable error strings including JSON paths.
               - validate_document(name: str, document: dict) -> None using
                 jsonschema.Draft202012Validator with FORMAT_CHECKER enabled;
                 raises SchemaValidationError listing ALL violations."""),
        target_files=(
            "iadf/__init__.py",
            "iadf/schemas/__init__.py",
            "iadf/schemas/evidence_receipt.schema.json",
            "iadf/schemas/aoe.schema.json",
            "iadf/schemas/acm.schema.json",
        ),
        depends_on=(),
    ),
    TaskSpec(
        id="TASK-02-FSM-CANONICAL",
        title="FSM canonica a 16 stati happy-path + DDL PostgreSQL iadf_sql_v1 (ADD §16.6, §19, §20)",
        objective=textwrap.dedent(f"""\
            Deliverables:

            1. iadf/core/__init__.py — package marker.

            2. iadf/core/fsm.py — the deterministic canonical state machine of
               ADD §20.1, pure and hermetic (no I/O, no randomness, no global
               mutation, no imports beyond typing/dataclasses and
               iadf.schemas for RESULT_ALGEBRA re-export checks):
               - HAPPY_PATH: tuple of EXACTLY these 16 states in this order
                 (ADD §20.1 "mandatory happy path is exactly"):
                 {_HAPPY_LIST}
               - FAILURE_ROUTING: tuple ("REPAIR", "FRONTIER_DIAGNOSIS",
                 "AUTO_ROLLBACK") — non-terminal failure states.
               - TERMINAL_STATES: frozenset of the five canonical terminals
                 (ADD §20.1, FR-IADF-040): COMPLETE, ROLLED_BACK, ABORTED,
                 SUPERSEDED, QUARANTINED.
               - TECHNICAL_SUBSTATES: frozenset of the finite controller-owned
                 substates (never terminals, never wait for a person):
                 TECHNICAL_PAUSE, CONFLICT_RESOLUTION, REBASING.
               - STATES: tuple of the 23 top-level states =
                 {_STATES_LIST}
               - CANONICAL_TRANSITIONS: frozenset of EXACTLY these 29
                 (from_state, to_state) edges from the §20.1 state diagram —
                 the 15 sequential happy-path edges plus 14 failure-routing
                 edges — and nothing else:
            {_EDGES_BLOCK}
               - class InvalidTransitionError(Exception).
               - class IadfStateMachine with pure methods:
                 can_transition(current: str, target: str) -> bool;
                 transition(current: str, target: str) -> str returning target
                 or raising InvalidTransitionError when the edge is not in
                 CANONICAL_TRANSITIONS, when either state is unknown, or when
                 current is terminal (ADD §20.4 rule 7: terminal states are
                 immutable; recovery is a new linked run);
                 is_terminal(state: str) -> bool.

            3. db/iadf_sql_v1.sql — valid, idempotent PostgreSQL 18 DDL for
               the relational projection `iadf_sql_v1.{{entity_snake_case}}`
               (ADD §19.2 table-wide rule, §19.3 indexing). It MUST:
               - Begin with CREATE SCHEMA IF NOT EXISTS iadf_sql_v1;
               - Use CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
                 everywhere (applying the file twice must be a no-op), and be
                 executable in a single psql -f pass with ON_ERROR_STOP=1.
               - Define these SEVEN tables, all inside schema iadf_sql_v1:

               a) workflow_executions — §19.2 WorkflowExecution row:
                  id uuid PRIMARY KEY;
                  manifest_id uuid NOT NULL UNIQUE;
                  project_id uuid NOT NULL;
                  state text NOT NULL CHECK (state IN ({_STATES_SQL}));
                  version integer NOT NULL DEFAULT 0;
                  current_changeset_id uuid;
                  current_repair_attempt_id uuid;
                  current_deployment_id uuid;
                  created_at/updated_at timestamptz NOT NULL DEFAULT now().
                  Index idx_workflow_executions_project_state on
                  (project_id, state, updated_at) — §19.3 primary indexes.

               b) workflow_states — §19.2 WorkflowState row (append-only):
                  id uuid PRIMARY KEY;
                  execution_id uuid NOT NULL REFERENCES
                  iadf_sql_v1.workflow_executions(id);
                  sequence bigint NOT NULL;
                  state text NOT NULL CHECK (state IN the same 23 values);
                  prior_state text;
                  command_type text NOT NULL;
                  idempotency_key text NOT NULL;
                  policy_digest text;
                  entered_at timestamptz NOT NULL DEFAULT now();
                  exited_at timestamptz;
                  UNIQUE (execution_id, sequence) — §19.3 (run_id, sequence);
                  UNIQUE (command_type, idempotency_key) — §19.3.

               c) changesets — §19.2 ChangeSet row + §13.1 QueuePort substrate
                  (PostgreSQL leasing is performed over this table by
                  TASK-03): id uuid PRIMARY KEY;
                  execution_id uuid NOT NULL REFERENCES
                  iadf_sql_v1.workflow_executions(id);
                  version integer NOT NULL DEFAULT 0;
                  status text NOT NULL DEFAULT 'PENDING' CHECK (status IN
                  ('PENDING','LEASED','RUNNING','COMPLETED','FAILED',
                  'QUARANTINED'));
                  scope_digest text NOT NULL CHECK
                  (scope_digest ~ '^[a-f0-9]{{64}}$');
                  depends_on uuid[] NOT NULL DEFAULT '{{}}';
                  lease_owner text;
                  lease_expires_at timestamptz;
                  attempt integer NOT NULL DEFAULT 0;
                  created_at/updated_at timestamptz NOT NULL DEFAULT now().
                  Index idx_changesets_lease on
                  (status, lease_expires_at);
                  index idx_changesets_execution on (execution_id, status).

               d) repair_attempts — §19.2 RepairAttempt row (immutable
                  ordinal/lineage; §24.3 bounded algorithm):
                  id uuid PRIMARY KEY;
                  changeset_id uuid NOT NULL REFERENCES
                  iadf_sql_v1.changesets(id);
                  ordinal integer NOT NULL CHECK (ordinal >= 1);
                  status text NOT NULL;
                  input_sha text NOT NULL;
                  fingerprint_before text NOT NULL;
                  fingerprint_after text;
                  created_at timestamptz NOT NULL DEFAULT now();
                  UNIQUE (changeset_id, ordinal).

               e) evidence_receipts — §19.2 EvidenceReceipt row; result MUST
                  be constrained to the 11-value algebra of §20.2:
                  id uuid PRIMARY KEY;
                  subject_digest text NOT NULL CHECK
                  (subject_digest ~ '^[a-f0-9]{{64}}$');
                  gate_id text NOT NULL;
                  issuer_id text NOT NULL;
                  result text NOT NULL CHECK (result IN ({_ALGEBRA_SQL}));
                  schema_id text NOT NULL;
                  policy_digest text;
                  binding_digest text;
                  issued_at timestamptz NOT NULL DEFAULT now();
                  expires_at timestamptz;
                  signature text NOT NULL;
                  Index idx_evidence_receipts_subject on
                  (subject_digest, gate_id, issuer_id) — §19.3.

               f) token_ledgers — §19.2 TokenLedger row (append-only;
                  uncached/cached/write/output token categories, §22 metering):
                  id uuid PRIMARY KEY;
                  sequence bigint GENERATED ALWAYS AS IDENTITY;
                  manifest_id uuid NOT NULL;
                  binding_id uuid NOT NULL;
                  agent_run_id uuid;
                  invocation_id uuid;
                  uncached_input_tokens bigint NOT NULL DEFAULT 0 CHECK (>= 0);
                  cached_input_tokens bigint NOT NULL DEFAULT 0 CHECK (>= 0);
                  cache_write_tokens bigint NOT NULL DEFAULT 0 CHECK (>= 0);
                  output_tokens bigint NOT NULL DEFAULT 0 CHECK (>= 0);
                  recorded_at timestamptz NOT NULL DEFAULT now();
                  Index idx_token_ledgers_manifest on
                  (manifest_id, binding_id, recorded_at) — §19.2 "indexed by
                  manifest+binding+time".

               g) outbox_events — §19.2 OutboxEvent row (append-only payload;
                  IADF-ADR-004 transactional outbox):
                  sequence bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY;
                  event_id uuid NOT NULL UNIQUE;
                  aggregate_id uuid NOT NULL;
                  aggregate_version integer NOT NULL;
                  topic text NOT NULL;
                  payload jsonb NOT NULL DEFAULT '{{}}'::jsonb;
                  status text NOT NULL DEFAULT 'PENDING' CHECK (status IN
                  ('PENDING','DISPATCHED'));
                  created_at timestamptz NOT NULL DEFAULT now();
                  Index idx_outbox_events_status on (status, sequence).

               The SQL file must contain every one of the 23 state literals
               and every one of the 11 result literals verbatim, so hermetic
               tests can assert alignment between fsm.py, iadf.schemas
               RESULT_ALGEBRA and the DDL CHECK constraints by reading the
               file as text (tests NEVER connect to a database)."""),
        target_files=(
            "iadf/core/__init__.py",
            "iadf/core/fsm.py",
            "db/iadf_sql_v1.sql",
        ),
        depends_on=("TASK-01-SCHEMAS",),
        requires_db=True,
        ddl_files=("db/iadf_sql_v1.sql",),
    ),
    TaskSpec(
        id="TASK-03-QUEUE-LEASING",
        title="QueuePort PostgreSQL con leasing atomico FOR UPDATE SKIP LOCKED (ADD §13.1, §16.6, §19.2)",
        objective=textwrap.dedent("""\
            ADD §13.1: "`QueuePort` initially uses PostgreSQL leasing
            (`FOR UPDATE SKIP LOCKED`) while all state effects remain
            controller commands". ADD §19.2 WorkLease row: UUID/version/expiry;
            task, worker, attempt; mutable lease with full event audit.
            Leasing operates over the iadf_sql_v1.changesets table created by
            TASK-02 (status/lease_owner/lease_expires_at/attempt columns).

            Deliverables:
            1. iadf/ports/__init__.py — package marker.
            2. iadf/ports/queue_port.py — driver-agnostic PostgreSQL queue
               port, fully testable WITHOUT a live database (tests inject a
               fake DB-API connection factory that records SQL and params;
               no psycopg import at module level):
               - Module-level SQL constants using named-parameter style
                 %(param_name)s and NEVER string interpolation of values:
                 * SQL_LEASE_NEXT — one atomic statement:
                   UPDATE iadf_sql_v1.changesets ... WHERE id = (
                     SELECT id FROM iadf_sql_v1.changesets
                     WHERE status = 'PENDING'
                        OR (status = 'LEASED' AND lease_expires_at < now())
                     ORDER BY created_at
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED
                   )
                   RETURNING id, execution_id, attempt;
                   setting status='LEASED', lease_owner=%(worker_id)s,
                   lease_expires_at = now() + make_interval(secs =>
                   %(lease_seconds)s), attempt = attempt + 1,
                   updated_at = now().
                   The constant MUST contain the literal
                   "FOR UPDATE SKIP LOCKED".
                 * SQL_HEARTBEAT — extends lease_expires_at, guarded by
                   "AND lease_owner = %(worker_id)s AND status = 'LEASED'".
                 * SQL_COMPLETE — sets status='COMPLETED', clears lease_owner
                   and lease_expires_at, same worker/status guard.
                 * SQL_FAIL — sets status='FAILED', clears the lease, same
                   worker/status guard.
                 Worker guards make every mutation idempotent and safe against
                 expired-lease races (ADD §17.4: duplicate worker result —
                 first valid committed result wins).
               - @dataclass(frozen=True) LeasedTask: id (str),
                 execution_id (str), attempt (int).
               - class QueuePort(connection_factory:
                 Callable[[], Connection]):
                 lease_next(worker_id: str, lease_seconds: int)
                   -> Optional[LeasedTask];
                 heartbeat(changeset_id: str, worker_id: str,
                   lease_seconds: int) -> bool;
                 complete(changeset_id: str, worker_id: str) -> bool;
                 fail(changeset_id: str, worker_id: str) -> bool.
                 Every method: opens a connection from the factory, uses an
                 explicit transaction (commit on success, rollback on ANY
                 exception, connection closed in a finally block), uses
                 cursors as context managers, validates lease_seconds > 0
                 (ValueError), and returns booleans from rowcount (or the
                 RETURNING row for lease_next)."""),
        target_files=(
            "iadf/ports/__init__.py",
            "iadf/ports/queue_port.py",
        ),
        depends_on=("TASK-02-FSM-CANONICAL",),
        requires_db=True,
    ),
    TaskSpec(
        id="TASK-04-MODEL-ROUTER",
        title="ModelRouter statico: EU residency, PriceBinding, ledger token atomico (ADD §21, §22)",
        objective=textwrap.dedent("""\
            ADD §22.1: model selection is an empirically maintained policy;
            a model never routes itself. §22.3: paid calls with
            confidential/source data use ONLY an exact EU + retention-eligible
            binding, with NO fallback; provider outage or price-binding expiry
            means deny. §22.5 defines ModelBinding/PriceBinding minimum
            fields. §19.2 TokenLedger: append-only, uncached/cached/write/
            output token categories.

            Deliverables:
            1. iadf/routing/__init__.py — package marker.
            2. iadf/routing/model_router.py (stdlib only: dataclasses,
               decimal, threading, typing, datetime):
               - class ResidencyViolationError(Exception).
               - class PriceBindingExpiredError(Exception).
               - @dataclass(frozen=True) PriceBinding (ADD §22.5):
                 currency (str, default "EUR");
                 effective_from, effective_to (datetime, timezone-aware);
                 input_per_mtok, cached_input_per_mtok, cache_write_per_mtok,
                 output_per_mtok (Decimal);
                 regional_multiplier (Decimal, default Decimal("1"));
                 source_url (str); source_hash (str);
                 __post_init__ raises ValueError on any negative rate or
                 non-positive multiplier;
                 valid_at(now: datetime) -> bool checking the effective
                 interval (§22.5 staleness is enforced by interval).
               - @dataclass(frozen=True) ModelBinding (ADD §22.5 subset):
                 alias (str); provider (str); model_id (str — exact pinned
                 snapshot, §22.4 model_snapshot_is_pinned); endpoint_base_url
                 (str); region (str); eu_resident (bool); retention_mode
                 (str, e.g. "zero-retention"); allowed_data_classes
                 (frozenset of str among PUB/INT/CONF/SRC/SEC);
                 tier (str, one of "cheap","main","reviewer","frontier" —
                 §22.3 routing lanes); price (PriceBinding).
               - DEFAULT_REGISTRY: Tuple[ModelBinding, ...] with at least 4
                 entries covering all four tiers, at least one with
                 eu_resident=False (illustrative values are fine; §22.2 is a
                 dated research snapshot, not runtime truth).
               - class ModelRouter:
                 __init__(registry: Iterable[ModelBinding]) building an
                 internal dict keyed by alias (ValueError on duplicate alias);
                 route(alias: str, data_class: str = "CONF",
                       require_eu: bool = True,
                       now: Optional[datetime] = None) -> ModelBinding.
                 Deterministic eligibility subset of §22.4, evaluated in this
                 order: KeyError on unknown alias;
                 ResidencyViolationError when require_eu and not eu_resident;
                 ValueError when data_class not in allowed_data_classes;
                 PriceBindingExpiredError when not
                 price.valid_at(now or datetime.now(timezone.utc)).
                 There is NO fallback path of any kind (FR-IADF-039).
               - class TokenLedger — atomic in-process usage ledger
                 (§19.2, §31): thread-safe via threading.Lock;
                 record(alias: str, uncached_input: int, cached_input: int,
                        cache_write: int, output: int) -> None rejecting any
                 negative value with ValueError (append-only accumulation);
                 totals(alias: str) -> Tuple[int, int, int, int]
                 (zeros when unknown);
                 cost(alias: str, router: ModelRouter) -> Decimal computed
                 ONLY with Decimal arithmetic (never float):
                 (uncached/1_000_000*input_per_mtok
                  + cached/1_000_000*cached_input_per_mtok
                  + cache_write/1_000_000*cache_write_per_mtok
                  + output/1_000_000*output_per_mtok)
                 * regional_multiplier;
                 snapshot() -> Dict[str, Tuple[int, int, int, int]] returning
                 an atomic copy taken under the lock."""),
        target_files=(
            "iadf/routing/__init__.py",
            "iadf/routing/model_router.py",
        ),
        depends_on=(),
    ),
    TaskSpec(
        id="TASK-05-SANDBOX-RUNNER",
        title="SandboxRunner: container effimeri rootless gVisor, capability ristrette (ADD §17, §21.2, §25.3)",
        objective=textwrap.dedent("""\
            ADD §25.3: baseline agent sandboxes are single-task, rootless OCI
            containers using gVisor `runsc`, read-only base image, tmpfs work
            area with quota, dropped Linux capabilities, no host mount and
            separate network namespace. §21.2 tool-loop guard: the sandbox
            kills the process at a limit; the result is TIMEOUT, never a
            request for more permission. SEC-IADF-002: ephemeral, rootless,
            resource-limited, deny egress.

            Deliverables:
            1. iadf/sandbox/__init__.py — package marker.
            2. iadf/sandbox/runner.py — lifecycle manager for ephemeral
               rootless containers, hermetic-testable via executor injection
               (tests inject a fake executor and assert the EXACT argv; tests
               never launch a real container):
               - ALLOWED_CAPABILITIES: frozenset({"CHOWN",
                 "NET_BIND_SERVICE"}) — restricted grant catalogue.
               - class CapabilityDeniedError(Exception).
               - @dataclass(frozen=True) CapabilityGrant: name (str);
                 __post_init__ raises CapabilityDeniedError when name is not
                 in ALLOWED_CAPABILITIES (§25.2: intersection, never union).
               - @dataclass(frozen=True) SandboxSpec:
                 image (str); command (Tuple[str, ...]);
                 workdir (str = "/work"); memory_mb (int = 512);
                 cpus (float = 1.0); pids_limit (int = 256);
                 tmpfs_mb (int = 256); timeout_s (int = 120);
                 grants (Tuple[CapabilityGrant, ...] = ());
                 network (bool = False); use_gvisor (bool = True).
                 __post_init__ validates memory_mb/pids_limit/tmpfs_mb/
                 timeout_s > 0, cpus > 0 and non-empty command (ValueError).
               - @dataclass(frozen=True) SandboxResult: exit_code (int),
                 stdout (str), stderr (str), timed_out (bool).
               - class SandboxRunner(runtime: str = "podman", executor=None):
                 build_argv(spec) -> List[str] producing EXACTLY, in order:
                 [runtime, "run", "--rm", "--pull=never"]
                 + (["--runtime", "runsc"] if spec.use_gvisor else [])
                 + ["--network",
                    ("none" if not spec.network else "slirp4netns"),
                    "--user", "1000:1000", "--cap-drop", "ALL"]
                 + ["--cap-add", g.name for each grant, in order]
                 + ["--security-opt", "no-new-privileges",
                    "--pids-limit", str(spec.pids_limit),
                    "--memory", f"{spec.memory_mb}m",
                    "--cpus", str(spec.cpus),
                    "--read-only",
                    "--tmpfs", f"{spec.workdir}:rw,size={spec.tmpfs_mb}m",
                    "--workdir", spec.workdir,
                    spec.image, *spec.command];
                 run(spec) -> SandboxResult invoking the executor (default:
                 subprocess.run) with timeout=spec.timeout_s,
                 stdin=subprocess.DEVNULL, capture_output=True, text=True,
                 env=os.environ.copy(); catches subprocess.TimeoutExpired and
                 returns SandboxResult(exit_code=124, timed_out=True, with
                 any partial stdout/stderr decoded) — §21.2 kill-at-limit.
                 NEVER uses shell=True anywhere. Teardown is implicit and
                 guaranteed by "--rm" (single-task ephemeral container)."""),
        target_files=(
            "iadf/sandbox/__init__.py",
            "iadf/sandbox/runner.py",
        ),
        depends_on=(),
    ),
]

# ==============================================================================
# ECCEZIONI, UTILITY E PRIMITIVE DETERMINISTICHE
# ==============================================================================

class EngineError(Exception):
    """Errore operativo recuperabile a livello di engine."""


class ProtocolError(EngineError):
    """La risposta del modello non rispetta il protocollo Tool Calling atteso."""


class PlanRejected(EngineError):
    """Il piano dell'Architect viola i guardrail deterministici (Red-Proof)."""


class QuarantineError(EngineError):
    """Bounded Self-Healing esaurito: il sistema deve entrare in QUARANTINED."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def head_tail(text: str, max_chars: int) -> str:
    """Tronca in modo simmetrico preservando inizio e fine (stack trace utili)."""
    text = text or ""
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    omitted = len(text) - max_chars
    return (
        text[:half]
        + f"\n... [TRONCATO: {omitted} caratteri omessi] ...\n"
        + text[-half:]
    )


def env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _parse_retry_after(value: Optional[str]) -> float:
    """Interpreta l'header `retry-after` (secondi oppure HTTP-date)."""
    default = 60.0
    if not value:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            target = email.utils.parsedate_to_datetime(value)
            seconds = (target - datetime.now(timezone.utc)).total_seconds()
        except Exception:
            seconds = default
    return float(min(max(seconds, 5.0), 900.0))


_REL_PATH_RE = re.compile(r"^[A-Za-z0-9_./-]+$")


def _check_rel_path(path_str: str) -> str:
    """Guardrail: solo percorsi relativi, ben formati, dentro il repository."""
    candidate = (path_str or "").strip()
    if (
        not candidate
        or candidate.startswith(("/", "~"))
        or ".." in Path(candidate).parts
        or not _REL_PATH_RE.match(candidate)
    ):
        raise PlanRejected(f"Percorso non valido o non relativo: {candidate!r}")
    if candidate == ".git" or candidate.startswith(".git/"):
        raise PlanRejected(f"Percorso vietato: {candidate!r}")
    return candidate


@dataclass
class CmdResult:
    """Esito normalizzato di una subprocess (stdout/stderr/exit code/timeout)."""

    argv: List[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False


def run_cmd(argv: List[str], cwd: Path, timeout: int = SUBPROCESS_TIMEOUT) -> CmdResult:
    """Esecuzione subprocess con i guardrail obbligatori:
    timeout esplicito, stdin=DEVNULL (niente deadlock interattivi) e
    propagazione esplicita dell'ambiente (credenziali Aider/LiteLLM)."""
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
        )
        return CmdResult(
            argv=list(argv),
            returncode=completed.returncode,
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
            duration_s=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return CmdResult(
            argv=list(argv),
            returncode=124,
            stdout=out,
            stderr=err + f"\n[TIMEOUT dopo {timeout}s]",
            duration_s=time.monotonic() - started,
            timed_out=True,
        )
    except FileNotFoundError as exc:
        return CmdResult(
            argv=list(argv),
            returncode=127,
            stdout="",
            stderr=f"Eseguibile non trovato: {exc}",
            duration_s=time.monotonic() - started,
        )


# ==============================================================================
# STATE PERSISTENCE & IDEMPOTENZA (iadf_state.json)
# ==============================================================================

class StateManager:
    """Persistenza atomica dello stato su disco.

    Ogni transizione di fase viene salvata immediatamente (write su file
    temporaneo + os.replace atomico): in caso di crash o riavvio l'engine
    riprende dall'ultimo task incompleto senza duplicare i commit esistenti
    (FR-IADF-034: resume from canonical state without repeating committed
    effects)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: Dict[str, Any] = self._load()

    def _default(self) -> Dict[str, Any]:
        now = utc_now_iso()
        return {
            "schema_version": 2,
            "engine_version": ENGINE_VERSION,
            "system_status": "IDLE",
            "completed_tasks": [],
            "current_task": None,
            "scaffold_done": False,
            "failure_capsules": [],
            "history": [],
            "stats": {"api_calls": 0, "rate_limit_pauses": 0, "aider_runs": 0, "commits": 0},
            "created_at": now,
            "updated_at": now,
        }

    def _load(self) -> Dict[str, Any]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("la radice dello stato non è un oggetto JSON")
                base = self._default()
                base.update(data)
                base.setdefault("stats", self._default()["stats"])
                return base
            except (json.JSONDecodeError, ValueError) as exc:
                backup = self.path.with_suffix(".corrupt.json")
                try:
                    shutil.copy2(self.path, backup)
                except OSError:
                    backup = Path("(backup non riuscito)")
                LOG.error(
                    "Stato corrotto (%s): backup in %s, ripartenza da stato pulito.",
                    exc, backup,
                )
        return self._default()

    def save(self) -> None:
        self.state["updated_at"] = utc_now_iso()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    # ---- API di alto livello -------------------------------------------------
    def record(self, event: str, task: Optional[str] = None, detail: str = "") -> None:
        self.state["history"].append(
            {"ts": utc_now_iso(), "event": event, "task": task, "detail": detail[:2000]}
        )
        self.state["history"] = self.state["history"][-500:]
        self.save()

    def set_current_task(self, task_id: str) -> None:
        self.state["current_task"] = {
            "id": task_id,
            "phase": "PLANNING",
            "attempt": 0,
            "plan": None,
            "started_at": utc_now_iso(),
        }
        self.state["system_status"] = "RUNNING"
        self.save()

    def update_current(self, **fields: Any) -> None:
        current = self.state.get("current_task") or {}
        current.update(fields)
        self.state["current_task"] = current
        self.save()

    def clear_current(self) -> None:
        self.state["current_task"] = None
        self.save()

    def mark_completed(self, task_id: str) -> None:
        if task_id not in self.state["completed_tasks"]:
            self.state["completed_tasks"].append(task_id)
        self.save()

    def is_completed(self, task_id: str) -> bool:
        return task_id in self.state["completed_tasks"]

    def add_capsule(self, capsule: Dict[str, Any]) -> None:
        self.state["failure_capsules"].append(capsule)
        self.state["failure_capsules"] = self.state["failure_capsules"][-50:]
        self.save()


# ==============================================================================
# FAILURE CAPSULE (ADD §24.2: contesto strutturato, senza segreti né CoT)
# ==============================================================================

@dataclass
class FailureCapsule:
    """Fotografia compatta di un fallimento, iniettata nel prompt di repair.

    Conforme al principio ADD §24.2: SHA del subject, gate falliti con
    risultati tipizzati, errori normalizzati con slice di log limitate,
    ipotesi/patch precedenti; esclude segreti e sorgente non correlata."""

    task_id: str
    attempt: int
    phase: str  # AIDER | VERIFY_GREEN | VERIFY_DB | REVIEW
    timestamp: str
    head_sha: str = ""
    pytest_exit: Optional[int] = None
    pytest_tail: str = ""
    ruff_tail: str = ""
    verdict: str = ""
    reasoning: str = ""
    diff_stat: str = ""
    extra: str = ""

    def to_prompt_block(self) -> str:
        lines = [
            f"=== FAILURE CAPSULE | task={self.task_id} | phase={self.phase} "
            f"| attempt={self.attempt} | head={self.head_sha or 'n/a'} "
            f"| ts={self.timestamp} ==="
        ]
        if self.pytest_exit is not None:
            lines.append(f"gate exit code: {self.pytest_exit}")
        if self.pytest_tail:
            lines.append("--- gate output (full stack trace) ---\n" + self.pytest_tail)
        if self.ruff_tail:
            lines.append("--- linter output ---\n" + self.ruff_tail)
        if self.verdict:
            lines.append(f"--- adversarial reviewer ({self.verdict}) ---\n" + self.reasoning)
        elif self.reasoning:
            lines.append("--- note ---\n" + self.reasoning)
        if self.diff_stat:
            lines.append("--- diff stat of the failed attempt ---\n" + self.diff_stat)
        if self.extra:
            lines.append("--- extra ---\n" + self.extra)
        return "\n".join(lines)


# ==============================================================================
# OPERAZIONI GIT (workspace isolation, rollback, commit, push)
# ==============================================================================

class GitOps:
    """Wrapper deterministico attorno a git nel repository di lavoro."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _git(self, *args: str, timeout: int = SUBPROCESS_TIMEOUT) -> CmdResult:
        return run_cmd(["git", *args], cwd=self.root, timeout=timeout)

    def is_repo(self) -> bool:
        return self._git("rev-parse", "--is-inside-work-tree").returncode == 0

    def has_head(self) -> bool:
        return self._git("rev-parse", "HEAD").returncode == 0

    def user_configured(self) -> bool:
        email_res = self._git("config", "user.email")
        name_res = self._git("config", "user.name")
        return bool((email_res.stdout or "").strip()) and bool((name_res.stdout or "").strip())

    def is_clean(self) -> bool:
        res = self._git("status", "--porcelain")
        return res.returncode == 0 and not (res.stdout or "").strip()

    def ls_files(self) -> List[str]:
        res = self._git("ls-files")
        return [l for l in (res.stdout or "").splitlines() if l.strip()]

    def head_subjects(self, n: int = 300) -> List[str]:
        res = self._git("log", "--format=%s", "-n", str(n))
        if res.returncode != 0:
            return []
        return [l for l in (res.stdout or "").splitlines() if l.strip()]

    def head_short(self) -> str:
        return (self._git("rev-parse", "--short", "HEAD").stdout or "").strip()

    def current_branch(self) -> str:
        res = self._git("rev-parse", "--abbrev-ref", "HEAD")
        return (res.stdout or "").strip() or "main"

    def rollback(self) -> None:
        """Isolamento workspace: `git reset --hard` + `git clean -fd`,
        proteggendo SEMPRE gli artefatti dell'engine (stato, log, script,
        cache Aider). Il Red Test viene riscritto dal chiamante subito dopo
        (Red Test Preservation)."""
        reset = self._git("reset", "--hard", "HEAD")
        clean_args: List[str] = ["clean", "-fd"]
        for pattern in PROTECTED_PATTERNS:
            clean_args += ["-e", pattern]
        clean = self._git(*clean_args)
        if reset.returncode != 0 or clean.returncode != 0:
            raise EngineError(
                "Rollback git fallito: "
                f"reset='{(reset.stderr or '').strip()}' clean='{(clean.stderr or '').strip()}'"
            )

    def stage_all(self) -> None:
        res = self._git("add", "-A")
        if res.returncode != 0:
            raise EngineError(f"git add -A fallito: {res.stderr}")

    def add_paths(self, paths: List[str]) -> None:
        if not paths:
            return
        res = self._git("add", "--", *paths)
        if res.returncode != 0:
            raise EngineError(f"git add fallito: {res.stderr}")

    def staged_files(self) -> List[str]:
        res = self._git("diff", "--cached", "--name-only")
        return [l for l in (res.stdout or "").splitlines() if l.strip()]

    def diff_cached(self) -> str:
        return self._git("diff", "--cached").stdout or ""

    def diff_cached_stat(self) -> str:
        return self._git("diff", "--cached", "--stat").stdout or ""

    def commit(self, message: str) -> CmdResult:
        return self._git("commit", "-m", message)

    def push(self, remote: str, branch: str) -> CmdResult:
        res = self._git("push", remote, branch)
        blob = ((res.stderr or "") + (res.stdout or "")).lower()
        if res.returncode != 0 and ("no upstream" in blob or "set-upstream" in blob):
            res = self._git("push", "-u", remote, branch)
        return res

    def has_remote(self, remote: str) -> bool:
        return self._git("remote", "get-url", remote).returncode == 0


# ==============================================================================
# SCAFFOLDING (ADD §13.1/§15.1: 6 deployable) E RUNTIME LOCALE (§17.2 LOCAL-SYNTH)
# ==============================================================================

class ScaffoldManager:
    """Crea in modo idempotente lo scheletro dei 6 deployable isolati e il
    runtime sintetico locale (docker-compose.synth.yml con PostgreSQL
    18-alpine e MinIO). Scrive solo i file mancanti e li committa una volta."""

    def __init__(self, cfg: EngineConfig, git: GitOps, state: StateManager) -> None:
        self.cfg = cfg
        self.git = git
        self.state = state

    def ensure(self) -> None:
        created: List[str] = []

        for name, role in DEPLOYABLES.items():
            readme = self.cfg.repo_root / name / "README.md"
            if not readme.exists():
                readme.parent.mkdir(parents=True, exist_ok=True)
                readme.write_text(
                    f"# {name}\n\n{role}\n\n"
                    "Deployable isolato del baseline IADF: privilegio e failure "
                    "isolation ne giustificano l'esistenza (ADD §15.2). "
                    "I ruoli agent sono configurazioni, non servizi.\n",
                    encoding="utf-8",
                )
                created.append(str(readme.relative_to(self.cfg.repo_root)))

        otel_cfg = self.cfg.repo_root / "otel-collector" / "config.yaml"
        if not otel_cfg.exists():
            otel_cfg.parent.mkdir(parents=True, exist_ok=True)
            otel_cfg.write_text(textwrap.dedent("""\
                # OTel Collector minimale per il profilo LOCAL-SYNTH (ADD §15.1).
                # Non autoritativo: la perdita di telemetria non può creare PASS
                # (OPS-IADF-002).
                receivers:
                  otlp:
                    protocols:
                      grpc:
                        endpoint: 0.0.0.0:4317
                      http:
                        endpoint: 0.0.0.0:4318
                processors:
                  batch: {}
                exporters:
                  debug:
                    verbosity: basic
                service:
                  pipelines:
                    traces:
                      receivers: [otlp]
                      processors: [batch]
                      exporters: [debug]
                    metrics:
                      receivers: [otlp]
                      processors: [batch]
                      exporters: [debug]
                    logs:
                      receivers: [otlp]
                      processors: [batch]
                      exporters: [debug]
                """), encoding="utf-8")
            created.append("otel-collector/config.yaml")

        compose = self.cfg.repo_root / self.cfg.compose_file
        if not compose.exists():
            compose.write_text(DOCKER_COMPOSE_SYNTH, encoding="utf-8")
            created.append(self.cfg.compose_file)

        db_readme = self.cfg.repo_root / "db" / "README.md"
        if not db_readme.exists():
            db_readme.parent.mkdir(parents=True, exist_ok=True)
            db_readme.write_text(
                "# db/\n\nDDL canonica `iadf_sql_v1` (ADD §19): generata da "
                "TASK-02-FSM-CANONICAL e applicata due volte (idempotenza) su "
                "PostgreSQL 18 del profilo LOCAL-SYNTH, montata read-only in "
                "`/ddl` dentro il container `postgres`.\n",
                encoding="utf-8",
            )
            created.append("db/README.md")

        if created:
            # Staging esplicito dei soli file creati: mai `add -A`, per non
            # tracciare accidentalmente stato/log dell'engine.
            self.git.add_paths(created)
            if self.git.staged_files():
                res = self.git.commit(
                    "chore(scaffold): 6 deployable isolati e runtime LOCAL-SYNTH "
                    "(ADD §13.1, §15.1, §17.2)"
                )
                if res.returncode != 0:
                    raise EngineError(f"Commit dello scaffolding fallito: {res.stderr}")
                LOG.info("Scaffolding creato e committato: %s", ", ".join(created))
        self.state.state["scaffold_done"] = True
        self.state.save()


class LocalRuntime:
    """Gestione del runtime sintetico locale: avvio e healthcheck programmatico
    di PostgreSQL 18 prima dei test su database e applicazione deterministica
    della DDL (VERIFY_DB, due passaggi per provare l'idempotenza)."""

    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg
        self._compose_argv: Optional[List[str]] = None
        self._compose_probed = False
        self._postgres_healthy = False

    # -------------------------------------------------------------- detection
    def compose_argv(self) -> Optional[List[str]]:
        if self._compose_probed:
            return self._compose_argv
        self._compose_probed = True
        if shutil.which("docker"):
            probe = run_cmd(["docker", "compose", "version"], cwd=self.cfg.repo_root, timeout=30)
            if probe.returncode == 0:
                self._compose_argv = ["docker", "compose"]
                return self._compose_argv
        for candidate in ("docker-compose", "podman-compose"):
            if shutil.which(candidate):
                self._compose_argv = [candidate]
                return self._compose_argv
        self._compose_argv = None
        return None

    @property
    def available(self) -> bool:
        return self.compose_argv() is not None

    def _compose(self, *args: str, timeout: int = SUBPROCESS_TIMEOUT) -> CmdResult:
        base = self.compose_argv()
        if base is None:
            return CmdResult(argv=list(args), returncode=127, stdout="",
                             stderr="compose non disponibile", duration_s=0.0)
        return run_cmd(
            base + ["-f", self.cfg.compose_file, *args],
            cwd=self.cfg.repo_root,
            timeout=timeout,
        )

    # ------------------------------------------------------------ healthcheck
    def ensure_postgres(self, healthy_wait_s: int = 150) -> bool:
        """Avvia il servizio postgres del profilo LOCAL-SYNTH e attende che
        `pg_isready` risponda dall'interno del container (healthcheck
        programmatico richiesto prima dei test su database)."""
        if self._postgres_healthy:
            return True
        if not self.available:
            LOG.warning(
                "Nessun runtime compose (docker compose / docker-compose / "
                "podman-compose): salto avvio PostgreSQL LOCAL-SYNTH."
            )
            return False
        if not (self.cfg.repo_root / self.cfg.compose_file).exists():
            LOG.warning("%s assente: salto avvio PostgreSQL.", self.cfg.compose_file)
            return False

        LOG.info("LOCAL-SYNTH: avvio PostgreSQL 18 (compose up -d postgres)...")
        up = self._compose("up", "-d", "postgres", timeout=self.cfg.subprocess_timeout)
        if up.returncode != 0:
            LOG.warning(
                "compose up postgres fallito (exit=%s): %s",
                up.returncode, head_tail(up.stderr or up.stdout, 800),
            )
            return False

        deadline = time.monotonic() + healthy_wait_s
        while time.monotonic() < deadline:
            probe = self._compose(
                "exec", "-T", "postgres",
                "pg_isready", "-U", self.cfg.pg_user, "-d", self.cfg.pg_db,
                timeout=30,
            )
            if probe.returncode == 0:
                self._postgres_healthy = True
                LOG.info("LOCAL-SYNTH: PostgreSQL healthy (pg_isready OK).")
                return True
            time.sleep(2.0)
        LOG.warning("PostgreSQL non healthy entro %ss.", healthy_wait_s)
        return False

    # --------------------------------------------------------------- VERIFY_DB
    def apply_ddl(self, rel_sql_path: str) -> CmdResult:
        """Applica la DDL con psql (ON_ERROR_STOP=1) DUE volte: il secondo
        passaggio prova l'idempotenza richiesta dall'ADD (§19.2 table-wide
        rule). Il file è visibile nel container via mount ./db -> /ddl."""
        basename = Path(rel_sql_path).name
        last = CmdResult(argv=[], returncode=1, stdout="", stderr="non eseguito", duration_s=0.0)
        for round_no in (1, 2):
            last = self._compose(
                "exec", "-T", "postgres",
                "psql", "-U", self.cfg.pg_user, "-d", self.cfg.pg_db,
                "-v", "ON_ERROR_STOP=1", "-f", f"/ddl/{basename}",
                timeout=300,
            )
            if last.returncode != 0:
                last.stderr = f"[VERIFY_DB passaggio {round_no}/2] " + (last.stderr or "")
                return last
        return last


# ==============================================================================
# GATEWAY ANTHROPIC (Tool Calling nativo + resilienza rate limit §17.4/§24.1)
# ==============================================================================

class AnthropicGateway:
    """Unico punto di accesso all'API Anthropic.

    - Tool Calling nativo (parametro `tools` + `tool_choice` forzato): il
      payload strutturato arriva come dict già parsato dall'SDK, azzerando gli
      errori di parsing JSON manuale.
    - `anthropic.RateLimitError` (HTTP 429): lettura header `retry-after`,
      stato TECHNICAL_PAUSE persistito (ADD §20.1 substate tecnico), pausa
      passiva e ripristino automatico.
    - Errori transitori (rete, HTTP 5xx/529): backoff esponenziale + jitter
      limitato (ADD §24.1 "infrastructure transient").
    """

    def __init__(self, state: StateManager) -> None:
        self.state = state
        # I retry sono gestiti qui (max_retries=0) per onorare `retry-after`
        # e persistere lo stato TECHNICAL_PAUSE fra un tentativo e l'altro.
        self.client = anthropic.Anthropic(timeout=float(SUBPROCESS_TIMEOUT), max_retries=0)

    def call(
        self,
        *,
        model: str,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[Dict[str, Any]] = None,
        max_tokens: int = 4000,
        temperature: float = 0.0,
    ) -> Any:
        rate_pauses = 0
        transient = 0
        while True:
            try:
                kwargs: Dict[str, Any] = dict(
                    model=model,
                    system=system,
                    messages=messages,
                    max_tokens=max_tokens,
                )
                if tools is not None:
                    kwargs["tools"] = tools
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice
                message = self.client.messages.create(**kwargs)
                self.state.state["stats"]["api_calls"] += 1
                self.state.save()
                return message

            except anthropic.RateLimitError as exc:
                rate_pauses += 1
                self.state.state["stats"]["rate_limit_pauses"] += 1
                if rate_pauses > MAX_RATE_LIMIT_PAUSES:
                    raise EngineError(
                        f"Rate limit persistente dopo {MAX_RATE_LIMIT_PAUSES} pause: interrompo."
                    ) from exc
                headers = getattr(getattr(exc, "response", None), "headers", None) or {}
                wait = _parse_retry_after(headers.get("retry-after")) + random.uniform(0.5, 3.0)
                previous_status = self.state.state.get("system_status", "RUNNING")
                self.state.state["system_status"] = "TECHNICAL_PAUSE"
                self.state.record(
                    "TECHNICAL_PAUSE",
                    detail=(
                        f"HTTP 429 su {model}: pausa passiva {wait:.1f}s "
                        f"(retry-after onorato, tentativo {rate_pauses}/{MAX_RATE_LIMIT_PAUSES})"
                    ),
                )
                LOG.warning(
                    "TECHNICAL_PAUSE | HTTP 429 su %s: attendo %.1fs (retry-after onorato).",
                    model, wait,
                )
                time.sleep(wait)
                self.state.state["system_status"] = (
                    "RUNNING" if previous_status == "TECHNICAL_PAUSE" else previous_status
                )
                self.state.save()

            except anthropic.APIConnectionError as exc:
                transient += 1
                if transient > MAX_TRANSIENT_RETRIES:
                    raise EngineError(
                        f"Errore di connessione persistente verso l'API Anthropic: {exc}"
                    ) from exc
                backoff = min(120.0, (2 ** transient) * 2.0) + random.uniform(0.0, 2.0)
                LOG.warning(
                    "Errore di connessione API (%s): retry %d/%d fra %.1fs",
                    exc, transient, MAX_TRANSIENT_RETRIES, backoff,
                )
                time.sleep(backoff)

            except anthropic.APIStatusError as exc:
                status = getattr(exc, "status_code", None)
                if status is not None and (status >= 500 or status == 529):
                    transient += 1
                    if transient > MAX_TRANSIENT_RETRIES:
                        raise EngineError(
                            f"Errore server API persistente (HTTP {status})."
                        ) from exc
                    backoff = min(120.0, (2 ** transient) * 2.0) + random.uniform(0.0, 2.0)
                    LOG.warning(
                        "HTTP %s dall'API Anthropic: retry %d/%d fra %.1fs",
                        status, transient, MAX_TRANSIENT_RETRIES, backoff,
                    )
                    time.sleep(backoff)
                else:
                    raise

    @staticmethod
    def extract_tool_input(message: Any, tool_name: str) -> Dict[str, Any]:
        """Estrae l'input strutturato del tool richiesto (già dict, mai JSON raw)."""
        for block in getattr(message, "content", None) or []:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == tool_name
            ):
                payload = getattr(block, "input", None)
                if isinstance(payload, dict):
                    return payload
        stop = getattr(message, "stop_reason", "unknown")
        if stop == "max_tokens":
            raise ProtocolError(
                f"Output troncato (stop_reason=max_tokens) prima del tool `{tool_name}`."
            )
        raise ProtocolError(
            f"Nessun blocco tool_use `{tool_name}` nella risposta (stop_reason={stop})."
        )


# ==============================================================================
# TOOL SCHEMAS (output strutturato senza parsing JSON manuale)
# ==============================================================================

ARCHITECT_TOOL: Dict[str, Any] = {
    "name": "submit_task_plan",
    "description": (
        "Registra il piano TDD completo per il task corrente: specifica, "
        "Red Test pytest, percorso del file di test e prompt per il coder."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "specification_markdown": {
                "type": "string",
                "description": "Specifica tecnica completa e implementabile, in Markdown.",
            },
            "test_file_path": {
                "type": "string",
                "description": "Percorso relativo di un file NUOVO sotto tests/, es. tests/test_task01_schemas.py",
            },
            "test_code": {
                "type": "string",
                "description": "Contenuto integrale del file pytest (Red-Proof).",
            },
            "coder_prompt": {
                "type": "string",
                "description": "Istruzioni operative per Aider (senza incollare il codice del test).",
            },
            "target_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Esattamente i target file autoritativi del task.",
            },
        },
        "required": [
            "specification_markdown",
            "test_file_path",
            "test_code",
            "coder_prompt",
            "target_files",
        ],
    },
}

REVIEWER_TOOL: Dict[str, Any] = {
    "name": "submit_review",
    "description": "Verdetto del quality gate sul diff e sui report di test.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["PASS", "REPAIR"],
                "description": "PASS autorizza il commit; REPAIR attiva il ciclo di riparazione.",
            },
            "reasoning": {
                "type": "string",
                "description": "Motivazione sintetica e verificabile del verdetto.",
            },
            "repair_instructions": {
                "type": "string",
                "description": "Solo per REPAIR: correzioni minime e concrete richieste.",
            },
        },
        "required": ["verdict", "reasoning"],
    },
}

DIAGNOSTICIAN_TOOL: Dict[str, Any] = {
    "name": "submit_diagnosis",
    "description": "Diagnosi frontier della causa radice dopo ripetuti fallimenti di repair.",
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "root_cause_analysis": {
                "type": "string",
                "description": "Analisi della causa radice più probabile, basata sulle failure capsule.",
            },
            "repair_strategy": {
                "type": "string",
                "description": "Strategia di riparazione minima e concreta per l'ultimo tentativo.",
            },
            "files_to_focus": {
                "type": "array",
                "items": {"type": "string"},
                "description": "File esatti su cui concentrare la correzione.",
            },
        },
        "required": ["root_cause_analysis", "repair_strategy"],
    },
}


# ==============================================================================
# SYSTEM PROMPT DEI TRE TIER (densi: l'ADD non viene mai inviato a runtime)
# ==============================================================================

ARCHITECT_SYSTEM = textwrap.dedent("""\
    You are the ARCHITECT tier of the IADF autonomous TDD pipeline
    (contract-first, red-proof-first: no production code before a valid red
    test). Your output is consumed by machines: respond EXCLUSIVELY via the
    `submit_task_plan` tool.

    Produce, for the given task:
    1. `specification_markdown`: a precise, implementation-ready specification
       (public API, data contracts, error semantics, edge cases) faithful to
       the authoritative objective in the request.
    2. `test_file_path`: a NEW file under `tests/` (e.g.
       `tests/test_task01_schemas.py`), different from every existing test
       file listed in the request.
    3. `test_code`: the COMPLETE pytest file. Hard requirements:
       - Hermetic: no network, no live databases or containers, no writes
         outside pytest's `tmp_path`, no sleeps, no environment mutation.
         SQL deliverables are validated by READING the .sql file as text and
         asserting required literals/structure — never by connecting.
       - Deterministic and self-contained; import only the Python standard
         library, pytest, jsonschema and pydantic (plus the production
         modules under test).
       - It MUST FAIL on the current codebase (Red-Proof) solely because the
         production code does not exist yet or is incomplete — NEVER because
         of syntax errors, missing fixtures, or bugs in the test itself.
       - Import the production modules exactly at the paths listed in the
         task's authoritative target files.
       - Assert the EXACT normative literals given in the objective (state
         names, result algebra values, SQL fragments such as
         "FOR UPDATE SKIP LOCKED", argv ordering): these are ADD contracts.
    4. `coder_prompt`: unambiguous instructions for a code-generation CLI
       (Aider): exact files to create/modify (ONLY the task's target files),
       required public API, and the acceptance criterion "make the provided
       pytest file pass WITHOUT modifying it". Do NOT paste the full test code
       here (it is provided to the coder separately).
    5. `target_files`: echo the task's authoritative target files verbatim.

    If feedback about a previously rejected plan is provided, fix the root
    cause it describes before anything else.""")

REVIEWER_SYSTEM = textwrap.dedent("""\
    You are the ADVERSARIAL REVIEWER (quality gate) of the IADF pipeline.
    Respond EXCLUSIVELY via the `submit_review` tool. You are a proposal
    filter: only deterministic gates plus your REPAIR verdict block a commit;
    you never hold release authority.

    You receive: the task specification, the staged file list, the full
    `git diff --cached`, the pytest report, the linter report and (when the
    task ships DDL) the VERIFY_DB report of psql applying the SQL twice on a
    real PostgreSQL 18.

    Return verdict "PASS" only if ALL of the following hold:
    - pytest is green AND the diff plausibly implements the specification
      (not a trivial hack that games the test, e.g. hardcoded expected
      values, tautological asserts, or tests weakened/edited);
    - changes are confined to the task's authoritative target files (plus the
      untouched acceptance test file);
    - no secrets or credentials, no `shell=True` on untrusted input, no
      `eval`/`exec` on external data, no disabled TLS verification, no new
      network calls, no new third-party dependencies beyond
      pydantic/jsonschema;
    - prohibited repair mutations are absent (ADD §24.5): no disabled or
      skipped tests, no weakened assertions, no error-to-warning conversion,
      no swallowed exit codes, no arbitrary timeout inflation, no suppression
      comments added solely to manufacture green;
    - acceptable code quality: clear naming, explicit error handling, no dead
      code, no TODO/placeholder stubs.

    Otherwise return "REPAIR" with a factual `reasoning` and concrete,
    minimal `repair_instructions`. Be strict but pragmatic: do NOT request
    out-of-scope refactors or style-only changes the linter does not flag.""")

DIAGNOSTICIAN_SYSTEM = textwrap.dedent("""\
    You are the FRONTIER DIAGNOSTICIAN of the IADF pipeline (ADD §24.3 step
    4): a single read-only, high-attention diagnosis invoked only after the
    first repair attempt has failed. Respond EXCLUSIVELY via the
    `submit_diagnosis` tool.

    Given the task specification, the immutable red test and the failure
    capsules in chronological order, identify the single most likely ROOT
    CAUSE and prescribe one concrete, minimal, bounded repair strategy for
    the FINAL attempt. Name exact files, symbols and changes. Never propose
    modifying the acceptance test, weakening an assertion, or expanding
    scope beyond the task's target files (ADD §24.5). You cannot apply the
    patch yourself; the normal executor will.""")


# ==============================================================================
# TASK RUNNER — CICLO TDD A 6 STEP CON BOUNDED SELF-HEALING (ADD §23, §24)
# ==============================================================================

@dataclass
class TaskPlan:
    """Piano TDD prodotto dall'Architect e validato dai guardrail."""

    specification: str
    test_file: str
    test_code: str
    coder_prompt: str
    target_files: List[str]


class TaskRunner:
    """Esegue un TaskSpec attraverso il ciclo completo:

    Step A  Architect (Tool Calling)  -> spec + Red Test + coder prompt
    Step B  Save Test                 -> scrittura del file di test
    Step C  Verify-Red DETERMINISTICO -> pytest DEVE fallire (exit != 0)
    Step D  Aider CLI headless        -> implementazione sui soli target file
    Step E  Verify-Green DETERMINISTICO + linter (+ VERIFY_DB se DDL) + diff
    Step F  Adversarial Review        -> PASS (commit/push) o REPAIR (bounded)
    """

    def __init__(
        self,
        cfg: EngineConfig,
        state: StateManager,
        git: GitOps,
        gateway: AnthropicGateway,
        runtime: LocalRuntime,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.git = git
        self.gateway = gateway
        self.runtime = runtime

    # --------------------------------------------------------------- pipeline
    def run_task(self, task: TaskSpec) -> None:
        LOG.info("=" * 78)
        LOG.info("TASK %s — %s", task.id, task.title)
        LOG.info("=" * 78)
        self.state.set_current_task(task.id)

        # Avvio + healthcheck programmatico di PostgreSQL PRIMA dei test su
        # database (profilo LOCAL-SYNTH), come richiesto per i task con DB.
        db_ok = False
        if task.requires_db:
            db_ok = self.runtime.ensure_postgres()
            if not db_ok and self.cfg.require_db:
                raise EngineError(
                    f"{task.id} richiede PostgreSQL LOCAL-SYNTH ma il runtime "
                    "compose non è disponibile o non è healthy "
                    "(IADF_REQUIRE_DB=1)."
                )
            if not db_ok and task.ddl_files:
                LOG.warning(
                    "[%s] PostgreSQL non disponibile: VERIFY_DB saltato "
                    "(la DDL sarà validata solo testualmente dai test).",
                    task.id,
                )

        plan = self._plan_with_red_proof(task)

        capsules: List[FailureCapsule] = []
        diagnosis: Optional[str] = None

        for attempt in range(1 + MAX_REPAIR_ATTEMPTS):  # 0 = run iniziale
            if attempt > 0:
                LOG.warning(
                    "[%s] Rollback workspace (git reset --hard + clean -fd) "
                    "e ripristino del Red Test su disco.", task.id,
                )
                self.git.rollback()
                self._write_test(plan)  # Red Test Preservation

            # Frontier Diagnostician (ADD §24.3 step 4): una sola diagnosi per
            # task, dopo il fallimento del 1° repair, prima dell'ultimo run.
            if (
                attempt == MAX_REPAIR_ATTEMPTS
                and self.cfg.enable_opus
                and capsules
                and diagnosis is None
            ):
                diagnosis = self._frontier_diagnosis(task, plan, capsules)

            label = "iniziale" if attempt == 0 else f"repair {attempt}/{MAX_REPAIR_ATTEMPTS}"
            self.state.update_current(phase="IMPLEMENTING", attempt=attempt)
            LOG.info(
                "[%s] Step D — Aider CLI (tentativo %s, modello %s)...",
                task.id, label, self.cfg.aider_model,
            )
            coder_message = self._coder_message(task, plan, attempt, capsules, diagnosis)
            aider_res = self._run_aider(task, plan, coder_message)
            self.state.state["stats"]["aider_runs"] += 1
            self.state.save()

            if aider_res.returncode != 0:
                LOG.error(
                    "[%s] Aider terminato con exit=%s (timeout=%s).",
                    task.id, aider_res.returncode, aider_res.timed_out,
                )
                capsules.append(self._capsule(
                    task, attempt, "AIDER",
                    pytest_tail=head_tail(
                        aider_res.stdout + "\n" + aider_res.stderr, MAX_LOG_CHARS
                    ),
                    extra=f"aider exit={aider_res.returncode} timed_out={aider_res.timed_out}",
                ))
                continue

            # Integrità del contratto (ADD §23.3 anti-gaming): il Red Test
            # viene SEMPRE riscritto dal piano dopo l'implementer.
            self._write_test(plan)

            self.state.update_current(phase="VERIFY_GREEN", attempt=attempt)
            LOG.info("[%s] Step E — Verify-Green deterministico (pytest + ruff)...", task.id)
            green = self._run_pytest(self._green_paths(plan))
            ruff = self._run_ruff()

            if green.returncode != 0:
                LOG.error("[%s] Verify-Green FALLITO (pytest exit=%s).", task.id, green.returncode)
                capsules.append(self._capsule(
                    task, attempt, "VERIFY_GREEN",
                    pytest_exit=green.returncode,
                    pytest_tail=head_tail(green.stdout + "\n" + green.stderr, MAX_LOG_CHARS),
                    ruff_tail=head_tail(ruff.stdout + "\n" + ruff.stderr, 2000),
                ))
                continue
            LOG.info(
                "[%s] Verify-Green OK (pytest verde in %.1fs, linter exit=%s).",
                task.id, green.duration_s, ruff.returncode,
            )

            # VERIFY_DB deterministico: DDL applicata due volte su PG 18 reale.
            db_res: Optional[CmdResult] = None
            if task.ddl_files and db_ok:
                self.state.update_current(phase="VERIFY_DB", attempt=attempt)
                db_failed = False
                for rel_sql in task.ddl_files:
                    if not (self.cfg.repo_root / rel_sql).is_file():
                        db_res = CmdResult(argv=[], returncode=1, stdout="",
                                           stderr=f"DDL attesa mancante: {rel_sql}",
                                           duration_s=0.0)
                        db_failed = True
                        break
                    LOG.info("[%s] VERIFY_DB — psql doppio passaggio su %s...", task.id, rel_sql)
                    db_res = self.runtime.apply_ddl(rel_sql)
                    if db_res.returncode != 0:
                        db_failed = True
                        break
                if db_failed and db_res is not None:
                    LOG.error("[%s] VERIFY_DB FALLITO (exit=%s).", task.id, db_res.returncode)
                    capsules.append(self._capsule(
                        task, attempt, "VERIFY_DB",
                        pytest_exit=db_res.returncode,
                        pytest_tail=head_tail(
                            (db_res.stdout or "") + "\n" + (db_res.stderr or ""),
                            MAX_LOG_CHARS,
                        ),
                        extra="psql -v ON_ERROR_STOP=1 (doppio passaggio per idempotenza)",
                    ))
                    continue
                LOG.info("[%s] VERIFY_DB OK: DDL valida e idempotente su PostgreSQL 18.", task.id)

            self.state.update_current(phase="REVIEW", attempt=attempt)
            LOG.info("[%s] Step F — Adversarial Review (%s)...", task.id, self.cfg.reviewer_model)
            self.git.stage_all()
            staged = self.git.staged_files()
            diff = self.git.diff_cached()
            diff_stat = self.git.diff_cached_stat()
            verdict, reasoning, instructions = self._adversarial_review(
                task, plan, staged, diff, diff_stat, green, ruff, db_res
            )

            if verdict == "PASS":
                LOG.info("[%s] Review PASS: %s", task.id, head_tail(reasoning, 400))
                self._commit_and_push(task)
                self.state.mark_completed(task.id)
                self.state.clear_current()
                self.state.record("TASK_COMPLETED", task=task.id)
                LOG.info("[%s] COMPLETATO E COMMITTATO.", task.id)
                return

            LOG.warning("[%s] Review REPAIR: %s", task.id, head_tail(reasoning, 600))
            capsules.append(self._capsule(
                task, attempt, "REVIEW",
                pytest_exit=green.returncode,
                pytest_tail=head_tail(green.stdout, 4000),
                ruff_tail=head_tail(ruff.stdout + "\n" + ruff.stderr, 2000),
                verdict="REPAIR",
                reasoning=reasoning + (f"\nREPAIR INSTRUCTIONS: {instructions}" if instructions else ""),
                diff_stat=head_tail(diff_stat, 2000),
            ))

        # Bounded Self-Healing esaurito (ADD §24.3 step 5): workspace pulito,
        # Red Test preservato su disco per l'ispezione umana, quarantena.
        self.git.rollback()
        self._write_test(plan)
        last_phase = capsules[-1].phase if capsules else "sconosciuta"
        raise QuarantineError(
            f"{task.id}: {MAX_REPAIR_ATTEMPTS} tentativi di repair esauriti dopo il run "
            f"iniziale (ultima fase fallita: {last_phase}). Red Test preservato in "
            f"'{plan.test_file}' per analisi umana."
        )

    # ---------------------------------------------------- Step A-C: Red-Proof
    def _plan_with_red_proof(self, task: TaskSpec) -> TaskPlan:
        feedback: Optional[str] = None
        max_tokens = ARCHITECT_MAX_TOKENS

        for plan_attempt in range(1, MAX_PLAN_ATTEMPTS + 1):
            self.state.update_current(phase="PLANNING", plan_attempt=plan_attempt)
            LOG.info(
                "[%s] Step A — Architect (%s): generazione piano TDD (tentativo %d/%d)...",
                task.id, self.cfg.architect_model, plan_attempt, MAX_PLAN_ATTEMPTS,
            )
            try:
                message = self.gateway.call(
                    model=self.cfg.architect_model,
                    system=ARCHITECT_SYSTEM,
                    messages=[{"role": "user", "content": self._architect_content(task, feedback)}],
                    tools=[ARCHITECT_TOOL],
                    tool_choice={"type": "tool", "name": "submit_task_plan"},
                    max_tokens=max_tokens,
                )
                raw = AnthropicGateway.extract_tool_input(message, "submit_task_plan")
            except ProtocolError as exc:
                feedback = (
                    f"The previous plan was not extractable ({exc}). "
                    "Regenerate a more concise plan."
                )
                max_tokens = min(max_tokens + 8000, 32000)
                LOG.warning("[%s] %s", task.id, feedback)
                continue

            try:
                plan = self._sanitize_plan(task, raw)
            except PlanRejected as exc:
                feedback = str(exc)
                LOG.warning("[%s] Piano rigettato dai guardrail: %s", task.id, feedback)
                continue

            # Step B — Save Test
            self._write_test(plan)
            LOG.info("[%s] Step B — Red Test salvato in %s.", task.id, plan.test_file)

            # Step C — Verify-Red DETERMINISTICO: pytest DEVE fallire (§23.2).
            LOG.info("[%s] Step C — Verify-Red: pytest su %s (deve fallire)...", task.id, plan.test_file)
            red = self._run_pytest([plan.test_file])
            combined = (red.stdout or "") + (red.stderr or "")

            if red.timed_out:
                feedback = (
                    "The red test timed out: tests must be fast, hermetic and free of waits."
                )
            elif red.returncode == 0:
                feedback = (
                    "RED-PROOF VIOLATED (ADD §23.2): the test already passes on the "
                    "existing codebase. The plan must assert behaviour that is NOT "
                    "implemented yet."
                )
            elif red.returncode == 5:
                feedback = (
                    "pytest exit 5 (no tests collected): the file must contain valid "
                    "`test_*` functions."
                )
            elif red.returncode in (3, 4):
                feedback = (
                    f"pytest exit {red.returncode} (internal/usage error): the test file is "
                    f"malformed.\n{head_tail(combined, 3000)}"
                )
            elif "SyntaxError" in combined and plan.test_file in combined:
                feedback = (
                    "The red test itself contains a SyntaxError; fix the test code.\n"
                    + head_tail(combined, 3000)
                )
            else:
                LOG.info(
                    "[%s] RED VERIFICATO (pytest exit=%s): fallimento dovuto al codice "
                    "di produzione mancante.", task.id, red.returncode,
                )
                self.state.update_current(phase="RED_VERIFIED", plan=asdict(plan))
                return plan

            # Piano invalido: rimozione del test scritto e rigenerazione.
            self._remove_file(plan.test_file)
            LOG.warning("[%s] Verify-Red rigetta il piano: %s", task.id, head_tail(feedback, 500))

        raise QuarantineError(
            f"{task.id}: impossibile ottenere un piano con Red-Proof valido dopo "
            f"{MAX_PLAN_ATTEMPTS} tentativi. Ultimo problema: {feedback}"
        )

    def _sanitize_plan(self, task: TaskSpec, raw: Dict[str, Any]) -> TaskPlan:
        required = ("specification_markdown", "test_file_path", "test_code", "coder_prompt", "target_files")
        missing = [key for key in required if not raw.get(key)]
        if missing:
            raise PlanRejected(f"Missing plan fields: {missing}")

        test_file = _check_rel_path(str(raw["test_file_path"]))
        if not (test_file.startswith("tests/") and test_file.endswith(".py")):
            raise PlanRejected(
                f"`test_file_path` must be a NEW file under tests/*.py (got {test_file!r})."
            )
        if (self.cfg.repo_root / test_file).exists():
            raise PlanRejected(
                f"The file {test_file} already exists: choose a NEW, unique file name."
            )

        test_code = str(raw["test_code"])
        if "def test" not in test_code:
            raise PlanRejected("`test_code` does not contain any `test_*` function.")

        declared = raw.get("target_files")
        if not isinstance(declared, list) or not all(isinstance(x, str) for x in declared):
            raise PlanRejected("`target_files` must be a list of strings.")
        for entry in declared:
            _check_rel_path(entry)
        if {entry.strip() for entry in declared} != set(task.target_files):
            LOG.warning(
                "[%s] target_files del piano != roadmap: uso la roadmap come fonte autoritativa.",
                task.id,
            )

        return TaskPlan(
            specification=str(raw["specification_markdown"]),
            test_file=test_file,
            test_code=test_code if test_code.endswith("\n") else test_code + "\n",
            coder_prompt=str(raw["coder_prompt"]),
            target_files=list(task.target_files),
        )

    # ------------------------------------------------------------ esecuzioni
    def _run_pytest(self, paths: List[str]) -> CmdResult:
        argv = [
            sys.executable, "-m", "pytest", *paths,
            "-q", "--maxfail=50", "--tb=long", "--color=no",
            "-p", "no:cacheprovider",
        ]
        return run_cmd(argv, cwd=self.cfg.repo_root, timeout=self.cfg.subprocess_timeout)

    def _run_ruff(self) -> CmdResult:
        ruff_bin = shutil.which("ruff")
        argv = [ruff_bin, "check", "."] if ruff_bin else [sys.executable, "-m", "ruff", "check", "."]
        return run_cmd(argv, cwd=self.cfg.repo_root, timeout=120)

    def _run_aider(self, task: TaskSpec, plan: TaskPlan, message: str) -> CmdResult:
        """Invocazione Aider headless: target file come argomenti posizionali,
        `--yes --no-auto-commits`, repo map compatta Tree-sitter."""
        base_argv = [
            "aider",
            *plan.target_files,
            "--model", self.cfg.aider_model,
            "--message", message,
            "--yes",
            "--no-auto-commits",
        ]
        optional = ["--map-tokens", str(self.cfg.map_tokens), "--no-pretty"]
        result = run_cmd(base_argv + optional, cwd=self.cfg.repo_root, timeout=self.cfg.subprocess_timeout)
        blob = (result.stderr or "") + (result.stdout or "")
        if result.returncode == 2 and re.search(r"unrecognized arguments|no such option", blob, re.I):
            LOG.warning(
                "[%s] Flag opzionali Aider non supportati da questa versione: "
                "retry con il set minimo di flag.", task.id,
            )
            result = run_cmd(base_argv, cwd=self.cfg.repo_root, timeout=self.cfg.subprocess_timeout)
        return result

    def _green_paths(self, plan: TaskPlan) -> List[str]:
        # Suite completa (regression guard §24.4) se esiste tests/,
        # altrimenti il singolo file.
        return ["tests"] if (self.cfg.repo_root / "tests").is_dir() else [plan.test_file]

    # ------------------------------------------------------------ filesystem
    def _write_test(self, plan: TaskPlan) -> None:
        path = self.cfg.repo_root / plan.test_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.test_code, encoding="utf-8")

    def _remove_file(self, rel_path: str) -> None:
        try:
            (self.cfg.repo_root / rel_path).unlink()
        except FileNotFoundError:
            pass

    # ----------------------------------------------------- costruzione prompt
    def _repo_snapshot(self) -> str:
        files = self.git.ls_files()
        shown = files[:400]
        body = "\n".join(f"- {name}" for name in shown) or "(repository vuoto)"
        if len(files) > 400:
            body += f"\n... (+{len(files) - 400} altri file)"
        return body

    def _existing_test_names(self) -> str:
        tests_dir = self.cfg.repo_root / "tests"
        if not tests_dir.is_dir():
            return ""
        names = sorted(p.name for p in tests_dir.glob("test_*.py"))
        return "\n".join(f"- tests/{name}" for name in names)

    def _existing_snippets(self, target_files: Tuple[str, ...]) -> str:
        chunks: List[str] = []
        for rel in target_files:
            path = self.cfg.repo_root / rel
            if path.is_file():
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                shown = lines[:MAX_FILE_SNIPPET_LINES]
                chunks.append(
                    f"### {rel} (first {len(shown)} lines)\n```\n" + "\n".join(shown) + "\n```"
                )
        return "\n\n".join(chunks)

    def _architect_content(self, task: TaskSpec, feedback: Optional[str]) -> str:
        parts = [
            f"# TASK {task.id} — {task.title}",
            "## Objective (authoritative, extracted from the IADF ADD)\n" + task.objective,
            "## Target files (authoritative — echo them verbatim in `target_files`)\n"
            + "\n".join(f"- {f}" for f in task.target_files),
            "## Repository files (git ls-files)\n" + self._repo_snapshot(),
            "## Existing test files (your `test_file_path` must be NEW)\n"
            + (self._existing_test_names() or "(none)"),
        ]
        snippets = self._existing_snippets(task.target_files)
        if snippets:
            parts.append("## Current content of target files (if any)\n" + snippets)
        if feedback:
            parts.append("## PREVIOUS PLAN REJECTED — FIX THIS ROOT CAUSE FIRST\n" + feedback)
        return "\n\n".join(parts)

    def _coder_message(
        self,
        task: TaskSpec,
        plan: TaskPlan,
        attempt: int,
        capsules: List[FailureCapsule],
        diagnosis: Optional[str],
    ) -> str:
        parts: List[str] = [
            f"[IADF {task.id}] {task.title}",
            plan.coder_prompt.strip(),
            textwrap.dedent(f"""\
                === NON-NEGOTIABLE CONSTRAINTS ===
                - Modify ONLY these files (create them if missing): {', '.join(plan.target_files)}
                - NEVER modify {plan.test_file}: it is the acceptance contract (any change is reverted).
                - Acceptance criterion: `python -m pytest {plan.test_file}` must pass.
                - Production-grade code: type hints, docstrings, explicit errors; no TODOs, no placeholders, no dead code.
                - Do not add third-party dependencies beyond: pydantic, jsonschema.
                - Never disable/skip tests, weaken assertions, swallow exit codes or add suppressions to manufacture green."""),
            "=== ACCEPTANCE TEST (read-only contract) ===\n```python\n"
            + plan.test_code.rstrip() + "\n```",
        ]
        if attempt > 0:
            parts.append(
                f"=== REPAIR ATTEMPT {attempt}/{MAX_REPAIR_ATTEMPTS} ===\n"
                "The previous attempt failed. Fix the ROOT CAUSE described in the "
                "failure capsule below; do not paper over symptoms."
            )
            if capsules:
                parts.append(capsules[-1].to_prompt_block())
        if diagnosis:
            parts.append("=== FRONTIER DIAGNOSIS (follow this strategy) ===\n" + diagnosis)
        return "\n\n".join(parts)

    # --------------------------------------------------------------- reviewer
    def _review_content(
        self,
        task: TaskSpec,
        plan: TaskPlan,
        staged: List[str],
        diff: str,
        diff_stat: str,
        green: CmdResult,
        ruff: CmdResult,
        db_res: Optional[CmdResult],
    ) -> str:
        parts = [
            f"# ADVERSARIAL REVIEW REQUEST — {task.id} ({task.title})",
            "## Authoritative target files\n"
            + "\n".join(f"- {f}" for f in plan.target_files)
            + f"\n- {plan.test_file}  (acceptance test: must be untouched)",
            "## Specification\n" + head_tail(plan.specification, 8000),
            "## Staged files\n" + ("\n".join(f"- {f}" for f in staged) or "(none)"),
            "## Diff stat\n```\n" + head_tail(diff_stat, 2000) + "\n```",
            "## git diff --cached\n```diff\n" + head_tail(diff, MAX_DIFF_CHARS) + "\n```",
            f"## pytest report (exit={green.returncode})\n```\n"
            + head_tail(green.stdout + "\n" + green.stderr, MAX_LOG_CHARS) + "\n```",
            f"## ruff report (exit={ruff.returncode})\n```\n"
            + head_tail(ruff.stdout + "\n" + ruff.stderr, 4000) + "\n```",
        ]
        if db_res is not None:
            parts.append(
                f"## VERIFY_DB report — psql applied twice on PostgreSQL 18 "
                f"(exit={db_res.returncode})\n```\n"
                + head_tail((db_res.stdout or "") + "\n" + (db_res.stderr or ""), 4000)
                + "\n```"
            )
        return "\n\n".join(parts)

    def _adversarial_review(
        self,
        task: TaskSpec,
        plan: TaskPlan,
        staged: List[str],
        diff: str,
        diff_stat: str,
        green: CmdResult,
        ruff: CmdResult,
        db_res: Optional[CmdResult],
    ) -> Tuple[str, str, str]:
        content = self._review_content(task, plan, staged, diff, diff_stat, green, ruff, db_res)
        for review_try in range(1, 3):
            try:
                message = self.gateway.call(
                    model=self.cfg.reviewer_model,
                    system=REVIEWER_SYSTEM,
                    messages=[{"role": "user", "content": content}],
                    tools=[REVIEWER_TOOL],
                    tool_choice={"type": "tool", "name": "submit_review"},
                    max_tokens=REVIEWER_MAX_TOKENS,
                )
                raw = AnthropicGateway.extract_tool_input(message, "submit_review")
                verdict = str(raw.get("verdict", "")).strip().upper()
                if verdict not in ("PASS", "REPAIR"):
                    raise ProtocolError(f"Verdetto non valido: {verdict!r}")
                return (
                    verdict,
                    str(raw.get("reasoning", "")).strip(),
                    str(raw.get("repair_instructions", "") or "").strip(),
                )
            except ProtocolError as exc:
                LOG.warning("Review non strutturata (%s): retry %d/2.", exc, review_try)
        # Fail-safe conservativo (CON-004: evidenza mancante non avanza mai).
        return (
            "REPAIR",
            "Fail-safe: impossibile ottenere una review strutturata; per prudenza il diff non è autorizzato.",
            "Riesegui l'implementazione mantenendo il diff minimale e pienamente conforme alla specifica.",
        )

    # ------------------------------------------------------------ diagnostica
    def _diagnosis_content(self, task: TaskSpec, plan: TaskPlan, capsules: List[FailureCapsule]) -> str:
        blocks = "\n\n".join(c.to_prompt_block() for c in capsules[-4:])
        return "\n\n".join([
            f"# FRONTIER DIAGNOSIS REQUEST — {task.id} ({task.title})",
            "## Objective\n" + task.objective,
            "## Specification\n" + head_tail(plan.specification, 6000),
            "## Red test (immutable acceptance contract)\n```python\n"
            + head_tail(plan.test_code, 12000) + "\n```",
            "## Failure capsules (chronological)\n" + blocks,
        ])

    def _frontier_diagnosis(
        self, task: TaskSpec, plan: TaskPlan, capsules: List[FailureCapsule]
    ) -> Optional[str]:
        LOG.warning(
            "[%s] Attivazione Frontier Diagnostician (%s) prima dell'ultimo tentativo.",
            task.id, self.cfg.diagnostician_model,
        )
        try:
            message = self.gateway.call(
                model=self.cfg.diagnostician_model,
                system=DIAGNOSTICIAN_SYSTEM,
                messages=[{"role": "user", "content": self._diagnosis_content(task, plan, capsules)}],
                tools=[DIAGNOSTICIAN_TOOL],
                tool_choice={"type": "tool", "name": "submit_diagnosis"},
                max_tokens=DIAGNOSTICIAN_MAX_TOKENS,
            )
            raw = AnthropicGateway.extract_tool_input(message, "submit_diagnosis")
            text = (
                "ROOT CAUSE:\n" + str(raw.get("root_cause_analysis", "")).strip()
                + "\n\nREPAIR STRATEGY:\n" + str(raw.get("repair_strategy", "")).strip()
            )
            files = raw.get("files_to_focus") or []
            if isinstance(files, list) and files:
                text += "\n\nFILES TO FOCUS: " + ", ".join(str(f) for f in files)
            self.state.record("OPUS_DIAGNOSIS", task=task.id, detail=head_tail(text, 1500))
            return text
        except (ProtocolError, EngineError) as exc:
            LOG.warning("Diagnosi frontier non disponibile (%s): procedo senza.", exc)
            return None

    # ------------------------------------------------------------ commit/push
    def _capsule(self, task: TaskSpec, attempt: int, phase: str, **fields: Any) -> FailureCapsule:
        capsule = FailureCapsule(
            task_id=task.id, attempt=attempt, phase=phase,
            timestamp=utc_now_iso(), head_sha=self.git.head_short(), **fields,
        )
        self.state.add_capsule(asdict(capsule))
        return capsule

    def _commit_and_push(self, task: TaskSpec) -> None:
        self.git.stage_all()
        commit_msg = (
            f"feat({task.id.lower()}): {task.title}\n\n"
            f"[iadf-engine v{ENGINE_VERSION}] ciclo TDD completato: "
            "Red-Proof -> Verify-Green -> Adversarial Review PASS."
        )
        res = self.git.commit(commit_msg)
        if res.returncode != 0:
            raise EngineError(f"git commit fallito: {res.stderr or res.stdout}")
        self.state.state["stats"]["commits"] += 1
        self.state.save()
        LOG.info("[%s] Commit creato: %s", task.id, self.git.head_short())

        if not self.cfg.git_push:
            LOG.info("[%s] Push disabilitato (IADF_GIT_PUSH=0 o remote assente).", task.id)
            return
        branch = self.git.current_branch()
        push = self.git.push(self.cfg.git_remote, branch)
        if push.returncode != 0:
            detail = head_tail((push.stderr or "") + (push.stdout or ""), 1200)
            if self.cfg.strict_push:
                raise QuarantineError(f"git push fallito in modalità strict: {detail}")
            LOG.warning(
                "[%s] git push fallito (commit locale preservato; riprovare manualmente): %s",
                task.id, detail,
            )
            self.state.record("PUSH_FAILED", task=task.id, detail=detail)
        else:
            LOG.info("[%s] Push su %s/%s riuscito.", task.id, self.cfg.git_remote, branch)


# ==============================================================================
# ORCHESTRATORE, PREFLIGHT E CLI
# ==============================================================================

def setup_logging(repo_root: Path, verbose: bool) -> None:
    LOG.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.addHandler(console)
    try:
        file_handler = logging.FileHandler(repo_root / LOG_FILE_NAME, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s")
        )
        file_handler.setLevel(logging.DEBUG)
        LOG.addHandler(file_handler)
    except OSError as exc:
        LOG.warning("Log file non disponibile: %s", exc)


def ensure_gitignore(repo_root: Path) -> None:
    """Garantisce che stato/log/cache dell'engine non finiscano nei commit."""
    gi = repo_root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    lines = existing.splitlines()
    missing = [line for line in GITIGNORE_LINES if line not in lines]
    if missing:
        payload = (existing.rstrip("\n") + "\n" if existing else "") + "\n".join(missing) + "\n"
        gi.write_text(payload, encoding="utf-8")
        LOG.info(".gitignore aggiornato con gli artefatti dell'engine.")


def reconcile_with_git(state: StateManager, git: GitOps) -> None:
    """Idempotenza post-crash: se un commit `feat(task-id):` esiste già nella
    storia ma lo stato non lo registra (crash tra commit e save), il task
    viene riconciliato come completato senza rieseguirlo (FR-IADF-034)."""
    if not git.has_head():
        return
    subjects = git.head_subjects()
    for task in ROADMAP:
        if state.is_completed(task.id):
            continue
        needle = f"feat({task.id.lower()}):"
        if any(subject.lower().startswith(needle) for subject in subjects):
            LOG.warning(
                "Riconciliazione: commit di %s già in storia git -> marcato completato.",
                task.id,
            )
            state.mark_completed(task.id)
            state.record("RECONCILED_FROM_GIT", task=task.id)
    current = state.state.get("current_task")
    if current:
        LOG.warning(
            "Ripresa dopo interruzione: il task %s era in fase %s; verrà rieseguito "
            "dall'inizio in un workspace pulito.",
            current.get("id"), current.get("phase"),
        )


def preflight(cfg: EngineConfig, state: StateManager, git: GitOps, runtime: LocalRuntime) -> None:
    """Verifiche bloccanti di ambiente prima di qualunque side effect."""
    problems: List[str] = []

    def _die(msg: str) -> None:
        problems.append(msg)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        _die("ANTHROPIC_API_KEY assente: export ANTHROPIC_API_KEY='sk-ant-...'")

    model_lower = cfg.aider_model.lower()
    if model_lower.startswith("deepseek") and not os.environ.get("DEEPSEEK_API_KEY"):
        _die("DEEPSEEK_API_KEY assente (richiesta dal modello Aider "
             f"'{cfg.aider_model}'): export DEEPSEEK_API_KEY='sk-...'")

    if shutil.which("git") is None:
        _die("git non trovato nel PATH.")
    if shutil.which("aider") is None:
        _die("aider non trovato nel PATH: pip install aider-chat  "
             "(oppure: python -m pip install aider-install && aider-install)")

    pytest_probe = run_cmd([sys.executable, "-m", "pytest", "--version"], cwd=cfg.repo_root, timeout=60)
    if pytest_probe.returncode != 0:
        _die("pytest non disponibile nell'interprete corrente: pip install pytest")

    ruff_probe = run_cmd(
        [shutil.which("ruff") or sys.executable, *( [] if shutil.which("ruff") else ["-m", "ruff"] ), "--version"],
        cwd=cfg.repo_root, timeout=60,
    )
    if ruff_probe.returncode != 0:
        _die("ruff non disponibile: pip install ruff")

    for module, hint in (("jsonschema", "pip install jsonschema"), ("pydantic", "pip install pydantic")):
        probe = run_cmd([sys.executable, "-c", f"import {module}"], cwd=cfg.repo_root, timeout=60)
        if probe.returncode != 0:
            _die(f"Modulo Python '{module}' assente nell'ambiente dei test: {hint}")

    if not git.is_repo():
        _die(f"{cfg.repo_root} non è un repository git: eseguire prima `git init` e un commit iniziale.")
    else:
        if not git.has_head():
            _die("Il repository non ha commit (HEAD assente): creare un commit iniziale.")
        if not git.user_configured():
            _die("git user.name/user.email non configurati: "
                 "git config user.name 'IADF Bot' && git config user.email 'bot@iadf.local'")
        if not git.is_clean():
            _die("Working tree sporco: commit/stash delle modifiche prima di avviare l'engine "
                 "(l'engine esegue `git reset --hard` e `git clean -fd`).")

    if problems:
        for problem in problems:
            LOG.error("PREFLIGHT | %s", problem)
        raise SystemExit(2)

    # Avvisi non bloccanti + degradazioni controllate.
    if cfg.git_push and not git.has_remote(cfg.git_remote):
        LOG.warning(
            "Remote '%s' non configurato: push disabilitato per questa sessione "
            "(git remote add %s <url> per abilitarlo).", cfg.git_remote, cfg.git_remote,
        )
        cfg.git_push = False
    if cfg.git_push:
        gh_probe = run_cmd(["gh", "auth", "status"], cwd=cfg.repo_root, timeout=60)
        if gh_probe.returncode != 0:
            LOG.warning(
                "`gh auth status` non conclusivo: il push userà le credenziali git standard "
                "(credential helper / SSH). Autenticazione consigliata: gh auth login"
            )
    if not runtime.available:
        message = (
            "Nessun runtime compose rilevato (docker compose / docker-compose / "
            "podman-compose): il profilo LOCAL-SYNTH (PostgreSQL 18 + MinIO) e il "
            "gate VERIFY_DB saranno saltati."
        )
        if cfg.require_db:
            LOG.error("PREFLIGHT | %s (IADF_REQUIRE_DB=1)", message)
            raise SystemExit(2)
        LOG.warning(message)
    if not cfg.enable_opus:
        LOG.warning("Frontier Diagnostician (Opus) disabilitato: escalation non disponibile.")

    LOG.info("Preflight OK | repo=%s | architect=%s | reviewer=%s | opus=%s | aider=%s | push=%s | compose=%s",
             cfg.repo_root, cfg.architect_model, cfg.reviewer_model,
             cfg.diagnostician_model if cfg.enable_opus else "OFF",
             cfg.aider_model, cfg.git_push,
             " ".join(runtime.compose_argv() or ["assente"]))


def print_status(state: StateManager) -> None:
    data = state.state
    print(f"\n=== IADF ENGINE STATUS (v{ENGINE_VERSION}) ===")
    print(f"system_status : {data.get('system_status')}")
    print(f"scaffold_done : {data.get('scaffold_done')}")
    print(f"completed     : {len(data.get('completed_tasks', []))}/{len(ROADMAP)} "
          f"{data.get('completed_tasks', [])}")
    current = data.get("current_task")
    if current:
        print(f"current_task  : {current.get('id')} | phase={current.get('phase')} "
              f"| attempt={current.get('attempt')}")
    stats = data.get("stats", {})
    print(f"stats         : api_calls={stats.get('api_calls', 0)} "
          f"rate_limit_pauses={stats.get('rate_limit_pauses', 0)} "
          f"aider_runs={stats.get('aider_runs', 0)} commits={stats.get('commits', 0)}")
    capsules = data.get("failure_capsules", [])
    if capsules:
        last = capsules[-1]
        print(f"last_failure  : {last.get('task_id')} phase={last.get('phase')} "
              f"attempt={last.get('attempt')} ts={last.get('timestamp')}")
    print()


def run_roadmap(cfg: EngineConfig, state: StateManager, git: GitOps, runtime: LocalRuntime) -> int:
    gateway = AnthropicGateway(state)
    runner = TaskRunner(cfg, state, git, gateway, runtime)
    state.state["system_status"] = "RUNNING"
    state.save()

    for task in ROADMAP:
        if state.is_completed(task.id):
            LOG.info("SKIP %s (già completato).", task.id)
            continue

        unmet = [dep for dep in task.depends_on if not state.is_completed(dep)]
        if unmet:
            state.state["system_status"] = "QUARANTINED"
            state.record("DEPENDENCY_BLOCK", task=task.id, detail=f"dipendenze mancanti: {unmet}")
            LOG.error("%s bloccato: dipendenze non soddisfatte %s.", task.id, unmet)
            return 3

        try:
            runner.run_task(task)
        except QuarantineError as exc:
            state.state["system_status"] = "QUARANTINED"
            state.update_current(phase="QUARANTINED")
            state.record("QUARANTINED", task=task.id, detail=str(exc))
            LOG.error("=" * 78)
            LOG.error("STATO: QUARANTINED — %s", exc)
            LOG.error(
                "Esecuzione ARRESTATA (ADD §24.3 step 5). Analizzare %s e le failure "
                "capsule in %s, correggere manualmente, poi rilanciare: lo stato "
                "riprenderà dal task incompleto.", LOG_FILE_NAME, STATE_FILE_NAME,
            )
            LOG.error("=" * 78)
            return 3
        except EngineError as exc:
            state.state["system_status"] = "ERROR"
            state.record("ENGINE_ERROR", task=task.id, detail=str(exc))
            LOG.error("Errore operativo su %s: %s", task.id, exc)
            LOG.error("Correggere l'ambiente e rilanciare (ripresa automatica dallo stato).")
            return 4
        except KeyboardInterrupt:
            state.state["system_status"] = "INTERRUPTED"
            state.record("INTERRUPTED", task=task.id)
            LOG.warning("Interrotto dall'utente: stato salvato, ripresa al prossimo avvio.")
            return 130

    state.state["system_status"] = "COMPLETE"
    state.clear_current()
    state.record("ROADMAP_COMPLETE")
    LOG.info("=" * 78)
    LOG.info("ROADMAP COMPLETATA: %d/%d task committati.", len(ROADMAP), len(ROADMAP))
    LOG.info("=" * 78)
    return 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="iadf_cost_optimized_engine.py",
        description=(
            "IADF Cost-Optimized Engine v2 — orchestratore autonomo Tiered "
            "Multi-Model (Architect: Sonnet, Coder: Aider/DeepSeek, Reviewer: "
            "Sonnet, Diagnostician: Opus) con TDD deterministico, scaffolding "
            "dei 6 deployable, runtime LOCAL-SYNTH e Bounded Self-Healing."
        ),
    )
    parser.add_argument("--repo", type=Path, default=Path.cwd(),
                        help="Root del repository di lavoro (default: cwd).")
    parser.add_argument("--status", action="store_true",
                        help="Mostra lo stato corrente ed esce.")
    parser.add_argument("--reset-state", action="store_true",
                        help="Elimina iadf_state.json (la storia git resta intatta).")
    parser.add_argument("--only-task", type=str, default=None,
                        help="Esegue solo il task indicato (es. TASK-03-QUEUE-LEASING).")
    parser.add_argument("--no-push", action="store_true",
                        help="Disabilita il push (i commit restano locali).")
    parser.add_argument("--no-opus", action="store_true",
                        help="Disabilita il Frontier Diagnostician (Opus).")
    parser.add_argument("--verbose", action="store_true",
                        help="Log console a livello DEBUG.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo.expanduser().resolve()
    if not repo_root.is_dir():
        sys.stderr.write(f"ERRORE: directory repository inesistente: {repo_root}\n")
        return 2

    cfg = EngineConfig(
        repo_root=repo_root,
        enable_opus=(not args.no_opus) and env_flag("IADF_ENABLE_OPUS", True),
        git_push=(not args.no_push) and env_flag("IADF_GIT_PUSH", True),
        strict_push=env_flag("IADF_STRICT_PUSH", False),
        git_remote=os.environ.get("IADF_GIT_REMOTE", "origin"),
        map_tokens=int(os.environ.get("IADF_MAP_TOKENS", "1024")),
        require_db=env_flag("IADF_REQUIRE_DB", False),
    )

    setup_logging(repo_root, verbose=args.verbose)
    state = StateManager(repo_root / STATE_FILE_NAME)
    git = GitOps(repo_root)
    runtime = LocalRuntime(cfg)

    if args.status:
        print_status(state)
        return 0
    if args.reset_state:
        try:
            (repo_root / STATE_FILE_NAME).unlink()
            print("Stato eliminato.")
        except FileNotFoundError:
            print("Nessuno stato da eliminare.")
        return 0

    LOG.info("IADF Cost-Optimized Engine v%s | repo=%s", ENGINE_VERSION, repo_root)
    preflight(cfg, state, git, runtime)
    ensure_gitignore(repo_root)

    # Scaffolding idempotente dei 6 deployable + runtime LOCAL-SYNTH (§13.1,
    # §15.1, §17.2): eseguito una sola volta, committato separatamente.
    ScaffoldManager(cfg, git, state).ensure()

    reconcile_with_git(state, git)

    global ROADMAP
    if args.only_task:
        selected = [task for task in ROADMAP if task.id == args.only_task]
        if not selected:
            LOG.error("Task sconosciuto: %s. Disponibili: %s",
                      args.only_task, ", ".join(task.id for task in ROADMAP))
            return 2
        ROADMAP = selected  # noqa: PLW0603 — restrizione esplicita richiesta da CLI

    return run_roadmap(cfg, state, git, runtime)


if __name__ == "__main__":
    sys.exit(main())


# ==============================================================================
# GUIDA COMPLETA ALLA CONFIGURAZIONE (documentazione operativa)
# ==============================================================================
_CONFIGURATION_GUIDE = """
================================================================================
GUIDA ALLA CONFIGURAZIONE — IADF COST-OPTIMIZED ENGINE v2.0.0
================================================================================

1) PREREQUISITI DI SISTEMA
   - Python >= 3.10 (consigliato 3.12/3.13), git >= 2.30.
   - GitHub CLI `gh` (consigliato per il push):    https://cli.github.com
   - Runtime compose per il profilo LOCAL-SYNTH (ADD §17.2), uno tra:
       Docker Engine + plugin `docker compose`  |  docker-compose  |  podman-compose
     Il compose avvia PostgreSQL 18-alpine (healthcheck pg_isready) e MinIO;
     senza compose l'engine degrada: VERIFY_DB saltato con warning
     (rendilo bloccante con IADF_REQUIRE_DB=1).

2) PACCHETTI PYTHON (stesso interprete con cui si lancia l'engine)
     pip install anthropic aider-chat pytest ruff jsonschema pydantic
   Note:
   - `aider-chat` fornisce il comando `aider` con repo map Tree-sitter
     integrata (nessun pacchetto tree-sitter separato da installare).
     In caso di conflitti di dipendenze: python -m pip install aider-install
     && aider-install
   - `jsonschema` e `pydantic` servono ai test generati (TASK-01+).

3) VARIABILI D'AMBIENTE — CREDENZIALI (obbligatorie)
     export ANTHROPIC_API_KEY="sk-ant-..."      # Architect/Reviewer (Sonnet) + Opus
     export DEEPSEEK_API_KEY="sk-..."           # Implementer via Aider/LiteLLM
   Il modello Implementer di default è `deepseek/deepseek-chat`
   (alternativa coder-oriented: deepseek/deepseek-coder). Con un provider
   diverso, impostare IADF_AIDER_MODEL e la relativa chiave LiteLLM
   (es. OPENROUTER_API_KEY per openrouter/..., GEMINI_API_KEY per gemini/...).

4) VARIABILI D'AMBIENTE — TUNING (opzionali, con default)
     IADF_ARCHITECT_MODEL       (default: claude-sonnet-5)
     IADF_REVIEWER_MODEL        (default: = architect)
     IADF_DIAGNOSTICIAN_MODEL   (default: claude-opus-5)
     IADF_AIDER_MODEL           (default: deepseek/deepseek-chat)
     IADF_ENABLE_OPUS           (default: 1; 0 disabilita l'escalation Opus)
     IADF_GIT_PUSH              (default: 1; 0 = commit solo locali)
     IADF_STRICT_PUSH           (default: 0; 1 = push fallito -> QUARANTINED)
     IADF_GIT_REMOTE            (default: origin)
     IADF_MAP_TOKENS            (default: 1024; repo map compatta di Aider)
     IADF_SUBPROCESS_TIMEOUT    (default: 600 secondi per ogni subprocess)
     IADF_ARCHITECT_MAX_TOKENS  (default: 16000)
     IADF_REQUIRE_DB            (default: 0; 1 = compose/PG obbligatori)
     IADF_PG_PORT               (default: 5433; porta host di PostgreSQL synth)
     IADF_MINIO_PORT / IADF_MINIO_CONSOLE_PORT   (default: 9000 / 9001)

5) COORDINATE GITHUB E AUTENTICAZIONE CLI
     gh auth login              # una tantum: HTTPS + protocollo git consigliati
     gh auth setup-git          # instrada le credenziali git via gh
     gh auth status             # verifica
     # In alternativa (senza gh): SSH (git@github.com:ORG/iadf-platform.git)
     # oppure PAT nel credential helper.

6) BOOTSTRAP DEL REPOSITORY DI LAVORO
     mkdir iadf-platform && cd iadf-platform
     git init -b main
     git config user.name  "IADF Engine Bot"
     git config user.email "iadf-bot@example.com"
     git commit --allow-empty -m "chore: bootstrap repository IADF"
     gh repo create ORG/iadf-platform --private --source=. --remote=origin --push
     cp /path/iadf_cost_optimized_engine.py .

   Al primo avvio l'engine committa da solo lo scaffolding (ADD §13.1/§15.1):
   i 6 deployable isolati `iadf-api/`, `iadf-controller/`, `iadf-worker/`,
   `iadf-release/`, `iadf-console/`, `otel-collector/` (+ config OTel),
   `docker-compose.synth.yml` (PostgreSQL 18-alpine + MinIO) e `db/README.md`.

7) ESECUZIONE
     python iadf_cost_optimized_engine.py                 # roadmap completa
     python iadf_cost_optimized_engine.py --status        # stato e statistiche
     python iadf_cost_optimized_engine.py --only-task TASK-04-MODEL-ROUTER
     python iadf_cost_optimized_engine.py --no-push --verbose
     python iadf_cost_optimized_engine.py --reset-state   # azzera lo stato

8) COMPORTAMENTO OPERATIVO
   - Ripresa: lo stato è in iadf_state.json; al riavvio i task completati
     vengono saltati e i commit `feat(task-xx):` già in storia git vengono
     riconciliati (nessuna duplicazione dopo un crash).
   - Rate limit: HTTP 429 -> TECHNICAL_PAUSE con rispetto di `retry-after`,
     poi ripresa trasparente.
   - Database: per i task con `requires_db` l'engine esegue `compose up -d
     postgres` e attende `pg_isready` PRIMA dei test; per TASK-02 applica la
     DDL due volte con psql (ON_ERROR_STOP=1) come gate VERIFY_DB.
   - Quarantena: dopo 2 repair falliti (con 1 diagnosi Opus prima del secondo)
     il run si arresta in QUARANTINED; workspace ripulito, Red Test preservato
     su disco, failure capsule in iadf_state.json.
   - Sicurezza: mai `shell=True`; percorsi validati; artefatti engine protetti
     da `git clean` e ignorati da git.
================================================================================
"""
