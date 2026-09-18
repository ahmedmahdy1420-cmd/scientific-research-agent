# =============================================================================
# IAM.
#
# Two roles per task, which is the distinction people most often collapse:
#
#   execution role - used BY ECS to start the container: pull the image, read
#                    the secrets it injects, create the log stream.
#   task role      - used BY the application at runtime: S3 and nothing else.
#
# Collapsing them would give application code permission to read every secret
# in the task definition, which is exactly what you do not want if the
# application is running model-influenced tool calls.
# =============================================================================

data "aws_iam_policy_document" "ecs_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }

    # Confused-deputy guard: only this account's ECS may assume these roles.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

# --- execution role ----------------------------------------------------------
resource "aws_iam_role" "ecs_execution" {
  name               = "${local.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution_managed" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    sid    = "ReadInjectedSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    # Enumerated, not wildcarded: this role can read these secrets and no
    # others, including none belonging to another environment.
    resources = [
      aws_secretsmanager_secret.database_url.arn,
      aws_secretsmanager_secret.database_readonly_url.arn,
      aws_secretsmanager_secret.jwt_secret.arn,
      aws_secretsmanager_secret.mcp_token.arn,
      aws_secretsmanager_secret.openai_api_key.arn,
      aws_secretsmanager_secret.redis_url.arn,
    ]
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "read-injected-secrets"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

# --- task role ---------------------------------------------------------------
resource "aws_iam_role" "ecs_task" {
  name               = "${local.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume_role.json
}

data "aws_iam_policy_document" "ecs_task" {
  statement {
    sid    = "DocumentStorage"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    # Scoped to the document prefix: the application cannot read or write
    # anything else in the bucket.
    resources = ["${aws_s3_bucket.documents.arn}/documents/*"]
  }

  statement {
    sid       = "ListDocumentPrefix"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.documents.arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["documents/*"]
    }
  }

  statement {
    sid    = "PublishMetricsAndTraces"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
    ]
    resources = ["*"] # neither API supports resource-level permissions

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.project_name]
    }
  }

  statement {
    sid    = "ExecuteCommandForDebugging"
    effect = "Allow"
    actions = [
      "ssmmessages:CreateControlChannel",
      "ssmmessages:CreateDataChannel",
      "ssmmessages:OpenControlChannel",
      "ssmmessages:OpenDataChannel",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ecs_task" {
  name   = "application-runtime"
  role   = aws_iam_role.ecs_task.id
  policy = data.aws_iam_policy_document.ecs_task.json
}

# --- supporting roles --------------------------------------------------------
data "aws_iam_policy_document" "rds_monitoring_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["monitoring.rds.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "rds_monitoring" {
  name               = "${local.name}-rds-monitoring"
  assume_role_policy = data.aws_iam_policy_document.rds_monitoring_assume.json
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${local.name}-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
}

data "aws_iam_policy_document" "flow_logs" {
  statement {
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.flow_logs.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs.json
}

# --- CI/CD deployment role ---------------------------------------------------
# GitHub Actions authenticates with OIDC. No long-lived AWS keys exist for CI.
data "aws_iam_policy_document" "github_actions_assume" {
  count = var.github_repository != "" ? 1 : 0

  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:*"]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  count              = var.github_repository != "" ? 1 : 0
  name               = "${local.name}-github-actions"
  assume_role_policy = data.aws_iam_policy_document.github_actions_assume[0].json
}

data "aws_iam_policy_document" "github_actions" {
  count = var.github_repository != "" ? 1 : 0

  statement {
    sid       = "PushImages"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [aws_ecr_repository.app.arn]
  }

  statement {
    sid    = "DeployServices"
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:UpdateService",
      "ecs:RunTask",
      "ecs:DescribeTasks",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "PassTaskRoles"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.ecs_execution.arn, aws_iam_role.ecs_task.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github_actions" {
  count  = var.github_repository != "" ? 1 : 0
  name   = "deploy"
  role   = aws_iam_role.github_actions[0].id
  policy = data.aws_iam_policy_document.github_actions[0].json
}
