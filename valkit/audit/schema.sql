-- Audit trail schema.
--
-- 21 CFR 11.10(e) requires a "secure, computer-generated, time-stamped audit
-- trail to independently record the date and time of operator entries and
-- actions that create, modify, or delete electronic records", and requires that
-- record changes "shall not obscure previously recorded information".
--
-- Two mechanisms enforce that here, and the distinction between them matters.
--
-- The triggers below are a guard rail. They stop an application bug or a
-- careless UPDATE at the SQL prompt, and they are the control an inspector can
-- watch fail in a demonstration. They are not a security boundary: anyone able
-- to DROP the trigger can bypass it.
--
-- The hash chain is the actual integrity control. Each row commits to the row
-- before it, so altering, deleting or re-ordering any record invalidates every
-- digest after it. Detection does not depend on the database cooperating, and a
-- chain digest recorded elsewhere - printed, notarised, or copied into a report
-- - lets a third party verify the trail without trusting this file at all.

CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER PRIMARY KEY,
    ts           TEXT    NOT NULL,   -- ISO-8601 UTC, always Zulu
    actor        TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    entity_type  TEXT    NOT NULL,
    entity_id    TEXT    NOT NULL,
    payload      TEXT    NOT NULL,   -- canonical JSON, secrets already redacted
    reason       TEXT,
    prev_hash    TEXT    NOT NULL,
    row_hash     TEXT    NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor  ON audit_log (actor);
CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log (ts);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log (action);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is not permitted');
END;
