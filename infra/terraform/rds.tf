resource "aws_db_subnet_group" "main" {
  name       = "${local.prefix}-db"
  subnet_ids = var.private_subnet_ids
  tags       = local.tags
}

resource "aws_security_group" "database" {
  name        = "${local.prefix}-db"
  description = "PostgreSQL, reachable only from the application tasks"
  vpc_id      = var.vpc_id
  tags        = local.tags
}

resource "aws_security_group_rule" "database_from_tasks" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.database.id
  source_security_group_id = aws_security_group.tasks.id
  description              = "PostgreSQL from the API and worker tasks"
}

resource "aws_db_instance" "main" {
  identifier     = "${local.prefix}-db"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.db_instance_class

  allocated_storage     = 100
  max_allocated_storage = 1000
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.database.arn

  db_name  = "valkit"
  username = "valkit"
  password = random_password.database.result

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  publicly_accessible    = false

  multi_az                = local.is_prod
  backup_retention_period = local.is_prod ? 35 : 7
  backup_window           = "03:00-04:00"
  maintenance_window      = "sun:04:00-sun:05:00"
  copy_tags_to_snapshot   = true

  # The audit trail lives here. Deleting it without a final snapshot would
  # destroy records that are required to be retained.
  deletion_protection       = local.is_prod
  skip_final_snapshot       = !local.is_prod
  final_snapshot_identifier = local.is_prod ? "${local.prefix}-final" : null

  enabled_cloudwatch_logs_exports = ["postgresql"]
  performance_insights_enabled    = local.is_prod
  auto_minor_version_upgrade      = false

  tags = local.tags
}
