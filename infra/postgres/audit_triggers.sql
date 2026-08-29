-- Append-only enforcement and tenant isolation for the audit trail.
--
-- As with the SQLite implementation, the distinction between the two controls
-- here matters and should not be blurred in a validation document.
--
-- The REVOKE and the trigger are guard rails. They stop an application bug and
-- a careless UPDATE at the psql prompt, and they can be demonstrated failing in
-- front of an inspector. They are not a security boundary: a superuser can drop
-- the trigger and grant the permission back.
--
-- The hash chain is the integrity control. Each row commits to the digest of
-- the row before it, so detection does not depend on the database cooperating.
-- The digest schema is documented in valkit/audit/store.py and is deliberately
-- reproducible by a third party without reading the code.

-- The application role can insert and read, and nothing else.
REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM valkit_app;
GRANT INSERT, SELECT ON audit_log TO valkit_app;

CREATE OR REPLACE FUNCTION audit_log_deny_modification()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'audit_log is append-only: % is not permitted (21 CFR 11.10(e))', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
CREATE TRIGGER audit_log_no_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_deny_modification();

DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;
CREATE TRIGGER audit_log_no_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_deny_modification();

DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_deny_modification();

-- Sequence allocation. The chain requires records to be appended strictly in
-- order, so the sequence is taken under an advisory lock scoped to the tenant
-- rather than from a sequence object, which would leave gaps on rollback and
-- break verification.
CREATE OR REPLACE FUNCTION audit_log_next_seq(p_tenant_id uuid)
RETURNS bigint
LANGUAGE plpgsql
AS $$
DECLARE
    next_seq bigint;
BEGIN
    PERFORM pg_advisory_xact_lock(hashtext(p_tenant_id::text));
    SELECT COALESCE(MAX(seq), 0) + 1 INTO next_seq
      FROM audit_log WHERE tenant_id = p_tenant_id;
    RETURN next_seq;
END;
$$;

-- Row-level security. One of three controls that together provide tenant
-- isolation; the others are per-tenant KMS keys and prefix-scoped IAM. None of
-- the three is sufficient alone.
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

CREATE POLICY audit_log_tenant_isolation ON audit_log
    USING (tenant_id = current_setting('valkit.tenant_id', true)::uuid);

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'agent', 'agent_version', 'eval_run', 'metric_result', 'document',
        'signature', 'evidence', 'change_control', 'drift_point'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY %I_tenant_isolation ON %I USING '
            '(tenant_id = current_setting(''valkit.tenant_id'', true)::uuid)', t, t);
    END LOOP;
END;
$$;

-- Signatures are records of who took responsibility. They are never updated or
-- deleted: a withdrawn approval is a new signature with the meaning 'rejected',
-- not the removal of the original.
REVOKE UPDATE, DELETE ON signature FROM valkit_app;

DROP TRIGGER IF EXISTS signature_no_update ON signature;
CREATE TRIGGER signature_no_update
    BEFORE UPDATE OR DELETE ON signature
    FOR EACH ROW EXECUTE FUNCTION audit_log_deny_modification();
