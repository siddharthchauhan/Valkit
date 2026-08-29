terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.5"
    }
  }
}

provider "aws" {
  region = var.region
}

data "aws_caller_identity" "current" {}

locals {
  prefix     = "${var.name}-${var.environment}"
  is_prod    = var.environment == "prod"
  account_id = data.aws_caller_identity.current.account_id

  tags = merge(
    {
      Application = "valkit"
      Environment = var.environment
      ManagedBy   = "terraform"
      # Evidence supporting regulated decisions lives here. Tagged so that a
      # cost or lifecycle policy written later cannot sweep it up by accident.
      DataClass = "gxp-validation-evidence"
    },
    var.tags,
  )
}

# Separate keys for evidence and for the database. A key compromise or an
# accidental policy change then has a bounded blast radius, and per-tenant keys
# can be layered on top of the evidence key in a multi-tenant deployment.

resource "aws_kms_key" "evidence" {
  description             = "${local.prefix} evidence vault"
  enable_key_rotation     = true
  deletion_window_in_days = local.is_prod ? 30 : 7
  tags                    = local.tags
}

resource "aws_kms_alias" "evidence" {
  name          = "alias/${local.prefix}-evidence"
  target_key_id = aws_kms_key.evidence.key_id
}

resource "aws_kms_key" "database" {
  description             = "${local.prefix} database"
  enable_key_rotation     = true
  deletion_window_in_days = local.is_prod ? 30 : 7
  tags                    = local.tags
}

resource "aws_kms_alias" "database" {
  name          = "alias/${local.prefix}-database"
  target_key_id = aws_kms_key.database.key_id
}

resource "random_password" "database" {
  length  = 32
  special = true
}

resource "aws_secretsmanager_secret" "database" {
  name       = "${local.prefix}/database"
  kms_key_id = aws_kms_key.database.arn
  tags       = local.tags
}

resource "aws_secretsmanager_secret_version" "database" {
  secret_id = aws_secretsmanager_secret.database.id
  secret_string = jsonencode({
    username = "valkit"
    password = random_password.database.result
  })
}
