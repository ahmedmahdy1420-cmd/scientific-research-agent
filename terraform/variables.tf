variable "project_name" {
  description = "Name prefix for every resource."
  type        = string
  default     = "fahem"
}

variable "environment" {
  description = "Deployment environment (staging, production)."
  type        = string
  default     = "staging"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "aws_region" {
  description = "AWS region."
  type        = string
  default     = "eu-west-1"
}

# --- networking --------------------------------------------------------------
variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "availability_zone_count" {
  description = "Number of AZs. Two is the minimum for a Multi-AZ RDS and an ALB."
  type        = number
  default     = 2

  validation {
    condition     = var.availability_zone_count >= 2
    error_message = "At least two availability zones are required."
  }
}

variable "enable_nat_gateway" {
  description = <<-EOT
    Create NAT gateways so private subnets reach the internet (needed to call
    the OpenAI API). NAT is one of the larger fixed costs in this stack; set
    false only if every egress goes through VPC endpoints.
  EOT
  type        = bool
  default     = true
}

# --- containers --------------------------------------------------------------
variable "api_image" {
  description = "Container image for the API and workers (ECR URI with tag)."
  type        = string
  default     = ""
}

variable "api_cpu" {
  description = "Fargate CPU units for the API task."
  type        = number
  default     = 1024
}

variable "api_memory" {
  description = "Fargate memory (MiB) for the API task."
  type        = number
  default     = 2048
}

variable "api_desired_count" {
  description = "Number of API tasks."
  type        = number
  default     = 2
}

variable "api_min_capacity" {
  description = "Minimum API tasks for autoscaling."
  type        = number
  default     = 2
}

variable "api_max_capacity" {
  description = "Maximum API tasks for autoscaling."
  type        = number
  default     = 8
}

variable "worker_cpu" {
  description = "Fargate CPU units for the Celery worker task."
  type        = number
  default     = 1024
}

variable "worker_memory" {
  description = <<-EOT
    Fargate memory (MiB) for the Celery worker. Ingestion parses PDFs in
    memory, so this is deliberately not minimal.
  EOT
  type        = number
  default     = 3072
}

variable "worker_desired_count" {
  description = "Number of Celery worker tasks."
  type        = number
  default     = 1
}

# --- database ----------------------------------------------------------------
variable "db_instance_class" {
  description = "RDS instance class. pgvector HNSW index builds are memory-hungry."
  type        = string
  default     = "db.t4g.medium"
}

variable "db_allocated_storage" {
  description = "Initial RDS storage in GiB."
  type        = number
  default     = 50
}

variable "db_max_allocated_storage" {
  description = "Storage autoscaling ceiling in GiB."
  type        = number
  default     = 250
}

variable "db_engine_version" {
  description = "PostgreSQL major version. 17 ships a pgvector-compatible build."
  type        = string
  default     = "17.5"
}

variable "db_name" {
  description = "Database name."
  type        = string
  default     = "research"
}

variable "db_username" {
  description = "Master username. The password is generated and stored in Secrets Manager."
  type        = string
  default     = "research"
}

variable "db_multi_az" {
  description = "Multi-AZ failover. Defaults on in production."
  type        = bool
  default     = null
}

variable "db_deletion_protection" {
  description = "Prevent accidental RDS deletion."
  type        = bool
  default     = null
}

# --- cache -------------------------------------------------------------------
variable "redis_node_type" {
  description = "ElastiCache node type for the Redis cache and Celery broker."
  type        = string
  default     = "cache.t4g.small"
}

# --- application -------------------------------------------------------------
variable "llm_reasoning_model" {
  description = "Model used for planning, synthesis and verification."
  type        = string
  default     = "gpt-5"
}

variable "llm_fast_model" {
  description = "Model used for classification and extraction."
  type        = string
  default     = "gpt-5-mini"
}

variable "embedding_model" {
  description = "Embedding model."
  type        = string
  default     = "text-embedding-3-large"
}

variable "embedding_dim" {
  description = <<-EOT
    Embedding width. Must match document_chunks.embedding, and must stay at or
    below 2000 for a pgvector HNSW index on the `vector` type.
  EOT
  type        = number
  default     = 1536

  validation {
    condition     = var.embedding_dim > 0 && var.embedding_dim <= 2000
    error_message = "embedding_dim must be between 1 and 2000 for an HNSW index."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention."
  type        = number
  default     = 30
}

variable "alarm_email" {
  description = "Email subscribed to the alarm topic. Empty disables the subscription."
  type        = string
  default     = ""
}

variable "allowed_ingress_cidrs" {
  description = <<-EOT
    CIDRs allowed to reach the load balancer. Defaults to the whole internet,
    which is correct for a public API but should be narrowed for an internal
    tool.
  EOT
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "acm_certificate_arn" {
  description = <<-EOT
    ACM certificate for HTTPS. When empty the ALB serves plain HTTP, which is
    acceptable only for a throwaway environment - `locals.tls_enabled` gates
    the HTTPS listener and the HTTP->HTTPS redirect on it.
  EOT
  type        = string
  default     = ""
}

variable "github_repository" {
  description = <<-EOT
    "owner/repo" for the GitHub Actions OIDC deployment role. Empty skips the
    role entirely, so a deployment without CI does not create unused IAM.
  EOT
  type        = string
  default     = ""
}
