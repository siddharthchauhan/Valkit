# Scheduled re-evaluation. A validated status with no monitoring decays
# silently, so this rule is what keeps the claim current.
resource "aws_cloudwatch_event_rule" "scheduled_reevaluation" {
  name                = "${local.prefix}-reevaluation"
  description         = "Trigger scheduled re-evaluation of validated agents"
  schedule_expression = "cron(0 6 ? * MON *)"
  tags                = local.tags
}

resource "aws_cloudwatch_event_target" "scheduled_reevaluation" {
  rule     = aws_cloudwatch_event_rule.scheduled_reevaluation.name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.events.arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.worker.arn
    launch_type         = "FARGATE"
    task_count          = 1

    network_configuration {
      subnets          = var.private_subnet_ids
      security_groups  = [aws_security_group.tasks.id]
      assign_public_ip = false
    }
  }

  input = jsonencode({
    containerOverrides = [{
      name    = "worker"
      command = ["python", "-m", "valkit.worker", "--scheduled"]
    }]
  })
}

resource "aws_iam_role" "events" {
  name = "${local.prefix}-events"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy" "events" {
  name = "${local.prefix}-events"
  role = aws_iam_role.events.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = aws_ecs_task_definition.worker.arn
      },
      {
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = [aws_iam_role.task.arn, aws_iam_role.execution.arn]
      },
    ]
  })
}

# A missed re-evaluation is a gap in the evidence that the agent stayed within
# its acceptance criteria, so it is alarmed rather than merely logged.
resource "aws_cloudwatch_metric_alarm" "missed_reevaluation" {
  alarm_name          = "${local.prefix}-missed-reevaluation"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ScheduledRunsCompleted"
  namespace           = "ValKit"
  period              = 86400 * 8
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_description   = "No scheduled re-evaluation completed in the last eight days."
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
  tags                = local.tags
}

# Evidence that fails verification means recorded evidence cannot be trusted,
# which is the most serious condition this system can be in.
resource "aws_cloudwatch_metric_alarm" "integrity_failure" {
  alarm_name          = "${local.prefix}-integrity-failure"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "IntegrityVerificationFailures"
  namespace           = "ValKit"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_description   = "An audit chain or evidence object failed verification."
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
  tags                = local.tags
}

resource "aws_cloudwatch_metric_alarm" "drift_critical" {
  alarm_name          = "${local.prefix}-drift-critical"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "CriticalDriftAlerts"
  namespace           = "ValKit"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_description   = "An agent fell below the acceptance target its package was signed against."
  alarm_actions       = var.alarm_topic_arn == "" ? [] : [var.alarm_topic_arn]
  tags                = local.tags
}
