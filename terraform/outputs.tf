output "api_url" {
  description = "Public base URL of the API."
  value       = local.tls_enabled ? "https://${aws_lb.main.dns_name}" : "http://${aws_lb.main.dns_name}"
}

output "api_docs_url" {
  description = "OpenAPI documentation."
  value       = "${local.tls_enabled ? "https" : "http"}://${aws_lb.main.dns_name}/docs"
}

output "ecr_repository_url" {
  description = "Push the application image here."
  value       = aws_ecr_repository.app.repository_url
}

output "ecs_cluster_name" {
  description = "ECS cluster name (used by the deploy workflow)."
  value       = aws_ecs_cluster.main.name
}

output "ecs_api_service_name" {
  value = aws_ecs_service.api.name
}

output "ecs_worker_service_name" {
  value = aws_ecs_service.worker.name
}

output "ecs_migrate_task_family" {
  description = "Task family the deploy workflow runs before switching traffic."
  value       = aws_ecs_task_definition.migrate.family
}

output "database_endpoint" {
  description = "RDS endpoint (private; not reachable from the internet)."
  value       = aws_db_instance.main.address
}

output "redis_endpoint" {
  description = "ElastiCache primary endpoint."
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "documents_bucket" {
  description = "S3 bucket holding uploaded documents."
  value       = aws_s3_bucket.documents.bucket
}

output "mcp_internal_url" {
  description = "MCP endpoint, resolvable only inside the VPC."
  value       = "http://${local.name}-mcp.${aws_service_discovery_private_dns_namespace.main.name}:8020/mcp"
}

output "log_group" {
  value = aws_cloudwatch_log_group.app.name
}

output "dashboard_url" {
  value = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${aws_cloudwatch_dashboard.main.dashboard_name}"
}

output "github_actions_role_arn" {
  description = "Role for the deploy workflow to assume via OIDC."
  value       = var.github_repository != "" ? aws_iam_role.github_actions[0].arn : null
}

output "private_subnet_ids" {
  description = "Needed by `aws ecs run-task` in the deploy workflow."
  value       = aws_subnet.private[*].id
}

output "app_security_group_id" {
  value = aws_security_group.app.id
}

output "post_apply_steps" {
  description = "What still has to happen by hand after the first apply."
  value       = <<-EOT
    1. Set the model provider key (Terraform never stores it):
         aws secretsmanager put-secret-value \
           --secret-id ${aws_secretsmanager_secret.openai_api_key.name} \
           --secret-string 'sk-...'
       Until this is set the application runs on its deterministic offline
       provider rather than failing to start.

    2. Create the read-only database role used by the SQL tool layer. Connect
       as the master user and run docker/postgres/init.sql, substituting the
       password from the ${aws_secretsmanager_secret.database_readonly_url.name}
       secret. This is not Terraform's job: it is in-database DDL.

    3. Build and push the image, then run migrations:
         aws ecs run-task --cluster ${aws_ecs_cluster.main.name} \
           --task-definition ${aws_ecs_task_definition.migrate.family} \
           --launch-type FARGATE --network-configuration '...'

    4. Seed the demo data if this is a demo environment (never in production).
  EOT
}
