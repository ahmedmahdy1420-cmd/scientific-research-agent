# =============================================================================
# Secrets Manager.
#
# Nothing sensitive is ever a plaintext ECS environment variable: every secret
# is injected by the agent at task start via `secrets` + `valueFrom`, so it
# never appears in the task definition, in `docker inspect`, or in CloudTrail.
#
# The OpenAI key is created EMPTY on purpose. Terraform must not be the system
# of record for a third-party credential - `ignore_changes` on the version
# means an operator can set it once (or a rotation function can replace it)
# without Terraform reverting it on the next apply.
# =============================================================================

resource "random_password" "jwt_secret" {
  length  = 64
  special = false # base62 keeps it safe in env files and shell commands
}

resource "random_password" "mcp_token" {
  length  = 48
  special = false
}

# --- database credentials ----------------------------------------------------
resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${local.name}/database-url"
  description             = "SQLAlchemy DSN for the application role"
  recovery_window_in_days = local.is_production ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id = aws_secretsmanager_secret.database_url.id
  secret_string = format(
    "postgresql+psycopg://%s:%s@%s:%s/%s",
    var.db_username,
    urlencode(random_password.db.result),
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    var.db_name,
  )
}

resource "aws_secretsmanager_secret" "database_readonly_url" {
  name                    = "${local.name}/database-readonly-url"
  description             = "DSN for the least-privilege role used by SQL tools"
  recovery_window_in_days = local.is_production ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "database_readonly_url" {
  secret_id = aws_secretsmanager_secret.database_readonly_url.id
  secret_string = format(
    "postgresql+psycopg://%s:%s@%s:%s/%s",
    "${var.db_username}_ro",
    urlencode(random_password.db_readonly.result),
    aws_db_instance.main.address,
    aws_db_instance.main.port,
    var.db_name,
  )
}

# --- application secrets -----------------------------------------------------
resource "aws_secretsmanager_secret" "jwt_secret" {
  name                    = "${local.name}/jwt-secret"
  description             = "HS256 signing key for API access tokens"
  recovery_window_in_days = local.is_production ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "jwt_secret" {
  secret_id     = aws_secretsmanager_secret.jwt_secret.id
  secret_string = random_password.jwt_secret.result
}

resource "aws_secretsmanager_secret" "mcp_token" {
  name                    = "${local.name}/mcp-auth-token"
  description             = "Bearer token clients present to the MCP server"
  recovery_window_in_days = local.is_production ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "mcp_token" {
  secret_id     = aws_secretsmanager_secret.mcp_token.id
  secret_string = random_password.mcp_token.result
}

resource "aws_secretsmanager_secret" "openai_api_key" {
  name                    = "${local.name}/openai-api-key"
  description             = "OpenAI API key. Set this out of band; Terraform never reads it."
  recovery_window_in_days = local.is_production ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "openai_api_key" {
  secret_id = aws_secretsmanager_secret.openai_api_key.id
  # Placeholder only. Replace with:
  #   aws secretsmanager put-secret-value \
  #     --secret-id <name>/openai-api-key --secret-string 'sk-...'
  # Until then the application falls back to its deterministic offline
  # provider rather than failing to start.
  secret_string = "REPLACE_ME"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret" "redis_url" {
  name                    = "${local.name}/redis-url"
  description             = "Redis connection URL"
  recovery_window_in_days = local.is_production ? 30 : 0
}

resource "aws_secretsmanager_secret_version" "redis_url" {
  secret_id     = aws_secretsmanager_secret.redis_url.id
  secret_string = "redis://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
}
