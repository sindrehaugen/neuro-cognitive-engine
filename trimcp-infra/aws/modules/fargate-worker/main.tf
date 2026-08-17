locals {
  cluster = "trimcp-${var.deployment_name}-${var.cluster_name_suffix}"

  # IAM role name prefix — truncated to avoid hitting the 64-char limit
  role_name_prefix = "trimcp-${substr(var.deployment_name, 0, 20)}"
}

# ---------------------------------------------------------------------------
# ECS Cluster
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/trimcp-${var.deployment_name}/worker"
  retention_in_days = var.environment == "prod" ? 90 : 14
}

resource "aws_cloudwatch_log_group" "orchestrator" {
  name              = "/ecs/trimcp-${var.deployment_name}/orchestrator"
  retention_in_days = var.environment == "prod" ? 90 : 14
}

resource "aws_ecs_cluster" "this" {
  name = local.cluster

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

# ---------------------------------------------------------------------------
# IAM — Assume Role trust policy (shared by both roles)
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_tasks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---------------------------------------------------------------------------
# IAM — Execution Role (shared — ECR pull + CW logs)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "exec" {
  name               = "${local.role_name_prefix}-ecs-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

resource "aws_iam_role_policy_attachment" "exec" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ---------------------------------------------------------------------------
# IAM — Orchestrator Task Role (control plane — full data-plane access)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "orchestrator" {
  name               = "${local.role_name_prefix}-ecs-orchestrator"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "orchestrator" {
  # Full access to all database secrets
  statement {
    sid    = "ReadAllSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = var.secrets_arns
  }

  # Full S3 read/write (media, blobs, exports)
  statement {
    sid       = "S3FullAccess"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
    resources = [
      var.s3_bucket_arn,
      "${var.s3_bucket_arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "orchestrator" {
  name   = "trimcp-orchestrator-inline"
  role   = aws_iam_role.orchestrator.id
  policy = data.aws_iam_policy_document.orchestrator.json
}

# ---------------------------------------------------------------------------
# IAM — Worker Task Role (restricted — untrusted MCP integration execution)
# ---------------------------------------------------------------------------

resource "aws_iam_role" "worker" {
  name               = "${local.role_name_prefix}-ecs-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume.json
}

data "aws_iam_policy_document" "worker" {
  # Scoped S3 access — only the worker prefix
  statement {
    sid    = "S3WorkerPrefix"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = [
      "${var.s3_bucket_arn}/${var.worker_s3_prefix}/*",
    ]
  }

  # ListBucket is required for S3 SDK operations (head, exists checks)
  statement {
    sid    = "S3ListBucket"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
    resources = [
      var.s3_bucket_arn,
    ]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["${var.worker_s3_prefix}/*"]
    }
  }

  # Worker-specific secrets only — NOT RDS/ElastiCache master credentials
  # Callers must populate var.worker_secrets_arns with only the secrets
  # workers genuinely need (e.g., a scoped DocumentDB user credential).
  dynamic "statement" {
    for_each = length(var.worker_secrets_arns) > 0 ? [1] : []
    content {
      sid    = "ReadWorkerSecrets"
      effect = "Allow"
      actions = [
        "secretsmanager:GetSecretValue",
      ]
      resources = var.worker_secrets_arns
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "trimcp-worker-inline"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# ---------------------------------------------------------------------------
# ECS Task Definitions
# ---------------------------------------------------------------------------

resource "aws_ecs_task_definition" "orchestrator" {
  family                   = "${var.service_name}-orchestrator"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.cpu)
  memory                   = tostring(var.memory)
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.orchestrator.arn

  container_definitions = jsonencode([
    {
      name         = "orchestrator"
      image        = var.container_image
      essential    = true
      command      = ["python", "server.py"]
      stopTimeout  = 35
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.orchestrator.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "orchestrator"
        }
      }
    }
  ])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.service_name}-worker"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.worker_cpu)
  memory                   = tostring(var.worker_memory)
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.worker.arn

  container_definitions = jsonencode([
    {
      name        = "worker"
      image       = var.worker_container_image
      essential   = true
      command     = ["python", "start_worker.py"]
      stopTimeout = 35
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.worker.name
          "awslogs-region"        = var.region
          "awslogs-stream-prefix" = "worker"
        }
      }
    }
  ])
}

# ---------------------------------------------------------------------------
# ECS Services
# ---------------------------------------------------------------------------

resource "aws_ecs_service" "orchestrator" {
  name            = "${var.service_name}-orchestrator"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.orchestrator.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_security_group_id]
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
}

resource "aws_ecs_service" "worker" {
  name            = "${var.service_name}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [var.app_security_group_id]
    assign_public_ip = false
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
}

# ---------------------------------------------------------------------------
# Autoscaling (Worker)
# ---------------------------------------------------------------------------

locals {
  worker_min_capacity     = 1
  worker_max_capacity     = 10
  scale_out_cpu_threshold = 70
  scale_in_cpu_threshold  = 30
}

resource "aws_appautoscaling_target" "worker" {
  max_capacity       = local.worker_max_capacity
  min_capacity       = local.worker_min_capacity
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "worker_scale_out" {
  name               = "${aws_ecs_service.worker.name}-scale-out"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 60
    metric_aggregation_type = "Average"
    step_adjustment {
      scaling_adjustment          = 2
      metric_interval_lower_bound = 0
    }
  }
}

resource "aws_appautoscaling_policy" "worker_scale_in" {
  name               = "${aws_ecs_service.worker.name}-scale-in"
  policy_type        = "StepScaling"
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  service_namespace  = aws_appautoscaling_target.worker.service_namespace

  step_scaling_policy_configuration {
    adjustment_type         = "ChangeInCapacity"
    cooldown                = 300
    metric_aggregation_type = "Average"
    step_adjustment {
      scaling_adjustment          = -1
      metric_interval_upper_bound = 0
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_cpu_high" {
  alarm_name          = "${aws_ecs_service.worker.name}-cpu-high"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = "2"
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = "60"
  statistic           = "Average"
  threshold           = local.scale_out_cpu_threshold
  alarm_description   = "Scale out when CPU > ${local.scale_out_cpu_threshold}%"
  alarm_actions       = [aws_appautoscaling_policy.worker_scale_out.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.this.name
    ServiceName = aws_ecs_service.worker.name
  }
}

resource "aws_cloudwatch_metric_alarm" "worker_cpu_low" {
  alarm_name          = "${aws_ecs_service.worker.name}-cpu-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = "2"
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = "60"
  statistic           = "Average"
  threshold           = local.scale_in_cpu_threshold
  alarm_description   = "Scale in when CPU < ${local.scale_in_cpu_threshold}%"
  alarm_actions       = [aws_appautoscaling_policy.worker_scale_in.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.this.name
    ServiceName = aws_ecs_service.worker.name
  }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "cluster_arn" {
  value = aws_ecs_cluster.this.id
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.worker.name
}

output "orchestrator_log_group_name" {
  value = aws_cloudwatch_log_group.orchestrator.name
}

output "orchestrator_role_arn" {
  value = aws_iam_role.orchestrator.arn
}

output "worker_role_arn" {
  value = aws_iam_role.worker.arn
}
