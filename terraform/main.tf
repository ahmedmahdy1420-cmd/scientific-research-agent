# =============================================================================
# Root configuration: naming, shared locals and the resources that do not
# belong to a single concern.
# =============================================================================

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  name = "${var.project_name}-${var.environment}"

  is_production = var.environment == "production"

  # Production defaults that can still be overridden explicitly.
  multi_az            = coalesce(var.db_multi_az, local.is_production)
  deletion_protection = coalesce(var.db_deletion_protection, local.is_production)

  tls_enabled = var.acm_certificate_arn != ""

  # The image is required to actually deploy, but `terraform validate` and
  # `plan` on a fresh checkout should not fail because of it.
  api_image = var.api_image != "" ? var.api_image : "${aws_ecr_repository.app.repository_url}:latest"

  common_tags = {
    Project     = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
  }
}

# --- container registry ------------------------------------------------------
resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE" # a tag always means the same bytes

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the 20 most recent images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = { type = "expire" }
      }
    ]
  })
}
