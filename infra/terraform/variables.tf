variable "name" {
  description = "Deployment name, used as a prefix for every resource."
  type        = string
  default     = "valkit"
}

variable "environment" {
  description = "Environment name (dev, staging, prod). Production deployments set retention and deletion protection."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of dev, staging, prod."
  }
}

variable "region" {
  description = "AWS region. Set to a European region for EU data residency; every resource here is regional."
  type        = string
  default     = "us-east-1"
}

variable "evidence_retention_years" {
  description = <<-EOT
    Retention for evidence objects, in years.

    Under Object Lock in Compliance mode this CANNOT be shortened afterwards by
    anyone, including the account root user, and storage is billed for the whole
    period. Set it from your own retention policy, not from this default.
  EOT
  type        = number
  default     = 10

  validation {
    condition     = var.evidence_retention_years >= 1 && var.evidence_retention_years <= 30
    error_message = "evidence_retention_years must be between 1 and 30; a longer period is almost certainly a mistake."
  }
}

variable "object_lock_mode" {
  description = <<-EOT
    COMPLIANCE or GOVERNANCE.

    COMPLIANCE is the correct setting for records supporting a regulatory
    submission: no principal can delete or shorten retention. GOVERNANCE allows
    a principal holding s3:BypassGovernanceRetention to do so, which is
    appropriate for a staging environment and not for production.
  EOT
  type        = string
  default     = "COMPLIANCE"

  validation {
    condition     = contains(["COMPLIANCE", "GOVERNANCE"], var.object_lock_mode)
    error_message = "object_lock_mode must be COMPLIANCE or GOVERNANCE."
  }
}

variable "single_tenant" {
  description = "Deploy into a customer VPC with a dedicated database and bucket."
  type        = bool
  default     = false
}

variable "deploy_local_model" {
  description = <<-EOT
    Provision an instance to host a local model for evaluating samples that
    contain protected health information. Required if the qualification set
    carries PHI: without it, ValKit refuses to run those samples rather than
    sending them to a hosted provider.
  EOT
  type        = bool
  default     = false
}

variable "vpc_id" {
  description = "Existing VPC to deploy into. Required when single_tenant is true."
  type        = string
  default     = ""
}

variable "private_subnet_ids" {
  description = "Private subnets for the database and the ECS tasks."
  type        = list(string)
  default     = []
}

variable "public_subnet_ids" {
  description = "Public subnets for the load balancer."
  type        = list(string)
  default     = []
}

variable "container_image" {
  description = "Container image for the API and workers."
  type        = string
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "worker_desired_count" {
  type    = number
  default = 2
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "bedrock_model_ids" {
  description = "Model identifiers the evaluation workers may invoke. Scoped rather than wildcarded so the task role cannot reach models outside the validated configuration."
  type        = list(string)
  default     = ["anthropic.claude-sonnet-4-20250514-v1:0"]
}

variable "alarm_topic_arn" {
  description = "SNS topic for CloudWatch alarms. Empty disables alarm actions."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
