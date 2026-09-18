# =============================================================================
# ECS on Fargate.
#
# Three services from ONE image, differing only in command:
#
#   api           - the FastAPI app, behind the ALB, autoscaled on CPU and on
#                   request count per target
#   worker        - the Celery worker: ingestion and evaluation. Not behind the
#                   load balancer and not autoscaled on CPU, because queue
#                   depth (not CPU) is the right signal for it.
#   mcp           - the MCP server, reachable only inside the VPC
#
# Migrations run as a one-off task in the deploy pipeline, never on container
# start: several tasks starting at once would race on the same DDL.
# =============================================================================

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enhanced"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
    base              = 1
  }
}

# --- shared container configuration -----------------------------------------
locals {
  # Non-sensitive configuration. Anything secret goes through `secrets` below.
  app_environment = [
    { name = "ENVIRONMENT", value = var.environment },
    { name = "DEBUG", value = "false" },
    { name = "LOG_LEVEL", value = "INFO" },
    { name = "LOG_FORMAT", value = "json" },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "STORAGE_BACKEND", value = "s3" },
    { name = "S3_BUCKET", value = aws_s3_bucket.documents.bucket },
    { name = "LLM_PROVIDER", value = "auto" },
    { name = "LLM_REASONING_MODEL", value = var.llm_reasoning_model },
    { name = "LLM_FAST_MODEL", value = var.llm_fast_model },
    { name = "LLM_JUDGE_MODEL", value = var.llm_reasoning_model },
    { name = "EMBEDDING_MODEL", value = var.embedding_model },
    { name = "EMBEDDING_DIM", value = tostring(var.embedding_dim) },
    { name = "MCP_ENABLED", value = "true" },
    { name = "MCP_SERVER_URL", value = "http://${local.name}-mcp.${aws_service_discovery_private_dns_namespace.main.name}:8020/mcp" },
    { name = "MCP_ALLOWED_HOSTS", value = jsonencode(["${local.name}-mcp.${aws_service_discovery_private_dns_namespace.main.name}:8020", "localhost:8020"]) },
    { name = "CLINICAL_TRIALS_API_URL", value = "http://${local.name}-mock.${aws_service_discovery_private_dns_namespace.main.name}:8001" },
    { name = "OTEL_ENABLED", value = "false" },
    { name = "RATE_LIMIT_ENABLED", value = "true" },
  ]

  app_secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
    { name = "DATABASE_READONLY_URL", valueFrom = aws_secretsmanager_secret.database_readonly_url.arn },
    { name = "REDIS_URL", valueFrom = aws_secretsmanager_secret.redis_url.arn },
    { name = "JWT_SECRET", valueFrom = aws_secretsmanager_secret.jwt_secret.arn },
    { name = "MCP_AUTH_TOKEN", valueFrom = aws_secretsmanager_secret.mcp_token.arn },
    { name = "OPENAI_API_KEY", valueFrom = aws_secretsmanager_secret.openai_api_key.arn },
  ]

  celery_environment = concat(local.app_environment, [
    { name = "CELERY_BROKER_URL", value = "redis://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/1" },
    { name = "CELERY_RESULT_BACKEND", value = "redis://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/2" },
  ])

  log_configuration = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.app.name
      "awslogs-region"        = var.aws_region
      "awslogs-stream-prefix" = "ecs"
      # Structlog already emits JSON; this keeps multi-line tracebacks together.
      "awslogs-multiline-pattern" = "^\\{"
    }
  }
}

# --- service discovery -------------------------------------------------------
resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${var.environment}.${var.project_name}.internal"
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "mcp" {
  name = "${local.name}-mcp"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"

    dns_records {
      ttl  = 10
      type = "A"
    }
  }

  health_check_custom_config {}
}

# --- API ---------------------------------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api_cpu
  memory                   = var.api_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name      = "api"
      image     = local.api_image
      essential = true

      command = [
        "gunicorn", "app.main:app",
        "--worker-class", "uvicorn.workers.UvicornWorker",
        # Two workers per vCPU is a reasonable starting point for an IO-bound
        # async app; the real number comes from load testing.
        "--workers", "2",
        "--bind", "0.0.0.0:8000",
        "--timeout", "120",
        "--graceful-timeout", "30",
        "--access-logfile", "-",
      ]

      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      environment  = local.app_environment
      secrets      = local.app_secrets

      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://localhost:8000/api/v1/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }

      logConfiguration       = local.log_configuration
      readonlyRootFilesystem = false # PyMuPDF writes temporary files
    }
  ])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  enable_execute_command = !local.is_production

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true # a failing deploy rolls itself back
  }

  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 90

  lifecycle {
    # The deploy pipeline updates the image; Terraform owns the shape of the
    # service, not which build is currently running.
    ignore_changes = [task_definition, desired_count]
  }

  depends_on = [aws_lb_listener.http]
}

# --- Celery worker -----------------------------------------------------------
resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker_cpu
  memory                   = var.worker_memory
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "worker"
      image     = local.api_image
      essential = true

      command = [
        "celery", "-A", "app.workers.celery_app.celery_app", "worker",
        "--loglevel=INFO",
        "--concurrency=2",
        "-Q", "ingestion,evaluation,default",
        # Bounds any slow leak in PDF parsing; the task is idempotent so a
        # recycled child costs nothing.
        "--max-tasks-per-child=100",
      ]

      environment = local.celery_environment
      secrets     = local.app_secrets

      healthCheck = {
        command     = ["CMD-SHELL", "celery -A app.workers.celery_app.celery_app inspect ping -d celery@$HOSTNAME || exit 1"]
        interval    = 60
        timeout     = 15
        retries     = 3
        startPeriod = 90
      }

      logConfiguration = local.log_configuration
    }
  ])
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name}-worker"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# --- MCP server --------------------------------------------------------------
resource "aws_ecs_task_definition" "mcp" {
  family                   = "${local.name}-mcp"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name         = "mcp"
      image        = local.api_image
      essential    = true
      command      = ["python", "-m", "app.mcp", "--transport", "streamable-http"]
      portMappings = [{ containerPort = 8020, protocol = "tcp" }]
      environment  = local.app_environment
      secrets      = local.app_secrets

      healthCheck = {
        command     = ["CMD-SHELL", "curl -fsS http://localhost:8020/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }

      logConfiguration = local.log_configuration
    }
  ])
}

resource "aws_ecs_service" "mcp" {
  name            = "${local.name}-mcp"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mcp.arn
  desired_count   = 1
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = aws_subnet.private[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.mcp.arn
  }

  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

# --- migration task ----------------------------------------------------------
# Not a service. The pipeline runs this with `aws ecs run-task` and waits for
# it to exit 0 before updating the API service.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${local.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name             = "migrate"
      image            = local.api_image
      essential        = true
      command          = ["alembic", "upgrade", "head"]
      environment      = local.app_environment
      secrets          = local.app_secrets
      logConfiguration = local.log_configuration
    }
  ])
}

# --- load balancer -----------------------------------------------------------
resource "aws_lb" "main" {
  name               = substr("${local.name}-alb", 0, 32)
  load_balancer_type = "application"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id

  enable_deletion_protection = local.is_production
  drop_invalid_header_fields = true
  idle_timeout               = 180 # an agent run can legitimately take a while

  tags = { Name = "${local.name}-alb" }
}

resource "aws_lb_target_group" "api" {
  name        = substr("${local.name}-api", 0, 32)
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    enabled             = true
    path                = "/api/v1/health"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  # Long enough for an in-flight agent run to finish during a deploy.
  deregistration_delay = 60

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  # With a certificate, port 80 only redirects. Without one (throwaway
  # environments) it serves directly.
  dynamic "default_action" {
    for_each = local.tls_enabled ? [1] : []
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  dynamic "default_action" {
    for_each = local.tls_enabled ? [] : [1]
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.api.arn
    }
  }
}

resource "aws_lb_listener" "https" {
  count = local.tls_enabled ? 1 : 0

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.acm_certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# --- autoscaling -------------------------------------------------------------
resource "aws_appautoscaling_target" "api" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.api.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.api_min_capacity
  max_capacity       = var.api_max_capacity
}

resource "aws_appautoscaling_policy" "api_cpu" {
  name               = "${local.name}-api-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = 65
    scale_in_cooldown  = 300 # slow to scale in: agent runs are long
    scale_out_cooldown = 60  # quick to scale out

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

resource "aws_appautoscaling_policy" "api_requests" {
  name               = "${local.name}-api-requests"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.api.service_namespace
  resource_id        = aws_appautoscaling_target.api.resource_id
  scalable_dimension = aws_appautoscaling_target.api.scalable_dimension

  target_tracking_scaling_policy_configuration {
    # CPU alone is a poor signal for an app that spends most of its time
    # waiting on a model provider; request count per target catches load that
    # never shows up as CPU.
    target_value       = 200
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      resource_label         = "${aws_lb.main.arn_suffix}/${aws_lb_target_group.api.arn_suffix}"
    }
  }
}
