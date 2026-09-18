# =============================================================================
# ElastiCache Redis: cache, Celery broker and rate-limit counters.
#
# Single-node in staging, replicated with automatic failover in production.
# Nothing here is a source of truth - a total loss means cold caches and
# re-queued Celery tasks, not lost data - so the sizing is modest on purpose.
# =============================================================================

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-redis"
  subnet_ids = aws_subnet.data[*].id
}

resource "aws_elasticache_parameter_group" "main" {
  name   = "${local.name}-redis7"
  family = "redis7"

  parameter {
    name = "maxmemory-policy"
    # Evict least-recently-used keys under pressure. Correct here because
    # everything in Redis is either a cache entry or a short-lived counter.
    value = "allkeys-lru"
  }
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = local.name
  description          = "Cache, Celery broker and rate limiting for ${local.name}"

  engine         = "redis"
  engine_version = "7.1"
  node_type      = var.redis_node_type
  port           = 6379

  num_cache_clusters         = local.is_production ? 2 : 1
  automatic_failover_enabled = local.is_production
  multi_az_enabled           = local.is_production

  subnet_group_name    = aws_elasticache_subnet_group.main.name
  security_group_ids   = [aws_security_group.redis.id]
  parameter_group_name = aws_elasticache_parameter_group.main.name

  at_rest_encryption_enabled = true
  transit_encryption_enabled = false # see README: enabling this requires rediss:// URLs

  snapshot_retention_limit = local.is_production ? 5 : 0
  maintenance_window       = "sun:05:00-sun:06:00"
  apply_immediately        = !local.is_production

  auto_minor_version_upgrade = true

  tags = { Name = local.name }
}
