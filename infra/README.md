# Deployment

Terraform for the AWS deployment described in `docs/architecture.md`, and the
PostgreSQL schema for the production audit trail.

## What this is not

**This has not been applied.** It is a coherent, reviewed configuration that
expresses the intended architecture and the constraints that matter; it is not
a tested deployment, and a real one will need adjustment for your account
structure, networking and tagging conventions. Treat it as a starting point that
gets the consequential details right, not as something to `terraform apply` into
a regulated environment on trust.

It also does not cover: DNS and certificates, the CI/CD pipeline that builds and
pushes the container image, log aggregation beyond CloudWatch defaults, backup
restore testing, or disaster recovery. Each is real work.

## The constraint that catches people out

**S3 Object Lock cannot be enabled on an existing bucket.** It has to be set at
creation, and it implies versioning. A deployment that discovers this after
accumulating evidence has to create a new bucket and copy everything, which
under Compliance-mode retention is not a small operation.

Related, and equally worth knowing before you commit: **Compliance-mode
retention cannot be shortened or removed by anyone, including the account root
user.** A mistaken hundred-year retention on a large object is billed for a
hundred years. Set `evidence_retention_years` deliberately, from your own
retention policy, and test in a separate bucket first.

`docs/data-protection.md` sets out the genuine tension between Compliance-mode
retention and a data subject's erasure request, and the three workable
positions.

## Layout

| File | Contents |
| --- | --- |
| `terraform/main.tf` | Providers, locals, KMS keys, common tags |
| `terraform/variables.tf` | Inputs, including the EU-residency and single-tenant switches |
| `terraform/s3_evidence.tf` | The evidence bucket: Object Lock, versioning, lifecycle, deny-delete policy |
| `terraform/rds.tf` | PostgreSQL, encrypted, in private subnets |
| `terraform/ecs.tf` | Fargate services for the API and the evaluation workers, behind an ALB |
| `terraform/monitoring.tf` | EventBridge schedules, CloudWatch alarms, log retention |
| `terraform/outputs.tf` | What a deployment needs to configure the application |
| `postgres/schema.sql` | Tables for agents, runs, documents, signatures, evidence and change control |
| `postgres/audit_triggers.sql` | Append-only enforcement and row-level security |

## Two deployment shapes

**Multi-tenant SaaS** suits a vendor validating its own AI features. Isolation
rests on PostgreSQL row-level security, per-tenant KMS keys and prefix-scoped
IAM together, not on any one of them.

**Single-tenant in the customer's VPC** suits a sponsor whose qualification data
contains protected health information. Set `single_tenant = true` and
`deploy_local_model = true`; the latter provisions an instance for the local
model that PHI-flagged cases are routed to.

## Least privilege

The evaluation workers need egress to the model provider. The evidence bucket
does not need ingress from the internet at all, and its policy denies delete
outright. The API task role can write evidence and read it back; it cannot
delete. Nothing in the deployment holds a role that can remove an object under
retention, because no such role can exist under Compliance mode.
