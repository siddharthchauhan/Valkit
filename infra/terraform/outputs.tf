output "evidence_bucket" {
  description = "Evidence vault bucket name."
  value       = aws_s3_bucket.evidence.id
}

output "evidence_kms_key_arn" {
  value = aws_kms_key.evidence.arn
}

output "object_lock_mode" {
  description = "COMPLIANCE means retention cannot be shortened by anyone, including the account root."
  value       = var.object_lock_mode
}

output "evidence_retention_days" {
  value = var.evidence_retention_years * 365
}

output "database_endpoint" {
  value = aws_db_instance.main.address
}

output "database_secret_arn" {
  value = aws_secretsmanager_secret.database.arn
}

output "api_url" {
  value = "https://${aws_lb.main.dns_name}"
}

output "local_model_private_ip" {
  description = "Local model host for PHI-bearing samples, when deployed."
  value       = var.deploy_local_model ? aws_instance.local_model[0].private_ip : null
}
