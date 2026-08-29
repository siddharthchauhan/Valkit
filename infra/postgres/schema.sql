-- Production schema for a multi-tenant ValKit deployment.
--
-- Mirrors the domain model in valkit/models.py. Where a column exists because
-- of a regulatory requirement rather than an application need, the comment says
-- which requirement.
--
-- Apply audit_triggers.sql after this file: it depends on these tables.

CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE tenant (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    kms_key_arn text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Audit trail
-- ---------------------------------------------------------------------------

-- 21 CFR 11.10(e). seq is gap-free per tenant and the chain is verified by
-- re-deriving row_hash across the whole trail; see valkit/audit/store.py for
-- the digest schema.
CREATE TABLE audit_log (
    tenant_id   uuid NOT NULL REFERENCES tenant(id),
    seq         bigint NOT NULL,
    ts          timestamptz NOT NULL,
    actor       text NOT NULL,
    action      text NOT NULL,
    entity_type text NOT NULL,
    entity_id   text NOT NULL,
    payload     jsonb NOT NULL,   -- secrets redacted before insert
    reason      text,
    prev_hash   char(64) NOT NULL,
    row_hash    char(64) NOT NULL,
    PRIMARY KEY (tenant_id, seq),
    UNIQUE (tenant_id, row_hash)
);

CREATE INDEX audit_log_entity_idx ON audit_log (tenant_id, entity_type, entity_id);
CREATE INDEX audit_log_actor_idx  ON audit_log (tenant_id, actor);
CREATE INDEX audit_log_ts_idx     ON audit_log (tenant_id, ts);
CREATE INDEX audit_log_action_idx ON audit_log (tenant_id, action);

-- ---------------------------------------------------------------------------
-- Agents and specifications
-- ---------------------------------------------------------------------------

CREATE TABLE agent (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id  uuid NOT NULL REFERENCES tenant(id),
    agent_id   text NOT NULL,
    owner      text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, agent_id)
);

CREATE TABLE agent_version (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id),
    agent_pk          uuid NOT NULL REFERENCES agent(id),
    version           text NOT NULL,
    -- Digest of the raw specification text. Quoted in the validation plan to
    -- identify exactly which file was reviewed and approved.
    spec_sha256       char(64) NOT NULL,
    spec_yaml         text NOT NULL,
    gamp_category     smallint NOT NULL,
    risk_class        text NOT NULL,
    derived_risk_class text NOT NULL,
    validation_status text NOT NULL DEFAULT 'draft',
    validated_at      timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, agent_pk, version),
    CHECK (gamp_category IN (1, 3, 4, 5)),
    CHECK (risk_class IN ('low', 'medium', 'high'))
);

-- ---------------------------------------------------------------------------
-- Evaluation
-- ---------------------------------------------------------------------------

CREATE TABLE eval_run (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id            uuid NOT NULL REFERENCES tenant(id),
    agent_version_id     uuid NOT NULL REFERENCES agent_version(id),
    run_id               text NOT NULL,
    status               text NOT NULL,
    model                text NOT NULL,
    judge_model          text,
    seed                 integer,
    dataset_ref          text NOT NULL,
    dataset_sha256       char(64) NOT NULL,
    dataset_file_sha256  char(64),
    -- Identifies the apparatus, for installation qualification. Two runs
    -- sharing this digest that disagree indicate non-determinism, which is
    -- itself a finding.
    harness_name         text NOT NULL,
    harness_version      text NOT NULL,
    harness_config_sha256 char(64) NOT NULL,
    started_at           timestamptz NOT NULL,
    finished_at          timestamptz,
    transcripts_ref      char(64),
    error                text,
    UNIQUE (tenant_id, run_id)
);

CREATE INDEX eval_run_version_idx ON eval_run (tenant_id, agent_version_id, started_at DESC);

CREATE TABLE metric_result (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id),
    eval_run_id    uuid NOT NULL REFERENCES eval_run(id),
    name           text NOT NULL,
    metric_type    text NOT NULL,
    n              integer NOT NULL,
    k              integer NOT NULL,
    point_estimate double precision NOT NULL,
    lower_bound    double precision,
    target         double precision,
    method         text NOT NULL,
    confidence     double precision NOT NULL,
    passed         boolean NOT NULL,
    critical       boolean NOT NULL DEFAULT true,
    failures       integer NOT NULL DEFAULT 0,
    errors         integer NOT NULL DEFAULT 0,
    rationale      text NOT NULL,
    UNIQUE (tenant_id, eval_run_id, name),
    CHECK (k >= 0 AND k <= n)
);

CREATE TABLE judge_calibration (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id         uuid NOT NULL REFERENCES tenant(id),
    eval_run_id       uuid NOT NULL REFERENCES eval_run(id) UNIQUE,
    judge_model       text NOT NULL,
    n                 integer NOT NULL,
    cohen_kappa       double precision NOT NULL,
    percent_agreement double precision NOT NULL,
    min_required      double precision NOT NULL,
    passed            boolean NOT NULL,
    confusion         jsonb NOT NULL,
    note              text
);

-- ---------------------------------------------------------------------------
-- Evidence
-- ---------------------------------------------------------------------------

-- Content-addressed: evidence_id is the SHA-256 of the bytes, so an identifier
-- that resolves is itself proof of integrity. 21 CFR 11.10(c).
CREATE TABLE evidence (
    tenant_id       uuid NOT NULL REFERENCES tenant(id),
    evidence_id     char(64) NOT NULL,
    kind            text NOT NULL,
    size_bytes      bigint NOT NULL,
    content_type    text NOT NULL,
    uri             text NOT NULL,
    stored_at       timestamptz NOT NULL,
    retention_until timestamptz NOT NULL,
    agent_id        text,
    run_id          text,
    metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (tenant_id, evidence_id)
);

CREATE INDEX evidence_run_idx       ON evidence (tenant_id, run_id);
CREATE INDEX evidence_retention_idx ON evidence (tenant_id, retention_until);

-- ---------------------------------------------------------------------------
-- Documents and signatures
-- ---------------------------------------------------------------------------

CREATE TABLE document (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant(id),
    agent_version_id uuid NOT NULL REFERENCES agent_version(id),
    doc_id           text NOT NULL,
    doc_type         text NOT NULL,
    title            text NOT NULL,
    version          text NOT NULL DEFAULT '1.0',
    status           text NOT NULL DEFAULT 'draft',
    content          text NOT NULL,
    -- The 21 CFR 11.70 link: a signature binds to this digest, so altering the
    -- content invalidates every signature on it.
    content_sha256   char(64) NOT NULL,
    template         text,
    generated_at     timestamptz NOT NULL,
    eval_run_id      uuid REFERENCES eval_run(id),
    supersedes       uuid REFERENCES document(id),
    UNIQUE (tenant_id, doc_id)
);

-- 21 CFR 11.50. Nothing here holds a credential value; components_used records
-- component NAMES only.
CREATE TABLE signature (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenant(id),
    document_id        uuid NOT NULL REFERENCES document(id),
    signature_id       text NOT NULL,
    document_sha256    char(64) NOT NULL,   -- 11.70 record link
    signer_id          text NOT NULL,
    printed_name       text NOT NULL,       -- 11.50(a)(1)
    signed_at          timestamptz NOT NULL, -- 11.50(a)(2), always UTC
    meaning            text NOT NULL,        -- 11.50(a)(3)
    components_used    text[] NOT NULL,
    session_id         text,
    is_first_in_session boolean NOT NULL,
    manifest_sha256    char(64) NOT NULL,
    reason             text,
    role               text,
    UNIQUE (tenant_id, signature_id),
    CHECK (meaning IN ('authored','reviewed','approved','executed','verified','rejected')),
    -- 11.50(a)(1) requires the individual's name, not their username.
    CHECK (printed_name <> signer_id)
);

CREATE INDEX signature_document_idx ON signature (tenant_id, document_id);

-- ---------------------------------------------------------------------------
-- Traceability
-- ---------------------------------------------------------------------------

CREATE TABLE requirement (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant(id),
    agent_version_id uuid NOT NULL REFERENCES agent_version(id),
    req_id           text NOT NULL,
    kind             text NOT NULL,
    text             text NOT NULL,
    rationale        text,
    source           text,
    critical         boolean NOT NULL DEFAULT true,
    parent_ids       text[] NOT NULL DEFAULT '{}',
    UNIQUE (tenant_id, agent_version_id, req_id)
);

CREATE TABLE test_case (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenant(id),
    agent_version_id uuid NOT NULL REFERENCES agent_version(id),
    test_id          text NOT NULL,
    phase            text NOT NULL,
    title            text NOT NULL,
    objective        text NOT NULL,
    acceptance_text  text NOT NULL,
    requirement_ids  text[] NOT NULL DEFAULT '{}',
    risk_ids         text[] NOT NULL DEFAULT '{}',
    metric_name      text,
    scripted         boolean NOT NULL DEFAULT true,
    UNIQUE (tenant_id, agent_version_id, test_id),
    CHECK (phase IN ('IQ', 'OQ', 'PQ'))
);

CREATE TABLE test_execution (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       uuid NOT NULL REFERENCES tenant(id),
    test_case_id    uuid NOT NULL REFERENCES test_case(id),
    eval_run_id     uuid NOT NULL REFERENCES eval_run(id),
    executed_at     timestamptz NOT NULL,
    executed_by     text,
    passed          boolean NOT NULL,
    observed_result text,
    evidence_refs   char(64)[] NOT NULL DEFAULT '{}'
);

-- ---------------------------------------------------------------------------
-- Monitoring and change control
-- ---------------------------------------------------------------------------

CREATE TABLE drift_point (
    id          bigserial PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenant(id),
    agent_id    text NOT NULL,
    metric      text NOT NULL,
    observed_at timestamptz NOT NULL,
    value       double precision NOT NULL,
    n           integer,
    lower_bound double precision,
    run_id      text
);

CREATE INDEX drift_point_series_idx ON drift_point (tenant_id, agent_id, metric, observed_at);

CREATE TABLE change_control (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenant(id),
    cc_id          text NOT NULL,
    agent_id       text NOT NULL,
    agent_version  text,
    trigger        text NOT NULL,
    reason         text NOT NULL,
    status         text NOT NULL,
    impact         text,
    required_scope text[] NOT NULL DEFAULT '{}',
    run_ids        text[] NOT NULL DEFAULT '{}',
    outcome        text,
    prior_version  text,
    new_version    text,
    opened_at      timestamptz NOT NULL,
    closed_at      timestamptz,
    UNIQUE (tenant_id, cc_id),
    CHECK (status IN ('open','impact_assessed','eval_in_progress','eval_complete',
                      'approved','rejected','closed'))
);

CREATE INDEX change_control_agent_idx ON change_control (tenant_id, agent_id, status);
