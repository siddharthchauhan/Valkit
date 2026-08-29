# The evidence vault.
#
# Object Lock must be enabled at bucket creation and cannot be added afterwards,
# which is why it appears here rather than in a separate resource. It implies
# versioning, so versioning is declared explicitly rather than left implicit.

resource "aws_s3_bucket" "evidence" {
  bucket              = "${local.prefix}-evidence-${local.account_id}"
  object_lock_enabled = true
  force_destroy       = false

  tags = local.tags

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    default_retention {
      mode = var.object_lock_mode
      # Expressed in days: S3 accepts days or years, and days makes the value
      # obvious in the console rather than requiring a conversion.
      days = var.evidence_retention_years * 365
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.evidence.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "evidence" {
  bucket                  = aws_s3_bucket.evidence.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Object Lock already prevents deletion within the retention period. This policy
# is a second, visible control: it denies the delete calls outright, so an
# attempt fails at the policy rather than producing an Object Lock error that
# reads like a transient fault. It also denies unencrypted transport.
resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyObjectDeletion"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.evidence.arn}/*"
      },
      {
        Sid       = "DenyRetentionWeakening"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObjectRetention", "s3:BypassGovernanceRetention"]
        Resource  = "${aws_s3_bucket.evidence.arn}/*"
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.evidence.arn,
          "${aws_s3_bucket.evidence.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })
}

# Evidence is read rarely after the validation is signed, but must stay
# retrievable. Tiering after a year cuts storage cost; nothing transitions to a
# class with a retrieval delay measured in hours, because an inspector asking
# for a record should not wait.
resource "aws_s3_bucket_lifecycle_configuration" "evidence" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "tier-cold-evidence"
    status = "Enabled"

    filter {
      prefix = "evidence/objects/"
    }

    transition {
      days          = 365
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = 1095
      storage_class = "GLACIER_IR"
    }
  }
}

resource "aws_s3_bucket_logging" "evidence" {
  count         = local.is_prod ? 1 : 0
  bucket        = aws_s3_bucket.evidence.id
  target_bucket = aws_s3_bucket.access_logs[0].id
  target_prefix = "evidence/"
}

resource "aws_s3_bucket" "access_logs" {
  count  = local.is_prod ? 1 : 0
  bucket = "${local.prefix}-access-logs-${local.account_id}"
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "access_logs" {
  count                   = local.is_prod ? 1 : 0
  bucket                  = aws_s3_bucket.access_logs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
