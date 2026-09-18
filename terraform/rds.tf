# =============================================================================
# RDS PostgreSQL with pgvector.
#
# The extension is created by the Alembic migration (`CREATE EXTENSION IF NOT
# EXISTS vector`), which is why `shared_preload_libraries` is not needed:
# pgvector is a plain extension, not a preload module. What the parameter group
# *does* do is raise the memory available for HNSW index builds and turn on the
# statement logging you want when a vector query starts taking seconds.
# =============================================================================

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-db"
  subnet_ids = aws_subnet.data[*].id
  tags       = { Name = "${local.name}-db-subnet-group" }
}

resource "aws_db_parameter_group" "main" {
  name   = "${local.name}-pg17"
  family = "postgres17"

  parameter {
    name  = "log_min_duration_statement"
    value = "1000" # log anything slower than a second
  }

  parameter {
    name         = "maintenance_work_mem"
    value        = "1048576" # 1 GiB, in kB: HNSW index builds need headroom
    apply_method = "pending-reboot"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  parameter {
    name  = "rds.force_ssl"
    value = "1" # reject unencrypted client connections
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "random_password" "db" {
  length  = 40
  special = true
  # Characters libpq would need escaping for in a DSN.
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "random_password" "db_readonly" {
  length           = 40
  special          = true
  override_special = "!#$%&*()-_=+[]{}<>:?"
}

resource "aws_db_instance" "main" {
  identifier     = local.name
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.database.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false

  multi_az                  = local.multi_az
  deletion_protection       = local.deletion_protection
  skip_final_snapshot       = !local.is_production
  final_snapshot_identifier = local.is_production ? "${local.name}-final-${formatdate("YYYYMMDDhhmmss", timestamp())}" : null

  backup_retention_period = local.is_production ? 14 : 3
  backup_window           = "02:00-03:00"
  maintenance_window      = "sun:03:30-sun:04:30"
  copy_tags_to_snapshot   = true

  auto_minor_version_upgrade = true
  apply_immediately          = !local.is_production

  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]

  lifecycle {
    # The password lives in Secrets Manager and is rotated there; Terraform
    # should not fight that, and a timestamped snapshot id must not force
    # replacement on every plan.
    ignore_changes = [password, final_snapshot_identifier]
  }

  tags = { Name = local.name }
}
