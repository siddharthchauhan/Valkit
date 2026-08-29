resource "aws_ecs_cluster" "main" {
  name = local.prefix

  setting {
    name  = "containerInsights"
    value = local.is_prod ? "enabled" : "disabled"
  }

  tags = local.tags
}

resource "aws_security_group" "tasks" {
  name        = "${local.prefix}-tasks"
  description = "API and evaluation workers"
  vpc_id      = var.vpc_id
  tags        = local.tags
}

# Workers need egress to reach the model provider and AWS APIs. The evidence
# bucket needs no ingress from anywhere.
resource "aws_security_group_rule" "tasks_egress" {
  type              = "egress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.tasks.id
  description       = "HTTPS to model providers and AWS service endpoints"
}

resource "aws_security_group" "alb" {
  name        = "${local.prefix}-alb"
  description = "Public load balancer"
  vpc_id      = var.vpc_id
  tags        = local.tags
}

resource "aws_security_group_rule" "alb_ingress" {
  type              = "ingress"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from clients"
}

resource "aws_security_group_rule" "alb_to_tasks" {
  type                     = "egress"
  from_port                = 8000
  to_port                  = 8000
  protocol                 = "tcp"
  security_group_id        = aws_security_group.alb.id
  source_security_group_id = aws_security_group.tasks.id
}

resource "aws_security_group_rule" "tasks_from_alb" {
  type                     = "ingress"
  from_port                = 8000
  to_port                  = 8000
  protocol                 = "tcp"
  security_group_id        = aws_security_group.tasks.id
  source_security_group_id = aws_security_group.alb.id
}

resource "aws_lb" "main" {
  name                       = local.prefix
  load_balancer_type         = "application"
  subnets                    = var.public_subnet_ids
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  enable_deletion_protection = local.is_prod
  tags                       = local.tags
}

resource "aws_lb_target_group" "api" {
  name        = "${local.prefix}-api"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = var.vpc_id

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = local.tags
}

# -- IAM -------------------------------------------------------------------

resource "aws_iam_role" "execution" {
  name = "${local.prefix}-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role" "task" {
  name = "${local.prefix}-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

# Deliberately narrow: the application writes and reads evidence and never
# deletes it. No role in this deployment can remove an object under retention,
# and under Compliance mode no such role could exist.
resource "aws_iam_role_policy" "task" {
  name = "${local.prefix}-task"
  role = aws_iam_role.task.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EvidenceReadWrite"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:PutObjectRetention",
          "s3:GetObject",
          "s3:GetObjectVersion",
          "s3:ListBucket",
        ]
        Resource = [
          aws_s3_bucket.evidence.arn,
          "${aws_s3_bucket.evidence.arn}/*",
        ]
      },
      {
        Sid      = "EvidenceEncryption"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.evidence.arn
      },
      {
        Sid      = "DatabaseCredential"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.database.arn
      },
      {
        Sid      = "InvokeValidatedModels"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = [for id in var.bedrock_model_ids : "arn:aws:bedrock:${var.region}::foundation-model/${id}"]
      },
    ]
  })
}

# -- Services --------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.prefix}/api"
  retention_in_days = local.is_prod ? 400 : 30
  kms_key_id        = aws_kms_key.database.arn
  tags              = local.tags
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.prefix}/worker"
  retention_in_days = local.is_prod ? 400 : 30
  kms_key_id        = aws_kms_key.database.arn
  tags              = local.tags
}

locals {
  common_environment = [
    { name = "VALKIT_EVIDENCE_BUCKET", value = aws_s3_bucket.evidence.id },
    { name = "VALKIT_EVIDENCE_KMS_KEY", value = aws_kms_key.evidence.arn },
    { name = "VALKIT_OBJECT_LOCK_MODE", value = var.object_lock_mode },
    { name = "VALKIT_RETENTION_YEARS", value = tostring(var.evidence_retention_years) },
    { name = "VALKIT_DB_HOST", value = aws_db_instance.main.address },
    { name = "AWS_REGION", value = var.region },
  ]

  common_secrets = [
    { name = "VALKIT_DB_CREDENTIAL", valueFrom = aws_secretsmanager_secret.database.arn },
  ]
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  tags                     = local.tags

  container_definitions = jsonencode([{
    name        = "api"
    image       = var.container_image
    essential   = true
    command     = ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment = local.common_environment
    secrets     = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # Evaluation is IO-bound on model calls rather than CPU-bound, so the worker
  # is sized for concurrency rather than compute.
  cpu                = "2048"
  memory             = "4096"
  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn      = aws_iam_role.task.arn
  tags               = local.tags

  container_definitions = jsonencode([{
    name        = "worker"
    image       = var.container_image
    essential   = true
    command     = ["python", "-m", "valkit.worker"]
    environment = local.common_environment
    secrets     = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.prefix}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  tags = local.tags
}

resource "aws_ecs_service" "worker" {
  name            = "${local.prefix}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  tags = local.tags
}

# A local model for evaluating samples that contain protected health
# information. Without it, ValKit refuses to evaluate PHI-flagged samples
# rather than sending them to a hosted provider.
resource "aws_instance" "local_model" {
  count                  = var.deploy_local_model ? 1 : 0
  ami                    = data.aws_ami.deep_learning[0].id
  instance_type          = "g5.2xlarge"
  subnet_id              = var.private_subnet_ids[0]
  vpc_security_group_ids = [aws_security_group.tasks.id]

  root_block_device {
    volume_size = 200
    encrypted   = true
    kms_key_id  = aws_kms_key.database.arn
  }

  tags = merge(local.tags, {
    Name      = "${local.prefix}-local-model"
    DataClass = "phi-processing"
  })
}

data "aws_ami" "deep_learning" {
  count       = var.deploy_local_model ? 1 : 0
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*"]
  }
}
