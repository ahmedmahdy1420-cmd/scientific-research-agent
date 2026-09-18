# =============================================================================
# CloudWatch.
#
# The application already emits one wide JSON event per agent run
# (`metric.agent_run`, see app/observability/metrics.py). Metric filters turn
# those log lines into CloudWatch metrics with no extra infrastructure, which
# is what makes it possible to alarm on *AI-specific* failure modes - cost per
# run and grounding failure rate - and not just CPU.
# =============================================================================

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "flow_logs" {
  name              = "/vpc/${local.name}/flow-logs"
  retention_in_days = 14
}

resource "aws_sns_topic" "alarms" {
  name = "${local.name}-alarms"
}

resource "aws_sns_topic_subscription" "alarms_email" {
  count = var.alarm_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

# --- metric filters over the structured logs ---------------------------------
resource "aws_cloudwatch_log_metric_filter" "agent_cost" {
  name           = "${local.name}-agent-cost"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.metric = \"agent_run\" }"

  metric_transformation {
    name          = "AgentRunCostUsd"
    namespace     = var.project_name
    value         = "$.cost_usd"
    unit          = "None"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "agent_latency" {
  name           = "${local.name}-agent-latency"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.metric = \"agent_run\" }"

  metric_transformation {
    name          = "AgentRunLatencyMs"
    namespace     = var.project_name
    value         = "$.total_latency_ms"
    unit          = "Milliseconds"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "ungrounded_answers" {
  name           = "${local.name}-ungrounded-answers"
  log_group_name = aws_cloudwatch_log_group.app.name
  # The quality regression signal that latency and 5xx dashboards cannot see.
  pattern = "{ $.event = \"agent.verified\" && $.grounded IS FALSE }"

  metric_transformation {
    name          = "UngroundedAnswers"
    namespace     = var.project_name
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "tool_denials" {
  name           = "${local.name}-tool-denials"
  log_group_name = aws_cloudwatch_log_group.app.name
  # A spike here means either a permission misconfiguration or someone probing.
  pattern = "{ $.event = \"tool.denied\" }"

  metric_transformation {
    name          = "ToolAuthorizationDenials"
    namespace     = var.project_name
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "llm_errors" {
  name           = "${local.name}-llm-errors"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"llm.failed\" }"

  metric_transformation {
    name          = "LLMFailures"
    namespace     = var.project_name
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "external_degradation" {
  name           = "${local.name}-external-degradation"
  log_group_name = aws_cloudwatch_log_group.app.name
  pattern        = "{ $.event = \"tool.degrading_to_cache\" }"

  metric_transformation {
    name          = "ExternalServiceDegradations"
    namespace     = var.project_name
    value         = "1"
    default_value = "0"
  }
}

# --- alarms ------------------------------------------------------------------
resource "aws_cloudwatch_metric_alarm" "api_5xx" {
  alarm_name          = "${local.name}-api-5xx"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "The API is returning server errors."
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
  ok_actions    = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "api_latency" {
  alarm_name          = "${local.name}-api-latency-p95"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "TargetResponseTime"
  namespace           = "AWS/ApplicationELB"
  period              = 300
  extended_statistic  = "p95"
  threshold           = 30 # seconds; an agent run is legitimately slow
  alarm_description   = "p95 API latency is above 30s."
  treat_missing_data  = "notBreaching"

  dimensions = {
    LoadBalancer = aws_lb.main.arn_suffix
    TargetGroup  = aws_lb_target_group.api.arn_suffix
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "agent_cost_spike" {
  alarm_name          = "${local.name}-agent-cost-spike"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = aws_cloudwatch_log_metric_filter.agent_cost.metric_transformation[0].name
  namespace           = var.project_name
  period              = 3600
  statistic           = "Sum"
  threshold           = 50 # USD per hour
  alarm_description   = "Hourly model spend is unusually high - check for a retry loop."
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "ungrounded_answers" {
  alarm_name          = "${local.name}-ungrounded-answers"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = aws_cloudwatch_log_metric_filter.ungrounded_answers.metric_transformation[0].name
  namespace           = var.project_name
  period              = 900
  statistic           = "Sum"
  threshold           = 5
  alarm_description   = <<-EOT
    The verifier is rejecting answers as ungrounded more often than usual.
    Typical causes: a model or prompt change, a retrieval regression, or a
    corpus that no longer covers what people are asking.
  EOT
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "llm_failures" {
  alarm_name          = "${local.name}-llm-failures"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = aws_cloudwatch_log_metric_filter.llm_errors.metric_transformation[0].name
  namespace           = var.project_name
  period              = 300
  statistic           = "Sum"
  threshold           = 10
  alarm_description   = "Model provider calls are failing after all retries."
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "database_cpu" {
  alarm_name          = "${local.name}-rds-cpu"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 80
  alarm_description   = "RDS CPU is high - often an unindexed query or a vector scan."
  treat_missing_data  = "notBreaching"

  dimensions = { DBInstanceIdentifier = aws_db_instance.main.id }

  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "database_storage" {
  alarm_name          = "${local.name}-rds-storage"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FreeStorageSpace"
  namespace           = "AWS/RDS"
  period              = 300
  statistic           = "Average"
  threshold           = 10 * 1024 * 1024 * 1024 # 10 GiB
  alarm_description   = "RDS free storage is low."
  treat_missing_data  = "breaching"

  dimensions = { DBInstanceIdentifier = aws_db_instance.main.id }

  alarm_actions = [aws_sns_topic.alarms.arn]
}

resource "aws_cloudwatch_metric_alarm" "worker_stopped" {
  alarm_name          = "${local.name}-worker-stopped"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  metric_name         = "RunningTaskCount"
  namespace           = "ECS/ContainerInsights"
  period              = 300
  statistic           = "Average"
  threshold           = 1
  alarm_description   = "No Celery workers are running; document ingestion has stalled."
  treat_missing_data  = "breaching"

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.worker.name
  }

  alarm_actions = [aws_sns_topic.alarms.arn]
}

# --- dashboard ---------------------------------------------------------------
resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = local.name

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "API traffic and errors"
          region = var.aws_region
          metrics = [
            ["AWS/ApplicationELB", "RequestCount", "LoadBalancer", aws_lb.main.arn_suffix],
            [".", "HTTPCode_Target_5XX_Count", ".", "."],
            [".", "HTTPCode_Target_4XX_Count", ".", "."],
          ]
          stat   = "Sum"
          period = 300
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Agent latency (p50 / p95 / p99)"
          region = var.aws_region
          metrics = [
            [var.project_name, "AgentRunLatencyMs", { stat = "p50" }],
            ["...", { stat = "p95" }],
            ["...", { stat = "p99" }],
          ]
          period = 300
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title   = "Model spend per hour (USD)"
          region  = var.aws_region
          metrics = [[var.project_name, "AgentRunCostUsd", { stat = "Sum" }]]
          period  = 3600
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "AI quality and safety signals"
          region = var.aws_region
          metrics = [
            [var.project_name, "UngroundedAnswers", { stat = "Sum" }],
            [".", "ToolAuthorizationDenials", { stat = "Sum" }],
            [".", "LLMFailures", { stat = "Sum" }],
            [".", "ExternalServiceDegradations", { stat = "Sum" }],
          ]
          period = 900
        }
      },
      {
        type   = "log"
        width  = 24
        height = 6
        properties = {
          title  = "Slowest agent runs (last hour)"
          region = var.aws_region
          query  = <<-EOT
            SOURCE '${aws_cloudwatch_log_group.app.name}'
            | fields @timestamp, run_id, user_id, category, total_latency_ms, cost_usd, tool_calls
            | filter metric = "agent_run"
            | sort total_latency_ms desc
            | limit 20
          EOT
        }
      },
    ]
  })
}
