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
| `terraform/ecs.tf` | Fargate services for the API (`uvicorn api.main:app`) and the worker (`python -m valkit.worker`), behind an ALB |
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

## Three things this configuration decides, and why

**The console is off by default** (`serve_console = false`). ValKit does not
authenticate anyone. `X-ValKit-Actor` says who a request claims to be for, and
the API records exactly that claim — it does not verify it. So the load balancer
needs an identity provider in front of it, populating that header from the
authenticated session, before the console is reachable by anyone. Served without
one, every audit record is only as trustworthy as the network. Signing is the
exception and is safe either way: a signature is verified against the identity
store's components regardless of the header, so a forged header can misattribute
a specification ingestion but not an approval.

**The API target group is sticky.** A validation in progress lives in the
process that started it. The durable records — the hash-chained trail and the
content-addressed evidence — are in Postgres and S3 and are shared; the pipeline
holding the un-signed documents is working state, so a client has to reach the
same instance from ingesting a specification through to signing. This is a
constraint rather than a design goal: an instance replacement loses in-flight
validations, though not their evidence, and re-running regenerates them.
Implementing the persistence in `postgres/schema.sql` removes it, after which
stickiness can be dropped and `api_desired_count` raised freely.

**The always-on worker service runs zero replicas.** EventBridge is the
scheduler here, and it runs exactly one task per firing. Running the service as
well would re-evaluate every due agent twice — doubling the model spend, and
putting two observations into the control chart for one point in time, which is
enough to move the limits and change what the SPC rules report. The service
exists for an environment without EventBridge, and the two are alternatives.
Above one replica it is worse again: the worker has no leader election.

## The seam this module does not close

The worker reads the specifications it re-evaluates from `worker_spec_dir`.
Getting them there is the deployment's job — an EFS mount, an S3 sync in the
entrypoint, or baking them into the image. A worker that finds nothing exits 2,
and the `missed-reevaluation` alarm treats missing data as breaching precisely
because a worker that never ran and one that ran and found nothing look the same
from outside, and both mean the evidence has a gap.
